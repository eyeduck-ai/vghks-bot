"""Read-only contract checks redact clinical/credential data from all reports."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools import check_sheet_contract as check
from vghks_bot.google_sheets import SheetsClient
from vghks_bot.selftest_calendar import FU_HEADERS, cell, monthly, row, workbook

SHEET_ID = "a" * 32
SECRET = "PRIVATE_PATIENT_CREDENTIAL_EVENT_SECRET123456"


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status
        self.ok = status < 400

    def json(self):
        return copy.deepcopy(self.body)


class FakeSession:
    def __init__(self, book=None, *, drive_status=200, role="writer", drive_error=None):
        self.book = book or workbook(monthly(ids=False))
        self.drive_status, self.role, self.calls, self.closed = drive_status, role, [], False
        self.drive_error = drive_error

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method != "GET":
            raise AssertionError("A validation call attempted a Google mutation")
        if "sheets.googleapis.com" in url:
            params = kwargs.get("params", {})
            if isinstance(params, list):
                tab = dict(params)["ranges"].split("'", 2)[1]
                sheet = next(sheet for sheet in self.book["sheets"] if sheet["properties"]["title"] == tab)
                return Response({"sheets": [{"data": [{"startRow": 0, "columnMetadata": sheet.get("column_widths", []),
                    "rowData": [{"values": values} for values in sheet["rows"]]}], "merges": []}]})
            return Response({"spreadsheetId": SHEET_ID, "properties": {"title": SECRET},
                "sheets": [{"properties": sheet["properties"], "protectedRanges": sheet.get("protectedRanges", [])}
                           for sheet in self.book["sheets"]]})
        if "/calendar/" in url:
            return Response({"accessRole": self.role, "timeZone": "Asia/Taipei", "summary": SECRET,
                             "items": [{"id": SECRET, "description": SECRET}]})
        if "/drive/" in url:
            return Response({"mimeType": check.SHEET_MIME, "capabilities": {"canEdit": True},
                             "emailAddress": SECRET, "error": self.drive_error or SECRET}, self.drive_status)
        raise AssertionError("Unexpected API target")

    def close(self):
        self.closed = True


class SheetContractTests(unittest.TestCase):
    def test_live_pipeline_only_gets_metadata_and_dryruns_without_saving_patient_data(self):
        original = workbook(monthly(ids=False))
        original["sheets"][0]["rows"][2][2] = cell(SECRET)
        original["sheets"][0]["rows"][2][3] = cell(SECRET)
        original["sheets"][0]["rows"][2][14] = cell(SECRET)
        before = copy.deepcopy(original)
        fake = FakeSession(original)
        session = check.ReadOnlySession(fake)
        report = check.build_report(SheetsClient(SHEET_ID, session), session, "synthetic@example.invalid")
        self.assertTrue(report["ok"])
        self.assertTrue(report["read_only"])
        self.assertGreater(report["sheet"]["migration"]["request_count"], 0)
        self.assertTrue(report["sheet"]["parsing"]["match"])
        self.assertEqual(report["sheet"]["parsing"]["monthly_main_count"], 2)
        self.assertEqual(report["sheet"]["parsing"]["monthly_monitor_count"], 2)
        self.assertEqual(report["calendar"]["access_role"], "writer")
        self.assertTrue(report["sheet_edit_permission"]["can_edit"])
        self.assertEqual(original, before, "dry-run mutated its input workbook")
        self.assertTrue(all(method == "GET" for method, _, _ in fake.calls))
        self.assertIsInstance(fake.calls[0][2]["params"], dict)
        self.assertIn("spreadsheetId", fake.calls[0][2]["params"]["fields"])
        calendar = next(kwargs for _, url, kwargs in fake.calls if "/calendar/" in url)
        drive = next(kwargs for _, url, kwargs in fake.calls if "/drive/" in url)
        self.assertEqual(calendar["params"]["fields"], "accessRole,timeZone")
        self.assertEqual(drive["params"]["fields"], "mimeType,capabilities(canEdit)")
        packed = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(SECRET, packed)
        self.assertNotIn("example.invalid", packed)
        self.assertNotIn(SHEET_ID, packed)
        self.assertNotRegex(packed, r"!\d+", "matching patient row references leaked")
        for column in report["sheet"]["tabs"][0]["technical_columns"]:
            self.assertFalse(column["existing"])
            self.assertTrue(column["needs_add"])
            self.assertTrue(column["hidden_planned"])

    def test_managed_tab_schemas_and_summary_headers_are_canonical_and_private(self):
        month = monthly()
        month["rows"][0][7] = {"userEnteredValue": {"formulaValue": '=N("SURGERY_MONTHLY_DIAGNOSIS_SUMMARY_V2")'},
                                  "formattedValue": "診斷｜98765 " + SECRET}
        month["rows"][2][17] = cell(SECRET)
        month["rows"][0].append(cell(SECRET))
        fu = {"properties": {"title": "FU", "sheetId": 2, "gridProperties": {"rowCount": 20, "columnCount": len(FU_HEADERS)}},
              "rows": [list(map(cell, FU_HEADERS)), row(FU_HEADERS, **{"病歷號": SECRET, "姓名": SECRET})]}
        all_tab = copy.deepcopy(fu)
        all_tab["properties"].update(title="All", sheetId=3)
        archive_headers = [*FU_HEADERS, "ArchiveRowType"]
        archive = {"properties": {"title": "刀表封存_2025", "sheetId": 4, "gridProperties": {"rowCount": 20, "columnCount": len(archive_headers)}},
                   "rows": [[], [], [], [], [], list(map(cell, archive_headers)),
                            row(archive_headers, **{"病歷號": SECRET, "姓名": SECRET, "ArchiveRowType": "patient"})]}
        state = {"properties": {"title": check.SYNC_STATE_TITLE, "sheetId": 5, "hidden": True,
                                "gridProperties": {"rowCount": 20, "columnCount": 3}},
                 "rows": [list(map(cell, check.STATE_HEADERS)), [cell(SECRET)]],
                 "column_widths": [{"hiddenByUser": True}] * 3}
        report = check.analyze_book(workbook(month, fu, all_tab, archive, state))
        self.assertEqual(len(report["tabs"]), 5)
        self.assertEqual(report["migration"]["issues"], [{"code": "invalid_sync_id", "tab": "202610", "count": 1}])
        labels = [item["name"] for item in report["tabs"][0]["headers"]]
        self.assertIn("診斷", labels)
        self.assertIn("其他欄位", labels)
        packed = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(SECRET, packed)
        self.assertNotIn("98765", packed)
        self.assertNotRegex(packed, r"!\d+")
        state_report = report["tabs"][-1]
        self.assertTrue(state_report["hidden"])
        self.assertTrue(all(item["hidden_existing"] for item in state_report["technical_columns"]))

    def test_parser_mismatch_discloses_only_row_address_and_not_route_values(self):
        book = workbook(monthly())
        scan = check.scan

        def different_route(value):
            result = scan(value)
            result["patients"][0]["hospital"] = SECRET
            return result

        with patch.object(check, "scan", side_effect=different_route):
            report = check.analyze_book(book)
        self.assertFalse(report["compatible"])
        self.assertFalse(report["parsing"]["match"])
        self.assertEqual(report["parsing"]["mismatches"], [{"row": "202610!3", "code": "route_mismatch"}])
        self.assertNotIn(SECRET, json.dumps(report, ensure_ascii=False))

    def test_drive_unavailable_never_claims_edit_permission_and_calendar_reader_is_not_writer(self):
        fake = FakeSession(drive_status=403, role="reader")
        session = check.ReadOnlySession(fake)
        report = check.build_report(SheetsClient(SHEET_ID, session), session, "synthetic")
        self.assertFalse(report["ok"])
        self.assertEqual(report["sheet_edit_permission"]["verification"], "unverified")
        self.assertIsNone(report["sheet_edit_permission"]["can_edit"])
        self.assertFalse(report["calendar"]["can_write"])
        self.assertNotIn(SECRET, json.dumps(report))

    def test_disabled_api_reasons_are_distinguished_without_disclosing_error_bodies(self):
        for error in ({"message": SECRET, "errors": [{"reason": "accessNotConfigured", "message": SECRET}]},
                      {"message": SECRET, "details": [{"reason": "SERVICE_DISABLED", "metadata": {"service": SECRET}}]},
                      {"message": SECRET, "errors": [{"reason": SECRET}]}):
            with self.subTest(structure=next(key for key in error if key != "message")):
                fake = FakeSession(drive_status=403, drive_error=error)
                session = check.ReadOnlySession(fake)
                report = check.build_report(SheetsClient(SHEET_ID, session), session, "synthetic")
                expected = "authorization_unverified" if error.get("errors", [{}])[0].get("reason") == SECRET else "api_not_enabled"
                self.assertEqual(report["sheet_edit_permission"]["code"], expected)
                self.assertEqual(report["sheet_edit_permission"]["verification"], "unverified")
                self.assertIsNone(report["sheet_edit_permission"]["can_edit"])
                self.assertFalse(report["ok"])
                self.assertNotIn(SECRET, json.dumps(report))

    def test_mutations_and_nonofficial_token_uri_are_rejected_before_network_or_key_parsing(self):
        fake = FakeSession()
        session = check.ReadOnlySession(fake)
        with self.assertRaisesRegex(check.ContractError, "mutation_blocked"):
            session.request("POST", "https://sheets.googleapis.com/", json={"secret": SECRET})
        self.assertEqual(fake.calls, [])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_text(json.dumps({"type": "service_account", "token_uri": "https://example.invalid/token", "private_key": SECRET}))
            with patch.object(check.service_account.Credentials, "from_service_account_info") as factory:
                with self.assertRaisesRegex(check.ContractError, "invalid_service_account"):
                    check.credentials_from_file(path)
                factory.assert_not_called()
            path.write_text(json.dumps({"type": "service_account", "token_uri": check.TOKEN_URI, "private_key": SECRET}))
            with patch.object(check.service_account.Credentials, "from_service_account_info", return_value="synthetic") as factory:
                self.assertEqual(check.credentials_from_file(path), "synthetic")
                self.assertEqual(factory.call_args.kwargs["scopes"], check.SCOPES)

    def test_optional_gas_version_never_echoes_fixture_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self.assertEqual(check.gas_contract(repo)["verification"], "unverified")
            fixture = repo / "tests" / "fixtures" / "schema_parsing.json"
            fixture.parent.mkdir(parents=True)
            fixture.write_text(json.dumps({"contract_version": "1.2", "cases": [SECRET]}))
            self.assertEqual(check.gas_contract(repo), {"verification": "version_available", "contract_version": "1.2"})
            fixture.write_text(SECRET)
            self.assertNotIn(SECRET, json.dumps(check.gas_contract(repo)))

    def test_cli_failures_are_safe_and_report_cannot_overwrite_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "synthetic-key.json"
            key.write_text(SECRET)
            report = Path(directory) / "report.json"
            arguments = ["--service-account", str(key), "--spreadsheet", SHEET_ID,
                         "--calendar-id", "synthetic", "--report", str(report)]
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output), patch.object(check, "make_session", side_effect=RuntimeError(SECRET)):
                self.assertEqual(check.main(arguments), 1)
            self.assertNotIn(SECRET, output.getvalue() + report.read_text())
            self.assertEqual(json.loads(report.read_text())["code"], "check_unavailable")
            arguments[-1] = str(key)
            with redirect_stdout(output), redirect_stderr(output):
                self.assertEqual(check.main(arguments), 2)
            self.assertEqual(key.read_text(), SECRET)


if __name__ == "__main__":
    unittest.main()
