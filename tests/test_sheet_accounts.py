"""Synthetic account credential copying and campus defaults; no Google calls."""
import copy
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from analysis_fixtures import MemorySheets, literal, soap_record
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from vghks_bot.bot import BotApplication
from vghks_bot.bot_server import BotServer
from vghks_bot.google_sheets import GoogleSettings, SheetUnavailable
from vghks_bot.selftest_bot import BotSyntheticSDK
from vghks_bot.settings import Settings
from vghks_bot.sheet_identity import header_columns
from vghks_bot.sheet_plan import cell_text, entries
from vghks_bot.sheet_service import account_hospital
from vghks_bot.storage import StorageError


class SheetAccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.key_data = {"type": "service_account", "token_uri": "https://oauth2.googleapis.com/token",
                        "client_email": "synthetic-source@example.iam.gserviceaccount.com",
                        "private_key": private.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.app = BotApplication(Settings(), self.path, BotSyntheticSDK)
        self.target_id = self.app.login({"username": "TARGET", "password": "synthetic", "campus": "聯醫"})["account"]["id"]
        self.source_id = self.app.login({"username": "SOURCE", "password": "synthetic", "campus": "高榮"})["account"]["id"]
        self.target, self.source = self.app.workspace(self.target_id), self.app.workspace(self.source_id)
        self.source.analysis.google.save({"spreadsheet_id": "synthetic-source-sheet-000000000", "key": self.key_data})

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def copy_settings(self):
        return self.target.analysis.handle("google/copy", {"source_account_id": self.source_id})

    def change_campus(self, campus):
        info = self.app.registry.account(self.target_id)
        self.app.registry.save(info["username"], info["label"], campus, "synthetic", False, self.target_id)

    def cohort(self, *, fu=False):
        record = soap_record("0000003")
        if fu:
            record["soap"] = "# FUNDUS synthetic followup"
            record["soap_structure"]["assessment_plan"] = record["soap"]
        self.target.store.library.save_record(record, "TARGET", "synthetic")
        cohort = {"id": "a" * 32, "name": "Synthetic", "members": [{"mrn": record["mrn"],
            "name": record["name"], "account_id": self.target_id}]}
        self.target.analysis.store.save_document("analysis_cohorts", cohort)
        return cohort, record

    def test_copy_reseals_credentials_and_keeps_accounts_independent(self):
        copied = self.copy_settings()
        self.assertEqual(copied["spreadsheet_id"], self.source.analysis.google.public()["spreadsheet_id"])
        self.assertNotIn("private_key", json.dumps(copied))
        self.assertEqual(account_hospital(self.target), "聯醫")
        with self.source.analysis.google.connect() as source_db, self.target.analysis.google.connect() as target_db:
            self.assertNotEqual(source_db.execute("SELECT secret FROM bot_google_config").fetchone()[0],
                                target_db.execute("SELECT secret FROM bot_google_config").fetchone()[0])
        self.source.analysis.google.remove_key()
        self.target.analysis.google.save({"spreadsheet_id": "synthetic-target-sheet-000000000"})
        reopened = GoogleSettings(self.target.store.directory)
        self.assertTrue(reopened.public()["configured"])
        self.assertNotEqual(reopened.public()["spreadsheet_id"], self.source.analysis.google.public()["spreadsheet_id"])
        client = reopened.client()
        try:
            self.assertTrue(client.session.credentials.sign_bytes(b"synthetic offline check"))
        finally:
            client.close()

    def test_source_listing_does_not_enter_other_accounts_or_create_databases(self):
        missing = self.app.registry.save("UNOPENED", "Unopened", "高榮", "", False)
        self.app.close()
        self.app = BotApplication(Settings(), self.path, BotSyntheticSDK)
        self.app.offline(self.target_id)
        self.target = self.app.workspace(self.target_id)
        before = list(BotSyntheticSDK.calls)
        value = self.target.analysis.handle("google/sources", {})
        self.assertEqual([row["id"] for row in value["sources"]], [self.source_id])
        self.assertEqual(set(self.app.workspaces), {self.target_id})
        self.assertFalse((self.path / "accounts" / missing["id"]).exists())
        self.assertNotIn("private_key", json.dumps(value))
        self.assertEqual(before, BotSyntheticSDK.calls)
        self.copy_settings()  # Source need not be entered or logged in.

    def test_copy_rejects_unknown_same_account_readonly_and_active_sheet_operations(self):
        for source in (self.target_id, "../outside", "f" * 32):
            with self.assertRaises(ValueError):
                self.target.sheets.copy_connection({"source_account_id": source})
        self.app.read_only = True
        with self.assertRaises(ValueError):
            self.copy_settings()
        self.app.read_only = False
        self.target.sheets.busy = True
        with self.assertRaises(ValueError):
            self.copy_settings()
        self.target.sheets.busy = False
        self.assertFalse(self.target.analysis.google.public()["configured"])

