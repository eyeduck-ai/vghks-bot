"""Monitoring begins on an explicit visit and remains active for that process."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from vghks_bot.bot import BotApplication
from vghks_bot.bot_server import BotServer
from vghks_bot.selftest_bot import BotSyntheticSDK, wait_task
from vghks_bot.settings import Settings


class ApprovalActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.app = BotApplication(Settings(), self.path, BotSyntheticSDK)
        self.key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.key)
        self.tracker = self.work.approvals.tracker

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_login_workbench_and_local_overviews_do_not_activate_monitor(self):
        self.assertFalse(self.tracker.overview()["monitor_started"])
        self.work.approvals.cases({})
        with patch.object(self.work.review, "start") as start:
            self.app.follow_up_tick()
            start.assert_not_called()
        self.assertFalse(self.tracker.monitor_started)

    def test_first_entry_starts_due_sync_and_later_ticks_continue_without_visit(self):
        self.assertTrue(self.tracker.enter()["monitor_started"])
        jobs = [t for t in self.work.review.db.all("task") if t["kind"] == "approval_sync"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(wait_task(self.work, jobs[0]["id"])["status"], "completed")
        self.work.idle.wait(3)
        self.tracker.enter()
        self.app.follow_up_tick()
        self.assertEqual(len([t for t in self.work.review.db.all("task") if t["kind"] == "approval_sync"]), 1)
        prefs = self.tracker.preferences()
        self.work.review.db.save("preferences", {**prefs, "next_check": "2000-01-01T00:00:00+08:00"})
        self.app.follow_up_tick()
        jobs = [t for t in self.work.review.db.all("task") if t["kind"] == "approval_sync"]
        self.assertEqual(len(jobs), 2)
        self.assertEqual(wait_task(self.work, jobs[0]["id"])["status"], "completed")

    def test_restart_forgets_activation_and_preserves_monitor_preferences(self):
        self.tracker.preferences({"enabled": True, "automatic": True, "hours": 6})
        self.tracker.monitor_started = True
        self.app.close()
        self.app = BotApplication(Settings(), self.path, BotSyntheticSDK)
        self.app.activate(self.key)
        self.work = self.app.workspace(self.key)
        self.tracker = self.work.approvals.tracker
        self.assertFalse(self.tracker.monitor_started)
        self.assertEqual(self.tracker.preferences()["hours"], 6)
        with patch.object(self.work.review, "start") as start:
            self.app.follow_up_tick()
            start.assert_not_called()

    def test_activation_is_per_account_and_survives_reconnect(self):
        other_key = self.app.login({"username": "SECOND", "password": "synthetic"})["account"]["id"]
        other = self.app.workspace(other_key)
        self.tracker.preferences({"enabled": False})
        self.tracker.enter()
        self.assertFalse(other.approvals.tracker.monitor_started)
        self.work.gateway.close()
        self.app.activate(self.key)
        self.assertTrue(self.tracker.monitor_started)
        self.assertFalse(other.approvals.tracker.monitor_started)

    def test_disabled_offline_readonly_and_paused_tasks_prevent_automatic_jobs(self):
        with patch.object(self.work.review, "start") as start:
            self.tracker.preferences({"enabled": False})
            self.tracker.enter()
            start.assert_not_called()
            self.tracker.preferences({"enabled": True})
            self.work.gateway.online = False
            self.app.follow_up_tick()
            start.assert_not_called()
            self.work.gateway.online = True
            self.app.read_only = True
            self.tracker.enter()
            start.assert_not_called()
            self.app.read_only = False
            self.work.review.db.save("task", {"id": "old-pause", "kind": "approval_sync", "status": "paused"})
            self.app.follow_up_tick()
            start.assert_not_called()

    def test_readonly_visit_can_activate_without_writing_or_scheduling(self):
        self.app.read_only = True
        with patch.object(self.work.review.db, "save", side_effect=AssertionError("readonly write")), patch.object(
                self.work.review, "start", side_effect=AssertionError("readonly network task")):
            self.tracker.enter()
        self.assertTrue(self.tracker.monitor_started)

    def test_http_read_routes_do_not_activate_and_entry_requires_csrf(self):
        with BotServer(0, self.app) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()

            def request(path, values=None, csrf=True):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
                headers = {"Content-Type": "application/json", "Cookie": "opd_session=" + self.app.session_token}
                if csrf:
                    headers["X-CSRF-Token"] = self.app.csrf_token
                connection.request("GET" if values is None else "POST", f"/api/accounts/{self.key}/" + path,
                                   None if values is None else json.dumps(values), headers)
                response = connection.getresponse()
                result = response.status, json.loads(response.read())
                connection.close()
                return result

            try:
                self.assertEqual(request("workbench")[0], 200)
                self.assertEqual(request("approvals/cases", {})[0], 200)
                self.app.follow_up_tick()
                self.assertFalse(self.tracker.monitor_started)
                self.assertEqual(request("approvals/enter", {}, csrf=False)[0], 403)
                self.assertFalse(self.tracker.monitor_started)
                self.app.read_only = True
                status, value = request("approvals/enter", {})
                self.assertEqual(status, 200)
                self.assertTrue(value["monitor_started"])
                self.assertFalse(any(t["kind"] == "approval_sync" for t in self.work.review.db.all("task")))
            finally:
                server.shutdown()
                thread.join(3)


if __name__ == "__main__":
    unittest.main()
