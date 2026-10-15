"""Synthetic two-phase archive requests, native snapshots and restart recovery."""
import copy
import json
import unittest
from datetime import date
from unittest.mock import patch

from analysis_fixtures import MemorySheets, book_fixture, literal

from vghks_bot.sheet_archive import (
    build_archive,
    build_archive_delete,
    verify_archive,
    verify_archive_complete,
)
from vghks_bot.sheet_identity import scan_identities
from vghks_bot.sheet_plan import native_cell
from vghks_bot.sheet_schema import cell_text, sheet_headers

TODAY = date(2026, 10, 12)


def prepared(month="202609", sid=101):
    book = book_fixture()
    sheet = book["sheets"][0]
    sheet["properties"].update(sheetId=sid, title=month)
    sheet["properties"]["gridProperties"]["columnCount"] = 24
    sheet["rows"][0].append(literal("ScheduleSyncId"))
    for index, row in enumerate(sheet["rows"][1:], 1):
        if not row:
            continue
        while len(row) < 24:
            row.append({})
        row[23] = literal(f"{sid * 100 + index:032x}")
        if cell_text(row[2]) == "◆ 刀日":
            row[0] = literal(f"{month[:4]}-{month[4:]}-15")
            row[22] = {}
        else:
            row[22] = literal(f"event-{sid}-{index}")
    sheet["column_widths"] = [{"pixelSize": 110} for _ in range(24)]
    sheet["column_widths"][22]["hiddenByUser"] = True
    sheet["column_widths"][23]["hiddenByUser"] = True
    book["all_sheet_properties"] = [copy.deepcopy(sheet["properties"])]
    return book


class ArchiveSheets(MemorySheets):
    """Execute the native planner requests, never substitute its expected book."""

    def write(self, requests):
        # The shared double lacks merge/delete. Keep these explicit in this
        # archive-only double and execute all cell/dimension writes normally.
        for request in requests:
            if "mergeCells" in request:
                value = copy.deepcopy(request["mergeCells"]["range"])
                sheet = next(s for s in self.book["sheets"] if s["properties"]["sheetId"] == value["sheetId"])
                sheet.setdefault("merges", []).append(value)
            elif "deleteSheet" in request:
                sid = request["deleteSheet"]["sheetId"]
                self.book["sheets"] = [s for s in self.book["sheets"] if s["properties"]["sheetId"] != sid]
            elif "updateDimensionProperties" in request and request["updateDimensionProperties"]["range"]["dimension"] == "ROWS":
                body = request["updateDimensionProperties"]
                dimensions = next(s for s in self.book["sheets"] if s["properties"]["sheetId"] == body["range"]["sheetId"]).setdefault("row_metadata", [])
                while len(dimensions) < body["range"]["endIndex"]:
                    dimensions.append({})
                for index in range(body["range"]["startIndex"], body["range"]["endIndex"]):
                    dimensions[index].update(copy.deepcopy(body["properties"]))
            else:
                super().write([request])
        self.book["all_sheet_properties"] = [copy.deepcopy(s["properties"]) for s in self.book["sheets"]]


