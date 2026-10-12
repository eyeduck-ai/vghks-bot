import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests
from google.auth.exceptions import TransportError

from vghks_bot.google_sheets import (
    GoogleSettings,
    SheetError,
    SheetsClient,
    SheetUnavailable,
    SheetUncertain,
)
from vghks_bot.selftest_analysis import check_google_and_sheet


class GoogleTests(unittest.TestCase):
    def test_public_distribution_starts_without_a_personal_sheet(self):
        with tempfile.TemporaryDirectory() as folder:
            google = GoogleSettings(Path(folder))
            self.assertEqual(google.public()["spreadsheet_id"], "")
            self.assertFalse(google.public()["configured"])
            google.save({"spreadsheet_id": "synthetic-configured-sheet-0000000"})
            self.assertEqual(GoogleSettings(Path(folder)).public()["spreadsheet_id"], "synthetic-configured-sheet-0000000")

    def test_connection_probe_reads_only_metadata_and_does_not_claim_write_permission(self):
        session = Mock()
        session.request.return_value = Mock(status_code=200, ok=True, json=Mock(return_value={
            "properties": {"title": "Synthetic"},
            "sheets": [{"properties": {"title": "202609"}}, {"properties": {"title": "FU"}}]}))
        value = SheetsClient("synthetic", session).connection_info()
        self.assertEqual(value["months"], ["202609"])
        self.assertEqual(value["title"], "Synthetic")
        self.assertIn("寫入仍取決於", value["message"])
        session.request.assert_called_once()
        self.assertEqual(session.request.call_args.args[0], "GET")
        self.assertNotIn("rowData", session.request.call_args.kwargs["params"]["fields"])

    def test_service_key_encryption_and_official_signer_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            check_google_and_sheet(Path(folder))
            google = GoogleSettings(Path(folder))
            self.assertFalse(google.public()["configured"])
            with self.assertRaisesRegex(ValueError, "官方位址"):
                google.save({"key": {"type": "service_account", "token_uri": "https://invalid.example/token"}})
            self.assertNotIn("private_key", google.public())

    def test_google_errors_distinguish_uncertain_write_from_rejected_write(self):
        session = Mock()
        client = SheetsClient("synthetic", session)
        for status in (401, 403, 404, 400):
            session.request.return_value = Mock(status_code=status, ok=False)
            with self.assertRaises(SheetError) as cm:
                client.write([])
            self.assertNotIsInstance(cm.exception, SheetUncertain)
        for status in (429, 500, 503):
            session.request.return_value = Mock(status_code=status, ok=False)
            with self.assertRaises(SheetUncertain):
                client.write([])
        session.request.side_effect = requests.Timeout()
        with self.assertRaises(SheetUncertain):
            client.write([])
        with self.assertRaises(SheetError):
            client.read()

    def test_month_reads_include_columns_beyond_az_and_ignore_other_tabs(self):
        session = Mock()
        responses = [{"properties": {}, "sheets": [
            {"properties": {"sheetId": 1, "title": "202609", "gridProperties": {"columnCount": 55, "rowCount": 401}}},
            {"properties": {"sheetId": 2, "title": "FU", "gridProperties": {"columnCount": 26, "rowCount": 1000}}}]},
            {"sheets": [{"data": [{"rowData": [{"values": [{"userEnteredValue": {"stringValue": "header"}}]}],
                                  "columnMetadata": [{"pixelSize": 120}]}]}]},
            {"sheets": [{"data": [{"startRow": 400, "rowData": [{"values": [{"note": "keep"}]}]}]}]}]
        session.request.side_effect = [Mock(status_code=200, ok=True, json=Mock(return_value=r)) for r in responses]
        book = SheetsClient("synthetic", session).read()
        self.assertEqual(len(book["sheets"]), 1)
        self.assertEqual(book["sheets"][0]["rows"][400][0]["note"], "keep")
        self.assertEqual(book["sheets"][0]["column_widths"], [{"pixelSize": 120}])
        self.assertIn(("ranges", "'202609'!A1:BC400"), session.request.call_args_list[1].kwargs["params"])
        fields = dict(session.request.call_args_list[1].kwargs["params"])["fields"]
        self.assertIn("effectiveValue", fields)

    def test_temporary_read_failure_is_retryable_but_write_remains_uncertain(self):
        session = Mock()
        client = SheetsClient("synthetic", session)
        for error in (requests.Timeout(), requests.ConnectionError(), TransportError("synthetic")):
            session.request.side_effect = error
            with self.assertRaises(SheetUnavailable):
                client.read_all()
            with self.assertRaises(SheetUncertain):
                client.write([])
        session.request.side_effect = None
        for status in (429, 500, 503):
            session.request.return_value = Mock(status_code=status, ok=False)
            with self.assertRaises(SheetUnavailable):
                client.read_all()
        session.request.return_value = Mock(status_code=403, ok=False)
        with self.assertRaises(SheetError) as result:
            client.read_all()
        self.assertNotIsInstance(result.exception, SheetUnavailable)

    def test_archive_reads_non_schedule_formula_dependencies_and_escapes_sheet_titles(self):
        session = Mock()
        meta = {"sheets": [
            {"properties": {"sheetId": 1, "title": "202609", "gridProperties": {"columnCount": 1, "rowCount": 2}}},
            {"properties": {"sheetId": 2, "title": "Doctor's stats", "gridProperties": {"columnCount": 1, "rowCount": 1}}}]}
        data = {"sheets": [{"data": [{"rowData": [{"values": [{"userEnteredValue": {"formulaValue": "='202609'!A2"}}]}]}]}]}
        session.request.side_effect = [Mock(status_code=200, ok=True, json=Mock(return_value=value)) for value in (meta, data, data)]
        book = SheetsClient("synthetic", session).read_for_archive()
        self.assertEqual(len(book["sheets"]), 2)
        self.assertEqual(book["sheets"][1]["rows"][0][0]["userEnteredValue"]["formulaValue"], "='202609'!A2")
        self.assertIn(("ranges", "'Doctor''s stats'!A1:A1"), session.request.call_args.kwargs["params"])

    def test_archive_native_snapshot_keeps_paginated_row_heights_and_display_formats(self):
        session = Mock()
        meta = {"sheets": [{"properties": {"sheetId": 1, "title": "202609",
            "gridProperties": {"columnCount": 1, "rowCount": 401}}, "conditionalFormats": [
            {"ranges": [{"sheetId": 1}], "booleanRule": {"condition": {"type": "NOT_BLANK"}}}]}]}
        parts = [meta, {"sheets": [{"data": [{"rowMetadata": [{"pixelSize": 36}],
            "rowData": [{"values": [{"effectiveFormat": {"textFormat": {"bold": True}}}]}]}]}]},
            {"sheets": [{"data": [{"startRow": 400,
                "rowMetadata": [{"pixelSize": 45, "hiddenByUser": True}]}]}]}]
        session.request.side_effect = [Mock(status_code=200, ok=True, json=Mock(return_value=value)) for value in parts]
        sheet = SheetsClient("synthetic", session).read_for_archive()["sheets"][0]
        self.assertEqual(sheet["row_metadata"][0], {"pixelSize": 36})
        self.assertEqual(sheet["row_metadata"][399], {})
        self.assertEqual(sheet["row_metadata"][400], {"pixelSize": 45, "hiddenByUser": True})
        self.assertTrue(sheet["rows"][0][0]["effectiveFormat"]["textFormat"]["bold"])
        self.assertTrue(sheet["conditionalFormats"])
        self.assertIn("rowMetadata", dict(session.request.call_args.kwargs["params"])["fields"])
