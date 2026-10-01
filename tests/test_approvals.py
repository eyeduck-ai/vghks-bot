import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from vghks_sdk.models.review import ReviewCasePart, ReviewCaseRef

from vghks_bot.approvals import order_names
from vghks_bot.bot import BotApplication
from vghks_bot.selftest_bot import BotSyntheticSDK, wait_task
from vghks_bot.settings import Settings


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

    def test_sync_indexes_existing_cases_once_then_only_new_case(self):
        first = self.run_task("approval_sync")
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["progress"]["total"], 6)
        self.assertEqual(first["progress"]["done"], 6)
        self.assertEqual(sorted(c[2] for c in BotSyntheticSDK.calls if c[1] == "review-orders"), ["1001", "1002", "1003"])
        rows = self.work.approvals.cases()["rows"]
        self.assertEqual(self.work.approvals.cases()["orders_fetched"], 3)
        self.assertEqual({r["id"]: r["order_names"] for r in rows}["1002"], ["合成醫囑 1002"])
        self.assertEqual(self.run_task("approval_sync")["progress"]["total"], 3)
        self.assertEqual(len([c for c in BotSyntheticSDK.calls if c[1] == "review-orders"]), 3)

        original = self.work.gateway.connection._review_rows()
        added = replace(original[0], reference=ReviewCaseRef("1004"), mrn="TEST004", patient_name="新增病人",
                        fields={**original[0].fields, "ApplySeq": "1004", "PatNo": "TEST004", "PatName": "新增病人"})
        with patch.object(self.work.gateway.connection.reviews, "get_cases", return_value=original + [added]):
            self.assertEqual(self.run_task("approval_sync")["status"], "completed")
        self.assertEqual(sorted(c[2] for c in BotSyntheticSDK.calls if c[1] == "review-orders"),
                         ["1001", "1002", "1003", "1004"])
        self.assertEqual(self.work.approvals.cases({"order_name": "1004"})["rows"][0]["id"], "1004")

    def test_order_names_filter_sort_and_offline_cache(self):
        self.assertEqual(order_names({"rows": [{"OrderName": " Alpha "}, {"ordername": "alpha"},
                                               {"ORDERNAME": " Beta\nTest "}]}), ["Alpha", "Beta Test"])
        names = {"1001": ("Zulu", "Alpha"), "1002": ("Beta",), "1003": ()}
        def orders(ref):
            values = names[ref.apply_seq]
            return ReviewCasePart(ref, "orders", tuple({"ApplySeq": ref.apply_seq, "OrderName": name}
                                                       for name in values), len(values))
        with patch.object(self.work.gateway.connection.reviews, "get_orders", side_effect=orders):
            self.assertEqual(self.run_task("approval_sync")["status"], "completed")
        self.assertEqual([r["id"] for r in self.work.approvals.cases({"sort": "order_asc"})["rows"]],
                         ["1002", "1001", "1003"])
        self.assertEqual([r["id"] for r in self.work.approvals.cases({"sort": "order_desc"})["rows"]],
                         ["1001", "1002", "1003"])
        self.assertEqual([r["id"] for r in self.work.approvals.cases({"order_name": "alpha"})["rows"]], ["1001"])
        self.assertEqual([r["id"] for r in self.work.approvals.cases({"q": "zulu"})["rows"]], ["1001"])
        self.assertTrue(next(r for r in self.work.approvals.cases()["rows"] if r["id"] == "1003")["orders_fetched"])
        self.assertEqual(self.run_task("approval_sync")["progress"]["total"], 3)
        self.app.logout(self.key)
        self.app.offline(self.key)
        before = list(BotSyntheticSDK.calls)
        self.assertEqual(self.work.approvals.cases({"order_name": "beta"})["total"], 1)
        self.assertEqual(BotSyntheticSDK.calls, before)
        with self.assertRaises(ValueError):
            self.work.approvals.cases({"sort": "unknown"})

    def test_failed_order_lookup_resumes_only_missing_orders(self):
        original = self.work.gateway.connection.reviews.get_orders
        attempts = []
        def failing(ref):
            attempts.append(ref.apply_seq)
            if ref.apply_seq == "1002":
                raise RuntimeError("synthetic order failure")
            return original(ref)
        with patch.object(self.work.gateway.connection.reviews, "get_orders", side_effect=failing):
            task = self.run_task("approval_sync")
        self.assertEqual(task["status"], "partial")
        self.assertEqual(task["progress"]["failed"], 1)
        self.assertEqual(sorted(attempts), ["1001", "1002", "1003"])
        self.assertEqual(self.work.approvals.cases()["orders_fetched"], 2)
        before = len(BotSyntheticSDK.calls)
        resumed = wait_task(self.work, self.work.review.start({"resume": task["id"]})["task_id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["progress"]["failed"], 0)
        self.assertEqual([c[2] for c in BotSyntheticSDK.calls[before:] if c[1] == "review-orders"], ["1002"])
        self.assertEqual(self.work.approvals.cases()["orders_fetched"], 3)

    def test_individually_saved_orders_are_reused_by_sync_and_detail(self):
        self.run_task()
        self.run_task("approval_case", apply_seq="1001", parts=["orders"])
        self.assertEqual(self.run_task("approval_sync")["status"], "completed")
        self.assertEqual(sorted(c[2] for c in BotSyntheticSDK.calls if c[1] == "review-orders"),
                         ["1001", "1002", "1003"])
        before = len(BotSyntheticSDK.calls)
        self.run_task("approval_case", apply_seq="1001", parts=["orders"])
        self.assertFalse(any(c[1] == "review-orders" for c in BotSyntheticSDK.calls[before:]))

    def test_sync_rejects_orders_from_another_case(self):
        original = self.work.gateway.connection.reviews.get_orders
        def wrong(ref):
            if ref.apply_seq == "1002":
                return ReviewCasePart(ReviewCaseRef("9999"), "orders", ({"OrderName": "不屬於此案"},), 1)
            return original(ref)
        with patch.object(self.work.gateway.connection.reviews, "get_orders", side_effect=wrong):
            task = self.run_task("approval_sync")
        self.assertEqual(task["status"], "partial")
        self.assertIsNone(self.work.approvals.detail({"apply_seq": "1002"})["parts"]["orders"])
        self.assertEqual(self.work.approvals.cases()["orders_fetched"], 2)
