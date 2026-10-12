"""Compare Windows sheet parsers with fixtures exported from the actual GAS code.

Offline by default. --gas-repo also executes the known GAS test exporters locally;
this tool never opens credentials, Sheets, or Calendar.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vghks_bot.calendar_sync import arrival, full_day, headers  # noqa: E402
from vghks_bot.sheet_identity import header_columns  # noqa: E402
from vghks_bot.sheet_plan import columns, typed_day  # noqa: E402
from vghks_bot.sheet_schema import (  # noqa: E402
    DAY_MARKER,
    HOSPITALS,
    canonical_header,
    cell_text,
    full_date,
    monthly_row,
)

FIXTURES = ROOT / "tests" / "fixtures"


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gas-repo", type=Path)
    args = parser.parse_args()
    parsing = json.loads((FIXTURES / "schema_parsing.json").read_text(encoding="utf-8"))
    formatting = json.loads((FIXTURES / "sheet_contract.json").read_text(encoding="utf-8"))
    if args.gas_repo:
        actual_parsing = exported_contract(args.gas_repo, "parser_contract.js", "parserContract")
        actual_formatting = exported_contract(args.gas_repo, "sheet_contract.js", "sheetContract")
        if parsing != actual_parsing or formatting != actual_formatting:
            raise ValueError("Saved fixtures differ from the supplied GAS source; review and regenerate them")
    count = check_contract(parsing, formatting)
    print(f"Shared Sheets contract passed: {count} synthetic cases (main, monitor, GAS)")


if __name__ == "__main__":
    main()
