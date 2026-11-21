"""HTTP-level tests: day writing, edge vetting, frozen queries, paging."""

from __future__ import annotations

import json
import os
import sqlite3

from app import db

from .conftest import minutes


def _bins(edges, counts):
    return {"mode": "bins", "boundaries": edges, "counts": counts}


def _legacy_bins_day(date: str, revision: int, intervals: list[dict]) -> None:
    """Insert a bins-day straight into storage, as the old release left it."""
    conn = db.connect(os.environ["NOISE_DB"])
    try:
        with db.write_txn(conn):
            cur = conn.execute(
                "INSERT INTO day (date, revision, source, created_at, updated_at)"
                " VALUES (?, ?, 'bins', 'legacy', 'legacy')",
                (date, revision),
            )
            day_id = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO bin_interval (day_id, idx, lower, upper, minutes)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (day_id, i, b["lower"], b["upper"], b["minutes"])
                    for i, b in enumerate(intervals)
                ],
            )
    finally:
        conn.close()


def _legacy_query(
    query_id: str,
    date: str,
    revision: int,
    source: str,
    counts: list[int],
    *,
    boundaries: list[int] | None = None,
    days_total: int = 1,
    page_size: int = 31,
) -> None:
    """Freeze a query snapshot the way the old release did (no audit rows)."""
    boundaries = boundaries or []
    conn = db.connect(os.environ["NOISE_DB"])
    try:
        day_id = conn.execute(
            "SELECT day_id FROM day WHERE date = ?", (date,)
        ).fetchone()["day_id"]
        definitions = [
            {
                "index": i,
                "lower": boundaries[i - 1] if i else None,
                "upper": boundaries[i] if i < len(boundaries) else None,
                "label": "legacy",
            }
            for i in range(len(boundaries) + 1)
        ]
        with db.write_txn(conn):
            conn.execute(
                "INSERT INTO query_log (query_id, created_at, start_date, end_date,"
                " requested_boundaries, ignored_duplicates, effective_boundaries,"
                " bin_definitions, rejected, missing_dates, page_size, days_total,"
                " pages_total) VALUES (?, 'legacy', ?, ?, ?, '[]', ?, ?, '[]', '[]',"
                " ?, ?, 1)",
                (
                    query_id,
                    date,
                    date,
                    json.dumps(boundaries),
                    json.dumps(boundaries),
                    json.dumps(definitions),
                    page_size,
                    days_total,
                ),
            )
            conn.execute(
                "INSERT INTO query_day (query_id, date, day_id, revision, source,"
                " counts, minutes_total) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (query_id, date, day_id, revision, source, json.dumps(counts), 1440),
            )
            conn.execute(
                "INSERT INTO query_page (query_id, page_no, dates) VALUES (?, 1, ?)",
                (query_id, json.dumps([date])),
            )
    finally:
        conn.close()


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["days_stored"] == 0


