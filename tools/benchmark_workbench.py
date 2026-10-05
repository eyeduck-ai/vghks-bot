"""Compare local snapshot/status costs using disposable synthetic databases."""
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
sys.path.insert(0, str(ROOT))

from vghks_bot.activity import search  # noqa: E402
from vghks_bot.bot import BotApplication  # noqa: E402
from vghks_bot.selftest_bot import BotSyntheticSDK  # noqa: E402
from vghks_bot.settings import Settings  # noqa: E402
from vghks_bot.workbench import snapshot, status  # noqa: E402


def measure(read):
    read()
    samples, result = [], None
    for _ in range(3):
        started = perf_counter()
        result = read()
        payload = json.dumps(result, ensure_ascii=False).encode()
        samples.append((perf_counter() - started) * 1000)
    return {"bytes": len(payload), "median_ms": round(statistics.median(samples), 3)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=int, default=1000)
    parser.add_argument("--report", type=Path, default=ROOT / ".build" / "workbench-benchmark.json")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="workbench-benchmark-") as directory:
        app = BotApplication(Settings(), Path(directory), BotSyntheticSDK)
        try:
            key = app.login({"username": "SYNTHETIC", "password": "synthetic"})["account"]["id"]
            work = app.workspace(key)
            members = [{"mrn": f"{i:08}", "name": f"合成病人{i}", "registrations": [], "source_records": [], "origins": ["manual"]} for i in range(65)]
            work.review.db.save_batch([("task", {"id": f"task{i:05}", "kind": "review", "name": "合成檢閱",
                "status": "completed", "created_at": "2026-10-04T08:00:00", "members": members}) for i in range(args.tasks)])
            if not args.tasks:
                raise ValueError("At least one task is required")
            work.review.save_note({"task_id": "task00000", "mrn": "00000000", "text": "synthetic note"})
            result = {"scope": "temporary synthetic database; no hospital requests", "tasks": args.tasks,
                      "patients_per_task": 65, "full_workbench": measure(lambda: snapshot(work)),
                      "compact_workbench": measure(lambda: snapshot(work, compact=True)),
                      "status": measure(lambda: status(work)),
                      "activity_page": measure(lambda: search(work, {"system": "門診系統"})),
                      "notes": measure(lambda: work.review.read_notes({"task_id": "task00000"}))}
            with patch("vghks_bot.library.sqlite3.connect", wraps=sqlite3.connect) as connect:
                work.review.read_notes({"task_id": "task00000"})
                result["notes"]["connections"] = connect.call_count
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            app.close()


if __name__ == "__main__":
    main()
