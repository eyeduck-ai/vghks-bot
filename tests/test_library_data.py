"""Clinical deletion retains unrelated data and requires a fresh account preview."""
import http.client
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from vghks_sdk.models import BinaryAsset

from vghks_bot.bot import BotApplication
from vghks_bot.bot_server import BotServer
from vghks_bot.jobs import BusyError
from vghks_bot.selftest_bot import BotSyntheticSDK, wait_task
from vghks_bot.settings import Settings, today
from vghks_bot.storage import read_lines


class LibraryDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = BotApplication(Settings(), Path(self.temp.name), BotSyntheticSDK)
        self.key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.key)
        self.data = self.work.library_data
        self.store = self.work.analysis.store

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def seed(self, mrn="TEST001"):
        for key, kind in (("visits", "visits"), ("numeric-history", "numeric"),
                          ("orders-history:*", "order_index"), ("scans-history", "scan_index"),
                          ("coverage", "coverage")):
            self.store.save_step(mrn, key, kind, [], "TEST")
        self.work.review.db.save("profile", {"id": mrn, "mrn": mrn, "name": "合成病人"})
        self.work.review.db.save("visits", {"id": mrn, "cases": []})
        self.work.review.db.save("registrations", {"id": mrn, "mrn": mrn, "payload": []})

    def delete(self, **values):
        preview = self.data.preview(values)
        return self.data.delete({**values, "fingerprint": preview["fingerprint"]})

    def test_inventory_includes_patient_without_soap_and_offline_never_calls_sdk(self):
        self.seed()
        self.app.offline(self.key)
        before = list(BotSyntheticSDK.calls)
        rows = self.data.read({"q": "合成"})["patients"]
        self.assertEqual([row["mrn"] for row in rows], ["TEST001"])
        self.assertIn("numeric", rows[0]["categories"])
        self.assertEqual(self.work.library_search({})["total"], 0)
        self.delete(mrns=["TEST001"], categories=["numeric"])
        self.assertEqual(before, BotSyntheticSDK.calls)
        self.assertIsNone(self.store.step("TEST001", "numeric-history"))
        self.assertIsNotNone(self.store.step("TEST001", "orders-history:*"))

    def test_category_delete_preserves_shared_assets_and_removes_all_versions(self):
        self.seed()
        shared = BinaryAsset(b"%PDF-1.4\nshared-test", "application/pdf")
        asset = self.store.save_asset("TEST001", "history-asset:download_pdf:1", shared, "TEST")
        self.store.save_asset("TEST001", "scan-asset:2", shared, "TEST")
        self.store.save_asset("TEST002", "download_pdf:3", shared, "TEST")
        inventory = self.data.read({"q": "TEST001"})["patients"][0]
        self.assertEqual(inventory["attachment_bytes"], len(shared.content))
        self.assertEqual(inventory["categories"]["orders"]["attachments"], 1)
        self.assertEqual(inventory["categories"]["scans"]["attachments"], 1)
        self.store.save_step("TEST001", "history-order:1", "order_report", {"assets": [asset]}, "TEST")
        self.store.save_step("TEST001", "history-order:1", "order_report", {"assets": [asset], "texts": ["new"]}, "TEST")
        result = self.delete(mrns=["TEST001"], categories=["orders"])
        self.assertEqual(result["attachments"], 0)
        self.assertTrue(self.store.asset(asset["digest"])[0].exists())
        self.assertFalse(any(row["key"].startswith("history-order") for row in self.store.raw_data("TEST001")["versions"]))
        self.delete(mrns=["TEST001"], categories=["scans"])
        self.assertTrue(self.store.asset(asset["digest"])[0].exists())
        result = self.delete(mrns=["TEST002"], categories=["orders"])
        self.assertEqual(result["attachments"], 1)
        self.assertFalse((self.store.assets / asset["digest"]).exists())

    def test_visit_dependencies_are_previewed_and_user_data_and_logs_survive(self):
        self.seed()
        self.work.review.db.save("set", {"id": "s", "members": [{"mrn": "TEST001", "name": "合成病人"}]})
        self.work.review.db.save("review_note", {"id": "t:TEST001", "text": "保留備註"})
        self.work.review.db.save("task", {"id": "t", "kind": "history", "mrn": "TEST001", "resource": "orders",
                                         "status": "completed", "name": "歷年醫囑"})
        self.work.review.db.item("t", "TEST001", {"status": "ready", "payload": "old clinical copy"})
        self.work.patient_tags_update({"mrns": "TEST001", "new_tag": "合成 TAG"})
        preview = self.data.preview({"mrns": ["TEST001"], "categories": ["visits"]})
        self.assertEqual(set(preview["cascaded"]), {"numeric", "orders", "scans", "analysis"})
        self.delete(mrns=["TEST001"], categories=["visits"])
        self.assertFalse(self.store.steps("TEST001"))
        for kind, key in (("set", "s"), ("review_note", "t:TEST001"), ("task", "t"), ("profile", "TEST001"), ("registrations", "TEST001")):
            self.assertIsNotNone(self.work.review.db.get(kind, key))
        self.assertEqual(self.work.review.db.items("t")[0]["status"], "deleted")
        self.assertEqual(self.work.tag_patients({})["total"], 1)

    def test_stale_preview_busy_foreground_and_read_only_are_rejected(self):
        self.seed()
        values = {"mrns": ["TEST001"], "categories": ["numeric"]}
        preview = self.data.preview(values)
        self.store.save_step("TEST001", "numeric-history", "numeric", ["changed"], "TEST")
        with self.assertRaisesRegex(ValueError, "資料已變更"):
            self.data.delete({**values, "fingerprint": preview["fingerprint"]})
        self.work.idle.clear()
        try:
            with self.assertRaises(BusyError):
                self.delete(**values)
        finally:
            self.work.idle.set()
        self.app.read_only = True
        with self.assertRaisesRegex(ValueError, "唯讀"):
            self.delete(**values)
        self.assertTrue(self.data.read({})["patients"])

    def test_preview_fingerprint_binds_account_patient_and_requested_categories(self):
        other_key = self.app.login({"username": "OTHER", "password": "synthetic"})["account"]["id"]
        other = self.app.workspace(other_key)
        with patch("vghks_bot.analysis_store.timestamp", return_value="2026-09-30T00:00:00+08:00"):
            for work in (self.work, other):
                work.analysis.store.save_step("TEST001", "numeric-history", "numeric", [], "TEST")
        values = {"mrns": ["TEST001"], "categories": ["numeric"]}
        first = self.data.preview(values)
        second = other.library_data.preview(values)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        with self.assertRaisesRegex(ValueError, "資料已變更"):
            other.library_data.delete({**values, "fingerprint": first["fingerprint"]})
        empty = {"mrns": ["TEST001"], "categories": ["orders"]}
        preview = self.data.preview(empty)
        with self.assertRaisesRegex(ValueError, "資料已變更"):
            self.data.delete({**empty, "categories": ["scans"], "fingerprint": preview["fingerprint"]})

    def test_clearing_numeric_keeps_canonical_soap_readable_without_refetch(self):
        wait_task(self.work, self.work.review.start({"kind": "resolve", "identifiers": "TEST001"})["task_id"])
        group = self.work.review.save_set({"mrns": "TEST001"})
        run = self.work.review.start({"set_id": group["id"]})["task_id"]
        record = wait_task(self.work, run)["items"][0]["records"][0]
        cohort = self.work.review.cohort({"set_id": group["id"]})
        self.store.save_step("TEST001", "cataract-soap", "cataract_soap",
                             {"record_id": record["id"], "status": "ready"}, "TEST")
        self.store.save_step("TEST001", "numeric-history", "numeric", [], "TEST")
        self.delete(mrns=["TEST001"], categories=["numeric"])
        before = list(BotSyntheticSDK.calls)
        result = self.work.analysis.results({"cohort_id": cohort["id"], "module": "cataract", "mrn": "TEST001"})
        self.assertEqual(result["numeric"], [])
        self.assertEqual(result["latest_soap"]["status"], "cached")
        self.assertTrue(result["latest_soap"]["record"]["soap"])
        self.assertTrue(result["latest_soap"]["updated_at"])
        status = self.work.analysis.cataract_status({"cohort_id": cohort["id"]})
        self.assertTrue(status["members"][0]["attempted"])
        self.assertTrue(status["members"][0]["cache_cleared"])
        self.assertEqual(BotSyntheticSDK.calls, before)

    def test_list_scope_and_cleared_marker_persist_after_restart(self):
        self.seed()
        day = today().isoformat()
        self.work.store.library.save_list("TEST", day, [{"mrn": "TEST001"}])
        rows = self.data.read({})["lists"]
        self.assertEqual(rows[0]["count"], 1)
        self.delete(lists=[{"account": "TEST", "day": day}])
        self.assertEqual(self.data.read({})["lists"], [])
        self.assertIsNotNone(self.store.step("TEST001", "numeric-history"))
        self.delete(mrns=["TEST001"], categories=["numeric"])
        self.app.close()
        self.app = BotApplication(Settings(), Path(self.temp.name), BotSyntheticSDK)
        self.app.offline(self.key)
        self.work = self.app.workspace(self.key)
        self.assertTrue(self.work.library_data.cleared("TEST001"))
        self.assertIsNone(self.work.analysis.store.step("TEST001", "numeric-history"))

    def test_legacy_index_copies_clear_by_patient_or_list_without_losing_audit(self):
        self.seed()
        day = today().isoformat()
        run = {"id": uuid.uuid4().hex, "account": "TEST", "counts": {}, "status": "completed"}
        journal = self.work.store.create(run)
        journal.append("visits", {"mrn": "ALIAS001", "lookup_mrn": "TEST001"})
        journal.append("visits", {"mrn": "OTHER"})
        journal.append("registrations", {"mrn": "TEST001", "visit_date": day})
        journal.append("registrations", {"mrn": "OTHER", "visit_date": "2020-01-01"})
        journal.append("issues", {"code": "SYNTHETIC", "mrn": "TEST001"})
        self.work.store.library.save_list("TEST", day, [])
        self.delete(mrns=["TEST001"], categories=["visits"])
        self.assertEqual(read_lines(journal.directory / "visits.jsonl")[0], [{"mrn": "OTHER"}])
        self.assertEqual(len(read_lines(journal.directory / "registrations.jsonl")[0]), 2)
        self.delete(lists=[{"account": "TEST", "day": day}])
        self.assertEqual(read_lines(journal.directory / "registrations.jsonl")[0], [{"mrn": "OTHER", "visit_date": "2020-01-01"}])
        self.assertEqual(self.work.store.metadata(run["id"]), run)
        self.assertEqual(read_lines(journal.directory / "issues.jsonl")[0][0]["code"], "SYNTHETIC")

    def test_new_http_routes_enforce_account_csrf_and_readonly(self):
        self.seed()
        other = self.app.login({"username": "OTHER", "password": "synthetic"})["account"]["id"]
        server = BotServer(0, self.app)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()

        def post(account, route, body, csrf=True):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            headers = {"Cookie": f"opd_session={self.app.session_token}", "Content-Type": "application/json"}
            if csrf:
                headers["X-CSRF-Token"] = self.app.csrf_token
            connection.request("POST", f"/api/accounts/{account}/library/data/{route}", json.dumps(body), headers)
            response = connection.getresponse()
            result = response.status, json.loads(response.read())
            connection.close()
            return result

        try:
            self.assertEqual(post(self.key, "read", {}, csrf=False)[0], 403)
            self.assertEqual(post(other, "read", {})[1]["patients"], [])
            values = {"mrns": ["TEST001"], "categories": ["numeric"]}
            self.assertEqual(post(other, "preview-delete", values)[0], 400)
            status, preview = post(self.key, "preview-delete", values)
            self.assertEqual(status, 200)
            self.app.read_only = True
            self.assertEqual(post(self.key, "read", {})[0], 200)
            self.assertEqual(post(self.key, "preview-delete", values)[0], 200)
            self.assertEqual(post(self.key, "delete", {**values, "fingerprint": preview["fingerprint"]})[0], 400)
        finally:
            server.shutdown()
            thread.join(2)
            server.server_close()