def test_explicit_bins_with_unbounded_inner_edges_are_rejected(client):
    response = client.put(
        "/days/2024-01-01",
        json={
            "mode": "bins",
            "intervals": [
                {"lower": None, "upper": 55, "minutes": 100},
                {"lower": None, "upper": None, "minutes": 500},
                {"lower": 65, "upper": None, "minutes": 840},
            ],
        },
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "bins_not_contiguous"
    assert error["problems"]
    assert client.get("/days/2024-01-01").status_code == 404


def test_overlapping_explicit_bins_are_rejected(client):
    response = client.put(
        "/days/2024-01-01",
        json={
            "mode": "bins",
            "intervals": [
                {"lower": None, "upper": 60, "minutes": 100},
                {"lower": 55, "upper": 65, "minutes": 500},
                {"lower": 65, "upper": None, "minutes": 840},
            ],
        },
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "bins_not_contiguous"
    assert error["problems"][0]["reason"] == "overlap"


def test_rejected_correction_keeps_the_stored_day_and_revision(client):
    good = {"mode": "bins", "boundaries": [55, 65], "counts": [100, 500, 840]}
    first = client.put("/days/2024-02-01", json=good).json()
    assert first["revision"] == 1

    for bad in (
        {
            "mode": "bins",
            "intervals": [
                {"lower": None, "upper": 55, "minutes": 100},
                {"lower": 55, "upper": None, "minutes": 500},
                {"lower": 65, "upper": None, "minutes": 840},
            ],
        },
        {
            "mode": "bins",
            "intervals": [
                {"lower": None, "upper": 55, "minutes": 100},
                {"lower": 55, "upper": 70, "minutes": 500},
                {"lower": 65, "upper": None, "minutes": 840},
            ],
        },
        {"mode": "bins", "boundaries": [55, 65], "counts": [100, 500]},
    ):
        response = client.put("/days/2024-02-01", json=bad)
        assert response.status_code == 422

    stored = client.get("/days/2024-02-01").json()
    assert stored["revision"] == 1
    assert stored["intervals"] == [
        {"lower": None, "upper": 55, "minutes": 100},
        {"lower": 55, "upper": 65, "minutes": 500},
        {"lower": 65, "upper": None, "minutes": 840},
    ]


def test_new_query_refuses_legacy_anomalous_day_without_a_snapshot(client):
    _legacy_bins_day(
        "2024-03-01",
        7,
        [
            {"lower": None, "upper": 55, "minutes": 600},
            {"lower": 70, "upper": None, "minutes": 840},
        ],
    )
    response = client.post(
        "/queries",
        json={
            "start_date": "2024-03-01",
            "end_date": "2024-03-02",
            "boundaries": [60],
        },
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "anomalous_day_bins"
    assert error["anomalous_dates"] == ["2024-03-01"]
    assert error["anomalies"][0]["date"] == "2024-03-01"
    assert error["anomalies"][0]["revision"] == 7
    assert error["anomalies"][0]["problems"]

    # No snapshot was frozen: querying for it fails, and nothing was logged.
    conn = sqlite3.connect(os.environ["NOISE_DB"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM query_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM query_day").fetchone()[0] == 0
    finally:
        conn.close()

    # Corrections are still allowed and clear the anomaly for the next query.
    fixed = client.put("/days/2024-03-01", json=_bins([55, 65], [600, 400, 440]))
    assert fixed.status_code == 200
    fresh = client.post(
        "/queries",
        json={
            "start_date": "2024-03-01",
            "end_date": "2024-03-01",
            "boundaries": [60],
        },
    )
    assert fresh.status_code == 201


def test_old_query_still_pages_and_audit_names_the_anomaly(client):
    # A snapshot frozen by the old release over a day whose bins leave a gap.
    _legacy_bins_day(
        "2024-04-01",
        3,
        [
            {"lower": None, "upper": 55, "minutes": 600},
            {"lower": 70, "upper": None, "minutes": 840},
        ],
    )
    _legacy_query("q-legacy-anomalous", "2024-04-01", 3, "bins", [1440])

    meta = client.get("/queries/q-legacy-anomalous").json()
    assert meta["audit"]["anomalous_total"] == 1
    anomaly = meta["audit"]["anomalous_days"][0]
    assert anomaly["date"] == "2024-04-01"
    assert anomaly["revision"] == 3
    assert anomaly["status"] == "anomalous"
    assert anomaly["original_bins"] == [
        {"lower": None, "upper": 55, "minutes": 600},
        {"lower": 70, "upper": None, "minutes": 840},
    ]

    # The day detail says which date/revision the verdict belongs to.
    day = client.get("/queries/q-legacy-anomalous/days/2024-04-01").json()
    assert day["audit"]["status"] == "anomalous"
    assert day["audit"]["revision"] == 3
    assert day["counts"] == [1440]

    # The frozen page still serves the original bins and counts.
    page = client.get("/queries/q-legacy-anomalous/pages/1").json()
    assert page["days"][0]["counts"] == [1440]
    assert page["days"][0]["revision"] == 3
    assert page["days"][0]["bin_status"] == "anomalous"
    assert page["days"][0]["anomalous"] is True


def test_legacy_query_becomes_unverifiable_after_overwrite(client):
    _legacy_bins_day(
        "2024-05-01",
        5,
        [
            {"lower": None, "upper": 55, "minutes": 600},
            {"lower": 55, "upper": None, "minutes": 840},
        ],
    )
    _legacy_query("q-legacy-overwritten", "2024-05-01", 5, "bins", [1440])
    # The reviewer corrects the day before ever opening the old query.
    client.put("/days/2024-05-01", json={"mode": "minutes", "values": minutes(l50=1440)})

    meta = client.get("/queries/q-legacy-overwritten").json()
    assert meta["audit"]["unverifiable_total"] == 1
    assert meta["audit"]["anomalous_total"] == 0
    entry = meta["audit"]["unverifiable_days"][0]
    assert entry["date"] == "2024-05-01"
    assert entry["revision"] == 5
    assert entry["status"] == "unverifiable"
    assert "no longer stored" in entry["note"]

    # Still frozen and still pageable under the original query id.
    page = client.get("/queries/q-legacy-overwritten/pages/1").json()
    assert page["days"][0]["revision"] == 5
    assert page["days"][0]["counts"] == [1440]
    assert page["days"][0]["bin_status"] == "unverifiable"


def test_verdict_survives_correction_and_restart(client):
    client.put("/days/2024-06-01", json=_bins([55], [600, 840]))
    created = client.post(
        "/queries",
        json={
            "start_date": "2024-06-01",
            "end_date": "2024-06-01",
            "boundaries": [55],
        },
    ).json()
    qid = created["query_id"]
    before = client.get(f"/queries/{qid}/audit").json()
    assert before["valid_total"] == 1
    assert before["days"][0]["status"] == "valid"
    assert before["days"][0]["revision"] == 1

    client.put("/days/2024-06-01", json={"mode": "minutes", "values": minutes(l50=1440)})

    from fastapi.testclient import TestClient

    import app.main as main_module

    with TestClient(main_module.app) as restarted:
        after = restarted.get(f"/queries/{qid}/audit").json()
        day = restarted.get(f"/queries/{qid}/days/2024-06-01").json()
        page = restarted.get(f"/queries/{qid}/pages/1").json()
    assert after == before  # verdict preserved byte-for-byte
    assert day["audit"] == before["days"][0]
    assert page["days"][0]["revision"] == 1
    assert page["days"][0]["bin_status"] == "valid"


def test_legacy_contiguous_day_is_audited_valid_and_stays_valid(client):
    _legacy_bins_day(
        "2024-07-01",
        4,
        [
            {"lower": None, "upper": 55, "minutes": 600},
            {"lower": 55, "upper": None, "minutes": 840},
        ],
    )
    _legacy_query(
        "q-legacy-valid", "2024-07-01", 4, "bins", [600, 840], boundaries=[55]
    )

    first = client.get("/queries/q-legacy-valid/audit").json()
    assert first["valid_total"] == 1
    assert first["days"][0]["status"] == "valid"
    assert first["days"][0]["revision"] == 4
    assert first["days"][0]["original_bins"] == [
        {"lower": None, "upper": 55, "minutes": 600},
        {"lower": 55, "upper": None, "minutes": 840},
    ]

    # Correcting the day afterwards must not erase the recorded judgement.
    client.put("/days/2024-07-01", json={"mode": "minutes", "values": minutes(l40=1440)})
    second = client.get("/queries/q-legacy-valid/audit").json()
    assert second == first
    day = client.get("/queries/q-legacy-valid/days/2024-07-01").json()
    assert day["counts"] == [600, 840]  # frozen snapshot unchanged
    assert day["revision"] == 4
    assert day["bin_status"] == "valid"
    assert day["original_bins"] == first["days"][0]["original_bins"]


def test_refusal_lists_every_anomalous_day_and_only_those(client):
    _legacy_bins_day(
        "2024-08-01",
        2,
        [
            {"lower": None, "upper": None, "minutes": 700},
            {"lower": None, "upper": None, "minutes": 740},
        ],
    )
    # A valid contiguous legacy day in the same range must not be reported.
    _legacy_bins_day(
        "2024-08-02",
        1,
        [
            {"lower": None, "upper": 55, "minutes": 600},
            {"lower": 55, "upper": None, "minutes": 840},
        ],
    )
    _legacy_bins_day(
        "2024-08-03",
        9,
        [
            {"lower": None, "upper": 55, "minutes": 600},
            {"lower": 70, "upper": None, "minutes": 840},
        ],
    )
    response = client.post(
        "/queries",
        json={
            "start_date": "2024-08-01",
            "end_date": "2024-08-03",
            "boundaries": [55, 65],
        },
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["anomalous_dates"] == ["2024-08-01", "2024-08-03"]
    assert [(a["date"], a["revision"]) for a in error["anomalies"]] == [
        ("2024-08-01", 2),
        ("2024-08-03", 9),
    ]


def test_write_and_read_minutes_day(client):
    values = minutes(l50=600, l62=600, l70=240)
    written = client.put("/days/2024-01-01", json={"mode": "minutes", "values": values})
    assert written.status_code == 200
    assert written.json()["revision"] == 1

    stored = client.get("/days/2024-01-01").json()
    assert stored["values"] == values
    assert stored["source"] == "minutes"


def test_correction_bumps_revision_and_replaces_previous_shape(client):
    client.put("/days/2024-01-01", json={"mode": "minutes", "values": minutes(l50=1440)})
    rewritten = client.put("/days/2024-01-01", json=_bins([60], [100, 1340]))
    assert rewritten.status_code == 200
    assert rewritten.json()["revision"] == 2

    stored = client.get("/days/2024-01-01").json()
    assert stored["source"] == "bins"
    assert stored["values"] is None
    assert stored["intervals"] == [
        {"lower": None, "upper": 60, "minutes": 100},
        {"lower": 60, "upper": None, "minutes": 1340},
    ]


def test_bad_minute_payload_rejected(client):
    response = client.put("/days/2024-01-01", json={"mode": "minutes", "values": [1, 2]})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "wrong_minute_count"
    assert client.get("/days/2024-01-01").status_code == 404


def test_bad_bin_payload_rejected(client):
    response = client.put(
        "/days/2024-01-01", json=_bins([60], [100, 100])  # does not sum to 1440
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "bin_total_mismatch"


def test_list_days(client):
    client.put("/days/2024-01-02", json={"mode": "minutes", "values": minutes(l50=1440)})
    client.put("/days/2024-01-05", json={"mode": "minutes", "values": minutes(l50=1440)})
    body = client.get(
        "/days", params={"start_date": "2024-01-01", "end_date": "2024-01-03"}
    ).json()
    assert [d["date"] for d in body["days"]] == ["2024-01-02"]


def test_query_rejects_edges_that_split_stored_bins(client):
    client.put(
        "/days/2024-03-01", json={"mode": "minutes", "values": minutes(l30=1440)}
    )
    client.put("/days/2024-03-02", json=_bins([55, 65], [100, 500, 840]))

    created = client.post(
        "/queries",
        json={
            "start_date": "2024-03-01",
            "end_date": "2024-03-02",
            "boundaries": [55, 60, 65, 70],
            "page_size": 1,
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["effective_boundaries"] == [55, 65]
    by_edge = {r["boundary"]: r for r in body["rejected"]}
    assert set(by_edge) == {60, 70}
    assert by_edge[60]["severed"] == [
        {
            "date": "2024-03-02",
            "original_bin": {"lower": 55, "upper": 65, "minutes": 500},
        }
    ]
    assert by_edge[70]["severed"][0]["original_bin"]["upper"] is None


def test_query_counts_use_kept_edges_and_boundary_goes_up(client):
    # 100 min below 55, 200 exactly 55, 300 exactly 60, ... etc.
    values = [54] * 100 + [55] * 200 + [60] * 300 + [65] * 400 + [70] * 440
    client.put("/days/2024-04-01", json={"mode": "minutes", "values": values})
    created = client.post(
        "/queries",
        json={
            "start_date": "2024-04-01",
            "end_date": "2024-04-01",
            "boundaries": [55, 60, 65, 70],
            "page_size": 31,
        },
    ).json()
    page = client.get(f"/queries/{created['query_id']}/pages/1").json()
    assert page["days"][0]["counts"] == [100, 200, 300, 400, 440]
    assert page["days"][0]["minutes_total"] == 1440


def test_missing_days_are_listed_and_paged_as_unavailable(client):
    client.put("/days/2024-05-01", json={"mode": "minutes", "values": minutes(l50=1440)})
    created = client.post(
        "/queries",
        json={
            "start_date": "2024-05-01",
            "end_date": "2024-05-03",
            "boundaries": [],
            "page_size": 2,
        },
    ).json()
    assert created["missing_dates"] == ["2024-05-02", "2024-05-03"]
    assert created["days_available"] == 1
    assert created["pages_total"] == 2
    page2 = client.get(f"/queries/{created['query_id']}/pages/2").json()
    assert [d["available"] for d in page2["days"]] == [False]
    assert page2["days"][0]["date"] == "2024-05-03"
    assert page2["days"][0]["counts"] is None


def test_query_frozen_after_correction_and_new_query_sees_correction(client):
    client.put(
        "/days/2024-06-01", json={"mode": "minutes", "values": minutes(l50=1000, l70=440)}
    )
    first = client.post(
        "/queries",
        json={
            "start_date": "2024-06-01",
            "end_date": "2024-06-01",
            "boundaries": [60],
            "page_size": 10,
        },
    ).json()
    before = client.get(f"/queries/{first['query_id']}/pages/1").json()
    assert before["days"][0]["counts"] == [1000, 440]

    correction = client.put(
        "/days/2024-06-01", json={"mode": "minutes", "values": minutes(l50=1440)}
    )
    assert correction.json()["revision"] == 2

    after_old = client.get(f"/queries/{first['query_id']}/pages/1").json()
    assert after_old["days"][0]["counts"] == [1000, 440]  # frozen
    assert after_old["days"][0]["revision"] == 1

    second = client.post(
        "/queries",
        json={
            "start_date": "2024-06-01",
            "end_date": "2024-06-01",
            "boundaries": [60],
            "page_size": 10,
        },
    ).json()
    after_new = client.get(f"/queries/{second['query_id']}/pages/1").json()
    assert after_new["days"][0]["counts"] == [1440, 0]
    assert after_new["days"][0]["revision"] == 2


def test_resume_paging_across_restart(client, tmp_path, monkeypatch):
    client.put("/days/2024-07-01", json={"mode": "minutes", "values": minutes(l50=1440)})
    client.put("/days/2024-07-02", json={"mode": "minutes", "values": minutes(l60=1440)})
    created = client.post(
        "/queries",
        json={
            "start_date": "2024-07-01",
            "end_date": "2024-07-02",
            "boundaries": [55],
            "page_size": 1,
        },
    ).json()
    qid = created["query_id"]
    page1 = client.get(f"/queries/{qid}/pages/1").json()

    # Simulate a process restart: a brand-new app instance on the same file.
    from fastapi.testclient import TestClient

    import app.main as main_module

    with TestClient(main_module.app) as restarted:
        meta = restarted.get(f"/queries/{qid}").json()
        assert meta["effective_boundaries"] == [55]
        assert meta["pages_total"] == 2
        page1_again = restarted.get(f"/queries/{qid}/pages/1").json()
        page2 = restarted.get(f"/queries/{qid}/pages/2").json()
    assert page1_again == page1
    assert page2["days"][0]["date"] == "2024-07-02"
    assert page2["days"][0]["counts"] == [0, 1440]


def test_duplicate_boundaries_are_deduplicated(client):
    created = client.post(
        "/queries",
        json={
            "start_date": "2024-08-01",
            "end_date": "2024-08-01",
            "boundaries": [55, 55, 60, 60],
        },
    ).json()
    assert created["requested_boundaries"] == [55, 60]
    assert created["ignored_duplicate_boundaries"] == [55, 60]
    assert created["effective_boundaries"] == [55, 60]


def test_query_range_validation(client):
    response = client.post(
        "/queries",
        json={"start_date": "2024-02-02", "end_date": "2024-02-01"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_range"


def test_query_day_lookup(client):
    client.put("/days/2024-09-01", json={"mode": "minutes", "values": minutes(l50=1440)})
    created = client.post(
        "/queries",
        json={
            "start_date": "2024-09-01",
            "end_date": "2024-09-02",
            "boundaries": [50],
        },
    ).json()
    qid = created["query_id"]
    day = client.get(f"/queries/{qid}/days/2024-09-01").json()
    # every reading is exactly 50 and an edge value goes to the higher band
    assert day["counts"] == [0, 1440]
    assert client.get(f"/queries/{qid}/days/2024-09-02").status_code == 404
    assert client.get(f"/queries/{qid}/days/2024-10-01").status_code == 404


def test_unknown_query_and_page(client):
    assert client.get("/queries/q-nope").status_code == 404
    created = client.post(
        "/queries",
        json={"start_date": "2024-10-01", "end_date": "2024-10-01"},
    ).json()
    assert client.get(f"/queries/{created['query_id']}/pages/2").status_code == 404
    assert client.get(f"/queries/{created['query_id']}/pages/0").status_code == 422
