"""Deterministic scheduling against isolated synthetic hospital sessions."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from vghks_sdk import RequestError

from vghks_bot.bot import BotApplication
from vghks_bot.bot_server import BotServer
from vghks_bot.selftest_bot import BotSyntheticSDK
from vghks_bot.settings import Settings


class QueueSDK(BotSyntheticSDK):
    entered = threading.Event()
    release = threading.Event()
    block = ""
    fail_numeric = ""
    fail_connection = ""

    def history(self, mrn, filters):
        value = super().history(mrn, filters)
        if mrn == type(self).block:
            type(self).block = ""
            type(self).entered.set()
            if not type(self).release.wait(8):
                raise AssertionError("synthetic blocking read timed out")
        if mrn == type(self).fail_numeric:
            raise ValueError("synthetic numerical report failure")
        if mrn == type(self).fail_connection:
            raise RequestError("synthetic offline response", code="NETWORK_TIMEOUT")
        return value


class CataractQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        QueueSDK.calls = []
        QueueSDK.block = QueueSDK.fail_numeric = QueueSDK.fail_connection = ""
        QueueSDK.on_soap = None
        QueueSDK.fail_case = ""
        QueueSDK.entered.clear()
        QueueSDK.release.clear()
        self.app = BotApplication(Settings(), Path(self.temp.name), QueueSDK)
        self.key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.key)
        self.cohort = self.work.analysis.save_cohort({"source": "manual", "account_id": self.key,
                                                     "name": "queue", "mrns": "TEST001 TEST002"})
        self.queue = self.work.analysis.cataract_queue
        self.now = 100.0
        self.queue.clock = lambda: self.now
        self.queue.delay = Mock(return_value=12)

    def tearDown(self):
        QueueSDK.release.set()
        self.app.close()
        self.temp.cleanup()

    def action(self, action="start", mrn="TEST001"):
        return self.queue.handle({"cohort_id": self.cohort["id"], "action": action, "mrn": mrn})

    def wait(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail("synthetic queue timeout: " + str(self.queue.snapshot()))

    def advance(self):
        self.now += 20
        self.queue.notify()

    def numeric_calls(self, mrn):
        return [row for row in QueueSDK.calls if row[1:3] == ("numeric-history", mrn)]

    def test_background_wait_releases_worker_then_priority_bypasses_wait(self):
        self.action()
        self.wait(lambda: self.queue.snapshot()["state"] == "waiting")
        self.assertEqual(self.queue.snapshot()["pending"], ["TEST002"])
        self.queue.delay.assert_called_with(10, 20)
        self.assertTrue(self.work.idle.wait(2))
        self.assertFalse(self.numeric_calls("TEST002"))
        self.action("prioritize", "TEST002")
        self.wait(lambda: self.queue.snapshot()["state"] == "completed")
        self.assertEqual(len(self.numeric_calls("TEST001")), 1)
        self.assertEqual(len(self.numeric_calls("TEST002")), 1)
        calls = list(QueueSDK.calls)
        self.action()
        self.action("prioritize", "TEST001")
        self.assertEqual(QueueSDK.calls, calls)

    def test_inflight_read_saved_before_preemption_and_resume_does_not_refetch(self):
        QueueSDK.block = "TEST001"
        self.action()
        self.assertTrue(QueueSDK.entered.wait(4))
        self.action("prioritize", "TEST002")
        QueueSDK.release.set()
        self.wait(lambda: self.queue.snapshot()["state"] == "waiting")
        self.assertEqual(self.queue.snapshot()["pending"], ["TEST001"])
        self.assertIsNotNone(self.work.analysis.store.step("TEST001", "numeric-history"))
        self.assertEqual([row[2] for row in QueueSDK.calls if row[1] == "numeric-history"], ["TEST001", "TEST002"])
        self.advance()
        self.wait(lambda: self.queue.snapshot()["state"] == "completed")
        self.assertEqual(len(self.numeric_calls("TEST001")), 1)
        runs = self.work.analysis.overview({})["runs"]
        self.assertTrue(all(run["status"] == "completed" and not run["issues"] for run in runs))

    def test_pause_finishes_read_resume_and_cleared_cache_never_auto_refilled(self):
        QueueSDK.block = "TEST001"
        self.action()
        self.assertTrue(QueueSDK.entered.wait(4))
        self.action("pause")
        QueueSDK.release.set()
        self.wait(lambda: self.queue.snapshot()["state"] == "paused" and not self.queue.snapshot()["active_mrn"])
        self.assertTrue(self.work.idle.wait(2))
        request = {"mrns": ["TEST001"], "categories": ["numeric"]}
        preview = self.work.library_data.preview(request)
        self.work.library_data.delete({**request, "fingerprint": preview["fingerprint"]})
        self.action("resume")
        self.wait(lambda: self.queue.snapshot()["state"] == "waiting")
        self.assertEqual(self.queue.snapshot()["pending"], ["TEST002"])
        self.assertEqual(len(self.numeric_calls("TEST001")), 1)
        self.assertIsNone(self.work.analysis.store.step("TEST001", "numeric-history"))
        self.advance()
        self.wait(lambda: self.queue.snapshot()["state"] == "completed")

    def test_failed_patient_not_retried_implicitly_other_patients_continue(self):
        QueueSDK.fail_numeric = "TEST001"
        self.action()
        self.wait(lambda: self.queue.snapshot()["state"] == "waiting")
        self.advance()
        self.wait(lambda: self.queue.snapshot()["state"] == "completed")
        self.action()
        self.action("prioritize", "TEST001")
        self.assertEqual(len(self.numeric_calls("TEST001")), 1)
        self.assertEqual(len(self.numeric_calls("TEST002")), 1)
        self.assertTrue(any(run["status"] == "partial" for run in self.work.analysis.overview({})["runs"]))

    def test_logout_pauses_and_restart_requires_explicit_resume(self):
        QueueSDK.block = "TEST001"
        self.action()
        self.assertTrue(QueueSDK.entered.wait(4))
        self.action("pause")
        QueueSDK.release.set()
        self.wait(lambda: not self.queue.snapshot()["active_mrn"])
        self.app.close()
        self.app = BotApplication(Settings(), Path(self.temp.name), QueueSDK)
        self.key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.key)
        self.queue = self.work.analysis.cataract_queue
        self.queue.clock = lambda: self.now
        self.queue.delay = Mock(return_value=12)
        self.action()
        self.assertEqual(self.queue.snapshot()["state"], "paused")
        self.assertEqual(len(self.numeric_calls("TEST001")), 1)
        self.action("resume")
        self.wait(lambda: self.queue.snapshot()["state"] == "waiting")
        self.advance()
        self.wait(lambda: self.queue.snapshot()["state"] == "completed")
        self.assertEqual(len(self.numeric_calls("TEST001")), 1)
        self.work.logout()
        self.assertEqual(self.queue.snapshot()["state"], "paused")
        with self.assertRaises(ValueError):
            self.action("resume")

    def test_queue_rejects_wrong_patient_account_readonly_and_bad_action(self):
        before = list(QueueSDK.calls)
        with self.assertRaises(ValueError):
            self.action("prioritize", "FOREIGN")
        with self.assertRaises(ValueError):
            self.action("invalid")
        other = self.app.login({"username": "OTHER", "password": "synthetic"})["account"]["id"]
        before = list(QueueSDK.calls)
        with self.assertRaises(ValueError):
            self.app.workspace(other).analysis.cataract_queue.handle({"cohort_id": self.cohort["id"], "action": "start"})
        self.app.read_only = True
        with self.assertRaises(ValueError):
            self.action()
        self.assertEqual(QueueSDK.calls, before)

    def test_logout_during_read_retains_current_patient_until_explicit_continue(self):
        QueueSDK.block = "TEST001"
        self.action()
        self.assertTrue(QueueSDK.entered.wait(4))
        logout = threading.Thread(target=self.work.logout)
        logout.start()
        self.wait(lambda: self.queue.snapshot()["state"] == "paused")
        QueueSDK.release.set()
        logout.join(4)
        self.assertFalse(logout.is_alive())
        self.wait(lambda: not self.queue.snapshot()["active_mrn"])
        self.assertIn("TEST001", self.queue.snapshot()["pending"])
        self.assertEqual(len(self.numeric_calls("TEST001")), 1)
        self.app.login({"id": self.key, "password": "synthetic"})
        self.action("prioritize", "TEST001")
        self.assertEqual(self.queue.snapshot()["state"], "paused")
        self.action("resume")
        self.wait(lambda: self.queue.snapshot()["state"] == "waiting")
        self.assertEqual(len(self.numeric_calls("TEST001")), 1)

    def test_unrecoverable_connection_pauses_without_automatic_retry(self):
        QueueSDK.fail_connection = "TEST001"
        self.action()
        self.wait(lambda: self.queue.snapshot()["state"] == "paused" and not self.queue.snapshot()["active_mrn"])
        self.assertFalse(self.work.gateway.online)
        self.assertIn("TEST001", self.queue.snapshot()["pending"])
        before = list(QueueSDK.calls)
        self.advance()
        self.assertEqual(QueueSDK.calls, before)
        QueueSDK.fail_connection = ""
        self.app.login({"id": self.key, "password": "synthetic"})
        self.action("resume")
        self.wait(lambda: self.queue.snapshot()["state"] == "waiting")
        self.assertEqual(self.queue.snapshot()["pending"], ["TEST002"])
        self.assertIsNotNone(self.work.analysis.store.step("TEST001", "numeric-history"))

    def test_most_recent_priority_and_collection_change_retire_old_work(self):
        self.cohort = self.work.analysis.save_cohort({"source": "manual", "account_id": self.key,
                                                     "name": "three", "mrns": "TEST001 TEST002 TEST003"})
        QueueSDK.block = "TEST001"
        self.action()
        self.assertTrue(QueueSDK.entered.wait(4))
        self.action("prioritize", "TEST002")
        self.action("prioritize", "TEST003")
        QueueSDK.release.set()
        self.wait(lambda: self.queue.snapshot()["state"] == "waiting")
        self.assertEqual([row[2] for row in QueueSDK.calls if row[1] == "numeric-history"], ["TEST001", "TEST003"])
        former = self.cohort
        self.cohort = self.work.analysis.save_cohort({"source": "manual", "account_id": self.key,
                                                     "name": "new", "mrns": "TEST004"})
        self.action(mrn="TEST004")
        self.wait(lambda: self.queue.snapshot()["state"] == "completed")
        self.assertFalse(self.numeric_calls("TEST002"))
        saved = self.work.analysis.store.document("analysis_cohorts", former["id"])["cataract_queue"]
        self.assertEqual(saved["state"], "paused")
        self.assertFalse(any(key[0] == former["id"] for key in self.queue.jobs))

    def test_removing_paused_collection_releases_background_schedule(self):
        self.action()
        self.wait(lambda: self.queue.snapshot()["state"] == "waiting")
        self.action("pause")
        self.work.analysis.delete_cohort({"id": self.cohort["id"]})
        self.advance()
        self.assertFalse(self.queue.snapshot()["pending"])
        self.assertFalse(self.numeric_calls("TEST002"))

    def test_scoped_http_queue_authorization_and_duplicate_submission(self):
        other = self.app.login({"username": "OTHER", "password": "synthetic"})["account"]["id"]
        with BotServer(0, self.app) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            def request(account, csrf=True):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
                headers = {"Content-Type": "application/json", "Cookie": "opd_session=" + self.app.session_token}
                if csrf:
                    headers["X-CSRF-Token"] = self.app.csrf_token
                connection.request("POST", f"/api/accounts/{account}/analysis/cataract/queue",
                                   json.dumps({"cohort_id": self.cohort["id"], "mrn": "TEST001", "action": "start"}), headers)
                response = connection.getresponse()
                status = response.status
                response.read()
                connection.close()
                return status
            try:
                self.assertEqual(request(self.key, csrf=False), 403)
                self.assertEqual(request(other), 400)
                QueueSDK.block = "TEST001"
                self.assertEqual(request(self.key), 200)
                self.assertTrue(QueueSDK.entered.wait(4))
                self.assertEqual(request(self.key), 200)
                self.assertEqual(len(self.work.analysis.overview({})["runs"]), 1)
                QueueSDK.release.set()
                self.wait(lambda: self.queue.snapshot()["state"] == "waiting")
            finally:
                QueueSDK.release.set()
                server.shutdown()
                thread.join(2)
