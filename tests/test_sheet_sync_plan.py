"""Synthetic, offline regression tests for shared Sheets planning contracts."""
import copy
import unittest

from analysis_fixtures import MemorySheets, book_fixture, literal, proposal, soap_record

from vghks_bot.analysis_store import digest
from vghks_bot.sheet_identity import (
    EVENT_ID_HEADER,
    SYNC_ID_HEADER,
    SYNC_STATE_TITLE,
    header_columns,
    plan_identity_setup,
    scan_identities,
)
from vghks_bot.sheet_plan import FUPlanner, Planner, cell_text, entries, verify
from vghks_bot.sheet_sync_state import (
    field_hash,
    plan_sync_state,
    read_sync_state,
    route_merge,
    three_way_fields,
)
from vghks_bot.surgery_candidates import candidates


def setup_book(book=None):
    client = MemorySheets(book or book_fixture())
    setup = plan_identity_setup(client.read())
    client.write(setup["requests"])
    return client


def uid_at(sheet, row):
    return cell_text(sheet["rows"][row][header_columns(sheet)[SYNC_ID_HEADER]])


class IdentityTests(unittest.TestCase):
    def test_setup_is_idempotent_and_keeps_event_ids_and_manual_content(self):
        original = book_fixture()
        setup = plan_identity_setup(original)
        self.assertFalse(any("addProtectedRange" in request for request in setup["requests"]))
        client = setup_book(original)
        migrated = client.book["sheets"][0]
        self.assertEqual(migrated["rows"][5][:23], original["sheets"][0]["rows"][5])
        self.assertEqual(len(scan_identities(client.book)["by_id"]), 6)
        self.assertFalse(plan_identity_setup(client.read())["requests"])
        self.assertEqual(migrated.get("protectedRanges", []), [])
        cols = header_columns(migrated)
        self.assertTrue(all(migrated["column_widths"][cols[label]]["hiddenByUser"]
                            for label in (EVENT_ID_HEADER, SYNC_ID_HEADER)))

    def test_trailing_grid_blank_columns_use_same_uid_column(self):
        book = book_fixture()
        second = copy.deepcopy(book["sheets"][0])
        second["properties"].update(sheetId=102, title="202610")
        second["properties"]["gridProperties"]["columnCount"] = 31
        book["sheets"][0]["properties"]["gridProperties"]["columnCount"] = 26
        book["sheets"].append(second)
        planned = plan_identity_setup(book)
        self.assertEqual([header_columns(s)[SYNC_ID_HEADER] for s in planned["book"]["sheets"]], [23, 23])
        self.assertFalse(any("appendDimension" in r for r in planned["requests"]))

    def test_alias_migration_and_archive_identity_preservation(self):
        book = book_fixture()
        book["sheets"][0]["rows"][0][22] = literal("日曆apiEventID")
        archive = {"properties": {"sheetId": 900, "title": "刀表封存_2025", "gridProperties": {"rowCount": 100, "columnCount": 5}},
                   "rows": [[], [], [], [], [], [literal(s) for s in ("病歷號", "姓名", "CalendarEventId", "ScheduleSyncId", "ArchiveRowType")],
                            [literal("0000009"), literal("封存患者"), literal("event-original"), literal("a" * 32), literal("patient")]], "merges": []}
        book["sheets"].append(archive)
        planned = plan_identity_setup(book)
        self.assertEqual(cell_text(planned["book"]["sheets"][0]["rows"][0][22]), EVENT_ID_HEADER)
        self.assertEqual(planned["book"]["sheets"][1]["rows"][6][2:4], archive["rows"][6][2:4])
        self.assertEqual(scan_identities(planned["book"])["by_id"]["a" * 32][0]["schema"], "archive")

    def test_duplicate_and_orphan_ids_are_kept_and_reported(self):
        client = setup_book()
        sheet = client.book["sheets"][0]
        col = header_columns(sheet)[SYNC_ID_HEADER]
        sheet["rows"][6] = copy.deepcopy(sheet["rows"][5])
        scan = scan_identities(client.book)
        self.assertTrue(any(i["code"] == "duplicate_sync_id" for i in scan["issues"]))
        orphan = [{} for _ in range(col + 1)]
        orphan[col] = literal("f" * 32)
        sheet["rows"].insert(1, orphan)
        scan = scan_identities(client.book)
        self.assertTrue(any(i["code"] == "orphan_sync_id" for i in scan["issues"]))
        self.assertIn("f" * 32, scan["by_id"])


