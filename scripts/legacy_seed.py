"""Seed a database with data as the previous release could leave it.

Run this once against the database a running instance is using.  It
reproduces the two historical situations the current release must handle:

* ``q-legacy-anomaly`` – a frozen query over a bins-day whose original bins
  are *not* contiguous (two bins that each claim the whole line, so they
  overlap).  The day is still stored at the revision the snapshot adopted,
  so the verdict can be verified as ``anomalous`` and the anomaly report can
  show its shape.
* ``q-legacy-overwritten`` – a frozen query over a gapped bins-day that was
  later corrected to minute readings *before* the upgrade.  The adopted
  original bins are gone for good, so the verdict is ``unverifiable`` while
  the snapshot's counts are still served.

The seed writes directly to SQLite (the current API rightly refuses to store
such days).  It leaves the audit table empty and then settles verdicts the
same way startup does, exactly like an upgrade over an old database.

Usage inside the container (where /data/noise.db lives)::

    docker compose exec api python scripts/legacy_seed.py

It is idempotent: existing days and queries are left untouched.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, service

ANOMALY_DAY = "2022-01-15"
OVERWRITTEN_DAY = "2022-01-16"
ANOMALY_QUERY = "q-legacy-anomaly"
OVERWRITTEN_QUERY = "q-legacy-overwritten"

# Two bins that each claim the whole line.  The old release only compared
# ``bin[i].lower == bin[i-1].upper`` and ``None == None`` slipped through.
OVERLAPPING_BINS = [
    {"lower": None, "upper": None, "minutes": 700},
    {"lower": None, "upper": None, "minutes": 740},
]
# A gap from 55 to 70, plus an unbounded middle bin: same defect, other shape.
GAPPED_BINS = [
    {"lower": None, "upper": 55, "minutes": 600},
    {"lower": 70, "upper": None, "minutes": 840},
]


def _insert_legacy_bins_day(conn, date_iso: str, revision: int, bins: list[dict]) -> None:
    exists = conn.execute("SELECT 1 FROM day WHERE date = ?", (date_iso,)).fetchone()
    if exists:
        print(f"  day {date_iso} already present, left untouched")
        return
    cur = conn.execute(
        "INSERT INTO day (date, revision, source, created_at, updated_at)"
        " VALUES (?, ?, 'bins', 'legacy', 'legacy')",
        (date_iso, revision),
    )
    day_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO bin_interval (day_id, idx, lower, upper, minutes)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (day_id, i, b["lower"], b["upper"], b["minutes"])
            for i, b in enumerate(bins)
        ],
    )
    print(f"  inserted legacy bins day {date_iso} (revision {revision})")


def _overwrite_with_minutes(conn, date_iso: str, revision: int) -> None:
    """Simulate the correction that happened before the upgrade."""
    row = conn.execute(
        "SELECT day_id, source, revision FROM day WHERE date = ?", (date_iso,)
    ).fetchone()
    if row is None:
        return
    if row["source"] != "bins":
        print(f"  day {date_iso} is already a minutes day, left untouched")
        return
    day_id = int(row["day_id"])
    conn.execute(
        "UPDATE day SET revision = ?, source = 'minutes', updated_at = 'legacy'"
        " WHERE day_id = ?",
        (revision, day_id),
    )
    conn.execute("DELETE FROM bin_interval WHERE day_id = ?", (day_id,))
    conn.executemany(
        "INSERT INTO reading (day_id, minute, value) VALUES (?, ?, ?)",
        [(day_id, i, 50) for i in range(1440)],
    )
    print(
        f"  day {date_iso} was corrected to minutes at revision {revision} "
        "after its snapshot was frozen"
    )


def _insert_legacy_query(
    conn, query_id: str, date_iso: str, revision: int, boundaries: list[int]
) -> None:
    exists = conn.execute(
        "SELECT 1 FROM query_log WHERE query_id = ?", (query_id,)
    ).fetchone()
    if exists:
        print(f"  query {query_id} already present, left untouched")
        return
    day_id = int(
        conn.execute("SELECT day_id FROM day WHERE date = ?", (date_iso,)).fetchone()[
            "day_id"
        ]
    )
    definitions = [
        {
            "index": i,
            "lower": boundaries[i - 1] if i else None,
            "upper": boundaries[i] if i < len(boundaries) else None,
            "label": (
                "(-inf, +inf)"
                if not boundaries
                else (f"<{boundaries[0]}" if i == 0 else f">={boundaries[-1]}")
            ),
        }
        for i in range(len(boundaries) + 1)
    ]
    # The old release counted each stored bin into one target band; with
    # these bins the stored total was 1440.
    conn.execute(
        "INSERT INTO query_log (query_id, created_at, start_date, end_date,"
        " requested_boundaries, ignored_duplicates, effective_boundaries,"
        " bin_definitions, rejected, missing_dates, page_size, days_total,"
        " pages_total) VALUES (?, 'legacy', ?, ?, ?, '[]', ?, ?, '[]', '[]',"
        " 31, 1, 1)",
        (
            query_id,
            date_iso,
            date_iso,
            json.dumps(boundaries),
            json.dumps(boundaries),
            json.dumps(definitions),
        ),
    )
    conn.execute(
        "INSERT INTO query_day (query_id, date, day_id, revision, source, counts,"
        " minutes_total) VALUES (?, ?, ?, ?, 'bins', ?, 1440)",
        (query_id, date_iso, day_id, revision, json.dumps([1440])),
    )
    conn.execute(
        "INSERT INTO query_page (query_id, page_no, dates) VALUES (?, 1, ?)",
        (query_id, json.dumps([date_iso])),
    )
    print(f"  inserted legacy query {query_id} over {date_iso} (revision {revision})")


def main() -> int:
    path = os.environ.get("NOISE_DB", str(db.default_db_path()))
    print(f"seeding legacy data into {path}")
    db.init_db(path)
    conn = db.connect(path)
    try:
        with db.write_txn(conn):
            _insert_legacy_bins_day(conn, ANOMALY_DAY, 2, OVERLAPPING_BINS)
            _insert_legacy_bins_day(conn, OVERWRITTEN_DAY, 1, GAPPED_BINS)
            _insert_legacy_query(conn, ANOMALY_QUERY, ANOMALY_DAY, 2, [])
            _insert_legacy_query(conn, OVERWRITTEN_QUERY, OVERWRITTEN_DAY, 1, [])
            _overwrite_with_minutes(conn, OVERWRITTEN_DAY, 2)
        settled = service.audit_pending_queries(conn)
        print(f"  settled bin verdicts for {settled} legacy quer(ies)")
    finally:
        conn.close()
    print(
        "\nSwagger walkthrough (http://127.0.0.1:8000/docs):\n"
        f"  1. GET  /queries/{ANOMALY_QUERY}          -> audit.anomalous_days=[{ANOMALY_DAY} rev 2]\n"
        f"  2. POST /queries                          -> 409 anomalous_day_bins with dates + revisions\n"
        f"  3. GET  /queries/{OVERWRITTEN_QUERY}      -> audit.unverifiable_days=[{OVERWRITTEN_DAY} rev 1]\n"
        "  4. POST /queries                          -> 409 (the anomalous day still blocks)\n"
        f"  5. PUT  /days/{ANOMALY_DAY}               -> correct the anomalous day\n"
        "  6. POST /queries                          -> 201; old queries still page unchanged\n"
        f"  7. GET  /queries/{ANOMALY_QUERY}          -> verdict stays 'anomalous'"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