    def test_copy_rollback_preserves_target_key_and_target_when_sql_write_fails(self):
        self.copy_settings()
        self.target.analysis.google.save({"spreadsheet_id": "synthetic-original-target-000000"})
        before = GoogleSettings.saved(self.target.store.directory, with_key=True)
        with self.target.analysis.google.connect() as db:
            db.execute("CREATE TRIGGER reject_copy BEFORE UPDATE OF spreadsheet_id ON bot_google_config BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(StorageError):
            self.copy_settings()
        self.assertEqual(GoogleSettings.saved(self.target.store.directory, with_key=True), before)

    def test_account_scoped_http_copy_requires_csrf_and_never_returns_key(self):
        with BotServer(0, self.app) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                path = f"/api/accounts/{self.target_id}/analysis/google/copy"
                headers = {"Content-Type": "application/json", "Cookie": "opd_session=" + self.app.session_token}
                body = json.dumps({"source_account_id": self.source_id})
                connection.request("POST", path, body, headers)
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 403)
                headers["X-CSRF-Token"] = self.app.csrf_token
                connection.request("POST", path, body, headers)
                response = connection.getresponse()
                result = json.loads(response.read())
                self.assertEqual(response.status, 200, result)
                self.assertTrue(result["configured"])
                self.assertNotIn("private_key", repr(result))
                connection.close()
            finally:
                server.shutdown()
                thread.join()

