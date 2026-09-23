import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vghks_sdk.models.review import ReviewCasePart, ReviewCaseRef

from opd_monitor.bot import BotApplication
from opd_monitor.selftest_bot import BotSyntheticSDK, wait_task
from opd_monitor.settings import Settings


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = BotApplication(Settings(), Path(self.temp.name), BotSyntheticSDK)
        self.key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.key)
        BotSyntheticSDK.calls = []

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def run_task(self, kind="approval_search", **values):
        key = self.work.review.start({"kind": kind, **values})["task_id"]
        return wait_task(self.work, key)

    def results(self, task, **values):
        return self.work.approvals.results({"id": task["id"], **values})

    def test_query_preserves_distinct_cases_and_status_meanings(self):
        task = self.run_task()
        self.assertEqual(task["status"], "completed")
        rows = self.results(task)["cases"]
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["mrn"] for r in rows].count("TEST001"), 2)
        self.assertEqual([r["review_label"] for r in rows], ["補件", "同意備查", "未辨識"])
        self.assertEqual([r["approved"] for r in rows], [None, True, None])
        self.assertEqual(rows[0]["application_status"], "已送件")
        self.assertIn("合成病人", rows[0]["patient_name"])
        self.assertEqual([c[1] for c in BotSyntheticSDK.calls], ["review-cases"])

    def test_cache_force_and_immutable_query_snapshots(self):
        original = self.run_task()
        cached = self.run_task()
        self.assertTrue(self.results(cached)["cached"])
        self.assertEqual(len(BotSyntheticSDK.calls), 1)
        self.run_task(force=True)
        self.assertEqual(len(BotSyntheticSDK.calls), 2)
        empty = self.run_task(filters={"mrn": "EMPTY"})
        self.assertEqual(self.results(empty)["total"], 0)
        before = list(BotSyntheticSDK.calls)
        self.assertEqual(self.results(original, q="TEST001")["total"], 2)
        self.assertEqual(self.results(original, decision="4")["total"], 1)
        self.assertEqual(self.results(original, offset=2, limit=1)["cases"][0]["verify_code"], "9")
        self.assertEqual(BotSyntheticSDK.calls, before)

    def test_lazy_parts_retry_only_failed_parts(self):
        self.run_task()
        self.assertFalse(any(self.work.approvals.detail({"apply_seq": "1001"})["parts"].values()))
        real = BotSyntheticSDK.review_part

        def fail_orders(sdk, ref, kind):
            if kind == "orders":
                raise ValueError("synthetic failure")
            return real(sdk, ref, kind)

        with patch.object(BotSyntheticSDK, "review_part", fail_orders):
            task = self.run_task("approval_case", apply_seq="1001", parts=["orders", "attachments", "pacs"])
        self.assertEqual(task["status"], "partial")
        before = list(BotSyntheticSDK.calls)
        self.work.review.start({"resume": task["id"]})
        self.assertEqual(wait_task(self.work, task["id"])["status"], "completed")
        self.assertEqual(BotSyntheticSDK.calls[len(before):], [("TEST", "review-orders", "1001")])

    def test_account_isolation_and_offline_reads(self):
        original = self.run_task()
        other = self.app.login({"username": "OTHER", "password": "synthetic"})["account"]["id"]
        self.assertEqual(self.app.workspace(other).approvals.overview()["queries"], [])
        with self.assertRaises(ValueError):
            self.app.workspace(other).approvals.detail({"apply_seq": "1001"})
        self.app.logout(self.key)
        self.app.offline(self.key)
        before = list(BotSyntheticSDK.calls)
        self.assertEqual(self.results(original)["total"], 3)
        self.assertEqual(BotSyntheticSDK.calls, before)
        with self.assertRaises(ValueError):
            self.run_task()

    def test_validation_and_failed_query_preserve_saved_results(self):
        for filters in ({"mrn": "<script>"}, {"start_date": "bad"}, {"doctor_card": ""},
                        {"start_date": "2026-02-02", "end_date": "2026-01-01"}):
            with self.assertRaises(ValueError):
                self.run_task(filters=filters)
        original = self.run_task()
        task = self.run_task(filters={"mrn": "FAIL"})
        self.assertEqual(task["status"], "failed")
        self.assertEqual(self.results(original)["total"], 3)

    def test_mismatched_reference_never_replaces_cached_part(self):
        self.run_task()
        self.run_task("approval_case", apply_seq="1001", parts=["orders"])
        before = self.work.approvals.detail({"apply_seq": "1001"})["parts"]["orders"]
        with patch.object(BotSyntheticSDK, "review_part", return_value=ReviewCasePart(ReviewCaseRef("9999"), "orders", (), 0)):
            task = self.run_task("approval_case", apply_seq="1001", parts=["orders"], force=True)
        self.assertEqual(task["status"], "partial")
        self.assertEqual(self.work.approvals.detail({"apply_seq": "1001"})["parts"]["orders"], before)

    def test_options_cache(self):
        task = self.run_task("approval_options", department="70")
        self.assertEqual(task["items"][0]["options"]["InsuSectNo"][0]["Text"], "眼科")
        before = list(BotSyntheticSDK.calls)
        self.run_task("approval_options", department="70")
        self.assertEqual(BotSyntheticSDK.calls, before)
