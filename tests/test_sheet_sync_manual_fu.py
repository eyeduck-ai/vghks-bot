"""Atomic mixed manual previews with synthetic FU drafts and monthly events."""
import copy
import unittest
from types import SimpleNamespace

from analysis_fixtures import MemorySheets, book_fixture, literal, soap_record

from vghks_bot.analysis_store import digest
from vghks_bot.google_sheets import SheetError
from vghks_bot.settings import Settings
from vghks_bot.sheet_identity import SYNC_ID_HEADER, header_columns, plan_identity_setup
from vghks_bot.sheet_plan import FU_HEADERS, cell_text, fingerprint, verify
from vghks_bot.sheet_sync_core import SheetSyncCore
from vghks_bot.sheet_sync_state import field_hash, plan_sync_state, read_sync_state
from vghks_bot.surgery_candidates import candidates


def fu_proposal(mrn="0000003", record_id="synthetic-encounter"):
    version = digest([record_id, "synthetic version"])
    return {"id": digest(["fu", mrn])[:32], "kind": "fu",
            "fields": {"mrn": mrn, "name": "合成追蹤患者", "hospital": "高榮", "tag": "FU"},
            "source_records": [record_id], "source_versions": [{"record_id": record_id, "version": version}],
            "source_bindings": [{"record_id": record_id, "field": "soap", "source_version": version,
                                 "excerpt": "# FU synthetic text must not reach cloud state"}]}


def surgery_proposal(mrn="0000003"):
    record = soap_record(mrn)
    source = candidates([record], structured_only=True)[0]
    return {"id": source["id"], "fields": source["fields"], "source_fields": source["fields"],
            "source_records": [record["id"]], "source_bindings": source["sources"],
            "source_versions": [{"record_id": record["id"], "version": source["sources"][0]["source_version"]}]}


def fu_sheet(book):
    return next(sheet for sheet in book["sheets"] if sheet["properties"]["title"] in {"FU", "All"})


class Documents:
    def __init__(self):
        self.values = {}

    def save_document(self, table, value):
        self.values[table, value["id"]] = copy.deepcopy(value)

    def documents(self, table):
        return [copy.deepcopy(value) for (name, _), value in self.values.items() if name == table]


