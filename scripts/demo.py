"""End-to-end demo against a running instance.

Usage:
    python scripts/demo.py [BASE_URL]

Defaults to http://127.0.0.1:8000.  Requires only the standard library.

Scenarios
---------
1. A day of per-minute readings and a day of original-bin counts.
2. Create a query whose candidate edges get vetted: edges that split an
   original bin are dropped and reported with the offending day and bin.
3. Page through the frozen result, resuming page by page.
4. Correct a day, show the old query still serves its frozen numbers and a
   new query picks up the correction.
5. Anomalous historical bins (only if ``scripts/legacy_seed.py`` ran first):
   a new query is refused with the dates and revisions, an old query still
   pages and its audit names the anomaly or says the original bins are gone,
   and after corrections a new query succeeds.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")


def call(method: str, path: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def show(title: str, payload) -> None:
    print(f"\n== {title} ==")
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def rows(days: list[dict]) -> None:
    print(f"  {'date':<12} {'available':<10} {'rev':<4} {'source':<8} counts")
    for day in days:
        counts = day["counts"]
        print(
            f"  {day['date']:<12} {day['available']!s:<10} "
            f"{day['revision']!s:<4} {day['source']!s:<8} {counts}"
        )


def minutes(values: list[tuple[int, int]]) -> list[int]:
    out: list[int] = []
    for level, count in values:
        out.extend([level] * count)
    assert len(out) == 1440, len(out)
    return out


def main() -> int:
    status, health = call("GET", "/health")
    print(f"health: {status} {health}")

    # 2023: 100 min <55, 200 min ==55, 300 ==60, 400 ==65, 440 ==70.
    day_2023 = minutes([(54, 100), (55, 200), (60, 300), (65, 400), (70, 440)])
    status, written = call(
        "PUT", "/days/2023-06-01", {"mode": "minutes", "values": day_2023}
    )
    print(f"PUT 2023-06-01 (minutes): {status} rev={written.get('revision')}")

    # 2024 is only known as two coarse original bins: [null,65) and [65,null).
    status, written = call(
        "PUT",
        "/days/2024-06-01",
        {"mode": "bins", "boundaries": [65], "counts": [600, 840]},
    )
    print(f"PUT 2024-06-01 (bins):    {status} rev={written.get('revision')}")

    request = {
        "start_date": "2023-06-01",
        "end_date": "2024-06-01",
        "boundaries": [55, 60, 65, 70],
        "page_size": 366,
    }
    status, query = call("POST", "/queries", request)
    print(f"\nPOST /queries: {status}")
    show(
        "edge vetting",
        {
            "requested": query["requested_boundaries"],
            "effective": query["effective_boundaries"],
            "rejected": [
                {
                    "boundary": item["boundary"],
                    "severed": item["severed"],
                }
                for item in query["rejected"]
            ],
            "bins": query["bins"],
            "days_total": query["days_total"],
            "days_available": query["days_available"],
            "missing_days": len(query["missing_dates"]),
            "pages_total": query["pages_total"],
        },
    )
    query_id = query["query_id"]

    # Page 1 has 366 days of which only two carry data; page 2 holds the last.
    status, page = call("GET", f"/queries/{query_id}/pages/1")
    available = [d for d in page["days"] if d["available"]]
    print(
        f"\n== page 1 of {page['pages_total']}: "
        f"{len(available)} available of {len(page['days'])} days =="
    )
    rows(available)
    status, page = call("GET", f"/queries/{query_id}/pages/{page['pages_total']}")
    print(f"\n== page {page['page_no']} of {page['pages_total']} ==")
    rows([d for d in page["days"] if d["available"]])

    # Correction: 2024-06-01 re-sent with finer detail. Storage accepts it;
    # the frozen query keeps its old view, a new query re-vets from scratch.
    corrected = minutes([(50, 300), (64, 300), (70, 840)])
    status, written = call(
        "PUT", "/days/2024-06-01", {"mode": "minutes", "values": corrected}
    )
    print(f"\nPUT 2024-06-01 correction: {status} rev={written.get('revision')}")

    status, page = call("GET", f"/queries/{query_id}/pages/{query['pages_total']}")
    print(f"\n== old query {query_id[:10]}... after correction (unchanged) ==")
    rows([d for d in page["days"] if d["available"]])

    status, fresh = call("POST", "/queries", request)
    status, page = call(
        "GET", f"/queries/{fresh['query_id']}/pages/{fresh['pages_total']}"
    )
    show(
        "new query: correction visible, all four edges now survive",
        {
            "query_id": fresh["query_id"],
            "effective": fresh["effective_boundaries"],
            "rejected": fresh["rejected"],
        },
    )
    print(f"\n== new query {fresh['query_id'][:10]}... page ==")
    rows([d for d in page["days"] if d["available"]])

    demo_anomalies()
    return 0


def demo_anomalies() -> None:
    """Show the anomalous-historical-bins flow when the seed data is present."""
    status, seeded = call("GET", "/queries/q-legacy-anomaly")
    if status == 404:
        print(
            "\n== anomalous historical bins: skipped =="
            "\n  run `docker compose exec api python scripts/legacy_seed.py` first"
        )
        return

    print("\n" + "=" * 72)
    print("== anomalous historical bins ==")
    show(
        "old query q-legacy-anomaly: audit verdicts",
        seeded.get("audit"),
    )

    # A new query over the seeded range is refused, and says why and which
    # revision it saw.  No snapshot is created.
    refused_request = {
        "start_date": "2022-01-15",
        "end_date": "2022-01-16",
        "boundaries": [55, 65],
    }
    status, refused = call("POST", "/queries", refused_request)
    print(f"\nPOST /queries over anomalous days -> {status} {refused['error']['code']}")
    show(
        "refusal: dates and revisions, no snapshot",
        {
            "anomalous_dates": refused["error"]["anomalous_dates"],
            "anomalies": [
                {"date": a["date"], "revision": a["revision"],
                 "problems": [p["code"] + ":" + str(p.get("reason")) for p in a["problems"]]}
                for a in refused["error"]["anomalies"]
            ],
        },
    )

    # The old queries keep paging under their original ids and show verdicts.
    for qid, label in (
        ("q-legacy-anomaly", "still-stored original bins -> anomalous"),
        ("q-legacy-overwritten", "original bins overwritten -> unverifiable"),
    ):
        status, meta = call("GET", f"/queries/{qid}")
        status, page = call("GET", f"/queries/{qid}/pages/1")
        print(f"\n== old query {qid} ({label}) ==")
        rows(page["days"])
        day = page["days"][0]
        print(
            f"  bin_status={day['bin_status']} anomalous={day['anomalous']} "
            f"revision={day['revision']}"
        )
        if meta["audit"]["unverifiable_days"]:
            print(f"  note: {meta['audit']['unverifiable_days'][0]['note']}")

    # Correct both anomalous days, then a new query succeeds.
    corrected = minutes([(50, 300), (64, 300), (70, 840)])
    for day_iso in ("2022-01-15", "2022-01-16"):
        status, written = call(
            "PUT",
            f"/days/{day_iso}",
            {"mode": "minutes", "values": corrected},
        )
        print(f"\nPUT {day_iso} correction: {status} rev={written.get('revision')}")
    status, fresh = call("POST", "/queries", refused_request)
    print(f"POST /queries after corrections -> {status}")
    show(
        "new query succeeds; old queries untouched",
        {
            "query_id": fresh["query_id"],
            "effective_boundaries": fresh["effective_boundaries"],
            "rejected": fresh["rejected"],
        },
    )
    status, old_meta = call("GET", "/queries/q-legacy-anomaly")
    show(
        "old query verdicts after correction + restart-worth of reads",
        old_meta["audit"],
    )


if __name__ == "__main__":
    raise SystemExit(main())
