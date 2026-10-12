"""Offline structural management, durable commit recovery and literal IOL reports."""
import copy
import json
import unittest
from types import SimpleNamespace

from analysis_fixtures import MemorySheets, book_fixture, literal
from test_sheet_archive import prepared

from vghks_bot.google_sheets import SheetError, SheetUnavailable, SheetUncertain
from vghks_bot.sheet_identity import (
    EVENT_ID_HEADER,
    SYNC_ID_HEADER,
    header_columns,
    plan_identity_setup,
)
from vghks_bot.sheet_management import PLAN_KIND, STANDARD_HEADERS, SheetManagement
from vghks_bot.sheet_plan import entries


class Documents:
    def __init__(self):
        self.values = {}

    def save(self, kind, value):
        json.dumps(value, ensure_ascii=False)
        self.values[kind, value["id"]] = copy.deepcopy(value)
        return copy.deepcopy(value)

    def get(self, kind, key, *, required=True):
        value = self.values.get((kind, key))
        if value is None and required:
            raise ValueError("saved document missing")
        return copy.deepcopy(value)

    def all(self, kind):
        return [copy.deepcopy(value) for (table, _), value in self.values.items() if table == kind]


class Sheets(MemorySheets):
    def __init__(self, book, db):
        super().__init__(book)
        self.db, self.offline = db, False
        self.pending_seen, self.reads, self.archive_reads = [], 0, 0
        self.fail_after_write = self.reject_write = False

    def read_all(self):
        self.reads += 1
        if self.offline:
            raise SheetUnavailable("synthetic network unavailable")
        return super().read()

    def read_for_archive(self):
        self.archive_reads += 1
        book = self.read_all()
        book["all_sheet_properties"] = [copy.deepcopy(sheet["properties"]) for sheet in book["sheets"]]
        return book

    def write(self, requests):
        if requests and all("deleteDeveloperMetadata" in request for request in requests):
            self.marker_writes += 1
        else:
            self.pending_seen.append([plan["status"] for plan in self.db.all(PLAN_KIND)])
            self.writes += 1
        if self.timeout_before_commit:
            raise SheetUncertain("synthetic timeout")
        if self.reject_write:
            raise SheetError("synthetic write refused")
        # Match the real field-mask behavior for the existing fixture double.
        requests = copy.deepcopy(requests)
        grids = {sheet["properties"]["sheetId"]: copy.deepcopy(sheet["properties"]["gridProperties"])
                 for sheet in self.book["sheets"]}
        for request in requests:
            if "addSheet" in request:
                props = request["addSheet"]["properties"]
                grids[props["sheetId"]] = copy.deepcopy(props["gridProperties"])
            elif "appendDimension" in request:
                body = request["appendDimension"]
                grids[body["sheetId"]]["rowCount" if body["dimension"] == "ROWS" else "columnCount"] += body["length"]
            if "updateSheetProperties" in request:
                body = request["updateSheetProperties"]
                props = body["properties"]
                if "gridProperties" in props:
                    props["gridProperties"] = {**grids[props["sheetId"]], **props["gridProperties"]}
        staged = MemorySheets(copy.deepcopy(self.book))
        staged.write([request for request in requests if not {"mergeCells", "deleteSheet"} & request.keys()])
        for request in requests:
            if "mergeCells" in request:
                merged = copy.deepcopy(request["mergeCells"]["range"])
                sheet = next(sheet for sheet in staged.book["sheets"] if sheet["properties"]["sheetId"] == merged["sheetId"])
                sheet.setdefault("merges", []).append(merged)
            elif "deleteSheet" in request:
                sid = request["deleteSheet"]["sheetId"]
                staged.book["sheets"] = [sheet for sheet in staged.book["sheets"] if sheet["properties"]["sheetId"] != sid]
        self.book = staged.book
        if self.timeout_after_commit:
            raise SheetUncertain("synthetic response lost")
        if self.fail_after_write:
            self.offline = True


