import http.client
import json
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from vghks_sdk import ParseError
from vghks_sdk.models import SurgeryRecord

from opd_monitor.bot import BotApplication
from opd_monitor.bot_server import BotServer
from opd_monitor.selftest_bot import BotSyntheticSDK, wait_task
from opd_monitor.settings import Settings, today
from opd_monitor.surgery_schedule import default_range, schedule_rows


class SurgeryScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.app = BotApplication(Settings(), self.path, BotSyntheticSDK)
        self.key = self.app.login({"username": "AB42", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.key)
        BotSyntheticSDK.calls = []

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def fetch(self, **values):
        task = wait_task(self.work, self.work.review.start({"kind": "surgery_schedule", **values})["task_id"])
        self.assertTrue(self.work.idle.wait(3))
        return task

    def test_two_calendar_months_including_year_and_month_end(self):
        for start, expected in ((date(2026, 12, 31), "2027-02-28"), (date(2023, 12, 31), "2024-02-29"),
                                (date(2026, 7, 31), "2026-09-30"), (date(2026, 9, 24), "2026-11-24")):
            self.assertEqual(default_range(start), {"start": start.isoformat(), "end": expected, "department": "OPH"})

    def test_current_account_dates_original_rows_and_local_reopen(self):
        task = self.fetch()
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["progress"]["done"], 1)
        view = self.work.surgery_schedule.overview()
        self.assertEqual(task["start"], today().isoformat())
        self.assertEqual(task["end"], default_range()["end"])
        self.assertEqual(len(view["result"]["rows"]), 3)
        self.assertEqual({r["group"] for r in view["result"]["rows"]}, {"upcoming"})
        self.assertEqual(view["result"]["rows"][0]["patient_mrn"], "00000002")
        self.assertEqual(view["result"]["rows"][0]["name"], "合成病人2")
        self.assertEqual(BotSyntheticSDK.calls[0][2], "AB42")
        self.assertEqual(BotSyntheticSDK.calls[0][-1], {"department": "OPH"})
        self.app.close()
        self.app = BotApplication(Settings(), self.path, BotSyntheticSDK)
        self.app.offline(self.key)
        self.work = self.app.workspace(self.key)
        self.assertEqual(self.work.surgery_schedule.overview()["result"], view["result"])
        self.assertEqual(len(BotSyntheticSDK.calls), 1)
        with self.assertRaises(ValueError):
            self.work.review.start({"kind": "surgery_schedule"})

    def test_past_and_cancelled_rows_are_retained_and_same_content_is_shared(self):
        start, end = (today()-timedelta(days=10)).isoformat(), (today()+timedelta(days=15)).isoformat()
        first, second = self.fetch(start=start, end=end), self.fetch(start=start, end=end)
        snapshots = self.work.review.db.all("surgery_schedule_snapshot")
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(len(self.work.review.db.all("surgery_schedule_result")), 2)
        rows = self.work.surgery_schedule.overview()["result"]["rows"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["group"], "past")
        self.assertEqual(rows[0]["status"], "已取消")
        old = self.work.surgery_schedule.overview({"task_id": first["id"]})["result"]
        self.assertEqual(old["task_id"], first["id"])
        self.assertNotEqual(old["fetched_at"], self.work.surgery_schedule.overview()["result"]["fetched_at"])
        self.assertEqual(second["doctor_card"], "AB42")
        with patch("opd_monitor.surgery_schedule.today", return_value=today()+timedelta(days=30)):
            self.assertEqual({r["group"] for r in self.work.surgery_schedule.overview()["result"]["rows"]}, {"past"})
        self.assertEqual(len(BotSyntheticSDK.calls), 2)

    def test_failure_does_not_replace_last_result_and_resume_is_available(self):
        old = self.fetch()
        with patch.object(self.work.gateway.connection.surgery, "get_schedule", side_effect=ParseError("synthetic", code="OPPL_SURGERY_LIST_MISSING")):
            failed = self.fetch()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.work.surgery_schedule.overview()["result"]["task_id"], old["id"])
        diagnostics = self.work.diagnostics.query({"task_id": failed["id"]})["items"]
        self.assertTrue(diagnostics)
        resumed = wait_task(self.work, self.work.review.start({"resume": failed["id"]})["task_id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(self.work.surgery_schedule.overview()["result"]["task_id"], failed["id"])

    def test_empty_response_is_success_and_old_task_keeps_its_snapshot(self):
        old = self.fetch()
        with patch.object(self.work.gateway.connection.surgery, "get_schedule", return_value=[]):
            self.assertEqual(self.fetch()["status"], "completed")
        self.assertEqual(self.work.surgery_schedule.overview()["result"]["rows"], [])
        self.assertEqual(len(self.work.surgery_schedule.overview({"task_id": old["id"]})["result"]["rows"]), 3)

    def test_isolation_validation_and_readonly(self):
        task = self.fetch()
        other = self.app.login({"username": "SECOND", "password": "synthetic"})["account"]["id"]
        other_work = self.app.workspace(other)
        self.assertIsNone(other_work.surgery_schedule.overview()["result"])
        with self.assertRaises(ValueError):
            other_work.surgery_schedule.overview({"task_id": task["id"]})
        for options in ({"start": "bad"}, {"start": "2026-11-02", "end": "2026-10-01"},
                        {"department": "<script>"}, {"department": None}, {"doctor_card": "OTHER"}):
            with self.assertRaises(ValueError):
                self.work.review.start({"kind": "surgery_schedule", **options})
        self.app.read_only = True
        self.assertIsNotNone(self.work.surgery_schedule.overview()["result"])
        with self.assertRaises(ValueError):
            self.work.review.start({"kind": "surgery_schedule"})

    def test_cancel_does_not_publish_partial_response_and_duplicate_is_not_queued(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(4)
            return [SurgeryRecord(patient_mrn="00000001")]
        with patch.object(self.work.gateway.connection.surgery, "get_schedule", side_effect=blocked):
            key = self.work.review.start({"kind": "surgery_schedule"})["task_id"]
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(ValueError):
                    self.work.review.start({"kind": "surgery_schedule"})
                self.work.review.stop(key)
            finally:
                release.set()
            self.assertEqual(wait_task(self.work, key)["status"], "paused")
        self.assertIsNone(self.work.surgery_schedule.overview()["result"])
        self.assertEqual(wait_task(self.work, self.work.review.start({"resume": key})["task_id"])["status"], "completed")

    def test_roc_dates_doctor_suffix_unknown_dates_and_duplicate_patients(self):
        records = [{"patient_mrn": "0001", "doctor_card": doc, "surgery_date": day, "extra": {}}
                   for doc, day in (("AB42B", "1150924"), ("AB42", "115/09/23"),
                                    ("AB420", "2026-09-24"), ("AB42", "unknown"), ("AB42", "2026-09-24"))]
        rows = schedule_rows(records, "AB42", "2026-09-24")
        self.assertEqual([r["group"] for r in rows], ["upcoming", "past", "unconfirmed", "unconfirmed", "upcoming"])
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["raw_date"], "1150924")

    def test_http_guards_local_overview_and_scoped_iframe_policy(self):
        with BotServer(0, self.app) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            def request(path, body=None, csrf=True, cookie=True):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                headers = {"Content-Type": "application/json"}
                if cookie:
                    headers["Cookie"] = "opd_session="+self.app.session_token
                if csrf:
                    headers["X-CSRF-Token"] = self.app.csrf_token
                connection.request("GET" if body is None else "POST", path, None if body is None else json.dumps(body), headers)
                response = connection.getresponse()
                result = response.status, dict(response.getheaders()), response.read()
                connection.close()
                return result
            try:
                path = f"/api/accounts/{self.key}/surgery/overview"
                self.assertEqual(request(path, {}, cookie=False)[0], 401)
                self.assertEqual(request(path, {}, csrf=False)[0], 403)
                self.assertEqual(request(path, {})[0], 200)
                self.assertEqual(BotSyntheticSDK.calls, [])
                html = request("/", cookie=False)
                self.assertIn("frame-src 'self' http://wmc01p:29080", html[1]["Content-Security-Policy"])
                self.assertIn("frame-ancestors 'self'", html[1]["Content-Security-Policy"])
                self.assertNotIn(b'<iframe id="operatingRoomFrame" src=', html[2])
                self.assertEqual(request("/surgery-system.js", cookie=False)[0], 200)
                self.assertEqual(request("/surgery-system.css", cookie=False)[0], 200)
                self.app.read_only = True
                self.assertEqual(request(path, {})[0], 200)
                self.assertEqual(request(f"/api/accounts/{self.key}/tasks/start", {"kind": "surgery_schedule"})[0], 400)
            finally:
                server.shutdown()
                thread.join(2)
