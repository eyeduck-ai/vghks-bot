"""Synthetic-only Google API validation payloads; never reads the live clinical sheet."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from analysis_fixtures import book_fixture, proposal  # noqa: E402

from opd_monitor.sheet_plan import Planner, verify  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["fixture", "plan", "verify"])
    parser.add_argument("--book", type=Path)
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--sheet-id", default="synthetic")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "fixture":
        result = book_fixture()
    else:
        book = json.loads(args.book.read_text(encoding="utf-8"))
        if args.mode == "plan":
            moving = proposal(date="2026-10-09")
            moving["target"] = "202609!6"
            result = Planner(book, args.sheet_id, [moving, proposal(mrn="0000003", side="OS")]).build()
        else:
            preview = json.loads(args.preview.read_text(encoding="utf-8"))
            result = {"verification": verify(book, preview)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
