"""Regression cases found by reviewing long-lived monitoring workspaces."""
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

import test_followup_earnings as baseline

from opd_monitor.analysis_fetch import clean
from opd_monitor.selftest_bot import wait_task


class MonitorAuditTests(unittest.TestCase):
    setUp = baseline.FollowUpTests.setUp
    tearDown = baseline.FollowUpTests.tearDown
    task = baseline.FollowUpTests.task
    query = baseline.FollowUpTests.query
    configure = baseline.EarningsArchiveTests.configure

    def test_older_query_cannot_reopen_case_after_newer_detail(self):
        self.query()
        entered, release = threading.Event(), threading.Event()

        def delayed(value):
            if isinstance(value, list):
                entered.set()
                release.wait(5)
            return clean(value)

        original = self.work.gateway.connection.reviews.get_case

        def approved(ref):
            case = original(ref)
            return replace(case, verify_code="1", fields={**case.fields, "VerifyCode": "1"})

        with patch("opd_monitor.approvals.clean", side_effect=delayed), patch.object(
                self.work.gateway.connection.reviews, "get_case", approved):
            key = self.work.review.start({"kind": "approval_search", "force": True})["task_id"]
            try:
                self.assertTrue(entered.wait(3))
                refreshed = wait_task(self.work, self.work.review.start({
                    "kind": "approval_refresh", "ids": ["1001"]})["task_id"])
                self.assertEqual(refreshed["status"], "completed")
            finally:
                release.set()
            self.assertEqual(wait_task(self.work, key)["status"], "completed")
        self.assertEqual(self.db.get("approval_case", "1001")["case"]["verify_code"], "1")
        self.assertTrue(self.db.get("approval_tracking", "1001")["closed"])
        self.assertEqual(self.work.approvals.results({"id": key})["cases"][0]["verify_code"], "4")

    def test_paused_tracker_remains_visible_after_twenty_newer_jobs(self):
        self.query()
        self.db.save("task", {"id": "old-pause", "kind": "approval_refresh", "status": "paused"})
        for i in range(25):
            self.db.save("task", {"id": str(i), "kind": "approval_refresh", "status": "completed"})
        visible = self.tracker.overview()["tasks"]
        self.assertIn("old-pause", {t["id"] for t in visible})

    def test_older_detail_cannot_replace_newer_detail_cache(self):
        self.query()
        entered, release = threading.Event(), threading.Event()
        original = self.work.gateway.connection.reviews.get_case
        requests = []

        def changing(ref):
            requests.append(ref)
            case = original(ref)
            code = "4" if len(requests) == 1 else "1"
            return replace(case, verify_code=code, fields={**case.fields, "VerifyCode": code})

        def delayed(value):
            if getattr(value, "verify_code", None) == "4":
                entered.set()
                release.wait(5)
            return clean(value)

        values = {"kind": "approval_case", "apply_seq": "1001", "force": True}
        with patch("opd_monitor.approvals.clean", side_effect=delayed), patch.object(
                self.work.gateway.connection.reviews, "get_case", changing):
            key = self.work.review.start(values)["task_id"]
            try:
                self.assertTrue(entered.wait(3))
                newer = wait_task(self.work, self.work.review.start(values)["task_id"])
                self.assertEqual(newer["status"], "completed")
            finally:
                release.set()
            self.assertEqual(wait_task(self.work, key)["status"], "completed")
        self.assertEqual(self.db.get("approval_case", "1001")["case"]["verify_code"], "1")
        self.assertEqual(self.db.get("approval_part", "1001:detail")["payload"]["verify_code"], "1")
        self.assertEqual(self.db.item(key, "detail")["payload"]["verify_code"], "4")

    def test_older_query_cannot_replace_newer_query_cache(self):
        self.query()
        entered, release = threading.Event(), threading.Event()
        original = self.work.gateway.connection.reviews.get_cases
        requests = []

        def changing(filters):
            requests.append(filters)
            cases = original(filters)
            return cases if len(requests) == 1 else [replace(c, verify_code="1") for c in cases]

        def delayed(value):
            if isinstance(value, list) and value[0].verify_code == "4":
                entered.set()
                release.wait(5)
            return clean(value)

        with patch("opd_monitor.approvals.clean", side_effect=delayed), patch.object(
                self.work.gateway.connection.reviews, "get_cases", changing):
            key = self.work.review.start({"kind": "approval_search", "force": True})["task_id"]
            try:
                self.assertTrue(entered.wait(3))
                newer = wait_task(self.work, self.work.review.start({
                    "kind": "approval_search", "force": True})["task_id"])
                self.assertEqual(newer["status"], "completed")
            finally:
                release.set()
            self.assertEqual(wait_task(self.work, key)["status"], "completed")
        cached = self.query()
        self.assertEqual(len(requests), 2)
        self.assertEqual(self.work.approvals.results({"id": cached["id"]})["cases"][0]["verify_code"], "1")
        self.assertEqual(self.work.approvals.results({"id": key})["cases"][0]["verify_code"], "4")

    def test_resume_cannot_overlap_another_earnings_job(self):
        self.configure()
        previous = self.task(kind="earnings_options")
        self.db.save("task", {**previous, "status": "paused"})
        entered, release = threading.Event(), threading.Event()
        original = self.work.gateway.connection.earnings.get_report

        def slow(context, period):
            entered.set()
            release.wait(5)
            return original(context, period)

        with patch.object(self.work.gateway.connection.earnings, "get_report", slow):
            key = self.work.review.start({"kind": "earnings_capture", "all_available": True})["task_id"]
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaisesRegex(ValueError, "處理中"):
                    self.work.review.start({"resume": previous["id"]})
            finally:
                release.set()
            wait_task(self.work, key)

    def test_resume_cannot_overlap_another_approval_refresh(self):
        self.query()
        previous = self.task(kind="approval_refresh", ids=["1001"])
        entered, release = threading.Event(), threading.Event()
        original = self.work.gateway.connection.reviews.get_case

        def slow(ref):
            entered.set()
            release.wait(5)
            return original(ref)

        with patch.object(self.work.gateway.connection.reviews, "get_case", slow):
            key = self.work.review.start({"kind": "approval_refresh", "ids": ["1003"]})["task_id"]
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaisesRegex(ValueError, "執行或排隊"):
                    self.work.review.start({"resume": previous["id"]})
            finally:
                release.set()
            wait_task(self.work, key)