class ArchiveTests(unittest.TestCase):
    def stage(self, book=None, months=None):
        original = book or prepared()
        plan = build_archive(original, months or ["202609"], TODAY, "synthetic-journal-id")
        client = ArchiveSheets(copy.deepcopy(original))
        client.write(plan["requests"])
        return plan, client

    def test_copy_requests_keep_source_and_native_patient_contents(self):
        original = prepared()
        original["sheets"][0]["rows"][2][10] = {"userEnteredValue": {"formulaValue": '="SN60WF"'},
            "note": "合成人工公式", "userEnteredFormat": {"textFormat": {"strikethrough": True}},
            "textFormatRuns": [{"startIndex": 0, "format": {"bold": True}}]}
        original["sheets"][0]["rows"][3] = [{"note": "空白人工註記", "userEnteredFormat": {"backgroundColor": {"red": .5}}}]
        before = copy.deepcopy(original)
        plan, client = self.stage(original)
        self.assertEqual(original, before)
        self.assertTrue(all("deleteSheet" not in r and "addProtectedRange" not in r for r in plan["requests"]))
        self.assertEqual(verify_archive(plan, client.read()), "verified")
        self.assertEqual(client.book["sheets"][0], original["sheets"][0])
        archive = client.book["sheets"][1]
        columns = sheet_headers(archive, 5)
        self.assertEqual(cell_text(archive["rows"][5][columns["診斷"]]), "診斷")
        for index, source in enumerate(original["sheets"][0]["rows"][1:], 7):
            padded = source + [{} for _ in range(24 - len(source))]
            self.assertEqual([native_cell(c) for c in archive["rows"][index][:24]], [native_cell(c) for c in padded])
        self.assertEqual(archive["rows"][6][7]["userEnteredValue"], {"stringValue": "診斷｜1"})
        self.assertIn("COUNTIF(H2:H100", archive["rows"][6][7]["note"])
        self.assertNotIn("formulaValue", archive["rows"][6][7]["userEnteredValue"])
        self.assertIn("formulaValue", plan["sources"]["202609"]["rows"][0][7]["userEnteredValue"])
        self.assertEqual(plan["summary"]["months"][0]["header_formula_snapshots"], 1)
        self.assertTrue(plan["summary"]["requires_local_backup"])
        json.dumps(plan, ensure_ascii=False)

    def test_delete_requires_verified_copy_and_retains_restart_backup(self):
        original = prepared()
        plan = build_archive(original, ["202609"], TODAY)
        with self.assertRaisesRegex(ValueError, "禁止刪除"):
            build_archive_delete(plan, original)
        client = ArchiveSheets(original)
        client.write(plan["requests"])
        deletion = build_archive_delete(plan, client.read())
        self.assertEqual(deletion["sources"], plan["sources"])
        self.assertEqual(deletion["targets"], plan["targets"])
        self.assertEqual(verify_archive_complete(deletion, client.read()), "not_applied")
        client.write(deletion["requests"])
        restored = json.loads(json.dumps(deletion))
        self.assertEqual(verify_archive_complete(restored, client.read()), "verified")
        self.assertEqual(len(client.book["sheets"]), 1)
        self.assertEqual(client.book["sheets"][0]["properties"]["title"], "刀表封存_2026")
        self.assertEqual(scan_identities(client.read())["issues"], [])

    def test_uncertain_copy_distinguishes_before_and_after_commit_without_replay(self):
        book = prepared()
        plan = build_archive(book, ["202609"], TODAY)
        self.assertEqual(verify_archive(plan, book), "not_applied")
        client = ArchiveSheets(book)
        client.write(plan["requests"])
        persisted = json.loads(json.dumps(plan))
        self.assertEqual(verify_archive(persisted, client.read()), "verified")
        self.assertTrue(any(i["code"] == "duplicate_sync_id" for i in scan_identities(client.read())["issues"]))
        self.assertEqual(len(client.book["sheets"]), 2)

    def test_concurrent_source_edits_or_target_tamper_block_deletion(self):
        for location in ("source", "archive"):
            with self.subTest(location=location):
                plan, client = self.stage()
                sheet = client.book["sheets"][0 if location == "source" else 1]
                sheet["rows"][2 if location == "source" else 8][15] = literal("人工同時修改")
                self.assertEqual(verify_archive(plan, client.read()), "conflict")
                with self.assertRaises(ValueError):
                    build_archive_delete(plan, client.read())

    def test_missing_renamed_or_partial_target_never_authorizes_delete(self):
        for mode in ("missing", "renamed", "partial"):
            with self.subTest(mode=mode):
                plan, client = self.stage()
                if mode == "missing":
                    client.book["sheets"].pop()
                elif mode == "renamed":
                    client.book["sheets"][1]["properties"]["title"] = "人工改名"
                else:
                    client.book["sheets"][1]["rows"].pop()
                self.assertNotEqual(verify_archive(plan, client.read()), "verified")

    def test_only_past_valid_unique_string_months(self):
        for months in ([], ["202610"], ["202611"], ["202613"], ["199912"], [202609], ["202609", "202609"], [["202609"]]):
            with self.subTest(months=months), self.assertRaises(ValueError):
                build_archive(prepared(), months, TODAY)
        with patch("vghks_bot.sheet_archive.local_today", return_value=TODAY):
            self.assertEqual(build_archive(prepared(), ["202609"])["summary"]["patients"], 3)

    def test_missing_columns_ids_invalid_headers_and_orphan_manual_rows_rejected(self):
        for mode in ("header", "uid", "duplicate", "event", "date", "hospital", "manual", "unnamed"):
            with self.subTest(mode=mode):
                book = prepared()
                sheet = book["sheets"][0]
                if mode == "header":
                    sheet["rows"][0][15] = {}
                elif mode == "uid":
                    sheet["rows"][2][23] = {}
                elif mode == "duplicate":
                    sheet["rows"][2][23] = copy.deepcopy(sheet["rows"][1][23])
                elif mode == "event":
                    sheet["rows"][5][22] = copy.deepcopy(sheet["rows"][2][22])
                elif mode == "date":
                    sheet["rows"][1][0] = literal("2026-08-15")
                elif mode == "hospital":
                    sheet["rows"][1][1] = {}
                elif mode == "manual":
                    sheet["rows"][3] = [{} for _ in range(15)] + [literal("孤立人工備註")]
                else:
                    sheet["rows"][2][16] = literal("未命名人工欄")
                with self.assertRaises(ValueError):
                    build_archive(book, ["202609"], TODAY)

    def test_append_different_later_month_keeps_existing_archive(self):
        plan, client = self.stage(prepared("202608", 100), ["202608"])
        deletion = build_archive_delete(plan, client.read())
        client.write(deletion["requests"])
        old = copy.deepcopy(client.book["sheets"][0])
        client.book["sheets"].append(prepared()["sheets"][0])
        client.book["all_sheet_properties"] = [copy.deepcopy(s["properties"]) for s in client.book["sheets"]]
        second = build_archive(client.read(), ["202609"], TODAY)
        client.write(second["requests"])
        self.assertEqual(verify_archive(second, client.read()), "verified")
        self.assertEqual(client.book["sheets"][0]["rows"][:len(old["rows"])], old["rows"])
        with self.assertRaises(ValueError):
            build_archive(client.read(), ["202609"], TODAY)

    def test_untrusted_existing_archive_rejected(self):
        plan, client = self.stage(prepared("202608", 100), ["202608"])
        client.write(build_archive_delete(plan, client.read())["requests"])
        client.book["sheets"][0]["rows"][7][-1] = literal("PATIENT")
        client.book["sheets"].append(prepared()["sheets"][0])
        with self.assertRaises(ValueError):
            build_archive(client.read(), ["202609"], TODAY)

    def test_existing_archive_patient_arrival_cannot_contain_full_date(self):
        plan, client = self.stage(prepared("202608", 100), ["202608"])
        client.write(build_archive_delete(plan, client.read())["requests"])
        client.book["sheets"][0]["rows"][8][0] = literal("2026-08-15")
        client.book["sheets"].append(prepared()["sheets"][0])
        with self.assertRaisesRegex(ValueError, "封存病人列"):
            build_archive(client.read(), ["202609"], TODAY)

    def test_named_custom_column_preserved_and_format_only_trailing_grid_ignored(self):
        book = prepared()
        sheet = book["sheets"][0]
        sheet["rows"][0].append(literal("人工術後欄"))
        sheet["rows"][2].append({"userEnteredValue": {"stringValue": "人工"}, "note": "留存"})
        sheet["rows"][0].extend([{"userEnteredFormat": {"backgroundColor": {"blue": .1}}} for _ in range(10)])
        sheet["properties"]["gridProperties"]["columnCount"] = 35
        plan, client = self.stage(book)
        archive = client.book["sheets"][1]
        cols = sheet_headers(archive, 5)
        self.assertEqual(archive["rows"][8][cols["人工術後欄"]], sheet["rows"][2][24])
        self.assertEqual(archive["properties"]["gridProperties"]["columnCount"], 27)
        self.assertEqual(verify_archive(plan, client.read()), "verified")

    def test_body_relative_or_dynamic_or_deleted_month_reference_formula_rejected(self):
        for formula, other in (("=A2", False), ('=INDIRECT("A2")', False), ("='202609'!A2", True)):
            book = prepared()
            if other:
                book["sheets"].append({"properties": {"sheetId": 500, "title": "人工報表"},
                                      "rows": [[{"userEnteredValue": {"formulaValue": formula}}]]})
            else:
                book["sheets"][0]["rows"][2][14] = {"userEnteredValue": {"formulaValue": formula}}
            with self.subTest(formula=formula), self.assertRaises(ValueError):
                build_archive(book, ["202609"], TODAY)

    def test_body_whole_row_positional_array_and_named_formula_refuse_copy(self):
        for formula in ("=SUM(2:2)", "=SUM($2:$2)", "=SUM($2 : $2)", "=ROW()", "=COLUMN()",
                        '=CELL("address")', "=SHEET()", "=SHEETS()", "=SEQUENCE(2)",
                        "=ARRAYFORMULA(1)", "={1,2}", "=NamedRange", '=IF(TRUE,"x","y")'):
            book = prepared()
            book["sheets"][0]["rows"][2][14] = {"userEnteredValue": {"formulaValue": formula}}
            with self.subTest(formula=formula), self.assertRaisesRegex(ValueError, "純常數運算"):
                build_archive(book, ["202609"], TODAY)

    def test_scalar_constant_formula_is_copied_and_verified(self):
        book = prepared()
        book["sheets"][0]["rows"][2][14] = {"userEnteredValue": {"formulaValue": "=1+2*(3-1)"}}
        plan, client = self.stage(book)
        self.assertEqual(verify_archive(plan, client.read()), "verified")
        self.assertEqual(client.book["sheets"][1]["rows"][8][14]["userEnteredValue"], {"formulaValue": "=1+2*(3-1)"})

    def test_row_height_hidden_rows_and_default_readback_are_preserved(self):
        book = prepared()
        book["sheets"][0]["row_metadata"] = [{"pixelSize": 35}, {"pixelSize": 30}, {"pixelSize": 55, "hiddenByUser": True}]
        plan, client = self.stage(book)
        archive = client.book["sheets"][1]
        self.assertEqual(archive["row_metadata"][5], {"pixelSize": 35})
        self.assertEqual(archive["row_metadata"][6], {"pixelSize": 35})
        self.assertEqual(archive["row_metadata"][8], {"pixelSize": 55, "hiddenByUser": True})
        archive["row_metadata"].extend([{"pixelSize": 21, "hiddenByUser": False} for _ in range(50)])
        self.assertEqual(verify_archive(plan, client.read()), "verified")
        client.book["sheets"][0]["row_metadata"][2]["pixelSize"] = 56
        with self.assertRaisesRegex(ValueError, "禁止刪除"):
            build_archive_delete(plan, client.read())

    def test_conditional_appearance_is_frozen_with_original_rules_in_source_backup(self):
        book = prepared()
        source = book["sheets"][0]
        source["conditionalFormats"] = [{"ranges": [{"sheetId": 101, "startRowIndex": 1}],
            "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": "=$F2=TRUE"}]},
                            "format": {"backgroundColor": {"red": 1}}}}]
        source["rows"][2][2]["userEnteredFormat"] = {"textFormat": {"fontSize": 12, "foregroundColor": {"red": 1}}}
        source["rows"][2][2]["effectiveFormat"] = {"backgroundColor": {"red": 1},
            "backgroundColorStyle": {"rgbColor": {"red": .7}}, "numberFormat": {"type": "NUMBER"},
            "textFormat": {"fontSize": 19, "bold": True, "italic": True, "strikethrough": True,
                           "foregroundColor": {"blue": 1}, "foregroundColorStyle": {"rgbColor": {"green": .6}}}}
        plan, client = self.stage(book)
        archive = client.book["sheets"][1]
        fmt = archive["rows"][8][2]["userEnteredFormat"]
        self.assertEqual(fmt["backgroundColorStyle"], {"rgbColor": {"red": .7}})
        self.assertNotIn("backgroundColor", fmt)
        self.assertNotIn("numberFormat", fmt)
        self.assertEqual(fmt["textFormat"]["fontSize"], 12)
        self.assertTrue(fmt["textFormat"]["strikethrough"])
        self.assertNotIn("foregroundColor", fmt["textFormat"])
        self.assertEqual(fmt["textFormat"]["foregroundColorStyle"], {"rgbColor": {"green": .6}})
        self.assertFalse(archive.get("conditionalFormats"))
        self.assertEqual(plan["sources"]["202609"]["conditionalFormats"], source["conditionalFormats"])
        self.assertTrue(plan["summary"]["months"][0]["conditional_format_snapshot"])
        self.assertEqual(verify_archive(plan, client.read()), "verified")
        # Real Sheets returns these legacy RGB duplicates beside ColorStyle.
        fmt["backgroundColor"] = {"red": .7}
        fmt["textFormat"]["foregroundColor"] = {"green": .6}
        self.assertEqual(verify_archive(plan, client.read()), "verified")
        fmt["backgroundColorStyle"] = {"rgbColor": {"red": .8}}
        self.assertEqual(verify_archive(plan, client.read()), "conflict")
        fmt["backgroundColorStyle"] = {"rgbColor": {"red": .7}}
        source["conditionalFormats"][0]["booleanRule"]["condition"]["values"][0]["userEnteredValue"] = "=$F2=FALSE"
        client.book["sheets"][0]["conditionalFormats"] = source["conditionalFormats"]
        self.assertEqual(verify_archive(plan, client.read()), "conflict")

    def test_concurrent_conditional_effect_blocks_source_delete(self):
        book = prepared()
        book["sheets"][0]["conditionalFormats"] = [{"ranges": [{"sheetId": 101}]}]
        book["sheets"][0]["rows"][2][2]["effectiveFormat"] = {"textFormat": {"bold": True}}
        plan, client = self.stage(book)
        client.book["sheets"][0]["rows"][2][2]["effectiveFormat"]["textFormat"]["bold"] = False
        self.assertEqual(verify_archive(plan, client.read()), "conflict")
        with self.assertRaisesRegex(ValueError, "禁止刪除"):
            build_archive_delete(plan, client.read())

    def test_validation_formulas_and_range_dropdowns_refuse_copy(self):
        cases = (("CUSTOM_FORMULA", '=$C2<>""'), ("ONE_OF_RANGE", "$A$2:$A$5"),
                 ("NUMBER_GREATER", " =ROW()"), ("TEXT_CONTAINS", "=A2"))
        for kind, value in cases:
            book = prepared()
            book["sheets"][0]["rows"][2][14]["dataValidation"] = {
                "condition": {"type": kind, "values": [{"userEnteredValue": value}]}, "strict": True}
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "資料驗證"):
                build_archive(book, ["202609"], TODAY)

    def test_literal_list_dropdown_is_preserved_including_equals_literal(self):
        book = prepared()
        validation = {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": "=literal"},
                      {"userEnteredValue": "OD"}]}, "strict": True, "showCustomUi": True}
        book["sheets"][0]["rows"][2][14]["dataValidation"] = validation
        plan, client = self.stage(book)
        self.assertEqual(verify_archive(plan, client.read()), "verified")
        self.assertEqual(client.book["sheets"][1]["rows"][8][14]["dataValidation"], validation)

    def test_existing_archive_conditional_rules_are_backed_up_and_frozen_in_preview(self):
        plan, client = self.stage(prepared("202608", 100), ["202608"])
        client.write(build_archive_delete(plan, client.read())["requests"])
        target = client.book["sheets"][0]
        target["conditionalFormats"] = [{"ranges": [{"sheetId": target["properties"]["sheetId"]}],
            "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": "=TRUE"}]}}}]
        client.book["sheets"].append(prepared()["sheets"][0])
        before = copy.deepcopy(client.read())
        migration = build_archive(client.read(), ["202609"], TODAY)
        self.assertEqual(client.read(), before)
        self.assertEqual(migration["original_targets"]["刀表封存_2026"]["conditionalFormats"], before["sheets"][0]["conditionalFormats"])
        self.assertTrue(migration["summary"]["months"][0]["migrated_existing_format"])
        client.write(migration["requests"])
        self.assertEqual(verify_archive(migration, client.read()), "verified")
        self.assertFalse(client.book["sheets"][0].get("conditionalFormats"))

    def test_backfill_preserves_annual_id_native_cells_row_dimensions_and_merges(self):
        book = prepared()
        book["sheets"][0]["row_metadata"] = [{}, {}, {"pixelSize": 57, "hiddenByUser": True}]
        book["sheets"][0]["merges"] = [{"sheetId": 101, "startRowIndex": 2, "endRowIndex": 3,
                                        "startColumnIndex": 14, "endColumnIndex": 16}]
        book["sheets"][0]["rows"][2][15] = {}
        plan, client = self.stage(book)
        client.write(build_archive_delete(plan, client.read())["requests"])
        old = copy.deepcopy(client.book["sheets"][0])
        client.book["sheets"].append(prepared("202606", 99)["sheets"][0])
        backfill = build_archive(client.read(), ["202606"], TODAY)
        client.write(backfill["requests"])
        self.assertEqual(verify_archive(backfill, client.read()), "verified")
        result = client.book["sheets"][0]
        self.assertEqual(result["properties"]["sheetId"], old["properties"]["sheetId"])
        count = backfill["summary"]["months"][0]["copied_rows"]
        self.assertEqual(result["rows"][6 + count:], old["rows"][6:])
        self.assertEqual(result["row_metadata"][8 + count], {"pixelSize": 57, "hiddenByUser": True})

    def test_reserved_source_header_cannot_overwrite_existing_archive_metadata(self):
        for label in ("ArchiveRowType", "ArchiveSourceMonth"):
            plan, client = self.stage(prepared("202608", 100), ["202608"])
            client.write(build_archive_delete(plan, client.read())["requests"])
            source = prepared()["sheets"][0]
            source["rows"][0].append(literal(label))
            source["rows"][2].append(literal("manual custom value"))
            source["properties"]["gridProperties"]["columnCount"] = 25
            client.book["sheets"].append(source)
            before = copy.deepcopy(client.read())
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "封存專用欄名"):
                build_archive(client.read(), ["202609"], TODAY)
            self.assertEqual(client.read(), before)

    def test_complete_all_sheet_formula_read_required_before_deletion(self):
        plan, client = self.stage()
        client.book["all_sheet_properties"].append({"sheetId": 999, "title": "尚未讀取"})
        with self.assertRaisesRegex(ValueError, "所有分頁公式"):
            build_archive_delete(plan, client.read())

    def test_named_ranges_bound_to_source_refuse_copy_or_concurrent_delete(self):
        book = prepared()
        book["namedRanges"] = [{"name": "手術總覽", "range": {"sheetId": 101, "startRowIndex": 1}}]
        with self.assertRaisesRegex(ValueError, "命名範圍"):
            build_archive(book, ["202609"], TODAY)
        plan, client = self.stage()
        client.book["namedRanges"] = book["namedRanges"]
        with self.assertRaisesRegex(ValueError, "命名範圍"):
            build_archive_delete(plan, client.read())

    def test_source_charts_or_other_chart_source_ranges_refuse_copy(self):
        for other in (False, True):
            book = prepared()
            chart = {"chartId": 50, "spec": {"basicChart": {"series": [{"series": {"sourceRange": {
                "sources": [{"sheetId": 101, "startColumnIndex": 9}]}}}]}}}
            if other:
                book["sheets"].append({"properties": {"title": "人工圖表", "sheetId": 900}, "rows": [], "charts": [chart]})
            else:
                book["sheets"][0]["charts"] = [chart]
            with self.subTest(other=other), self.assertRaisesRegex(ValueError, "圖表"):
                build_archive(book, ["202609"], TODAY)

    def test_body_merge_remapped_without_deleting_source(self):
        book = prepared()
        source = book["sheets"][0]
        source["rows"][2][13] = {}
        source["merges"] = [{"sheetId": 101, "startRowIndex": 2, "endRowIndex": 3,
                             "startColumnIndex": 12, "endColumnIndex": 14}]
        plan, client = self.stage(book)
        self.assertEqual(verify_archive(plan, client.read()), "verified")
        self.assertEqual(client.book["sheets"][1]["merges"][0]["startRowIndex"], 8)
        source["merges"][0]["startRowIndex"] = 0
        with self.assertRaisesRegex(ValueError, "合併儲存格"):
            build_archive(book, ["202609"], TODAY)

    def test_new_custom_header_reuses_truly_empty_archive_grid_tail(self):
        plan, client = self.stage(prepared("202608", 100), ["202608"])
        client.write(build_archive_delete(plan, client.read())["requests"])
        archive = client.book["sheets"][0]
        archive["properties"]["gridProperties"]["columnCount"] = 30
        archive["rows"][5].extend([{"userEnteredFormat": {"textFormat": {"bold": True}}} for _ in range(4)])
        source = prepared()["sheets"][0]
        source["rows"][0].append(literal("新人工欄"))
        source["rows"][2].append(literal("保留"))
        source["properties"]["gridProperties"]["columnCount"] = 25
        client.book["sheets"].append(source)
        second = build_archive(client.read(), ["202609"], TODAY)
        client.write(second["requests"])
        self.assertEqual(verify_archive(second, client.read()), "verified")
        cols = sheet_headers(client.book["sheets"][0], 5)
        self.assertEqual(cols["新人工欄"], 26)
        self.assertEqual(client.book["sheets"][0]["properties"]["gridProperties"]["columnCount"], 30)

    def test_new_sheet_id_does_not_collide_with_unloaded_metadata(self):
        book = prepared()
        book["all_sheet_properties"].append({"sheetId": 50, "title": "不相關"})
        with patch("vghks_bot.sheet_archive.secrets.randbelow", side_effect=[49, 60]):
            plan = build_archive(book, ["202609"], TODAY)
        self.assertEqual(plan["targets"]["刀表封存_2026"]["properties"]["sheetId"], 61)

    def test_strict_protection_is_retained_and_never_weakened(self):
        book = prepared()
        book["sheets"][0]["protectedRanges"] = [{"protectedRangeId": 1, "range": {"sheetId": 101}}]
        before = copy.deepcopy(book)
        with self.assertRaisesRegex(ValueError, "嚴格保護"):
            build_archive(book, ["202609"], TODAY)
        self.assertEqual(book, before)


if __name__ == "__main__":
    unittest.main()
