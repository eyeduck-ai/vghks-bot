"""Compare Windows sheet parsers with fixtures exported from the actual GAS code.

Offline by default. --gas-repo also executes the known GAS test exporters locally;
this tool never opens credentials, Sheets, or Calendar.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vghks_sheet_core.calendar import arrival, full_day, headers, scan  # noqa: E402

from vghks_bot.sheet_archive import build_archive  # noqa: E402
from vghks_bot.sheet_archive_lock import LOCK_ID, LOCK_KEY  # noqa: E402
from vghks_bot.sheet_identity import header_columns  # noqa: E402
from vghks_bot.sheet_management import _report_rows  # noqa: E402
from vghks_bot.sheet_plan import columns, entries, typed_day  # noqa: E402
from vghks_bot.sheet_schema import (  # noqa: E402
    DAY_MARKER,
    HOSPITALS,
    canonical_header,
    cell_text,
    full_date,
    monthly_row,
)
from vghks_bot.sheet_statistics import statistics  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def gas_revision():
    manifest = json.loads((FIXTURES / "gas_contract.json").read_text(encoding="utf-8"))
    if (manifest.get("schema") != 1 or manifest.get("repository") != "eyeduck-ai/op_spreadsheet"
            or not re.fullmatch(r"[0-9a-f]{40}", manifest.get("revision", ""))):
        raise ValueError("Invalid GAS contract revision manifest")
    return manifest["revision"]


def literal(value):
    return {"userEnteredValue": {"numberValue" if isinstance(value, (int, float)) else "stringValue": value}}


def fixture_sheet(labels):
    return {"properties": {"title": "202610", "sheetId": 1,
            "gridProperties": {"columnCount": len(labels), "rowCount": 100}},
            "rows": [[literal(label) for label in labels]], "merges": []}


def check_contract(parsing, formatting):
    """Raise with a synthetic case index only; never echo clinical sheet values."""
    count = 0
    for index, case in enumerate(parsing["date_cases"]):
        value = literal(case["input"])
        for parser in (full_date, typed_day, full_day):
            if parser(value) != case["iso"]:
                raise ValueError(f"Date contract case {index} differs")
        count += 1
    sheet = fixture_sheet(formatting["monthly_headers"])
    _, monitor_cols = headers(sheet)
    main_cols = header_columns(sheet)
    if main_cols != monitor_cols or set(HOSPITALS) != set(formatting["hospitals"]) or DAY_MARKER != formatting["header_marker"]:
        raise ValueError("Field or hospital contract differs")
    columns(sheet)
    for index, case in enumerate(parsing["row_cases"]):
        values = {"日期／報到時間": case["date"], "病歷號": case["marker"],
                  "醫院": case["hospital"], "CalendarEventId": case.get("event", "")}
        row = [literal(values.get(label, "")) for label in formatting["monthly_headers"]]
        mapping = {"DATE_HEADER": "day", "HYBRID_CONFLICT": "invalid_header", "PATIENT": "patient", "BLANK": "blank"}
        if monthly_row(row, main_cols)["kind"] != mapping[case["kind"]]:
            raise ValueError(f"Row contract case {index} differs")
        count += 1
    recognized = {"TIME": "日期／報到時間", "DIAGNOSIS": "診斷", "EVENT_ID": "CalendarEventId"}
    for index, case in enumerate(parsing["header_cases"]):
        cell = literal(case["header"])
        if case.get("formula"):
            cell = {"userEnteredValue": {"formulaValue": case["formula"]}, "formattedValue": case["header"]}
        actual = canonical_header(cell)
        if (actual if actual == recognized[case["key"]] else "") != case["canonical"]:
            raise ValueError(f"Header contract case {index} differs")
        count += 1
    for index, case in enumerate(parsing["time_cases"]):
        cell = literal(case["input"])
        item = {"time_cell": cell, "time": cell_text(cell), "sheet": "202610"}
        try:
            parsed_time, note = arrival(item)
            invalid = False
        except ValueError:
            parsed_time, note, invalid = "", "", True
        if (parsed_time, note, invalid) != (case["time"], case["note"], case["invalid"]):
            raise ValueError(f"Time contract case {index} differs")
        count += 1
    return count


def exported_contract(repository, script, function):
    module = (Path(repository).resolve() / "tests" / script).resolve()
    result = subprocess.run(["node", "-e", f"process.stdout.write(JSON.stringify(require(process.argv[1]).{function}()))", str(module)],
                            check=True, capture_output=True, encoding="utf-8")
    return json.loads(result.stdout)


def check_workflows(contract):
    for phase in ("creation", "moved"):
        book = {"sheets": [{"properties": {"title": item["name"], "sheetId": item["id"],
                "gridProperties": {"columnCount": len(item["rows"][0]), "rowCount": 100}},
                "rows": [[literal(value) for value in row] for row in item["rows"]], "merges": []}
                for item in contract[phase]]}
        monitoring = scan(book)
        if monitoring["issues"]:
            raise ValueError(f"GAS {phase} creates monitor diagnostics")
        found = [item for item in monitoring["patients"] if item["uid"] == contract["patient_sync_id"]]
        planned = [item for sheet in book["sheets"] for item in entries(sheet)
                   if item["kind"] == "patient" and item["sync_id"] == contract["patient_sync_id"]]
        if len(found) != 1 or len(planned) != 1:
            raise ValueError(f"GAS {phase} patient not uniquely identified by both EXEs")
        expected = (contract[f"{phase}_date"], contract[f"{phase}_hospital"])
        if ((found[0]["date"], found[0]["hospital"]) != expected
                or (planned[0]["fields"]["date"], planned[0]["fields"]["hospital"]) != expected
                or found[0]["event_id"] != contract["patient_event_id"]
                or arrival(found[0]) != ("08:30", "")
                or found[0]["fields"]["人工備註"] != "人工欄位保留"
                or found[0]["fields"].get("醫院", "") != contract["patient_hospital_cells"][phase]):
            raise ValueError(f"GAS {phase} field or permanent identity contract differs")
    return 2


def check_archives(contract, gas_repo=None):
    """Read real GAS request outputs in both EXEs and continue in either tool."""
    if contract.get("schema") != 2 or contract["lock"] != {"id": LOCK_ID, "key": LOCK_KEY}:
        raise ValueError("GAS and Windows archive ownership contract differs")
    baseline = None
    identities = None
    for name in ("before", "after_september", "during_backfill", "after_backfill"):
        book = contract[name]
        rows, issues = _report_rows(book)
        if any(issue["code"] == "conflicting_report_sync_id" for issue in issues):
            raise ValueError("GAS archive changed a source identity's contents")
        result = statistics(rows, book)
        counts = (result["total"], result["eye_count"], result["models"], result["procedures"], result["excluded_cancelled"])
        monitored = scan(book)
        if monitored["issues"] and name != "during_backfill":
            raise ValueError("GAS archive causes monitor diagnostics")
        if baseline is None:
            baseline, identities = counts, monitored["identities"]
        elif counts != baseline or set(monitored["identities"]) != set(identities):
            raise ValueError("GAS archive changed counts or removed Calendar-protective IDs")
    from datetime import date

    plan = build_archive(contract["after_september"], ["202606"], date(2026, 10, 15))
    if not plan["summary"]["months"][0]["backfill"]:
        raise ValueError("Windows could not continue a GAS annual archive")
    if gas_repo:
        with tempfile.TemporaryDirectory(prefix="archive-contract-") as directory:
            path = Path(directory) / "windows-requests.json"
            path.write_text(json.dumps({"book": contract["after_september"], "requests": plan["requests"],
                                       "source_ids": [source["properties"]["sheetId"] for source in plan["sources"].values()]}), encoding="utf-8")
            subprocess.run(["node", "tests/archive_contract.js", "--verify-input", str(path)], cwd=gas_repo, check=True,
                           capture_output=True, text=True)
    return 8


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gas-repo", type=Path)
    args = parser.parse_args()
    revision = gas_revision()
    parsing = json.loads((FIXTURES / "schema_parsing.json").read_text(encoding="utf-8"))
    formatting = json.loads((FIXTURES / "sheet_contract.json").read_text(encoding="utf-8"))
    workflows = json.loads((FIXTURES / "gas_workflows.json").read_text(encoding="utf-8"))
    archives = json.loads((FIXTURES / "gas_archives.json").read_text(encoding="utf-8"))
    if args.gas_repo:
        actual_revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=args.gas_repo,
                                         check=True, capture_output=True, text=True).stdout.strip()
        if actual_revision != revision:
            raise ValueError("GAS source revision differs from the pinned contract")
        actual_parsing = exported_contract(args.gas_repo, "parser_contract.js", "parserContract")
        actual_formatting = exported_contract(args.gas_repo, "sheet_contract.js", "sheetContract")
        actual_workflows = exported_contract(args.gas_repo, "workflow_contract.js", "workflowContract")
        actual_archives = exported_contract(args.gas_repo, "archive_contract.js", "archiveContract")
        if parsing != actual_parsing or formatting != actual_formatting or workflows != actual_workflows or archives != actual_archives:
            raise ValueError("Saved fixtures differ from the supplied GAS source; review and regenerate them")
    count = check_contract(parsing, formatting) + check_workflows(workflows) + check_archives(archives, args.gas_repo)
    print(f"Shared Sheets contract passed: {count} synthetic cases (main, monitor, GAS {revision[:7]})")


if __name__ == "__main__":
    main()