    def test_fu_defaults_to_account_campus_and_only_hospital_is_editable(self):
        cohort, _ = self.cohort(fu=True)
        row = self.target.analysis.surgery_candidates({"cohort_id": cohort["id"]})["candidates"][0]
        self.assertEqual(row["fields"]["hospital"], "聯醫")
        proposal = self.target.analysis.proposals({"cohort_id": cohort["id"],
            "proposals": [{"id": row["id"], "fields": {"hospital": "高榮"}}]})[0]
        self.assertEqual(proposal["fields"]["hospital"], "高榮")
        for fields in ({"mrn": "OTHER"}, {"tag": "manual"}, {"date": "2026-11-01"},
                       {"hospital": ""}, {"hospital": []}, {"hospital": "unknown"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.target.analysis.proposals({"cohort_id": cohort["id"], "proposals": [{"id": row["id"], "fields": fields}]})

    def test_surgery_and_manual_draft_defaults_allow_manual_override(self):
        cohort, _ = self.cohort()
        row = self.target.analysis.surgery_candidates({"cohort_id": cohort["id"]})["candidates"][0]
        self.assertEqual(row["fields"]["hospital"], "聯醫")
        self.assertNotIn("hospital", row["evidence"])
        proposals = self.target.analysis.proposals({"cohort_id": cohort["id"],
            "proposals": [{"id": row["id"], "fields": {"hospital": "高榮"}}]})
        self.assertEqual(proposals[0]["fields"]["hospital"], "高榮")
        draft = self.target.analysis.surgery_draft({"cohort_id": cohort["id"], "mrn": "0000003"})["candidate"]
        self.assertEqual(draft["fields"]["hospital"], "聯醫")
        self.assertEqual(self.target.sheet_management.status()["default_hospital"], "聯醫")

    def test_campus_change_cannot_move_existing_surgery_when_updating_iol_or_date(self):
        cohort, record = self.cohort()
        client = MemorySheets()
        self.target.analysis.client_factory = lambda: client
        state = SimpleNamespace(check_cancel=lambda: None)
        first = self.target.analysis.automatic_sheet_sync([record], cohort["id"], state)
        self.assertFalse(first["pending"], first)
        patient = next(item for sheet in client.book["sheets"] if sheet["properties"]["title"].isdigit()
                       for item in entries(sheet) if item["kind"] == "patient" and item["fields"]["mrn"] == record["mrn"])
        uid = patient["sync_id"]
        self.assertEqual(patient["route"]["hospital"], "聯醫")
        self.change_campus("高榮")
        changed = copy.deepcopy(record)
        changed["soap"] = changed["soap"].replace("SN60WF", "ZCB00").replace("20260930", "20261001")
        changed["soap_structure"]["assessment_plan"] = changed["soap"]
        self.target.store.library.save_record(changed, "TARGET", "synthetic")
        result = self.target.analysis.automatic_sheet_sync([changed], cohort["id"], state)
        self.assertFalse(result["pending"], result)
        after = next(item for sheet in client.book["sheets"] if sheet["properties"]["title"].isdigit()
                     for item in entries(sheet) if item["kind"] == "patient" and item["sync_id"] == uid)
        self.assertEqual(after["route"]["hospital"], "聯醫")
        self.assertEqual(after["route"]["date"], "2026-10-01")
        self.assertEqual(after["fields"]["iol"], "ZCB00")

    def test_unknown_legacy_campus_requires_selection_for_new_rows(self):
        self.change_campus("legacy campus")
        cohort, record = self.cohort()
        self.assertEqual(account_hospital(self.target), "")
        self.assertEqual(self.target.analysis.surgery_candidates({"cohort_id": cohort["id"]})["candidates"][0]["fields"]["hospital"], "")
        client = MemorySheets()
        self.target.analysis.client_factory = lambda: client
        result = self.target.analysis.automatic_sheet_sync([record], cohort["id"], SimpleNamespace(check_cancel=lambda: None))
        self.assertTrue(result["pending"])
        self.assertEqual(client.writes, 0)

    def test_manual_iol_update_keeps_existing_campus_until_explicit_destination(self):
        cohort, record = self.cohort()
        client = MemorySheets()
        self.target.analysis.client_factory = lambda: client
        self.target.analysis.automatic_sheet_sync([record], cohort["id"], SimpleNamespace(check_cancel=lambda: None))

        def patient():
            return next(item for sheet in client.book["sheets"] if sheet["properties"]["title"].isdigit()
                        for item in entries(sheet) if item["kind"] == "patient" and item["fields"]["mrn"] == record["mrn"])

        uid = patient()["sync_id"]
        self.change_campus("高榮")
        row = self.target.analysis.surgery_candidates({"cohort_id": cohort["id"]})["candidates"][0]
        self.assertEqual(row["fields"]["hospital"], "高榮")
        proposal = {"id": row["id"], "fields": {"iol": "MANUAL-LENS"}}
        preview = self.target.analysis.preview({"cohort_id": cohort["id"], "proposals": [proposal]})
        self.assertTrue(preview["can_apply"], preview)
        self.assertEqual(self.target.analysis.apply({"id": preview["id"]})["status"], "applied")
        self.assertEqual(patient()["route"]["hospital"], "聯醫")
        self.assertEqual(patient()["fields"]["iol"], "MANUAL-LENS")
        proposal["destination"] = {"date": row["fields"]["date"], "hospital": "高榮"}
        preview = self.target.analysis.preview({"cohort_id": cohort["id"], "proposals": [proposal]})
        self.assertTrue(preview["can_apply"], preview)
        self.assertEqual(self.target.analysis.apply({"id": preview["id"]})["status"], "applied")
        self.assertEqual(patient()["route"]["hospital"], "高榮")
        self.assertEqual(patient()["sync_id"], uid)

    def test_fu_manual_campus_is_written_and_permanent_binding_preserves_manual_fields(self):
        cohort, record = self.cohort(fu=True)
        client = MemorySheets()
        self.target.analysis.client_factory = lambda: client
        candidate = self.target.analysis.surgery_candidates({"cohort_id": cohort["id"]})["candidates"][0]
        preview = self.target.analysis.preview({"cohort_id": cohort["id"],
            "proposals": [{"id": candidate["id"], "fields": {"hospital": "高榮"}}]})
        self.assertTrue(preview["can_apply"], preview)
        self.assertEqual(self.target.analysis.apply({"id": preview["id"]})["status"], "applied")
        sheet = next(sheet for sheet in client.book["sheets"] if sheet["properties"]["title"] == "FU")
        cols = header_columns(sheet)
        row = sheet["rows"][1]
        uid = cell_text(row[cols["ScheduleSyncId"]])
        self.assertEqual(cell_text(row[cols["醫院"]]), "高榮")
        for label in ("TEL", "Condition", "日期", "Plan", "心得"):
            self.assertFalse(cell_text(row[cols[label]]))
        row[cols["醫院"]] = literal("聯醫")
        row[cols["Tag"]] = literal("人工分類")
        row[cols["Plan"]] = literal("人工補填")
        self.change_campus("高榮")
        result = self.target.analysis.automatic_sheet_sync([record], cohort["id"], SimpleNamespace(check_cancel=lambda: None))
        self.assertFalse(result["pending"], result)
        sheet = next(sheet for sheet in client.book["sheets"] if sheet["properties"]["title"] == "FU")
        self.assertEqual(len(sheet["rows"]), 2)
        self.assertEqual(cell_text(sheet["rows"][1][cols["ScheduleSyncId"]]), uid)
        self.assertEqual(cell_text(sheet["rows"][1][cols["醫院"]]), "聯醫")
        self.assertEqual(cell_text(sheet["rows"][1][cols["Plan"]]), "人工補填")

    def test_deferred_sync_retains_original_campus_after_account_setting_changes(self):
        self.target.analysis.google.public = Mock(return_value={"configured": True, "spreadsheet_id": "synthetic-source-sheet-000000000"})
        self.target.analysis.automatic_sheet_sync = Mock(side_effect=SheetUnavailable("Synthetic outage"))
        self.target.sheet_sync.run()
        self.assertTrue(self.target.idle.wait(10))
        saved = self.target.sheet_sync.status()["deferred"][0]
        self.assertEqual(saved["default_hospital"], "聯醫")
        self.change_campus("高榮")
        self.target.analysis.automatic_sheet_sync.side_effect = None
        self.target.analysis.automatic_sheet_sync.return_value = {"updated": 0, "pending": []}
        self.target.sheet_sync.retry({"id": saved["id"]})
        self.assertEqual(self.target.analysis.automatic_sheet_sync.call_args.kwargs["default_hospital"], "聯醫")


if __name__ == "__main__":
    unittest.main()
