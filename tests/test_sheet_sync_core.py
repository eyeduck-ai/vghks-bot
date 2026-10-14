"""Integration checks for fresh-source bindings, manual edits and interrupted writes."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from analysis_fixtures import MemorySheets, book_fixture, literal, soap_record

from vghks_bot.analysis import Analysis
from vghks_bot.analysis_store import digest
from vghks_bot.bot import BotApplication
from vghks_bot.google_sheets import SheetUnavailable
from vghks_bot.selftest_bot import BotSyntheticSDK
from vghks_bot.settings import Settings, today
from vghks_bot.sheet_identity import header_columns
from vghks_bot.sheet_plan import cell_text, entries
from vghks_bot.sheet_sync_core import SheetSyncCore
from vghks_bot.sheet_sync_state import read_sync_state


class Documents:
    def __init__(self):
        self.values = {}

    def save_document(self, table, value):
        self.values[table, value["id"]] = copy.deepcopy(value)

    def documents(self, table):
        return [copy.deepcopy(v) for (t, _), v in self.values.items() if t == table]


class SyncCoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Documents()
        self.core = SheetSyncCore(SimpleNamespace(store=self.store, app=SimpleNamespace(settings=Settings())))
        book = book_fixture()
        names = ["病歷號", "姓名", "TEL", "醫院", "Tag", "Condition", "日期", "Plan", "心得", "CalendarEventId"]
        book["sheets"].append({"properties": {"sheetId": 102, "title": "FU",
                               "gridProperties": {"rowCount": 100, "columnCount": 10}},
                               "rows": [[literal(n) for n in names]], "merges": []})
        self.client = MemorySheets(book)
        self.record = soap_record("0000003")
        self.scan = SimpleNamespace(check_cancel=lambda: None)

    def run_sync(self, records=None):
        return self.core.automatic(self.client, records or [self.record], "cohort", self.scan)

    def row(self):
        return next(e for sheet in self.client.book["sheets"] if sheet["properties"]["title"].isdigit()
                    for e in entries(sheet)
                    if e["kind"] == "patient" and e["fields"]["mrn"] == "0000003")

    def test_manual_route_wins_while_iol_changes_and_id_binding_survives(self):
        result = self.run_sync()
        self.assertEqual(result["updated"], 1, result)
        entry = self.row()
        uid = entry["sync_id"]
        day = self.client.book["sheets"][0]["rows"][entry["day_index"]]
        day[0] = literal("2026/10/12 Mon")
        day[1] = literal("聯醫")
        entry["row"][0] = literal("09:30")
        entry["row"][22] = literal("stable-calendar-id")
        self.record["soap"] = self.record["soap"].replace("SN60WF", "DIB00")
        self.record["soap_structure"]["assessment_plan"] = self.record["soap"]
        result = self.run_sync()
        self.assertEqual(self.row()["fields"]["iol"], "DIB00", result)
        self.assertEqual(self.row()["route"]["date"], "2026-10-12")
        self.assertEqual(self.row()["route"]["hospital"], "聯醫")
        self.assertEqual(self.row()["sync_id"], uid)
        self.assertEqual(cell_text(self.row()["row"][0]), "09:30")
        self.assertEqual(cell_text(self.row()["row"][22]), "stable-calendar-id")
        self.record["soap"] = self.record["soap"].replace("20260930", "20261019")
        self.record["soap_structure"]["assessment_plan"] = self.record["soap"]
        result = self.run_sync()
        self.assertEqual(self.row()["route"]["date"], "2026-10-12", result)
        self.assertTrue(any("人工位置" in p["reason"] for p in result["pending"]), result)

    def test_manual_field_is_preserved_and_deleted_binding_does_not_recreate(self):
        self.run_sync()
        entry = self.row()
        entry["row"][10] = literal("MANUAL-LENS")
        self.record["soap"] = self.record["soap"].replace("SN60WF", "DIB00")
        self.record["soap_structure"]["assessment_plan"] = self.record["soap"]
        result = self.run_sync()
        self.assertEqual(self.row()["fields"]["iol"], "MANUAL-LENS")
        self.assertTrue(result["pending"])
        self.client.book["sheets"][0]["rows"][self.row()["index"]] = []
        result = self.run_sync()
        self.assertEqual(result["updated"], 0)
        self.assertTrue(any("已綁定" in p["reason"] for p in result["pending"]))

    def test_fu_uses_existing_literal_tag_and_keeps_permanent_binding(self):
        record = soap_record("0000004")
        record["soap"] = "# FUNDUS literal matches existing FU tag"
        record["soap_structure"]["assessment_plan"] = record["soap"]
        result = self.run_sync([record])
        self.assertEqual(result["updated"], 1, result)
        sheet = next(s for s in self.client.book["sheets"] if s["properties"]["title"] == "FU")
        cols = header_columns(sheet)
        row = sheet["rows"][1]
        self.assertEqual(cell_text(row[cols["Tag"]]), "FU")
        for label in ("TEL", "Condition", "日期", "Plan", "心得"):
            self.assertFalse(cell_text(row[cols[label]]))
        row[cols["Tag"]] = literal("人工分類")
        row[cols["Plan"]] = literal("人工補填")
        self.assertEqual(self.run_sync([record])["updated"], 0)
        self.assertEqual(len(sheet["rows"]), 2)
        self.assertEqual(cell_text(row[cols["Plan"]]), "人工補填")
        sheet["rows"][1] = []
        self.assertEqual(self.run_sync([record])["updated"], 0)
        self.assertEqual(len(sheet["rows"]), 2)

    def test_timeout_after_commit_does_not_create_duplicate(self):
        self.client.timeout_after_commit = True
        result = self.run_sync()
        self.assertTrue(result["pending"])
        self.client.timeout_after_commit = False
        writes = self.client.writes
        result = self.run_sync()
        self.assertEqual(self.client.writes, writes)
        self.assertTrue(result["pending"])
        self.assertEqual(len([e for e in entries(self.client.book["sheets"][0])
                              if e["kind"] == "patient" and e["fields"]["mrn"] == "0000003"]), 1)

    def test_wrong_patient_uid_is_pending_and_shared_state_contains_no_soap(self):
        self.run_sync()
        shared = read_sync_state(self.client.book)["states"]
        self.assertTrue(shared)
        self.assertNotIn("# Arrange", repr(shared))
        self.assertNotIn("SN60WF", repr(shared))
        self.row()["row"][2] = literal("0000009")
        result = self.run_sync()
        self.assertEqual(result["updated"], 0)
        self.assertTrue(any("病人不符" in p["reason"] for p in result["pending"]))

    def test_manual_preview_override_records_original_soap_separately(self):
        from vghks_bot.surgery_candidates import candidates

        source = candidates([self.record], structured_only=True)[0]
        fields = {**source["fields"], "date": "2026-10-12", "iol": "MANUAL-LENS"}
        proposal = {"id": source["id"], "fields": fields, "source_fields": source["fields"],
                    "source_records": [self.record["id"]], "source_bindings": source["sources"],
                    "new_event": True, "destination": {"date": "2026-10-12", "hospital": "聯醫"}}
        preview = self.core.build_manual(self.client.read(), self.client.key, [proposal])
        self.client.write(preview["requests"])
        result = self.run_sync()
        self.assertEqual(self.row()["fields"]["iol"], "MANUAL-LENS", result)
        self.assertEqual(self.row()["route"]["date"], "2026-10-12")
        self.assertEqual(self.row()["route"]["hospital"], "聯醫")

    def test_multiple_intermediate_sources_quarantine_old_single_binding(self):
        self.run_sync()
        original = self.record["soap"]
        self.record["soap"] += "\n\n# Arrange CATA OD (IOL DIB00 T-0.50) on 20261012"
        self.record["soap_structure"]["assessment_plan"] = self.record["soap"]
        self.assertTrue(self.run_sync()["pending"])
        self.record["soap"] = original.replace("20260930", "20261012")
        self.record["soap_structure"]["assessment_plan"] = self.record["soap"]
        result = self.run_sync()
        self.assertTrue(any("多段" in p["reason"] for p in result["pending"]), result)
        self.assertEqual(self.row()["route"]["date"], "2026-09-30")

    def test_unchanged_hourly_scan_does_not_write_another_batch(self):
        self.run_sync()
        self.run_sync()  # normalizes the source route to the newly assigned block
        writes = self.client.writes
        self.run_sync()
        self.assertEqual(self.client.writes, writes)

    def test_new_encounter_different_date_is_not_implicitly_rescheduling_an_old_event(self):
        self.run_sync()
        self.record = soap_record("0000003", day="2026-10-10", scheduled="20261019")
        result = self.run_sync()
        self.assertEqual(self.row()["route"]["date"], "2026-09-30", result)
        self.assertTrue(any("不同就診來源" in p["reason"] for p in result["pending"]), result)

    def test_manual_draft_does_not_invent_an_observed_source_date(self):
        from vghks_bot.surgery_candidates import candidates

        fields = {**candidates([self.record])[0]["fields"], "date": "2026-10-12"}
        proposal = {"id": "manual", "fields": fields, "source_fields": {"mrn": fields["mrn"], "name": fields["name"], "date": ""},
                    "source_records": [], "new_event": True,
                    "destination": {"date": "2026-10-12", "hospital": "高榮"}}
        preview = self.core.build_manual(self.client.read(), self.client.key, [proposal])
        self.client.write(preview["requests"])
        result = self.run_sync()
        self.assertEqual(self.row()["route"]["date"], "2026-10-12", result)
        self.assertTrue(any("人工位置" in p["reason"] for p in result["pending"]), result)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        BotSyntheticSDK.calls = []
        self.app = BotApplication(Settings(), Path(self.temp.name), BotSyntheticSDK)
        key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(key)
        self.work.analysis.google.public = Mock(return_value={"configured": True})
        self.work.analysis.automatic_sheet_sync = Mock(return_value={"updated": 0, "pending": []})

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_each_run_fetches_own_today_list_and_fresh_soap_and_offline_does_not_queue(self):
        for _ in range(2):
            self.work.sheet_sync.run()
            self.assertTrue(self.work.idle.wait(10))
        history = self.work.sheet_sync.status()["history"]
        self.assertEqual(len(history), 2)
        self.assertEqual(self.work.analysis.automatic_sheet_sync.call_count, 2)
        self.assertTrue(all(r["date"] == today().isoformat()
                            for call in self.work.analysis.automatic_sheet_sync.call_args_list
                            for r in call.args[0]))
        self.work.sheet_sync.settings({"enabled": True, "interval_minutes": 60})
        self.work.gateway.online = False
        self.work.sheet_sync.schedule()
        self.assertFalse(self.work.sheet_sync.status()["running"])

    def test_google_outage_retains_sources_and_explicit_retry_does_not_need_hospital_login(self):
        from vghks_bot.sheet_sync import SheetSync

        self.work.analysis.automatic_sheet_sync.side_effect = SheetUnavailable("Google synthetic outage")
        self.work.sheet_sync.run()
        self.assertTrue(self.work.idle.wait(10))
        status = SheetSync(self.work).status()
        self.assertTrue(status["last_result"]["deferred"])
        self.assertEqual(len(status["deferred"]), 1)
        saved = status["deferred"][0]
        self.assertTrue(self.work.analysis.store.document("analysis_cohorts", saved["cohort_id"])["members"])
        self.work.gateway.online = False
        self.work.analysis.automatic_sheet_sync.side_effect = None
        result = self.work.sheet_sync.retry({"id": saved["id"]})
        self.assertFalse(result.get("deferred"))
        self.assertFalse(self.work.sheet_sync.status()["deferred"])
        count = self.work.analysis.automatic_sheet_sync.call_count
        self.work.sheet_sync.retry({"id": saved["id"]})
        self.assertEqual(self.work.analysis.automatic_sheet_sync.call_count, count)

    def test_offline_saved_batch_rejects_changed_source_and_changed_spreadsheet(self):
        self.work.analysis.google.public.return_value = {"configured": True, "spreadsheet_id": "synthetic-original"}
        self.work.analysis.automatic_sheet_sync.side_effect = SheetUnavailable("synthetic outage")
        self.work.sheet_sync.run()
        self.assertTrue(self.work.idle.wait(10))
        saved = self.work.sheet_sync.status()["deferred"][0]
        self.work.analysis.google.public.return_value = {"configured": True, "spreadsheet_id": "synthetic-other"}
        with self.assertRaisesRegex(ValueError, "設定已改變"):
            self.work.sheet_sync.retry({"id": saved["id"]})
        self.work.analysis.google.public.return_value = {"configured": True, "spreadsheet_id": "synthetic-original"}
        cohort = self.work.analysis.store.document("analysis_cohorts", saved["cohort_id"])
        record = next(s["record"] for m in cohort["members"] for s in m["handoff_sources"])
        changed = {**record, "soap": record["soap"] + "\nChanged synthetic SOAP"}
        self.work.store.library.save_record(changed, "TEST", "synthetic")
        with self.assertRaisesRegex(ValueError, "版本已變更"):
            self.work.sheet_sync.retry({"id": saved["id"]})
        self.assertEqual(self.work.analysis.automatic_sheet_sync.call_count, 1)

    def test_retry_settings_race_never_retargets_saved_sources_and_recovers_on_original_sheet(self):
        self.work.analysis.google.public.return_value = {"configured": True, "spreadsheet_id": "synthetic-original"}
        self.work.analysis.automatic_sheet_sync.side_effect = SheetUnavailable("synthetic outage")
        self.work.sheet_sync.run()
        self.assertTrue(self.work.idle.wait(10))
        saved = self.work.review.db.all("sheet_sync_deferred")[0]
        # Use the real lock/client boundary, but only an in-memory client and
        # synthetic core. No service-account material or network is accessed.
        self.work.analysis.automatic_sheet_sync = Analysis.automatic_sheet_sync.__get__(self.work.analysis)
        client = MemorySheets(book_fixture())
        client.key = "synthetic-other"
        client.read = Mock(side_effect=AssertionError("wrong sheet must not be read"))

        def changed_client():
            self.assertTrue(self.work.analysis.sheet_busy)
            self.assertTrue(self.app.sheet_lock.locked())
            self.work.analysis.google.public.return_value = {"configured": True, "spreadsheet_id": client.key}
            return client

        self.work.analysis.client_factory = Mock(side_effect=changed_client)
        self.work.gateway.online = False
        with patch.object(SheetSyncCore, "automatic", return_value={"updated": 0, "pending": []}) as automatic:
            result = self.work.sheet_sync.retry({"id": saved["id"]})
            self.assertTrue(result["deferred"])
            automatic.assert_not_called()
            client.read.assert_not_called()
            self.assertEqual(client.writes, 0)
            blocked = self.work.review.db.get("sheet_sync_deferred", saved["id"])
            self.assertEqual(blocked["status"], "blocked")
            for field in ("spreadsheet_id", "cohort_id", "source_versions", "created_at"):
                self.assertEqual(blocked[field], saved[field], field)
            with self.assertRaisesRegex(ValueError, "設定已改變"):
                self.work.sheet_sync.retry({"id": saved["id"]})
            self.assertEqual(self.work.analysis.client_factory.call_count, 1)
            # Restoring the original destination permits a fresh re-plan of
            # the same intent, without hospital login or evidence replacement.
            client.key = "synthetic-original"
            self.work.analysis.google.public.return_value = {"configured": True, "spreadsheet_id": client.key}
            self.work.analysis.client_factory.side_effect = None
            self.work.analysis.client_factory.return_value = client
            result = self.work.sheet_sync.retry({"id": saved["id"]})
            self.assertFalse(result.get("deferred"))
            automatic.assert_called_once()
            completed = self.work.review.db.get("sheet_sync_deferred", saved["id"])
            self.assertEqual(completed["status"], "completed")
            for field in ("spreadsheet_id", "cohort_id", "source_versions", "created_at"):
                self.assertEqual(completed[field], saved[field], field)
            self.assertEqual(result["created_at"], saved["created_at"])

    def test_automatic_sheet_sync_without_expected_destination_keeps_existing_call_contract(self):
        client = MemorySheets(book_fixture())
        analysis = SimpleNamespace(sheet_operation=lambda callback: callback(client))
        state = SimpleNamespace(check_cancel=lambda: None)
        with patch.object(SheetSyncCore, "automatic", return_value={"updated": 0, "pending": []}) as automatic:
            result = Analysis.automatic_sheet_sync(analysis, [], "synthetic-cohort", state)
        self.assertEqual(result["updated"], 0)
        automatic.assert_called_once_with(client, [], "synthetic-cohort", state)

    def test_handoff_version_change_before_preview_is_rejected_and_draft_is_explicit(self):
        record = soap_record("0000003")
        self.work.store.library.save_record(record, "TEST", "synthetic")
        current = self.work.store.library.get_record(record["id"])
        cohort = {"id": "a" * 32, "name": "handoff", "members": [{"mrn": "0000003", "name": "test",
            "account_id": self.work.account_id, "handoff_sources": [{"record_id": record["id"],
                "version": digest([current["soap"], current.get("soap_structure")]), "record": current}]}]}
        self.work.analysis.store.save_document("analysis_cohorts", cohort)
        candidate = self.work.analysis.surgery_candidates({"cohort_id": cohort["id"]})["candidates"][0]
        record["soap"] += "\n- Plan: new version"
        self.work.store.library.save_record(record, "TEST", "synthetic")
        with self.assertRaisesRegex(ValueError, "版本"):
            self.work.analysis.proposals({"cohort_id": cohort["id"], "proposals": [{"id": candidate["id"]}]})
        draft = self.work.analysis.surgery_draft({"cohort_id": cohort["id"], "mrn": "0000003"})["candidate"]
        self.assertTrue(draft["manual"])
        self.assertEqual(draft["fields"]["mrn"], "0000003")
        with self.assertRaisesRegex(ValueError, "填妥"):
            self.work.analysis.proposals({"cohort_id": cohort["id"], "proposals": [{"id": draft["id"]}]})

    def test_manual_handoff_fu_requires_no_surgery_fields_and_keeps_fixed_minimal_values(self):
        record = soap_record("0000004")
        record["soap"] = "# FUNDUS literal existing followup tag\nSynthetic plan"
        record["soap_structure"]["assessment_plan"] = record["soap"]
        self.work.store.library.save_record(record, "TEST", "synthetic")
        current = self.work.store.library.get_record(record["id"])
        cohort = {"id": "b" * 32, "name": "FU handoff", "members": [{"mrn": record["mrn"],
            "name": "test", "account_id": self.work.account_id,
            "handoff_sources": [{"record_id": record["id"], "record": current}]}]}
        self.work.analysis.store.save_document("analysis_cohorts", cohort)
        rows = self.work.analysis.surgery_candidates({"cohort_id": cohort["id"]})["candidates"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "fu")
        proposals = self.work.analysis.proposals({"cohort_id": cohort["id"],
            "proposals": [{"id": rows[0]["id"], "fields": rows[0]["fields"]}]})
        self.assertEqual(proposals[0]["fields"], {"mrn": record["mrn"], "name": record["name"],
                                                "hospital": "高榮", "tag": "FU"})
        self.assertTrue(proposals[0]["source_bindings"][0]["source_version"])
        override = self.work.analysis.proposals({"cohort_id": cohort["id"],
            "proposals": [{"id": rows[0]["id"], "fields": {"hospital": "聯醫"}}]})
        self.assertEqual(override[0]["fields"]["hospital"], "聯醫")
        for updates in ({"date": "2026-10-12"}, {"tag": "manual"}):
            with self.assertRaisesRegex(ValueError, "FU 只新增"):
                self.work.analysis.proposals({"cohort_id": cohort["id"],
                    "proposals": [{"id": rows[0]["id"], "fields": updates}]})
        changed = {**record, "soap": record["soap"] + "\nchanged"}
        self.work.store.library.save_record(changed, "TEST", "synthetic")
        with self.assertRaisesRegex(ValueError, "版本"):
            self.work.analysis.proposals({"cohort_id": cohort["id"], "proposals": [{"id": rows[0]["id"]}]})
