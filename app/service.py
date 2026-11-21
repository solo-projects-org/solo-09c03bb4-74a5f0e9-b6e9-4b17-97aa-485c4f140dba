"""Domain logic: day storage, candidate-edge vetting, frozen queries.

Vocabulary
----------
* A **day** is submitted either as 1440 integer readings ("minutes") or as
  counts per original bin ("bins": half-open intervals carrying their
  original edges).
* A **query** takes a date range plus candidate band edges.  Every candidate
  that would cut through a non-empty stored bin of a bins-day anywhere in
  the range is discarded and reported with the offending day and bin.
  Per-day per-band minute counts are then computed under the surviving
  edges and frozen.
* A reading exactly on an edge belongs to the higher band, i.e. bands are
  ``[lower, upper)`` and the lowest band is ``(-inf, first)``.

Every function takes an open connection; the API layer owns connections and
transactions are opened here at the granularity of one operation.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from .db import read_txn, write_txn

MINUTES_PER_DAY = 1440
MAX_RANGE_DAYS = 3660
MAX_BOUNDARIES = 100
MAX_PAGE_SIZE = 366
MIN_LEVEL = -100
MAX_LEVEL = 300
MIN_BOUNDARY = -1000
MAX_BOUNDARY = 1000
SEVERED_LIST_LIMIT = 50
ANOMALY_LIST_LIMIT = 50

# Values of ``query_day_audit.status``.
AUDIT_VALID = "valid"
AUDIT_ANOMALOUS = "anomalous"
AUDIT_UNVERIFIABLE = "unverifiable"
AUDIT_NOT_APPLICABLE = "not_applicable"


class ServiceError(Exception):
    """A request-level failure with a stable machine-readable code."""

    def __init__(self, status: int, code: str, message: str, **details: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def iso(day: date) -> str:
    return day.isoformat()


def daterange(start: date, end: date) -> list[date]:
    return [date.fromordinal(n) for n in range(start.toordinal(), end.toordinal() + 1)]


# ---------------------------------------------------------------------------
# Submission normalisation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Interval:
    """A half-open band ``[lower, upper)`` with ``None`` meaning unbounded."""

    lower: int | None
    upper: int | None
    minutes: int

    def to_json(self) -> dict[str, Any]:
        return {"lower": self.lower, "upper": self.upper, "minutes": self.minutes}

    def contains_strictly(self, boundary: int) -> bool:
        """True if ``boundary`` lies strictly inside this non-empty bin."""
        if self.minutes <= 0:
            return False
        above_lower = self.lower is None or self.lower < boundary
        below_upper = self.upper is None or self.upper > boundary
        return above_lower and below_upper


def interval_problems(intervals: Iterable[Interval]) -> list[dict[str, Any]]:
    """Structural defects that stop bins from tiling ``(-inf, +inf)``.

    A day's original bins must be contiguous and pairwise disjoint: the first
    bin alone is open at the bottom, the last alone is open at the top, every
    inner bin is bounded on both sides, and each bin begins exactly where the
    previous one ended.  The check is structural: zero-minute bins still have
    to be placed correctly.

    Each problem is a JSON-friendly dict.  The same helper validates new
    submissions and audits bins that were stored before the rule existed.
    """
    items = list(intervals)
    if not items:
        return [{"code": "no_bins", "message": "at least one bin is required"}]

    problems: list[dict[str, Any]] = []
    last = len(items) - 1
    for i, item in enumerate(items):
        if item.lower is not None and item.upper is not None and item.lower >= item.upper:
            problems.append(
                {
                    "code": "invalid_bin_range",
                    "bin": i,
                    "message": f"bin {i} lower must be below upper",
                    "lower": item.lower,
                    "upper": item.upper,
                }
            )
        if i == 0 and item.lower is not None:
            problems.append(
                {
                    "code": "bins_do_not_cover_day",
                    "bin": i,
                    "message": "the first bin must be open at the bottom (lower = null)",
                    "lower": item.lower,
                }
            )
        if i > 0 and item.lower is None:
            problems.append(
                {
                    "code": "bins_not_contiguous",
                    "bin": i,
                    "reason": "unbounded_inner_bin",
                    "message": (
                        f"bin {i} must be bounded at the bottom; only the first "
                        "bin may be open at the bottom"
                    ),
                    "lower": None,
                }
            )
        if i < last and item.upper is None:
            problems.append(
                {
                    "code": "bins_not_contiguous",
                    "bin": i,
                    "reason": "unbounded_inner_bin",
                    "message": (
                        f"bin {i} must be bounded at the top; only the last "
                        "bin may be open at the top"
                    ),
                    "upper": None,
                }
            )
        if i == last and item.upper is not None:
            problems.append(
                {
                    "code": "bins_do_not_cover_day",
                    "bin": i,
                    "message": "the last bin must be open at the top (upper = null)",
                    "upper": item.upper,
                }
            )

    for i in range(1, len(items)):
        previous_upper = items[i - 1].upper
        lower = items[i].lower
        if lower == previous_upper:
            continue
        # None == None is reported above as an unbounded inner bin; here only
        # a genuine gap or overlap needs a second problem entry.
        if lower is None or previous_upper is None:
            continue
        overlap = lower < previous_upper
        problems.append(
            {
                "code": "bins_not_contiguous",
                "bin": i,
                "reason": "overlap" if overlap else "gap",
                "message": (
                    f"bin {i} must start exactly where bin {i - 1} ends"
                    + ("; the bins overlap" if overlap else "; the bins leave a gap")
                ),
                "previous_upper": previous_upper,
                "lower": lower,
            }
        )
    return problems


@dataclass(frozen=True)
class NormalizedDay:
    source: str  # 'minutes' | 'bins'
    values: tuple[int, ...] | None = None
    intervals: tuple[Interval, ...] | None = None


def _check_int(value: Any, what: str, code: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ServiceError(422, code, f"{what} must be an integer")
    return value


def _check_level(value: Any, what: str) -> int:
    _check_int(value, what, "invalid_level")
    if not MIN_LEVEL <= value <= MAX_LEVEL:
        raise ServiceError(
            422,
            "invalid_level",
            f"{what} must be between {MIN_LEVEL} and {MAX_LEVEL}",
            value=value,
        )
    return value


def _check_boundary(value: Any, what: str) -> int:
    _check_int(value, what, "invalid_boundary")
    if not MIN_BOUNDARY <= value <= MAX_BOUNDARY:
        raise ServiceError(
            422,
            "invalid_boundary",
            f"{what} must be between {MIN_BOUNDARY} and {MAX_BOUNDARY}",
            value=value,
        )
    return value


def normalize_minutes(values: Iterable[int]) -> NormalizedDay:
    vals = [_check_level(v, f"minute {i}") for i, v in enumerate(values)]
    if len(vals) != MINUTES_PER_DAY:
        raise ServiceError(
            422,
            "wrong_minute_count",
            f"expected exactly {MINUTES_PER_DAY} minute values, got {len(vals)}",
            expected=MINUTES_PER_DAY,
            got=len(vals),
        )
    return NormalizedDay(source="minutes", values=tuple(vals))


def normalize_intervals(intervals: Iterable[Interval]) -> NormalizedDay:
    items = list(intervals)
    if not items:
        raise ServiceError(422, "empty_bins", "at least one bin is required")
    if len(items) > MAX_BOUNDARIES + 1:
        raise ServiceError(
            422, "too_many_bins", f"at most {MAX_BOUNDARIES + 1} bins are allowed"
        )

    total = 0
    for i, item in enumerate(items):
        _check_int(item.minutes, f"bin {i} minutes", "invalid_bin_minutes")
        if not 0 <= item.minutes <= MINUTES_PER_DAY:
            raise ServiceError(
                422,
                "invalid_bin_minutes",
                f"bin {i} minutes must be between 0 and {MINUTES_PER_DAY}",
                bin=i,
            )
        if item.lower is not None:
            _check_boundary(item.lower, f"bin {i} lower")
        if item.upper is not None:
            _check_boundary(item.upper, f"bin {i} upper")
        total += item.minutes

    # Every inner bin must inherit both edges: an explicit ``null`` inside the
    # list would otherwise make a bin unbounded and leave a hole in the day.
    problems = interval_problems(items)
    if problems:
        first = problems[0]
        raise ServiceError(
            422,
            first["code"],
            first["message"],
            bin=first.get("bin"),
            problems=problems,
        )
    if total != MINUTES_PER_DAY:
        raise ServiceError(
            422,
            "bin_total_mismatch",
            f"bin minutes must sum to {MINUTES_PER_DAY}, got {total}",
            expected=MINUTES_PER_DAY,
            got=total,
        )
    return NormalizedDay(source="bins", intervals=tuple(items))


def intervals_from_boundaries(boundaries: list[int], counts: list[int]) -> list[Interval]:
    """Build contiguous bins from ``n`` original edges and ``n + 1`` counts."""
    for i, boundary in enumerate(boundaries):
        _check_boundary(boundary, f"boundary {i}")
    for i in range(1, len(boundaries)):
        if boundaries[i] <= boundaries[i - 1]:
            raise ServiceError(
                422,
                "boundaries_not_ascending",
                "original boundaries must be strictly ascending",
                bin=i,
            )
    if len(counts) != len(boundaries) + 1:
        raise ServiceError(
            422,
            "count_mismatch",
            "counts must have exactly one more entry than boundaries",
            boundaries=len(boundaries),
            counts=len(counts),
        )
    edges: list[int | None] = [None, *boundaries, None]
    return [
        Interval(lower=edges[i], upper=edges[i + 1], minutes=counts[i])
        for i in range(len(edges) - 1)
    ]


# ---------------------------------------------------------------------------
# Day storage
# ---------------------------------------------------------------------------


def upsert_day(conn: sqlite3.Connection, day: date, norm: NormalizedDay) -> dict:
    """Replace the whole day atomically and bump its revision."""
    if norm.source == "bins":
        # ``normalize_intervals`` already enforces this; checking again means a
        # direct service caller cannot smuggle in a day that does not tile the
        # line.  It runs before ``write_txn``, so a rejection leaves both the
        # stored day and its revision untouched.
        problems = interval_problems(norm.intervals or ())
        if problems:
            first = problems[0]
            raise ServiceError(
                422,
                first["code"],
                first["message"],
                bin=first.get("bin"),
                problems=problems,
            )
    day_iso = iso(day)
    now = utcnow()
    with write_txn(conn):
        row = conn.execute(
            "SELECT day_id, revision FROM day WHERE date = ?", (day_iso,)
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO day (date, revision, source, created_at, updated_at)"
                " VALUES (?, 1, ?, ?, ?)",
                (day_iso, norm.source, now, now),
            )
            day_id = int(cur.lastrowid)
            revision = 1
        else:
            day_id = int(row["day_id"])
            revision = int(row["revision"]) + 1
            conn.execute(
                "UPDATE day SET revision = ?, source = ?, updated_at = ?"
                " WHERE day_id = ?",
                (revision, norm.source, now, day_id),
            )
            conn.execute("DELETE FROM reading WHERE day_id = ?", (day_id,))
            conn.execute("DELETE FROM bin_interval WHERE day_id = ?", (day_id,))

        if norm.source == "minutes":
            conn.executemany(
                "INSERT INTO reading (day_id, minute, value) VALUES (?, ?, ?)",
                [(day_id, i, v) for i, v in enumerate(norm.values or ())],
            )
        else:
            conn.executemany(
                "INSERT INTO bin_interval (day_id, idx, lower, upper, minutes)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (day_id, i, b.lower, b.upper, b.minutes)
                    for i, b in enumerate(norm.intervals or ())
                ],
            )
    return {
        "date": day_iso,
        "revision": revision,
        "source": norm.source,
        "minutes_total": MINUTES_PER_DAY,
        "updated_at": now,
    }


def _load_intervals(conn: sqlite3.Connection, day_id: int) -> list[Interval]:
    rows = conn.execute(
        "SELECT lower, upper, minutes FROM bin_interval WHERE day_id = ? ORDER BY idx",
        (day_id,),
    ).fetchall()
    return [
        Interval(lower=r["lower"], upper=r["upper"], minutes=int(r["minutes"]))
        for r in rows
    ]


def day_payload(conn: sqlite3.Connection, day: date, station: str) -> dict:
    with read_txn(conn):
        row = conn.execute(
            "SELECT day_id, revision, source, updated_at FROM day WHERE date = ?",
            (iso(day),),
        ).fetchone()
        if row is None:
            raise ServiceError(
                404, "day_not_found", f"no data stored for {iso(day)}", date=iso(day)
            )
        payload = {
            "station": station,
            "date": iso(day),
            "revision": int(row["revision"]),
            "source": row["source"],
            "minutes_total": MINUTES_PER_DAY,
            "updated_at": row["updated_at"],
            "values": None,
            "intervals": None,
        }
        if row["source"] == "minutes":
            payload["values"] = [
                int(r["value"])
                for r in conn.execute(
                    "SELECT value FROM reading WHERE day_id = ? ORDER BY minute",
                    (row["day_id"],),
                )
            ]
        else:
            payload["intervals"] = [
                b.to_json() for b in _load_intervals(conn, int(row["day_id"]))
            ]
        return payload


def list_days(conn: sqlite3.Connection, start: date, end: date) -> list[dict]:
    with read_txn(conn):
        rows = conn.execute(
            "SELECT date, revision, source, updated_at FROM day"
            " WHERE date BETWEEN ? AND ? ORDER BY date",
            (iso(start), iso(end)),
        ).fetchall()
    return [
        {
            "date": r["date"],
            "revision": int(r["revision"]),
            "source": r["source"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Bands and counts
# ---------------------------------------------------------------------------


def bin_definitions(boundaries: list[int]) -> list[dict]:
    """Bands implied by the surviving edges, lowest first."""
    defs = []
    for i in range(len(boundaries) + 1):
        lower = boundaries[i - 1] if i > 0 else None
        upper = boundaries[i] if i < len(boundaries) else None
        if lower is None and upper is None:
            label = "(-inf, +inf)"
        elif lower is None:
            label = f"<{upper}"
        elif upper is None:
            label = f">={lower}"
        else:
            label = f"[{lower},{upper})"
        defs.append({"index": i, "lower": lower, "upper": upper, "label": label})
    return defs


def day_bin_problems(intervals: Iterable[Interval]) -> list[dict[str, Any]]:
    """Anomalies of a stored bins-day: shape plus the 1440-minute total.

    A day's bins must tile the whole line exactly once, and a day still has to
    carry 1440 minutes.  Days written before the contiguity rule was tightened
    may fail either check; this is the same audit used for new queries and for
    frozen queries that predate the fix.
    """
    items = list(intervals)
    problems = interval_problems(items)
    total = sum(item.minutes for item in items)
    if not problems and total != MINUTES_PER_DAY:
        problems.append(
            {
                "code": "bin_total_mismatch",
                "message": f"bin minutes must sum to {MINUTES_PER_DAY}, got {total}",
                "expected": MINUTES_PER_DAY,
                "got": total,
            }
        )
    return problems


def _audit_details(
    *,
    problems: Iterable[dict[str, Any]] = (),
    original_bins: Iterable[Interval] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    return {
        "problems": list(problems),
        "original_bins": (
            None if original_bins is None else [b.to_json() for b in original_bins]
        ),
        "note": note,
    }


def _record_audit(
    conn: sqlite3.Connection,
    query_id: str,
    date_iso: str,
    revision: int,
    source: str,
    status: str,
    details: dict[str, Any],
    audited_at: str,
) -> None:
    """Write a verdict once; an existing verdict is never replaced."""
    conn.execute(
        "INSERT OR IGNORE INTO query_day_audit"
        " (query_id, date, revision, source, status, details, audited_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            query_id,
            date_iso,
            revision,
            source,
            status,
            json.dumps(details, sort_keys=True),
            audited_at,
        ),
    )


def _materialize_query_audits(conn: sqlite3.Connection, query_id: str) -> None:
    """Record verdicts for frozen days that do not have one yet.

    Queries frozen by a previous release have no verdict rows.  Such a query
    can still be audited as long as the day it adopted is *still* stored at the
    same revision; the verdict is then written and never recomputed, so a later
    correction or a restart cannot rewrite history.  Once the day has moved on
    (or was replaced by a minutes submission), the adopted original bins are
    gone and the day is permanently recorded as ``unverifiable``.
    """
    pending_query = (
        "SELECT qd.date, qd.revision, qd.source, d.day_id AS current_day_id,"
        " d.revision AS current_revision, d.source AS current_source"
        " FROM query_day AS qd"
        " LEFT JOIN day AS d ON d.date = qd.date"
        " LEFT JOIN query_day_audit AS a"
        "   ON a.query_id = qd.query_id AND a.date = qd.date"
        " WHERE qd.query_id = ? AND a.query_id IS NULL"
        " ORDER BY qd.date"
    )
    with read_txn(conn):
        if not conn.execute(pending_query, (query_id,)).fetchall():
            return
    with write_txn(conn):
        pending = conn.execute(pending_query, (query_id,)).fetchall()
        if not pending:
            return
        now = utcnow()
        for row in pending:
            revision = int(row["revision"])
            source = row["source"]
            if source == "minutes":
                # There are no original bins to verify: every edge is
                # computable from the full-resolution readings.  This holds
                # regardless of whether the readings are still stored.
                _record_audit(
                    conn,
                    query_id,
                    row["date"],
                    revision,
                    source,
                    AUDIT_NOT_APPLICABLE,
                    _audit_details(),
                    now,
                )
                continue
            same_revision = (
                row["current_revision"] is not None
                and int(row["current_revision"]) == revision
                and row["current_source"] == source
            )
            if not same_revision:
                _record_audit(
                    conn,
                    query_id,
                    row["date"],
                    revision,
                    source,
                    AUDIT_UNVERIFIABLE,
                    _audit_details(
                        note=(
                            f"revision {revision} of {row['date']} is no longer "
                            "stored, so its original bins cannot be checked; "
                            "the snapshot is still served unchanged"
                        )
                    ),
                    now,
                )
                continue
            intervals = _load_intervals(conn, int(row["current_day_id"]))
            problems = day_bin_problems(intervals)
            _record_audit(
                conn,
                query_id,
                row["date"],
                revision,
                source,
                AUDIT_ANOMALOUS if problems else AUDIT_VALID,
                _audit_details(problems=problems, original_bins=intervals),
                now,
            )


def audit_pending_queries(conn: sqlite3.Connection) -> int:
    """Settle every verdict still missing, once, at startup.

    The database may hold snapshots frozen by a previous release.  Right after
    the upgrade the adopted revisions may still be stored, so this is the last
    reliable moment to check them.  A day already overwritten is recorded as
    ``unverifiable``; a day still at the adopted revision is recorded as
    ``valid`` or ``anomalous`` and that verdict is never recomputed.  Later
    corrections and restarts therefore cannot change it.
    """
    query_ids = [
        row["query_id"]
        for row in conn.execute(
            "SELECT DISTINCT qd.query_id FROM query_day AS qd"
            " LEFT JOIN query_day_audit AS a"
            "   ON a.query_id = qd.query_id AND a.date = qd.date"
            " WHERE a.query_id IS NULL"
            " ORDER BY qd.query_id"
        )
    ]
    for query_id in query_ids:
        _materialize_query_audits(conn, query_id)
    return len(query_ids)


def _load_audits(conn: sqlite3.Connection, query_id: str) -> dict[str, dict[str, Any]]:
    audits: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT date, revision, source, status, details, audited_at"
        " FROM query_day_audit WHERE query_id = ? ORDER BY date",
        (query_id,),
    ):
        details = json.loads(row["details"])
        audits[row["date"]] = {
            "date": row["date"],
            "revision": int(row["revision"]),
            "source": row["source"],
            "status": row["status"],
            "problems": details.get("problems") or [],
            "original_bins": details.get("original_bins"),
            "note": details.get("note"),
            "audited_at": row["audited_at"],
        }
    return audits


def counts_from_minutes(values: Iterable[int], boundaries: list[int]) -> list[int]:
    """Equal to an edge goes to the higher band (``value >= edge``).

    ``bisect_right`` returns the number of edges ``<= value``, which is
    exactly the index of the band a reading on an edge falls into.
    """
    counts = [0] * (len(boundaries) + 1)
    for value in values:
        counts[bisect_right(boundaries, value)] += 1
    return counts


def counts_from_intervals(intervals: list[Interval], boundaries: list[int]) -> list[int]:
    counts = [0] * (len(boundaries) + 1)
    for interval in intervals:
        if interval.minutes == 0:
            continue
        # Callers must pass only vetted edges.  Checking this is cheap and
        # turns a silent miscount into a loud failure.
        if any(interval.contains_strictly(b) for b in boundaries):
            raise ValueError(f"edge splits stored bin {interval!r}")
        # No surviving edge lies strictly inside a non-empty stored bin, so
        # each non-empty bin sits wholly inside exactly one target band.
        index = 0 if interval.lower is None else bisect_right(boundaries, interval.lower)
        counts[index] += interval.minutes
    return counts


def _reject_candidates(
    candidates: list[int], bins_days: list[tuple[str, list[Interval]]]
) -> tuple[list[int], list[dict]]:
    """Discard candidates that split a non-empty stored bin; keep the rest.

    A bins-day only knows counts per original bin, so an edge inside such a
    bin cannot be honoured.  Each rejection names every offending day and the
    original bin (with its minutes); the per-rejection list is capped and
    flagged with ``severed_truncated`` plus ``severed_total``.

    Only ``bins`` days constrain the edges: a day submitted as minutes carries
    its full resolution, so any edge can be computed for it exactly.
    """
    rejected: list[dict] = []
    kept: list[int] = []
    for boundary in candidates:
        hits: list[dict] = []
        total = 0
        for day_iso, intervals in bins_days:
            for interval in intervals:
                if not interval.contains_strictly(boundary):
                    continue
                total += 1
                if len(hits) < SEVERED_LIST_LIMIT:
                    hits.append(
                        {"date": day_iso, "original_bin": interval.to_json()}
                    )
        if total:
            rejected.append(
                {
                    "boundary": boundary,
                    "reason": "would_split_original_bin",
                    "severed_total": total,
                    "severed_truncated": total > len(hits),
                    "severed": hits,
                }
            )
        else:
            kept.append(boundary)
    return kept, rejected


# ---------------------------------------------------------------------------
# Queries: freeze edges + per-day data, then page through the snapshot
# ---------------------------------------------------------------------------


def _snapshot_rows(conn: sqlite3.Connection, start: date, end: date):
    """Load every stored day in range, one day at a time.

    Readings are streamed per day through a cursor rather than materialised
    for the whole range, so a multi-year query stays within bounded memory.
    """
    rows = conn.execute(
        "SELECT day_id, date, revision, source, updated_at FROM day"
        " WHERE date BETWEEN ? AND ? ORDER BY date",
        (iso(start), iso(end)),
    ).fetchall()
    days: list[tuple[sqlite3.Row, Iterable[int] | None, list[Interval] | None]] = []
    bins_days: list[tuple[str, list[Interval]]] = []
    for row in rows:
        if row["source"] == "minutes":
            values: Iterable[int] = (
                int(r["value"])
                for r in conn.execute(
                    "SELECT value FROM reading WHERE day_id = ? ORDER BY minute",
                    (row["day_id"],),
                )
            )
            days.append((row, values, None))
        else:
            intervals = _load_intervals(conn, int(row["day_id"]))
            days.append((row, None, intervals))
            bins_days.append((row["date"], intervals))
    return days, bins_days


def create_query(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    candidate_boundaries: list[int],
    page_size: int,
    station: str,
) -> dict:
    """Vet edges and freeze the whole result in one transaction."""
    if start > end:
        raise ServiceError(422, "invalid_range", "start_date must not be after end_date")
    span = (end - start).days + 1
    if span > MAX_RANGE_DAYS:
        raise ServiceError(
            422,
            "range_too_long",
            f"date range may span at most {MAX_RANGE_DAYS} days",
            days=span,
        )
    if len(candidate_boundaries) > MAX_BOUNDARIES:
        raise ServiceError(
            422,
            "too_many_boundaries",
            f"at most {MAX_BOUNDARIES} candidate boundaries are allowed",
        )
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ServiceError(
            422,
            "invalid_page_size",
            f"page_size must be between 1 and {MAX_PAGE_SIZE}",
            page_size=page_size,
        )

    requested: list[int] = []
    duplicates: list[int] = []
    for boundary in candidate_boundaries:
        _check_boundary(boundary, "candidate boundary")
        if boundary in requested:
            if boundary not in duplicates:
                duplicates.append(boundary)
        else:
            requested.append(boundary)

    query_id = "q-" + uuid.uuid4().hex
    created_at = utcnow()

    with write_txn(conn):
        days, bins_days = _snapshot_rows(conn, start, end)
        available = {row["date"] for row, _, _ in days}

        # A stored bins-day may predate the contiguity rule.  Refuse to freeze
        # a snapshot from such a day: its counts would silently be ambiguous.
        # Nothing is inserted (the exception rolls the transaction back), and
        # the caller gets every offending date with the revision it read.
        anomalies = []
        audit_rows: list[tuple[str, str, int, str, str, str, str]] = []
        for row, _, intervals in days:
            if row["source"] == "bins":
                problems = day_bin_problems(intervals or [])
                if problems:
                    anomalies.append(
                        {
                            "date": row["date"],
                            "revision": int(row["revision"]),
                            "source": "bins",
                            "problems": problems,
                            "original_bins": [b.to_json() for b in intervals or []],
                        }
                    )
                    continue
                audit_rows.append(
                    (
                        query_id,
                        row["date"],
                        int(row["revision"]),
                        "bins",
                        AUDIT_VALID,
                        json.dumps(
                            _audit_details(original_bins=intervals or []),
                            sort_keys=True,
                        ),
                        created_at,
                    )
                )
            else:
                audit_rows.append(
                    (
                        query_id,
                        row["date"],
                        int(row["revision"]),
                        "minutes",
                        AUDIT_NOT_APPLICABLE,
                        json.dumps(_audit_details(), sort_keys=True),
                        created_at,
                    )
                )
        if anomalies:
            raise ServiceError(
                409,
                "anomalous_day_bins",
                (
                    f"{len(anomalies)} stored day(s) in range have bins that do "
                    "not continuously tile (-inf, +inf); no query was created"
                ),
                anomalies=anomalies,
                anomalous_days=anomalies,
                anomalous_dates=[item["date"] for item in anomalies],
                anomalies_total=len(anomalies),
            )

        effective, rejected = _reject_candidates(requested, bins_days)
        effective = sorted(effective)
        definitions = bin_definitions(effective)

        all_dates = [iso(d) for d in daterange(start, end)]
        missing = [d for d in all_dates if d not in available]

        day_rows = []
        for row, values, intervals in days:
            if values is not None:
                counts = counts_from_minutes(values, effective)
            else:
                counts = counts_from_intervals(intervals or [], effective)
            day_rows.append(
                (
                    query_id,
                    row["date"],
                    int(row["day_id"]),
                    int(row["revision"]),
                    row["source"],
                    json.dumps(counts),
                    sum(counts),
                )
            )

        pages = [
            all_dates[i : i + page_size] for i in range(0, len(all_dates), page_size)
        ]

        conn.execute(
            "INSERT INTO query_log (query_id, created_at, start_date, end_date,"
            " requested_boundaries, ignored_duplicates, effective_boundaries,"
            " bin_definitions, rejected, missing_dates, page_size, days_total,"
            " pages_total) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                query_id,
                created_at,
                iso(start),
                iso(end),
                json.dumps(requested),
                json.dumps(duplicates),
                json.dumps(effective),
                json.dumps(definitions),
                json.dumps(rejected),
                json.dumps(missing),
                page_size,
                len(all_dates),
                len(pages),
            ),
        )
        conn.executemany(
            "INSERT INTO query_day (query_id, date, day_id, revision, source,"
            " counts, minutes_total) VALUES (?, ?, ?, ?, ?, ?, ?)",
            day_rows,
        )
        conn.executemany(
            "INSERT INTO query_page (query_id, page_no, dates) VALUES (?, ?, ?)",
            [(query_id, i + 1, json.dumps(dates)) for i, dates in enumerate(pages)],
        )
        conn.executemany(
            "INSERT INTO query_day_audit (query_id, date, revision, source, status,"
            " details, audited_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            audit_rows,
        )

    return {
        "station": station,
        "query_id": query_id,
        "created_at": created_at,
        "start_date": iso(start),
        "end_date": iso(end),
        "requested_boundaries": requested,
        "ignored_duplicate_boundaries": duplicates,
        "effective_boundaries": effective,
        "rejected": rejected,
        "bins": definitions,
        "missing_dates": missing,
        "page_size": page_size,
        "pages_total": len(pages),
        "days_total": len(all_dates),
        "days_available": len(all_dates) - len(missing),
        "data_version": f"snapshot frozen at {created_at}",
    }


def _load_query_row(conn: sqlite3.Connection, query_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM query_log WHERE query_id = ?", (query_id,)).fetchone()
    if row is None:
        raise ServiceError(
            404, "query_not_found", f"unknown query_id {query_id!r}", query_id=query_id
        )
    return row


def _query_meta(row: sqlite3.Row, station: str) -> dict:
    missing = json.loads(row["missing_dates"])
    return {
        "station": station,
        "query_id": row["query_id"],
        "created_at": row["created_at"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "requested_boundaries": json.loads(row["requested_boundaries"]),
        "ignored_duplicate_boundaries": json.loads(row["ignored_duplicates"]),
        "effective_boundaries": json.loads(row["effective_boundaries"]),
        "rejected": json.loads(row["rejected"]),
        "bins": json.loads(row["bin_definitions"]),
        "missing_dates": missing,
        "page_size": int(row["page_size"]),
        "pages_total": int(row["pages_total"]),
        "days_total": int(row["days_total"]),
        "days_available": int(row["days_total"]) - len(missing),
        "data_version": f"snapshot frozen at {row['created_at']}",
    }


def get_query(conn: sqlite3.Connection, query_id: str, station: str) -> dict:
    with read_txn(conn):
        _load_query_row(conn, query_id)
    _materialize_query_audits(conn, query_id)
    with read_txn(conn):
        row = _load_query_row(conn, query_id)
        meta = _query_meta(row, station)
        meta["pages"] = [
            {"page_no": int(p["page_no"]), "dates": json.loads(p["dates"])}
            for p in conn.execute(
                "SELECT page_no, dates FROM query_page WHERE query_id = ?"
                " ORDER BY page_no",
                (query_id,),
            )
        ]
        audits = list(_load_audits(conn, query_id).values())
        anomalous_days = [
            _audit_day_payload(a) for a in audits if a["status"] == AUDIT_ANOMALOUS
        ]
        unverifiable_days = [
            _audit_day_payload(a) for a in audits if a["status"] == AUDIT_UNVERIFIABLE
        ]
        meta["audit"] = {
            "days_total": len(audits),
            "valid_total": sum(1 for a in audits if a["status"] == AUDIT_VALID),
            "anomalous_total": len(anomalous_days),
            "unverifiable_total": len(unverifiable_days),
            "anomalous_days": anomalous_days,
            "unverifiable_days": unverifiable_days,
        }
        return meta


def _audit_day_payload(audit: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": audit["date"],
        "revision": audit["revision"],
        "source": audit["source"],
        "status": audit["status"],
        "problems": audit["problems"],
        "original_bins": audit["original_bins"],
        "audited_at": audit["audited_at"],
        "note": audit["note"],
    }


def get_query_audit(conn: sqlite3.Connection, query_id: str, station: str) -> dict:
    """Verdicts for every frozen day: which revision, checked when.

    Materialises any verdict still missing first (older queries), then reads
    the stored verdicts.  A verdict that already exists is never recomputed,
    so later corrections and restarts keep the original judgement.
    """
    with read_txn(conn):
        _load_query_row(conn, query_id)
    _materialize_query_audits(conn, query_id)
    with read_txn(conn):
        row = _load_query_row(conn, query_id)
        audits = _load_audits(conn, query_id)
        days = []
        counts = {AUDIT_VALID: 0, AUDIT_ANOMALOUS: 0, AUDIT_UNVERIFIABLE: 0}
        anomalous_days = []
        unverifiable_days = []
        for audit in audits.values():
            day = _audit_day_payload(audit)
            if audit["status"] in counts:
                counts[audit["status"]] += 1
            if audit["status"] == AUDIT_ANOMALOUS:
                anomalous_days.append(day)
            elif audit["status"] == AUDIT_UNVERIFIABLE:
                unverifiable_days.append(day)
            days.append(day)
        return {
            "station": station,
            "query_id": query_id,
            "created_at": row["created_at"],
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "days_total": len(days),
            "valid_total": counts[AUDIT_VALID],
            "anomalous_total": counts[AUDIT_ANOMALOUS],
            "unverifiable_total": counts[AUDIT_UNVERIFIABLE],
            "anomalous_days": anomalous_days,
            "unverifiable_days": unverifiable_days,
            "days": days,
        }


def get_page(conn: sqlite3.Connection, query_id: str, page_no: int, station: str) -> dict:
    with read_txn(conn):
        _load_query_row(conn, query_id)
    _materialize_query_audits(conn, query_id)
    with read_txn(conn):
        row = _load_query_row(conn, query_id)
        page_row = conn.execute(
            "SELECT dates FROM query_page WHERE query_id = ? AND page_no = ?",
            (query_id, page_no),
        ).fetchone()
        if page_row is None:
            raise ServiceError(
                404,
                "page_not_found",
                f"query {query_id} has no page {page_no}",
                query_id=query_id,
                page_no=page_no,
                pages_total=int(row["pages_total"]),
            )
        dates = json.loads(page_row["dates"])
        placeholders = ",".join("?" for _ in dates)
        stored = {
            r["date"]: r
            for r in conn.execute(
                "SELECT date, revision, source, counts, minutes_total"
                f" FROM query_day WHERE query_id = ? AND date IN ({placeholders})",
                (query_id, *dates),
            )
        }
        audits = _load_audits(conn, query_id)
        days = []
        for day_iso in dates:
            day = stored.get(day_iso)
            if day is None:
                days.append(
                    {
                        "date": day_iso,
                        "available": False,
                        "revision": None,
                        "source": None,
                        "counts": None,
                        "minutes_total": None,
                        "bin_status": AUDIT_NOT_APPLICABLE,
                        "anomalous": False,
                        "original_bins": None,
                    }
                )
            else:
                audit = audits.get(day_iso)
                bin_status = audit["status"] if audit else AUDIT_UNVERIFIABLE
                days.append(
                    {
                        "date": day_iso,
                        "available": True,
                        "revision": int(day["revision"]),
                        "source": day["source"],
                        "counts": json.loads(day["counts"]),
                        "minutes_total": int(day["minutes_total"]),
                        "bin_status": bin_status,
                        "anomalous": bin_status == AUDIT_ANOMALOUS,
                        "original_bins": audit["original_bins"] if audit else None,
                    }
                )
        return {
            "station": station,
            "query_id": query_id,
            "page_no": page_no,
            "page_size": int(row["page_size"]),
            "pages_total": int(row["pages_total"]),
            "days_total": int(row["days_total"]),
            "effective_boundaries": json.loads(row["effective_boundaries"]),
            "bins": json.loads(row["bin_definitions"]),
            "days": days,
        }


def get_query_day(
    conn: sqlite3.Connection, query_id: str, day: date, station: str
) -> dict:
    with read_txn(conn):
        _load_query_row(conn, query_id)
    _materialize_query_audits(conn, query_id)
    with read_txn(conn):
        row = _load_query_row(conn, query_id)
        day_iso = iso(day)
        if not (row["start_date"] <= day_iso <= row["end_date"]):
            raise ServiceError(
                404,
                "date_outside_query",
                f"{day_iso} is outside the query range",
                query_id=query_id,
                date=day_iso,
            )
        stored = conn.execute(
            "SELECT date, revision, source, counts, minutes_total FROM query_day"
            " WHERE query_id = ? AND date = ?",
            (query_id, day_iso),
        ).fetchone()
        if stored is None:
            raise ServiceError(
                404,
                "day_missing_in_query",
                f"{day_iso} had no data when the query was frozen",
                query_id=query_id,
                date=day_iso,
            )
        audit = _load_audits(conn, query_id).get(day_iso)
        bin_status = audit["status"] if audit else AUDIT_UNVERIFIABLE
        return {
            "station": station,
            "query_id": query_id,
            "date": stored["date"],
            "available": True,
            "revision": int(stored["revision"]),
            "source": stored["source"],
            "counts": json.loads(stored["counts"]),
            "minutes_total": int(stored["minutes_total"]),
            "effective_boundaries": json.loads(row["effective_boundaries"]),
            "bins": json.loads(row["bin_definitions"]),
            "bin_status": bin_status,
            "anomalous": bin_status == AUDIT_ANOMALOUS,
            "original_bins": audit["original_bins"] if audit else None,
            "audit": audit,
        }
