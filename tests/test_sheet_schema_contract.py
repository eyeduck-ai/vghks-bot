"""Main-program parsing against fixtures exported from the actual GAS code."""
import json
import unittest

from tools.verify_sheet_contract import (
    FIXTURES,
    check_archives,
    check_contract,
    fixture_sheet,
)
from vghks_bot.sheet_plan import Planner, native_cell


class ThreeProgramContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsing = json.loads((FIXTURES / "schema_parsing.json").read_text(encoding="utf-8"))
        cls.formatting = json.loads((FIXTURES / "sheet_contract.json").read_text(encoding="utf-8"))

    def test_actual_gas_exported_cases_match_both_exes(self):
        self.assertGreaterEqual(check_contract(self.parsing, self.formatting), 50)

    def test_gas_native_archives_keep_eye_totals_and_calendar_ids_in_both_exes(self):
        contract = json.loads((FIXTURES / "gas_archives.json").read_text(encoding="utf-8"))
        self.assertEqual(check_archives(contract), 8)

    def test_month_creation_keeps_complete_gas_header_cells_and_dimension_formats(self):
        # Header formulas, custom columns, notes, validation and hidden IDs are
        # native Sheets cells, not reconstructed from an EXE's fixed positions.
        labels = ["人工欄", *self.formatting["monthly_headers"], ""]
        sheet = fixture_sheet(labels)
        sheet["rows"][0][labels.index("診斷")] = {
            "userEnteredValue": {"formulaValue": self.formatting["diagnosis_formula"]},
            "formattedValue": "診斷｜synthetic", "note": "synthetic note",
            "userEnteredFormat": {"textFormat": {"fontFamily": self.formatting["font"]["family"]}},
            "dataValidation": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": "CATA"}]}}}
        sheet["column_widths"] = [{"pixelSize": self.formatting["monthly_widths"].get(label, 80),
                                   "hiddenByUser": label in self.formatting["system_headers"]} for label in labels]
        planner = Planner({"sheets": [sheet]}, "synthetic-sheet-00000000000", [])
        created = planner.new_month("202611")
        self.assertEqual(created["rows"][0], sheet["rows"][0])
        written = next(request["updateCells"]["rows"][0]["values"] for request in planner.requests if "updateCells" in request)
        self.assertEqual(written, [native_cell(cell) for cell in sheet["rows"][0]])
        self.assertTrue(all("formattedValue" not in cell and "effectiveValue" not in cell for cell in written))
        self.assertEqual(created["column_widths"], sheet["column_widths"])

if __name__ == "__main__":
    unittest.main()