class SheetManagementTests(unittest.TestCase):
    def setUp(self):
        self.db = Documents()
        book = book_fixture()
        # The old preservation fixture deliberately shares Calendar IDs. They
        # are not needed for these independent structural-management cases.
        for row in book["sheets"][0]["rows"][1:]:
            if len(row) > 22:
                row[22] = {}
        self.client = Sheets(book, self.db)
        self.config = {"spreadsheet_id": self.client.key, "configured": True}
        analysis = SimpleNamespace(google=SimpleNamespace(public=lambda: copy.deepcopy(self.config)),
                                   sheet_operation=lambda callback: callback(self.client))
        self.app = SimpleNamespace(analysis=analysis, review=SimpleNamespace(db=self.db), read_only=False)
        self.manager = SheetManagement(self.app)

    def preview(self, action="create_month", **params):
        return self.manager.preview({"action": action, "params": params})

    def test_status_is_cache_only_and_refresh_needs_no_patient_list(self):
        initial = self.manager.status()
        self.assertTrue(initial["stale"])
        self.assertEqual(self.client.reads, 0)
        live = self.manager.refresh()
        self.assertTrue(live["online"])
        self.assertFalse(live["stale"])
        self.assertEqual(live["months"][0]["patient_count"], 3)
        self.assertEqual(len(live["months"][0]["days"]), 3)
        reads = self.client.reads
        cached = self.manager.status()
        self.assertTrue(cached["stale"])
        self.assertEqual(cached["cached_at"], live["cached_at"])
        self.assertEqual(self.client.reads, reads)
        self.assertEqual(self.client.writes, 0)

    def test_unconfigured_connection_is_explicit_and_intent_is_saved_as_draft(self):
        self.config["configured"] = False
        state = self.manager.status()
        self.assertFalse(state["configured"])
        self.assertFalse(state["connection"]["online"])
        draft = self.preview(month="2026-11")
        self.assertEqual(draft["status"], "draft")
        self.assertFalse(draft["can_apply"])
        self.assertEqual(draft["params"]["month"], "202611")
        self.assertEqual(self.client.reads, 0)
        self.assertEqual(len(self.db.all(PLAN_KIND)), 1)

    def test_create_empty_month_preserves_template_and_pending_before_single_write(self):
        source = copy.deepcopy(self.client.book["sheets"][0])
        source["rows"][0][16] = {"userEnteredValue": {"formulaValue": '="Manual Header"'}, "formattedValue": "Manual Header"}
        self.client.book["sheets"][0] = source
        preview = self.preview(month="202611")
        self.assertTrue(preview["can_apply"], preview)
        self.assertEqual(self.client.writes, 0)
        self.assertNotIn("requests", preview)
        self.assertNotIn("expected", preview)
        self.assertNotIn("baseline", preview)
        result = self.manager.apply({"id": preview["id"]})
        self.assertEqual(result["status"], "applied", result)
        self.assertEqual(self.client.writes, 1)
        self.assertIn("pending", self.client.pending_seen[0])
        created = self.client.book["sheets"][-1]
        self.assertEqual(created["properties"]["title"], "202611")
        self.assertEqual(created["properties"]["gridProperties"]["frozenRowCount"], 1)
        self.assertEqual(len(created["rows"]), 1)
        self.assertEqual(created["rows"][0][16]["userEnteredValue"], source["rows"][0][16]["userEnteredValue"])
        self.assertEqual(created.get("protectedRanges", []), [])
        self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "applied")
        self.assertEqual(self.client.writes, 1)

    def test_blank_workbook_bootstraps_explicit_standard_month_without_patients(self):
        self.client.book = {"properties": {"timeZone": "Asia/Taipei"}, "sheets": [], "developerMetadata": []}
        preview = self.preview(month="2026-11")
        self.assertTrue(preview["can_apply"], preview)
        self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "applied")
        sheet = self.client.book["sheets"][0]
        self.assertEqual(tuple(cell["userEnteredValue"]["stringValue"] for cell in sheet["rows"][0]), STANDARD_HEADERS)
        self.assertEqual(entries(sheet), [])
        self.assertEqual(sheet.get("protectedRanges", []), [])
        cols = header_columns(sheet)
        self.assertTrue(all(sheet["column_widths"][cols[label]]["hiddenByUser"] for label in (EVENT_ID_HEADER, SYNC_ID_HEADER)))

    def test_new_month_keeps_conditional_rules_and_only_header_height_without_patients(self):
        source = self.client.book["sheets"][0]
        source["properties"]["gridProperties"]["rowCount"] = 1500
        source["row_metadata"] = [{"pixelSize": 42}, {"pixelSize": 95, "hiddenByUser": True}]
        formula = "=COUNTIF('202609'!$H$2:$H$1200,H2)>0"
        source["conditionalFormats"] = [
            {"ranges": [{"sheetId": 101, "startRowIndex": 1, "endRowIndex": 1200,
                         "startColumnIndex": 7, "endColumnIndex": 8}],
             "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                             "format": {"backgroundColor": {"red": 1}}}},
            {"ranges": [{"sheetId": 101, "startColumnIndex": 10, "endColumnIndex": 11},
                        {"sheetId": 101, "startColumnIndex": 12, "endColumnIndex": 13}],
             "gradientRule": {"minpoint": {"color": {"green": 1}, "type": "MIN"},
                              "maxpoint": {"color": {"red": 1}, "type": "MAX"}}},
        ]
        original = copy.deepcopy(source)
        preview = self.preview(month="202611")
        self.assertTrue(preview["can_apply"], preview)
        self.assertTrue(any("條件格式" in change and "202609" in change for change in preview["changes"]))
        self.assertEqual(source, original)
        self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "applied")
        created = self.client.book["sheets"][-1]
        expected = copy.deepcopy(original["conditionalFormats"])
        for rule in expected:
            for grid_range in rule["ranges"]:
                grid_range["sheetId"] = created["properties"]["sheetId"]
        self.assertEqual(created["conditionalFormats"], expected)
        self.assertEqual(created["row_metadata"], [{"pixelSize": 42}])
        self.assertEqual(created["properties"]["gridProperties"]["rowCount"], 1500)
        self.assertEqual(len(created["rows"]), 1)
        self.assertEqual(entries(created), [])
        self.assertEqual(created["conditionalFormats"][0]["booleanRule"]["condition"]["values"][0]["userEnteredValue"], formula)

    def test_create_day_without_any_patients_and_two_hospitals_are_independent(self):
        self.client.book["sheets"][0]["rows"] = [self.client.book["sheets"][0]["rows"][0]]
        for hospital in ("高榮", "聯醫"):
            preview = self.preview("create_day", date="2026-11-02", hospital=hospital)
            self.assertTrue(preview["can_apply"], preview)
            self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "applied")
        created = next(sheet for sheet in self.client.book["sheets"] if sheet["properties"]["title"] == "202611")
        parsed = entries(created)
        self.assertEqual({entry["hospital"] for entry in parsed}, {"高榮", "聯醫"})
        self.assertTrue(all(entry["kind"] == "day" and entry["sync_id"] for entry in parsed))
        preview = self.preview("create_day", date="2026-11-02", hospital="高榮")
        self.assertEqual(preview["status"], "unchanged")

    def test_repair_only_changes_system_ids_hiding_and_header_freeze(self):
        original = copy.deepcopy(self.client.book["sheets"][0])
        preview = self.preview("repair")
        self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "applied")
        actual = self.client.book["sheets"][0]
        for expected_row, actual_row in zip(original["rows"], actual["rows"], strict=True):
            self.assertEqual(actual_row[:23], expected_row)
        self.assertEqual(actual["properties"]["gridProperties"]["frozenRowCount"], 1)
        self.assertEqual(self.preview("repair")["status"], "unchanged")

    def test_header_drift_or_protection_is_blocked_without_requests(self):
        second = copy.deepcopy(self.client.book["sheets"][0])
        second["properties"].update(sheetId=103, title="202610")
        second["rows"] = [second["rows"][0]]
        second["rows"][0][12] = literal("Different manual field")
        self.client.book["sheets"].append(second)
        blocked = self.preview(month="202611")
        self.assertEqual(blocked["status"], "blocked")
        self.assertFalse(blocked["can_apply"])
        self.assertEqual(blocked["request_count"], 0)
        self.client.book["sheets"].pop()
        self.client.book["sheets"][0]["protectedRanges"] = [{"range": {"sheetId": 101, "startColumnIndex": 22}}]
        blocked = self.preview("repair")
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(any(issue["code"] == "protected_identity_range" for issue in blocked["issues"]))
        self.assertEqual(self.client.writes, 0)

    def test_concurrent_sheet_edit_invalidates_preview_and_never_writes(self):
        preview = self.preview(month="202611")
        self.client.book["sheets"][0]["rows"][5][15] = literal("changed manual note")
        result = self.manager.apply({"id": preview["id"]})
        self.assertEqual(result["status"], "stale")
        self.assertFalse(result["can_apply"])
        self.assertEqual(self.client.writes, 0)

    def test_offline_cache_is_stale_and_offline_intent_never_uses_cache_to_write(self):
        live = self.manager.refresh()
        self.client.offline = True
        failed = self.manager.refresh()
        self.assertTrue(failed["stale"])
        self.assertFalse(failed["online"])
        self.assertEqual(failed["months"], live["months"])
        draft = self.preview(month="202611")
        self.assertEqual(draft["status"], "draft")
        with self.assertRaisesRegex(ValueError, "重新連線"):
            self.manager.apply({"id": draft["id"]})
        self.assertEqual(self.client.writes, 0)
        self.client.offline = False
        new = self.preview(month="202611")
        self.assertEqual(self.manager.apply({"id": new["id"]})["status"], "applied")

    def test_uncertain_committed_write_recovers_read_only_after_restart(self):
        preview = self.preview(month="202611")
        self.client.timeout_after_commit = True
        result = self.manager.apply({"id": preview["id"]})
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.client.writes, 1)
        restored = SheetManagement(self.app)
        result = restored.check({"id": preview["id"]})
        self.assertEqual(result["status"], "applied", result)
        self.assertEqual(restored.apply({"id": preview["id"]})["status"], "applied")
        self.assertEqual(self.client.writes, 1)

    def test_uncertain_uncommitted_write_is_never_blindly_resent(self):
        preview = self.preview(month="202611")
        self.client.timeout_before_commit = True
        self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "uncertain")
        self.assertEqual(self.manager.check({"id": preview["id"]})["status"], "uncertain")
        self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "uncertain")
        self.assertEqual(self.client.writes, 1)

    def test_new_preview_cannot_bypass_unresolved_batch_for_same_or_different_intent(self):
        old = self.preview("create_day", date="2026-11-02", hospital="高榮")
        other = self.preview(month="202612")
        self.client.timeout_before_commit = True
        self.assertEqual(self.manager.apply({"id": old["id"]})["status"], "uncertain")
        reads = self.client.reads
        same = self.preview("create_day", date="2026-11-02", hospital="高榮")
        self.assertEqual((same["id"], same["status"]), (old["id"], "uncertain"))
        blocked = self.preview("repair")
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["blocking_plan_id"], old["id"])
        self.assertEqual(self.manager.apply({"id": other["id"]})["status"], "blocked")
        self.assertEqual(self.client.reads, reads)
        self.assertEqual(self.client.writes, 1)

    def test_durable_pending_and_failed_batches_exclude_new_writes(self):
        for status in ("pending", "failed"):
            with self.subTest(status=status):
                self.setUp()
                old = self.preview(month="202611")
                plan = self.db.get(PLAN_KIND, old["id"])
                plan["status"] = status
                self.db.save(PLAN_KIND, plan)
                blocked = self.preview(month="202612")
                self.assertEqual((blocked["status"], blocked["blocking_plan_id"]), ("blocked", old["id"]))
                self.assertEqual(self.client.writes, 0)

    def test_successful_write_with_failed_readback_waits_for_read_only_check(self):
        preview = self.preview(month="202611")
        self.client.fail_after_write = True
        self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "uncertain")
        self.client.offline = False
        self.assertEqual(self.manager.check({"id": preview["id"]})["status"], "applied")
        self.assertEqual(self.client.writes, 1)

    def test_rejected_write_needs_fresh_preview_and_does_not_lock_future_management(self):
        preview = self.preview(month="202611")
        self.app.read_only = True
        with self.assertRaisesRegex(ValueError, "唯讀"):
            self.manager.apply({"id": preview["id"]})
        self.assertEqual(self.client.writes, 0)
        self.app.read_only = False
        self.client.reject_write = True
        self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "rejected")
        with self.assertRaisesRegex(ValueError, "預覽"):
            self.manager.apply({"id": preview["id"]})
        self.assertEqual(self.client.writes, 1)
        self.client.reject_write = False
        fresh = self.preview(month="202611")
        self.assertNotEqual(fresh["id"], preview["id"])
        self.assertTrue(fresh["can_apply"], fresh)
        self.assertEqual(self.manager.apply({"id": fresh["id"]})["status"], "applied")

    def test_iol_report_preserves_model_target_power_and_excludes_only_cancelled_rows(self):
        sheet = self.client.book["sheets"][0]
        sheet["rows"][2][10], sheet["rows"][2][11], sheet["rows"][2][12] = map(literal, ("SN60WF", "-0.50", "+20.5"))
        sheet["rows"][2][14] = literal("先前取消後重新評估；此列未取消")
        sheet["rows"][5][10] = literal("DIB00")
        sheet["rows"][5][2]["userEnteredFormat"] = {"textFormat": {"strikethrough": True}}
        sheet["rows"][8][10] = literal("SN60WF")
        sheet["rows"][8][6] = literal("OU")
        report = self.manager.report({"start": "2026-09-29", "end": "2026-09-30"})
        self.assertFalse(report["stale"])
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["excluded_cancelled"], 1)
        self.assertEqual(report["models"], [{"model": "SN60WF", "count": 2}])
        item = next(item for item in report["items"] if item["hospital"] == "聯醫")
        self.assertEqual((item["iol"], item["iol_target"], item["iol_final"]), ("SN60WF", "-0.50", "+20.5"))
        self.assertIn("OU", report["count_basis"])
        filtered = self.manager.report({"hospital": "聯醫"})
        self.assertEqual(filtered["total"], 1)
        self.client.offline = True
        cached = self.manager.report({"hospital": "聯醫"})
        self.assertTrue(cached["stale"])
        self.assertEqual(cached["items"], filtered["items"])
        self.assertEqual(self.client.writes, 0)

    def test_bad_date_hospital_month_and_report_range_are_rejected(self):
        for values in ({"action": "create_month", "month": "202613"},
                       {"action": "create_day", "date": "2026-11-02 08:00", "hospital": "高榮"},
                       {"action": "create_day", "date": "2026-11-02", "hospital": "另一院區"},
                       {"action": "create_day", "date": "2026-02-30", "hospital": "聯醫"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.manager.preview(values)
        with self.assertRaises(ValueError):
            self.manager.report({"start": "2026-11-02", "end": "2026-10-01"})
        self.assertEqual(self.client.writes, 0)

    def test_changed_settings_cannot_apply_a_different_spreadsheet(self):
        preview = self.preview(month="202611")
        self.client.key = "different-synthetic-spreadsheet-00000000"
        with self.assertRaisesRegex(ValueError, "刀表設定已改變"):
            self.manager.apply({"id": preview["id"]})
        self.assertEqual(self.client.writes, 0)

    def test_public_cache_and_plan_are_json_without_raw_native_snapshots(self):
        preview = self.preview("repair")
        public = json.dumps({"plan": preview, "status": self.manager.status()}, ensure_ascii=False)
        self.assertNotIn("userEnteredValue", public)
        self.assertNotIn("private_key", public)
        self.assertNotIn("0900-111-111", public)
        self.assertIn("requests", self.db.get(PLAN_KIND, preview["id"]))

    def test_new_month_does_not_need_a_preexisting_patient_template(self):
        book = plan_identity_setup(self.client.book)["book"]
        book["sheets"][0]["rows"] = [book["sheets"][0]["rows"][0]]
        self.client.book = book
        preview = self.preview(month="202611")
        self.assertTrue(preview["can_apply"])
        self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "applied")

    def prepare_archive(self):
        self.client.book = prepared()
        self.client.book["sheets"][0]["rows"][2][10] = literal("SN60WF")
        self.client.book["sheets"][0]["rows"][5][10] = literal("DIB00")
        self.client.book["sheets"][0]["rows"][8][10] = literal("SN60WF")
        return self.preview("archive", months=["202609"])

    def test_archive_two_phases_keep_backup_and_iol_totals_before_copy_and_after_delete(self):
        preview = self.prepare_archive()
        self.assertTrue(preview["can_apply"], preview)
        self.assertTrue(all(isinstance(change, str) for change in preview["changes"]))
        before = self.manager.report({})
        copied = self.manager.apply({"id": preview["id"]})
        self.assertEqual(copied["status"], "copied", copied)
        self.assertTrue(copied["can_finalize"])
        self.assertEqual(self.client.writes, 1)
        self.assertEqual(len(self.client.book["sheets"]), 2)
        during = self.manager.report({})
        self.assertEqual((during["total"], during["models"]), (before["total"], before["models"]))
        self.assertFalse(during["complete"])
        self.assertTrue(any(issue["code"] == "duplicate_report_sync_id" for issue in during["issues"]))
        self.assertEqual(self.manager.check({"id": preview["id"]})["status"], "copied")
        self.assertEqual(self.client.writes, 1)
        self.assertEqual(self.preview("repair")["status"], "blocked")
        applied = self.manager.apply({"id": preview["id"]})
        self.assertEqual(applied["status"], "applied", applied)
        self.assertEqual(self.client.writes, 2)
        saved = self.db.get(PLAN_KIND, preview["id"])
        self.assertIn("202609", saved["archive_plan"]["sources"])
        self.assertIn("刀表封存_2026", saved["archive_plan"]["targets"])
        self.assertIn("original_targets", saved["archive_plan"])
        self.assertTrue(saved["archive_plan"]["sources"]["202609"]["rows"])
        after = self.manager.report({})
        self.assertEqual((after["total"], after["models"]), (before["total"], before["models"]))
        self.assertTrue(after["complete"], after["issues"])
        self.assertTrue(all(item["position"].startswith("刀表封存_2026!") for item in after["items"]))
        self.assertGreater(self.client.archive_reads, 3)
        self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "applied")
        self.assertEqual(self.client.writes, 2)

    def test_uncertain_archive_copy_and_delete_recover_without_replaying_either_batch(self):
        preview = self.prepare_archive()
        self.client.timeout_after_commit = True
        self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "uncertain")
        restored = SheetManagement(self.app)
        self.assertEqual(restored.check({"id": preview["id"]})["status"], "copied")
        self.assertEqual(self.client.writes, 1)
        self.assertEqual(restored.apply({"id": preview["id"]})["status"], "uncertain")
        self.assertEqual(restored.check({"id": preview["id"]})["status"], "applied")
        self.assertEqual(restored.apply({"id": preview["id"]})["status"], "applied")
        self.assertEqual(self.client.writes, 2)
        self.assertEqual(len(self.client.book["sheets"]), 1)

    def test_rejected_archive_delete_retains_copy_guard_and_can_finalize_after_fix(self):
        preview = self.prepare_archive()
        values = {"id": preview["id"]}
        self.assertEqual(self.manager.apply(values)["status"], "copied")
        self.client.reject_write = True
        self.assertEqual(self.manager.apply(values)["status"], "copied")
        saved = self.db.get(PLAN_KIND, preview["id"])
        self.assertEqual((saved["phase"], saved["marker"]), ("copy", preview["id"] + ":copy"))
        self.assertEqual(self.preview("repair")["status"], "blocked")
        self.assertEqual(len(self.client.book["sheets"]), 2)
        self.client.reject_write = False
        restored = SheetManagement(self.app)
        self.assertEqual(restored.check(values)["status"], "copied")
        self.assertEqual(restored.apply(values)["status"], "applied")
        self.assertEqual(self.client.writes, 3)
        self.assertEqual(len(self.client.book["sheets"]), 1)

    def test_archive_displayed_cancellation_keeps_iol_totals_after_static_format_copy(self):
        self.client.book = prepared()
        sheet = self.client.book["sheets"][0]
        sheet["conditionalFormats"] = [{"ranges": [{"sheetId": sheet["properties"]["sheetId"]}],
            "booleanRule": {"condition": {"type": "NOT_BLANK"},
                            "format": {"textFormat": {"strikethrough": True}}}}]
        sheet["rows"][2][2]["effectiveFormat"] = {"textFormat": {"strikethrough": True}}
        before = self.manager.report({})
        preview = self.preview("archive", months=["202609"])
        self.assertTrue(any("樣式" in change for change in preview["changes"]), preview["changes"])
        self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "copied")
        self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "applied")
        after = self.manager.report({})
        self.assertEqual((after["total"], after["excluded_cancelled"]),
                         (before["total"], before["excluded_cancelled"]))

    def test_waiting_apply_and_check_reload_completed_archive_phase_under_lock(self):
        for method in ("apply", "check"):
            with self.subTest(method=method):
                self.setUp()
                preview = self.prepare_archive()
                values = {"id": preview["id"]}
                self.assertEqual(self.manager.apply(values)["status"], "copied")
                operation = self.app.analysis.sheet_operation

                def interleaved(callback, operation=operation, values=values):
                    self.app.analysis.sheet_operation = operation
                    self.assertEqual(self.manager.apply(values)["status"], "applied")
                    return callback(self.client)

                self.app.analysis.sheet_operation = interleaved
                self.assertEqual(getattr(self.manager, method)(values)["status"], "applied")
                saved = self.db.get(PLAN_KIND, preview["id"])
                self.assertEqual((saved["status"], saved["phase"]), ("applied", "delete"))
                self.assertEqual(self.client.writes, 2)
                self.assertTrue(self.preview(month="202611")["can_apply"])

    def test_waiting_apply_reloads_new_uncertain_status_without_a_second_post(self):
        preview = self.preview(month="202611")
        values = {"id": preview["id"]}
        operation = self.app.analysis.sheet_operation

        def interleaved(callback):
            self.app.analysis.sheet_operation = operation
            self.client.timeout_before_commit = True
            self.assertEqual(self.manager.apply(values)["status"], "uncertain")
            return callback(self.client)

        self.app.analysis.sheet_operation = interleaved
        self.assertEqual(self.manager.apply(values)["status"], "uncertain")
        self.assertEqual(self.client.writes, 1)

    def test_archive_changed_source_or_lost_receipt_never_deletes_and_blocks_other_writes(self):
        for mode in ("source", "receipt"):
            with self.subTest(mode=mode):
                self.setUp()
                preview = self.prepare_archive()
                self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "copied")
                if mode == "source":
                    self.client.book["sheets"][0]["rows"][2][14] = literal("concurrent clinical edit")
                else:
                    self.client.book["developerMetadata"] = []
                    plan = self.db.get(PLAN_KIND, preview["id"])
                    plan.pop("confirmed_markers", None)
                    self.db.save(PLAN_KIND, plan)
                self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "blocked")
                self.assertEqual(len(self.client.book["sheets"]), 2)
                self.assertEqual(self.preview("repair")["status"], "blocked")
                self.assertEqual(self.client.writes, 1)

    def test_archive_finalizes_with_local_receipt_after_cloud_marker_reclamation(self):
        preview = self.prepare_archive()
        self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "copied")
        self.assertEqual(self.client.book["developerMetadata"], [])
        self.assertEqual(self.client.marker_writes, 1)
        self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "applied")
        self.assertEqual(self.client.writes, 2)
        self.assertEqual(self.client.marker_writes, 2)
        self.assertEqual(len(self.client.book["sheets"]), 1)

    def test_read_only_reconciliation_preserves_unretired_cloud_marker(self):
        preview = self.preview("create_month", month="202610")
        self.client.timeout_after_commit = True
        self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "uncertain")
        self.client.timeout_after_commit = False
        self.app.read_only = True
        self.assertEqual(self.manager.check({"id": preview["id"]})["status"], "applied")
        self.assertEqual(self.client.marker_writes, 0)
        self.assertEqual(len(self.client.book["developerMetadata"]), 1)

    def test_report_conflicting_source_and_archive_identity_excludes_both_copies(self):
        preview = self.prepare_archive()
        self.assertEqual(self.manager.apply({"id": preview["id"]})["status"], "copied")
        self.client.book["sheets"][1]["rows"][8][10] = literal("DIFFERENT")
        report = self.manager.report({})
        self.assertEqual(report["total"], 2)
        self.assertFalse(report["complete"])
        self.assertNotIn("DIFFERENT", {item["model"] for item in report["models"]})
        self.assertTrue(any(issue["code"] == "conflicting_report_sync_id" for issue in report["issues"]))

    def test_archive_report_resets_invalid_or_cross_month_context_without_guessing_arrival_dates(self):
        preview = self.prepare_archive()
        self.manager.apply({"id": preview["id"]})
        self.manager.apply({"id": preview["id"]})
        archive = self.client.book["sheets"][0]
        for mode in ("hospital", "source_month", "patient_date"):
            with self.subTest(mode=mode):
                original = copy.deepcopy(archive["rows"])
                if mode == "hospital":
                    archive["rows"][7][1] = {}
                elif mode == "source_month":
                    cols = header_columns(archive, 5)
                    archive["rows"][7][cols["ArchiveSourceMonth"]] = literal("202608")
                else:
                    archive["rows"][8][0] = literal("2026-09-15")
                report = self.manager.report({})
                self.assertEqual(report["total"], 2)
                self.assertFalse(report["complete"])
                self.assertTrue(any(issue["code"] == "invalid_archive_context" for issue in report["issues"]))
                archive["rows"] = original


if __name__ == "__main__":
    unittest.main()
