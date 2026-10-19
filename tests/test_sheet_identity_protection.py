"""Identity migration never weakens or partially bypasses Sheets protections."""
import copy
import unittest

from analysis_fixtures import book_fixture, literal

from vghks_bot.sheet_identity import (
    EVENT_ID_HEADER,
    SYNC_ID_HEADER,
    plan_identity_setup,
)


def protection(range_, **flags):
    return {"protectedRangeId": 700, "range": range_, **flags}


class IdentityProtectionTests(unittest.TestCase):
    def assert_blocked(self, book, header):
        original = copy.deepcopy(book)
        setup = plan_identity_setup(book)
        self.assertFalse(setup["can_apply"])
        self.assertFalse(setup["changed"])
        self.assertEqual(setup["requests"], [])
        self.assertEqual(setup["book"], original)
        self.assertEqual(book, original)
        issue = next(issue for issue in setup["issues"] if issue["code"] == "protected_identity_range"
                     and issue["header"] == header)
        self.assertIn(header, issue["reason"])
        self.assertIn("編輯權限", issue["reason"])
        self.assertEqual(issue["protected_range_id"], 700)
        return issue

    def test_existing_event_column_strict_protection_blocks_entire_setup_despite_warning(self):
        book = book_fixture()
        sheet = book["sheets"][0]
        range_ = {"sheetId": 101, "startColumnIndex": 22, "endColumnIndex": 23}
        sheet["protectedRanges"] = [protection(range_), protection(range_, warningOnly=True)]
        second = copy.deepcopy(sheet)
        second["properties"].update(title="202610", sheetId=102)
        second["protectedRanges"] = []
        book["sheets"].append(second)
        issue = self.assert_blocked(book, EVENT_ID_HEADER)
        self.assertEqual(issue["column"], 22)

    def test_new_uid_in_existing_blank_grid_column_is_preflighted_before_header_creation(self):
        book = book_fixture()
        sheet = book["sheets"][0]
        sheet["properties"]["gridProperties"]["columnCount"] = 26
        sheet["protectedRanges"] = [protection({"sheetId": 101, "startColumnIndex": 23, "endColumnIndex": 24},
                                                warningOnly=False, requestingUserCanEdit=False)]
        self.assertEqual(self.assert_blocked(book, SYNC_ID_HEADER)["column"], 23)

    def test_legacy_event_alias_cannot_be_renamed_through_a_strict_protection(self):
        book = book_fixture()
        sheet = book["sheets"][0]
        sheet["rows"][0][22] = literal("日曆apiEventID")
        sheet["protectedRanges"] = [protection({"sheetId": 101, "startColumnIndex": 22})]
        self.assert_blocked(book, EVENT_ID_HEADER)

    def test_warning_only_or_explicit_requester_editor_allows_normal_migration(self):
        for flags in ({"warningOnly": True}, {"warningOnly": True, "requestingUserCanEdit": False},
                      {"requestingUserCanEdit": True}, {"warningOnly": False, "requestingUserCanEdit": True}):
            with self.subTest(flags=flags):
                book = book_fixture()
                book["sheets"][0]["protectedRanges"] = [protection({"sheetId": 101}, **flags)]
                setup = plan_identity_setup(book)
                self.assertTrue(setup["can_apply"])
                self.assertFalse(setup["issues"])
                self.assertTrue(setup["requests"])
                self.assertEqual(setup["book"]["sheets"][0]["protectedRanges"],
                                 book["sheets"][0]["protectedRanges"])
                self.assertFalse(any("addProtectedRange" in request or "updateProtectedRange" in request or "deleteProtectedRange" in request
                                     for request in setup["requests"]))

    def test_omitted_sheet_id_defaults_to_zero_and_unbounded_ranges_cover_technical_columns(self):
        for range_ in ({}, {"startColumnIndex": 22}, {"endColumnIndex": 23},
                       {"startRowIndex": 5}, {"endRowIndex": 1},
                       {"startRowIndex": 17, "endRowIndex": 18, "startColumnIndex": 22, "endColumnIndex": 23}):
            with self.subTest(range_=range_):
                book = book_fixture()
                sheet = book["sheets"][0]
                sheet["properties"]["sheetId"] = 0
                sheet["protectedRanges"] = [protection(range_)]
                self.assert_blocked(book, EVENT_ID_HEADER)

    def test_disjoint_columns_rows_or_sheet_do_not_block_technical_columns(self):
        for range_ in ({}, {"sheetId": 102}, {"sheetId": 101, "endColumnIndex": 22},
                       {"sheetId": 101, "startColumnIndex": 24},
                       {"sheetId": 101, "startRowIndex": 2, "endRowIndex": 2},
                       {"sheetId": 101, "startRowIndex": 100}):
            with self.subTest(range_=range_):
                book = book_fixture()
                book["sheets"][0]["protectedRanges"] = [protection(range_)]
                setup = plan_identity_setup(book)
                self.assertTrue(setup["can_apply"])
                self.assertFalse(setup["issues"])

    def test_fu_existing_event_and_new_uid_are_both_preflighted(self):
        for col, header in ((9, EVENT_ID_HEADER), (10, SYNC_ID_HEADER)):
            with self.subTest(col=col):
                book = book_fixture()
                fu = {"properties": {"sheetId": 900, "title": "FU",
                                      "gridProperties": {"rowCount": 100, "columnCount": 15}},
                      "rows": [[literal(label) for label in
                                ("病歷號", "姓名", "TEL", "醫院", "Tag", "Condition", "日期", "Plan", "心得", EVENT_ID_HEADER)],
                               [literal("3"), literal("測試追蹤")]],
                      "protectedRanges": [protection({"sheetId": 900, "startColumnIndex": col, "endColumnIndex": col + 1})]}
                book["sheets"].append(fu)
                self.assert_blocked(book, header)

    def test_archive_protection_is_checked_only_when_archive_migration_is_enabled(self):
        book = book_fixture()
        archive = {"properties": {"sheetId": 900, "title": "刀表封存_2025",
                                   "gridProperties": {"rowCount": 100, "columnCount": 5}},
                   "rows": [[], [], [], [], [], [literal(label) for label in
                                                  ("病歷號", "姓名", EVENT_ID_HEADER, SYNC_ID_HEADER, "ArchiveRowType")]],
                   "protectedRanges": [protection({"sheetId": 900})]}
        book["sheets"].append(archive)
        self.assert_blocked(book, SYNC_ID_HEADER)
        setup = plan_identity_setup(book, include_archives=False)
        self.assertTrue(setup["can_apply"])
        self.assertEqual(setup["book"]["sheets"][1], archive)

if __name__ == "__main__":
    unittest.main()
