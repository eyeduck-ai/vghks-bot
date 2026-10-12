"""Main manual/API and automatic pending reasons preserve actionable protection context."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from analysis_fixtures import MemorySheets, book_fixture, literal, proposal, soap_record

from vghks_bot.settings import Settings
from vghks_bot.sheet_sync_core import SheetSyncCore


def protected_book(title):
    book = book_fixture()
    if title == "FU":
        sheet = {"properties": {"title": "FU", "sheetId": 102,
                                 "gridProperties": {"rowCount": 100, "columnCount": 10}},
                 "rows": [[literal(label) for label in
                           ("病歷號", "姓名", "TEL", "醫院", "Tag", "Condition", "日期", "Plan", "心得", "CalendarEventId")]],
                 "merges": []}
        book["sheets"].append(sheet)
        col = 9
    else:
        sheet, col = book["sheets"][0], 22
    sheet["protectedRanges"] = [{"protectedRangeId": 700,
                                 "range": {"sheetId": sheet["properties"]["sheetId"],
                                           "startColumnIndex": col, "endColumnIndex": col + 1}}]
    return book


class MainProtectionSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.core = SheetSyncCore(SimpleNamespace(store=self.store, app=SimpleNamespace(settings=Settings())))

    def test_manual_preview_error_identifies_protected_sheet_and_technical_header_without_writes(self):
        for title in ("FU", "202609"):
            with self.subTest(title=title):
                client = MemorySheets(protected_book(title))
                original = copy.deepcopy(client.book)
                with self.assertRaises(ValueError) as raised:
                    self.core.build_manual(client.read(), client.key, [proposal(mrn="99")])
                self.assertIn(title, str(raised.exception))
                self.assertIn("CalendarEventId", str(raised.exception))
                self.assertIn("編輯權限", str(raised.exception))
                self.assertEqual(client.writes, 0)
                self.assertEqual(client.book, original)
        self.store.save_document.assert_not_called()

    def test_automatic_pending_identifies_protected_sheet_and_technical_header_without_writes(self):
        for title in ("FU", "202609"):
            with self.subTest(title=title):
                client = MemorySheets(protected_book(title))
                original = copy.deepcopy(client.book)
                result = self.core.automatic(client, [soap_record("0000003")], "cohort",
                                             SimpleNamespace(check_cancel=lambda: None))
                self.assertEqual(result["updated"], 0)
                self.assertEqual(len(result["pending"]), 1)
                reason = result["pending"][0]["reason"]
                self.assertIn(title, reason)
                self.assertIn("CalendarEventId", reason)
                self.assertIn("編輯權限", reason)
                self.assertEqual(client.writes, 0)
                self.assertEqual(client.book, original)
        self.store.save_document.assert_not_called()


if __name__ == "__main__":
    unittest.main()