class ManualFUTests(unittest.TestCase):
    def setUp(self):
        self.core = SheetSyncCore(None)
        self.client = MemorySheets()

    def apply(self, proposals):
        preview = self.core.build_manual(self.client.read(), self.client.key, proposals)
        self.assertTrue(preview["can_apply"], preview["items"])
        self.assertEqual(sum("createDeveloperMetadata" in request for request in preview["requests"]), 1)
        self.client.write(preview["requests"])
        self.assertEqual(verify(self.client.read(), preview), "verified")
        self.assertEqual({uid: read_sync_state(self.client.read())["states"][uid] for uid in preview["sync_states"]}, preview["sync_states"])
        return preview

    def automatic(self):
        record = soap_record("0000003")
        record["soap"] = "# FU synthetic followup"
        record["soap_structure"]["assessment_plan"] = record["soap"]
        core = SheetSyncCore(SimpleNamespace(store=Documents(), app=SimpleNamespace(settings=Settings())))
        return core.automatic(self.client, [record], "synthetic-cohort", SimpleNamespace(check_cancel=lambda: None))

    def test_fu_only_creates_four_field_draft_and_hash_only_source_state(self):
        proposal = fu_proposal()
        preview = self.apply([proposal])
        sheet = fu_sheet(self.client.book)
        cols, row = header_columns(sheet), sheet["rows"][1]
        self.assertEqual([cell_text(row[cols[label]]) for label in ("病歷號", "姓名", "醫院", "Tag")],
                         ["0000003", "合成追蹤患者", "高榮", "FU"])
        for label in ("TEL", "Condition", "日期", "Plan", "心得", "CalendarEventId"):
            self.assertFalse(cell_text(row[cols[label]]))
        state = preview["sync_states"][preview["items"][0]["sync_id"]]
        self.assertEqual(state["kind"], "fu")
        self.assertEqual(state["source_bindings"][0]["source_version"], proposal["source_versions"][0]["version"])
        self.assertNotIn("# FU", repr(state))
        self.assertEqual(preview["source_versions"], proposal["source_versions"])

    def test_mixed_fu_and_surgery_share_one_atomic_metadata_and_source_versions(self):
        fu, surgery = fu_proposal(), surgery_proposal()
        fu["source_versions"].extend(copy.deepcopy(surgery["source_versions"]))
        preview = self.apply([fu, surgery])
        self.assertEqual([item["id"] for item in preview["items"]], [fu["id"], surgery["id"]])
        self.assertEqual([item["kind"] for item in preview["items"]], ["fu", "surgery"])
        self.assertEqual(len(preview["expectations"]), 2)
        self.assertEqual({state["kind"] for state in preview["sync_states"].values()}, {"fu", "surgery"})
        self.assertEqual(len(preview["source_versions"]), 2)
        before = fingerprint(self.client.read())
        with self.assertRaises(SheetError):
            self.client.write(preview["requests"])
        self.assertEqual(fingerprint(self.client.read()), before)

    def test_same_mrn_in_same_batch_only_adds_one_fu_draft(self):
        first, second = fu_proposal(), fu_proposal("3", "other-encounter")
        preview = self.apply([first, second])
        self.assertEqual(len(fu_sheet(self.client.book)["rows"]), 2)
        self.assertEqual(preview["items"][0]["sync_id"], preview["items"][1]["sync_id"])
        self.assertEqual(preview["items"][1]["action"], "keep")

    def test_bound_fu_with_changed_tag_keeps_all_manual_cells_and_same_uid(self):
        first = self.apply([fu_proposal()])
        sheet = fu_sheet(self.client.book)
        cols = header_columns(sheet)
        sheet["rows"][1][cols["Tag"]] = literal("人工分類")
        sheet["rows"][1][cols["Plan"]] = literal("人工計畫")
        sheet["rows"][1][cols["日期"]] = literal("2026/11/1")
        original = copy.deepcopy(sheet["rows"][1])
        preview = self.apply([fu_proposal()])
        self.assertEqual(fu_sheet(self.client.book)["rows"][1], original)
        self.assertEqual(preview["items"][0]["sync_id"], first["items"][0]["sync_id"])
        self.assertIn("永久 FU", preview["items"][0]["reason"])
        self.assertEqual([next(iter(request)) for request in preview["requests"]], ["createDeveloperMetadata"])
        self.assertEqual(len(preview["expectations"]), 1)

    def test_bound_fu_patient_change_is_conflict_in_manual_and_pending_in_automatic(self):
        self.apply([fu_proposal()])
        sheet = fu_sheet(self.client.book)
        sheet["rows"][1][header_columns(sheet)["病歷號"]] = literal("0000009")
        before, writes = fingerprint(self.client.read()), self.client.writes
        preview = self.core.build_manual(self.client.read(), self.client.key, [fu_proposal()])
        self.assertFalse(preview["can_apply"])
        self.assertIn("病人不符", preview["items"][0]["reason"])
        result = self.automatic()
        self.assertEqual(result["updated"], 0)
        self.assertTrue(any("病人不符" in item["reason"] for item in result["pending"]), result)
        self.assertEqual(self.client.writes, writes)
        self.assertEqual(fingerprint(self.client.read()), before)

    def test_bound_fu_converted_to_header_is_conflict_and_not_a_new_patient(self):
        self.apply([fu_proposal()])
        sheet = fu_sheet(self.client.book)
        sheet["rows"][1][header_columns(sheet)["病歷號"]] = literal("◆ 刀日")
        preview = self.core.build_manual(self.client.read(), self.client.key, [fu_proposal()])
        self.assertFalse(preview["can_apply"])
        self.assertIn("刀日標題", preview["items"][0]["reason"])
        result = self.automatic()
        self.assertEqual(result["updated"], 0)
        self.assertTrue(any("刀日標題" in item["reason"] for item in result["pending"]), result)
        self.assertEqual(len(sheet["rows"]), 2)

    def test_bound_fu_renamed_identity_header_cannot_masquerade_as_deleted(self):
        self.apply([fu_proposal()])
        sheet = fu_sheet(self.client.book)
        sheet["rows"][0][header_columns(sheet)[SYNC_ID_HEADER]] = literal("人工重新命名 ID")
        preview = self.core.build_manual(self.client.read(), self.client.key, [fu_proposal()])
        self.assertFalse(preview["can_apply"])
        self.assertIn("欄位已變更", preview["items"][0]["reason"])
        result = self.automatic()
        self.assertEqual(result["updated"], 0)
        self.assertTrue(any("欄位已變更" in item["reason"] for item in result["pending"]), result)
        self.assertEqual(len(sheet["rows"]), 2)

    def test_multiple_permanent_fu_states_are_manual_conflict_and_automatic_pending(self):
        self.apply([fu_proposal()])
        self.client.book = plan_sync_state(self.client.book, {
            "e" * 32: {"kind": "fu", "mrn_hash": field_hash("mrn", "0000003")}})["book"]
        preview = self.core.build_manual(self.client.read(), self.client.key, [fu_proposal()])
        self.assertFalse(preview["can_apply"])
        self.assertIn("多個永久 FU", preview["items"][0]["reason"])
        result = self.automatic()
        self.assertEqual(result["updated"], 0)
        self.assertTrue(any("多個永久 FU" in item["reason"] for item in result["pending"]), result)
        self.assertEqual(len(fu_sheet(self.client.book)["rows"]), 2)

    def test_bound_fu_noop_readback_detects_patient_replaced_after_write(self):
        self.apply([fu_proposal()])
        preview = self.core.build_manual(self.client.read(), self.client.key, [fu_proposal()])
        self.assertEqual(len(preview["expectations"]), 1)
        self.client.write(preview["requests"])
        sheet = fu_sheet(self.client.book)
        sheet["rows"][1][header_columns(sheet)["病歷號"]] = literal("0000009")
        self.assertNotEqual(verify(self.client.read(), preview), "verified")

    def test_deleted_bound_fu_is_noop_even_when_mixed_with_new_surgery(self):
        first = self.apply([fu_proposal()])
        uid = first["items"][0]["sync_id"]
        fu_sheet(self.client.book)["rows"][1] = []
        preview = self.apply([surgery_proposal(), fu_proposal()])
        self.assertEqual(preview["items"][1]["action"], "keep")
        self.assertEqual(preview["items"][1]["sync_id"], uid)
        self.assertEqual(fu_sheet(self.client.book)["rows"][1], [])
        self.assertEqual(read_sync_state(self.client.book)["states"][uid]["kind"], "fu")
        self.assertEqual(len(preview["expectations"]), 1)  # Only the monthly insertion remains.
        self.assertFalse(self.automatic()["pending"])

    def test_archived_bound_fu_never_creates_replacement(self):
        first = self.apply([fu_proposal()])
        uid = first["items"][0]["sync_id"]
        sheet = fu_sheet(self.client.book)
        archive = {"properties": {"sheetId": 900, "title": "刀表封存_2026", "gridProperties": {
            "rowCount": 100, "columnCount": len(FU_HEADERS) + 1}}, "merges": [],
            "rows": [[], [], [], [], [], [*copy.deepcopy(sheet["rows"][0]), literal("ArchiveRowType")],
                     [*copy.deepcopy(sheet["rows"][1]), literal("PATIENT")]]}
        self.client.book["sheets"].append(archive)
        sheet["rows"][1] = []
        preview = self.apply([fu_proposal()])
        self.assertEqual(preview["items"][0]["sync_id"], uid)
        self.assertEqual(preview["items"][0]["position"], "刀表封存_2026!7")
        self.assertEqual(fu_sheet(self.client.book)["rows"][1], [])
        self.assertEqual(preview["expectations"][0]["sheet"], "刀表封存_2026")
        self.assertFalse(self.automatic()["pending"])
        archive = next(sheet for sheet in self.client.book["sheets"] if sheet["properties"]["title"] == "刀表封存_2026")
        archive["rows"][6][header_columns(archive, 5)["病歷號"]] = literal("0000009")
        preview = self.core.build_manual(self.client.read(), self.client.key, [fu_proposal()])
        self.assertFalse(preview["can_apply"])
        self.assertIn("病人不符", preview["items"][0]["reason"])

    def test_existing_unbound_fu_is_adopted_without_overwriting_manual_fields(self):
        headers = [literal(label) for label in FU_HEADERS[:-1]]
        row = [literal(value) for value in ("3", "人工姓名", "0900-000-000", "聯醫", "FU", "人工條件", "2026/12/1", "人工計畫", "人工心得", "existing-event")]
        book = {"properties": {"timeZone": "Asia/Taipei"}, "developerMetadata": [], "sheets": [{
            "properties": {"sheetId": 12, "title": "All", "gridProperties": {"rowCount": 100, "columnCount": 10}},
            "rows": [headers, row], "merges": []}]}
        self.client = MemorySheets(book)
        preview = self.apply([fu_proposal()])
        self.assertEqual(fu_sheet(self.client.book)["rows"][1][:10], row)
        self.assertEqual(preview["items"][0]["action"], "keep")
        self.assertEqual(len(preview["sync_states"]), 1)

    def test_conflicting_fu_rows_disable_entire_mixed_apply(self):
        self.apply([fu_proposal()])
        sheet = fu_sheet(self.client.book)
        cols = header_columns(sheet)
        duplicate = copy.deepcopy(sheet["rows"][1])
        duplicate[cols[SYNC_ID_HEADER]] = literal("a" * 32)
        sheet["rows"].append(duplicate)
        # Remove the FU binding to exercise the visible MRN+Tag ambiguity path.
        states = read_sync_state(self.client.book)
        state_sheet = next(s for s in self.client.book["sheets"] if s["properties"]["sheetId"] == states["sheet_id"])
        state_sheet["rows"] = state_sheet["rows"][:1]
        before = fingerprint(self.client.read())
        preview = self.core.build_manual(self.client.read(), self.client.key, [surgery_proposal(), fu_proposal()])
        self.assertFalse(preview["can_apply"])
        self.assertEqual(preview["items"][1]["status"], "conflict")
        self.assertEqual(fingerprint(self.client.read()), before)

    def test_fu_uid_bound_to_wrong_patient_is_rejected_before_any_mutation(self):
        book = book_fixture()
        fu = {"properties": {"sheetId": 12, "title": "FU", "gridProperties": {"rowCount": 100, "columnCount": len(FU_HEADERS)}},
              "rows": [[literal(label) for label in FU_HEADERS], [literal("0000003"), literal("合成患者"), {}, literal("高榮"), literal("FU")]], "merges": []}
        book["sheets"].append(fu)
        setup = plan_identity_setup(book)
        uid = cell_text(fu_sheet(setup["book"])["rows"][1][header_columns(fu_sheet(setup["book"]))[SYNC_ID_HEADER]])
        planned = plan_sync_state(setup["book"], {uid: {"kind": "fu", "mrn_hash": field_hash("mrn", "0000009")}})
        before = fingerprint(planned["book"])
        with self.assertRaisesRegex(ValueError, "病人不符"):
            self.core.build_manual(planned["book"], self.client.key, [fu_proposal()])
        self.assertEqual(fingerprint(planned["book"]), before)


if __name__ == "__main__":
    unittest.main()
