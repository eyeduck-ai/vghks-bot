"""Measure review pages on disposable synthetic data, with network forbidden."""
from __future__ import annotations

import argparse
import json
import socket
import sys
import tempfile
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools"), str(ROOT / "tests")]

from benchmark_efficiency import measure  # noqa: E402
from browser_bot_fixture import seed_large_review  # noqa: E402

from vghks_bot.bot import BotApplication  # noqa: E402
from vghks_bot.selftest_bot import BotSyntheticSDK  # noqa: E402
from vghks_bot.settings import Settings  # noqa: E402
from vghks_bot.workbench import snapshot  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[40, 1000, 5000, 20000])
    parser.add_argument("--report", type=Path, default=ROOT / ".build/review-paging-benchmark.json")
    args = parser.parse_args()
    if any(not 1 <= count <= 20000 for count in args.sizes):
        parser.error("sizes must be between 1 and 20000")
    report = {"scope": "disposable synthetic data; socket.connect denied; no user databases",
              "samples": 3, "corpora": []}
    with patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden in synthetic benchmark")):
        for count in args.sizes:
            with tempfile.TemporaryDirectory(prefix="review-page-benchmark-") as directory:
                app = BotApplication(Settings(), Path(directory), BotSyntheticSDK)
                try:
                    account = app.login({"username": "BENCHMARK", "password": "synthetic"})["account"]["id"]
                    work = app.workspace(account)
                    seed_large_review(work, count)
                    key = "synthetic-review-benchmark"
                    calls = list(BotSyntheticSDK.calls)
                    started = perf_counter()
                    work.review.pages.page({"id": key, "q": "missing synthetic phrase", "limit": 40})
                    cold_search_ms = round((perf_counter() - started) * 1000, 3)
                    row = {"patients": count, "cold_text_no_match_ms": cold_search_ms,
                        "page": measure(lambda work=work, key=key: work.review.pages.page({"id": key, "limit": 40})),
                        "last_page": measure(lambda work=work, key=key, count=count: work.review.pages.page({"id": key, "limit": 40, "focus_mrn": f"PAGE{count:06}"})),
                        "detail": measure(lambda work=work, key=key: work.review.pages.patient({"id": key, "mrn": "PAGE000001"})),
                        "notes_page": measure(lambda work=work, key=key, count=count: work.review.pages.notes({"task_id": key,
                            "mrns": [f"PAGE{i:06}" for i in range(1, min(count, 40)+1)]})),
                        "notes_summary": measure(lambda work=work, key=key: work.review.pages.notes({"task_id": key}, summary=True)),
                        "compact_workbench": measure(lambda work=work: snapshot(work, compact=True)),
                        "text_no_match": measure(lambda work=work, key=key: work.review.pages.page({"id": key, "q": "missing synthetic phrase", "limit": 40}))}
                    if count <= 1000:
                        row["legacy_full_results"] = measure(lambda work=work, key=key: work.review.results({"id": key}))
                    if row["page"]["soap_documents"] or row["last_page"]["soap_documents"]:
                        raise AssertionError("Patient list decoded SOAP bodies")
                    if row["detail"]["soap_documents"] != 1:
                        raise AssertionError("Single patient did not read exactly its SOAP snapshot")
                    if row["page"]["sql_statements"] > 25 or row["page"]["response_bytes"] > 120000:
                        raise AssertionError("Patient page exceeded its bounded read budget")
                    if calls != BotSyntheticSDK.calls:
                        raise AssertionError("Review read issued an SDK request")
                    report["corpora"].append(row)
                    print(json.dumps(row, ensure_ascii=False), flush=True)
                finally:
                    app.close()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