class StablePlannerTests(unittest.TestCase):
    def execute(self, client, proposals):
        planner = Planner(client.read(), client.key, proposals)
        preview = planner.build()
        self.assertTrue(preview["can_apply"])
        client.write(preview["requests"])
        self.assertEqual(verify(client.read(), preview), "verified")
        return preview

    def test_uid_following_manual_row_drag_preserves_sheet_route_and_arrival(self):
        client = setup_book()
        sheet = client.book["sheets"][0]
        uid = uid_at(sheet, 5)
        row = sheet["rows"].pop(5)
        sheet["rows"].insert(3, row)  # Entire row dragged under 聯醫 block.
        p = proposal()
        p["target_sync_id"] = uid
        preview = self.execute(client, [p])
        entry = next(e for e in entries(client.book["sheets"][0]) if e.get("sync_id") == uid)
        self.assertEqual(entry["fields"]["hospital"], "聯醫")
        self.assertEqual(cell_text(entry["row"][1]), "聯醫")
        self.assertEqual(cell_text(entry["row"][0]), "08:15")
        self.assertEqual(preview["items"][0]["action"], "update")

    def test_iol_only_uid_update_does_not_move_to_stale_soap_date(self):
        client = setup_book()
        p = proposal(date="2026-10-09")
        p.update(target_sync_id=uid_at(client.book["sheets"][0], 5), selected_fields=["iol"])
        preview = self.execute(client, [p])
        self.assertEqual(len(client.book["sheets"]), 1)
        self.assertEqual(preview["items"][0]["route"]["date"], "2026-09-30")

    def test_explicit_destination_moves_without_selecting_date_and_keeps_ids(self):
        client = setup_book()
        uid = uid_at(client.book["sheets"][0], 5)
        p = proposal()
        p.update(target_sync_id=uid, selected_fields=["iol"], destination={"date": "2026-10-09", "hospital": "聯醫"})
        preview = self.execute(client, [p])
        item = preview["items"][0]
        self.assertEqual((item["sync_id"], item["route"]["hospital"], item["action"]), (uid, "聯醫", "move"))
        new = next(s for s in client.book["sheets"] if s["properties"]["title"] == "202610")
        row = next(e["row"] for e in entries(new) if e["kind"] == "patient")
        self.assertEqual((cell_text(row[0]), cell_text(row[22]), row[15]["note"]), ("08:15", "event-keep", "manual cell note"))
        self.assertEqual(len(scan_identities(client.book)["by_id"][uid]), 1)

    def test_duplicate_same_day_hospital_requires_block_but_other_hospital_does_not(self):
        client = setup_book()
        sheet = client.book["sheets"][0]
        duplicate = copy.deepcopy(sheet["rows"][4])
        duplicate[header_columns(sheet)[SYNC_ID_HEADER]] = literal("b" * 32)
        sheet["rows"].extend([duplicate, []])
        p = proposal(mrn="0000099", name="新病人")
        with self.assertRaisesRegex(ValueError, "重複高榮刀日"):
            Planner(client.read(), client.key, [p]).build()
        p["destination"] = {"date": "2026-09-30", "hospital": "高榮", "block_sync_id": "b" * 32}
        self.execute(client, [p])

    def test_block_ref_selects_fresh_migrated_day_and_survives_earlier_row_insertions(self):
        book = book_fixture()
        book["sheets"][0]["rows"].extend([copy.deepcopy(book["sheets"][0]["rows"][4]), []])
        client = setup_book(book)
        target_uid = uid_at(client.book["sheets"][0], 9)
        first = proposal(mrn="0000098", date="2026-09-28", name="先插入")
        selected = proposal(mrn="0000099", name="指定區塊")
        selected["destination"] = {"date": "2026-09-30", "hospital": "高榮", "block_sync_id": "ref:202609!10"}
        preview = self.execute(client, [first, selected])
        self.assertEqual(preview["items"][1]["route"]["block_sync_id"], target_uid)
        self.assertEqual(preview["items"][1]["route"]["date"], "2026-09-30")
        selected["destination"]["hospital"] = "聯醫"
        with self.assertRaisesRegex(ValueError, "日期或醫院不符"):
            Planner(setup_book(book).read(), client.key, [selected]).build()
        selected["destination"].update(hospital="高榮", block_sync_id="ref:202609!6")
        with self.assertRaisesRegex(ValueError, "位置已變更"):
            Planner(setup_book(book).read(), client.key, [selected]).build()

    def test_block_ref_to_current_day_does_not_reorder_the_existing_patient(self):
        client = setup_book()
        selected = proposal()
        selected.update(target_sync_id=uid_at(client.book["sheets"][0], 5), selected_fields=["iol"],
                        destination={"date": "2026-09-30", "hospital": "高榮", "block_sync_id": "ref:202609!5"})
        preview = self.execute(client, [selected])
        self.assertEqual(preview["items"][0]["action"], "update")
        self.assertEqual(preview["items"][0]["position"], "202609!6")
        self.assertFalse(any("cutPaste" in request for request in preview["requests"]))

    def test_cross_month_header_drift_and_duplicate_uid_fail_closed(self):
        client = setup_book()
        second = copy.deepcopy(client.book["sheets"][0])
        second["properties"].update(sheetId=777, title="202610")
        second["rows"] = [second["rows"][0]]
        second["rows"][0][12] = literal("Different manual column")
        client.book["sheets"].append(second)
        p = proposal()
        p.update(target_sync_id=uid_at(client.book["sheets"][0], 5), destination={"date": "2026-10-09", "hospital": "高榮"})
        with self.assertRaisesRegex(ValueError, "欄位"):
            Planner(client.read(), client.key, [p]).build()
        client.book["sheets"].pop()
        client.book["sheets"][0]["rows"][6] = copy.deepcopy(client.book["sheets"][0]["rows"][5])
        with self.assertRaisesRegex(ValueError, "識別碼"):
            Planner(client.read(), client.key, [p]).build()

    def test_existing_uid_updates_under_duplicate_blocks_and_ignores_nonmonths(self):
        client = setup_book()
        sheet = client.book["sheets"][0]
        uid = uid_at(sheet, 5)
        day = copy.deepcopy(sheet["rows"][4])
        day[header_columns(sheet)[SYNC_ID_HEADER]] = literal("c" * 32)
        sheet["rows"].extend([day, []])
        client.book["sheets"].append({"properties": {"sheetId": 999, "title": SYNC_STATE_TITLE}, "rows": []})
        p = proposal()
        p.update(target_sync_id=uid, selected_fields=["iol"])
        self.execute(client, [p])


