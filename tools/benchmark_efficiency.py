"""Reproducible local read-cost acceptance using temporary synthetic data."""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import tempfile
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from test_cataract_reads import seed_patients  # noqa: E402

from vghks_bot.bot import BotApplication  # noqa: E402
from vghks_bot.library import encoded  # noqa: E402
from vghks_bot.library_search import ensure  # noqa: E402
from vghks_bot.selftest_bot import BotSyntheticSDK  # noqa: E402
from vghks_bot.settings import Settings  # noqa: E402
from vghks_bot.workbench import status  # noqa: E402


def measure(read):
    read()
    samples, measurements = [], []
    original_connect, original_loads = sqlite3.connect, json.loads
    for _ in range(3):
        counters = {"sql_statements": 0, "json_bytes": 0, "soap_bytes": 0, "soap_documents": 0}

        def traced(*args, counters=counters, **kwargs):
            db = original_connect(*args, **kwargs)
            def count(_):
                counters["sql_statements"] += 1
            db.set_trace_callback(count)
            return db

        def loads(value, *args, counters=counters, **kwargs):
            size = len(value.encode()) if isinstance(value, str) else len(value)
            counters["json_bytes"] += size
            if isinstance(value, str) and '"soap"' in value:
                counters["soap_bytes"] += size
                counters["soap_documents"] += 1
            return original_loads(value, *args, **kwargs)

        started = perf_counter()
        with patch("vghks_bot.library.sqlite3.connect", side_effect=traced), patch("json.loads", side_effect=loads):
            result = read()
        elapsed = (perf_counter() - started) * 1000
        payload = json.dumps(result, ensure_ascii=False).encode()
        samples.append(elapsed)
        measurements.append({**counters, "response_bytes": len(payload)})
    # Structural counters should be deterministic; timings are a warm median.
    if any(row != measurements[0] for row in measurements[1:]):
        raise AssertionError("Non-deterministic read counters")
    return {"median_ms": round(statistics.median(samples), 3), **measurements[0]}


def corpus(work, count):
    records = [{"id": f"synthetic-{i:06}", "mrn": f"TEST{i:06}", "name": f"合成病人{i:06}",
                "date": "2026-09-11", "section": "眼科", "case_no": str(i),
                "soap": "A+P：# FU (Straße) 100%_ " + "synthetic " * 850} for i in range(count)]
    with work.store.library.batch() as db:
        db.executemany("INSERT INTO records VALUES(?,?,?,?,?,?,?)", [(r["id"], r["mrn"], r["date"], r["section"], encoded(r),
                       "2026-10-04T08:00:00", "2026-10-04T08:00:00") for r in records])
        db.executemany("INSERT INTO sources VALUES(?,?,?)", [(r["id"], work.username, "synthetic") for r in records])
        work.review.db.save("task", {"id": "scoped", "kind": "review", "name": "Synthetic page", "status": "completed",
            "created_at": "2026-10-04T08:00:00", "members": [{"mrn": r["mrn"]} for r in records[:40]]})
        for r in records[:40]:
            work.review.db.item("scoped", r["mrn"], {"status": "reviewed", "records": [r]})
        work.review.db.save("task", {"id": "progress", "kind": "review", "name": "Synthetic progress", "status": "running",
            "created_at": "2026-10-04T08:00:00", "members": [{"mrn": r["mrn"]} for r in records[:1000]]})
        for r in records[:1000]:
            work.review.db.item("progress", r["mrn"], {"status": "reviewed", "records": [r]})
    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 5000, 20000])
    parser.add_argument("--report", type=Path, default=ROOT / ".build" / "efficiency-benchmark.json")
    args = parser.parse_args()
    report = {"scope": "disposable synthetic data; no hospital connection", "samples": 3, "corpora": []}
    for count in args.sizes:
        with tempfile.TemporaryDirectory(prefix="efficiency-") as directory:
            app = BotApplication(Settings(), Path(directory), BotSyntheticSDK)
            try:
                key = app.login({"username": "SYNTHETIC", "password": "synthetic"})["account"]["id"]
                work = app.workspace(key)
                corpus(work, count)
                started = perf_counter()
                ensure(work.store.library, work.settings)
                row = {"records": count, "cold_index_ms": round((perf_counter() - started) * 1000, 3),
                    "scoped_page": measure(lambda work=work: work.library_search({"task_id": "scoped", "limit": 40})),
                    "record_page": measure(lambda work=work: work.store.library.search({"limit": 40}, work.settings)),
                    "progress": measure(lambda work=work: work.review.task("progress", summary=True)),
                    "inventory": measure(lambda work=work: work.library_data.read({"q": "no such patient", "limit": 40})),
                    "status": measure(lambda work=work: status(work))}
                for name in ("scoped_page", "record_page"):
                    if row[name]["soap_documents"] != 40:
                        raise AssertionError("A page decoded records outside its scope")
                if row["progress"]["soap_documents"]:
                    raise AssertionError("Progress decoded clinical data")
                if count == args.sizes[0]:
                    cohort = seed_patients(work, count=65, order_count=10)
                    calls = list(BotSyntheticSDK.calls)
                    row["cataract_65_by_10"] = measure(lambda work=work, cohort=cohort: work.analysis.cataract_status({"cohort_id": cohort["id"]}))
                    if row["cataract_65_by_10"]["sql_statements"] > 100 or calls != BotSyntheticSDK.calls:
                        raise AssertionError("Cataract status exceeded its local query budget")
                report["corpora"].append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
            finally:
                app.close()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