class SharedStateTests(unittest.TestCase):
    def test_multi_block_quarantine_and_binding_counts_roundtrip(self):
        state = {"source_conflicted": ["encounter-id"], "source_bindings": [{
            "record_id": "encounter-id", "field": "assessment_plan", "block_hash": digest("block"),
            "event_hash": digest("event"), "source_version": digest("version"), "block_count": 2, "ordinal": 0}]}
        planned = plan_sync_state(book_fixture(), {"e" * 32: state})
        self.assertEqual(read_sync_state(planned["book"])["states"]["e" * 32], state)
        for key, value in (("block_count", 0), ("ordinal", -1), ("ordinal", True), ("block_count", "2")):
            invalid = copy.deepcopy(state)
            invalid["source_bindings"][0][key] = value
            with self.assertRaises(ValueError):
                plan_sync_state(book_fixture(), {"e" * 32: invalid})

    def test_three_way_preserves_repeated_source_and_conflicts_on_new_source(self):
        first = three_way_fields({"iol": ""}, {"iol": "A"})
        self.assertEqual(first["updates"], {"iol": "A"})
        repeated = three_way_fields({"iol": "MANUAL"}, {"iol": "A"}, first["baseline"])
        self.assertEqual(repeated["updates"], {})
        self.assertEqual(repeated["preserved"], ["iol"])
        changed = three_way_fields({"iol": "MANUAL"}, {"iol": "B"}, repeated["baseline"])
        self.assertIn("iol", changed["conflicts"])
        safe = three_way_fields({"iol": "A"}, {"iol": "B"}, first["baseline"])
        self.assertEqual(safe["updates"], {"iol": "B"})

    def test_manual_app_override_keeps_observed_source_distinct_from_written_value(self):
        state = {"fields": {"iol": field_hash("iol", "MANUAL")},
                 "source_fields": {"iol": field_hash("iol", "SOAP-OLD")}}
        repeat = three_way_fields({"iol": "MANUAL"}, {"iol": "SOAP-OLD"}, state)
        self.assertEqual(repeat["updates"], {})
        self.assertEqual(repeat["preserved"], ["iol"])
        change = three_way_fields({"iol": "MANUAL"}, {"iol": "SOAP-NEW"}, state)
        self.assertEqual(change["updates"], {"iol": "SOAP-NEW"})
        unknown_source = three_way_fields({"iol": "MANUAL"}, {"iol": "SOAP-OLD"}, {"fields": state["fields"]})
        self.assertIn("iol", unknown_source["conflicts"])

    def test_no_baseline_no_clear_and_canonical_mrn_adoption(self):
        result = three_way_fields({"iol": "MANUAL", "name": "名字", "mrn": "1"}, {"iol": "A", "name": "", "mrn": "0000001"})
        self.assertEqual(result["updates"], {})
        self.assertIn("iol", result["conflicts"])
        self.assertNotIn("name", result["baseline"]["fields"])
        self.assertIn("mrn", result["unchanged"])

    def test_route_has_separate_manual_move_authority(self):
        old = {"date": "2026-09-30", "hospital": "高榮", "block_sync_id": "a" * 32}
        new = {"date": "2026-10-09", "hospital": "聯醫", "block_sync_id": "b" * 32}
        desired = {"date": old["date"], "hospital": old["hospital"]}
        baseline = route_merge(old, desired)["baseline"]
        self.assertEqual(route_merge(new, desired, baseline)["action"], "preserve")
        self.assertEqual(route_merge(new, {"date": "2026-11-02", "hospital": "高榮"}, baseline)["action"], "pending")
        self.assertEqual(route_merge(old, {"date": new["date"], "hospital": new["hospital"]}, baseline)["action"], "move")
        self.assertEqual(route_merge(new, desired, baseline, explicit=True)["action"], "move")
        self.assertEqual(route_merge(old, new, {"route": old})["action"], "pending")

    def test_shared_state_roundtrip_and_narrow_upserts(self):
        client = setup_book()
        uid = uid_at(client.book["sheets"][0], 5)
        state = three_way_fields({}, {"iol": "SN60WF"})["baseline"]
        state.update(source_version=digest("synthetic version"), source_records=["synthetic-record"], kind="surgery")
        plan = plan_sync_state(client.read(), {uid: state})
        client.write(plan["requests"])
        self.assertEqual(read_sync_state(client.read())["states"], {uid: state})
        self.assertTrue(next(s for s in client.book["sheets"] if s["properties"]["title"] == SYNC_STATE_TITLE)["properties"]["hidden"])
        self.assertFalse(plan_sync_state(client.read(), {uid: state})["requests"])
        with self.assertRaisesRegex(ValueError, "不支援"):
            plan_sync_state(client.read(), {uid: {"soap": "raw SOAP must never be saved here"}})

    def test_corrupt_or_duplicate_state_never_overwrites_baseline(self):
        client = setup_book()
        uid = uid_at(client.book["sheets"][0], 5)
        plan = plan_sync_state(client.read(), {uid: {"fields": {"iol": field_hash("iol", "A")}}})
        client.write(plan["requests"])
        sheet = next(s for s in client.book["sheets"] if s["properties"]["title"] == SYNC_STATE_TITLE)
        sheet["rows"].append(copy.deepcopy(sheet["rows"][1]))
        read = read_sync_state(client.read())
        self.assertEqual(read["states"], {})
        self.assertTrue(read["issues"])
        self.assertFalse(plan_sync_state(client.read(), {uid: {}})["requests"])


class FUDraftTests(unittest.TestCase):
    def test_minimal_draft_repeat_preserves_all_manual_details(self):
        client = setup_book()
        draft = {"id": "fu-1", "fields": {"mrn": "0000001", "name": "測試患者", "tag": "FU", "hospital": "高榮"}}
        planner = FUPlanner(client.read(), client.key, [draft, copy.deepcopy(draft)])
        preview = planner.build()
        self.assertEqual(len(preview["items"]), 1)
        client.write(preview["requests"])
        sheet = next(s for s in client.book["sheets"] if s["properties"]["title"] == "FU")
        cols = header_columns(sheet)
        for label in ("TEL", "Condition", "日期", "Plan", "心得", EVENT_ID_HEADER):
            self.assertEqual(cell_text(sheet["rows"][1][cols[label]]), "")
        sheet["rows"][1][cols["Condition"]] = literal("manual details")
        sheet["rows"][1][cols["日期"]] = literal("2026/11/1")
        original = copy.deepcopy(sheet["rows"][1])
        draft["fields"]["name"] = "later SOAP name"
        preview = FUPlanner(client.read(), client.key, [draft]).build()
        client.write(preview["requests"])
        self.assertEqual(sheet["rows"][1], original)
        self.assertEqual(next(s for s in client.book["sheets"] if s["properties"]["title"] == "FU")["rows"][1], original)

    def test_duplicate_mrn_tag_is_pending_and_other_tags_are_independent(self):
        client = setup_book()
        draft = {"id": "fu-1", "fields": {"mrn": "0000001", "name": "測試", "tag": "FU"}}
        first = FUPlanner(client.read(), client.key, [draft]).build()
        client.write(first["requests"])
        sheet = next(s for s in client.book["sheets"] if s["properties"]["title"] == "FU")
        cols = header_columns(sheet)
        row = copy.deepcopy(sheet["rows"][1])
        row[cols[SYNC_ID_HEADER]] = literal("d" * 32)
        sheet["rows"].append(row)
        preview = FUPlanner(client.read(), client.key, [draft]).build()
        self.assertFalse(preview["can_apply"])
        self.assertEqual(preview["items"][0]["status"], "conflict")
        draft["fields"]["tag"] = "Retina"
        preview = FUPlanner(client.read(), client.key, [draft]).build()
        self.assertTrue(preview["can_apply"])


class StrictCandidateTests(unittest.TestCase):
    def test_structured_only_and_lineage_survives_single_block_edit(self):
        record = soap_record()
        original = candidates([record], structured_only=True, strict=True)[0]
        self.assertTrue(original["automatic_ready"])
        record["soap_structure"]["assessment_plan"] = record["soap_structure"]["assessment_plan"].replace("SN60WF", "NEW-LENS")
        changed = candidates([record], structured_only=True, strict=True)[0]
        self.assertEqual(original["sources"][0]["source_key"], changed["sources"][0]["source_key"])
        self.assertNotEqual(original["sources"][0]["block_hash"], changed["sources"][0]["block_hash"])
        record.pop("soap_structure")
        self.assertEqual(candidates([record], structured_only=True, strict=True), [])

    def test_multiple_sides_procedures_cancel_and_duplicate_blocks_are_pending(self):
        for header in ("# Arrange CATA OD OS (IOL A) on 20261030", "# Arrange CATA OD + VT OS on 20261030",
                       "# Arrange CATA OD on 20261030 cancelled", "# Arrange CATA OD/OS on 20261030"):
            record = soap_record()
            record["soap_structure"]["assessment_plan"] = header
            self.assertFalse(candidates([record], structured_only=True, strict=True)[0]["automatic_ready"])
        record = soap_record()
        text = record["soap_structure"]["assessment_plan"]
        record["soap_structure"]["assessment_plan"] = text + "\n\n" + text
        self.assertFalse(candidates([record], structured_only=True, strict=True)[0]["automatic_ready"])


if __name__ == "__main__":
    unittest.main()
