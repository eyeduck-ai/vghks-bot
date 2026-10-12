"""Incremental reviews prove request savings, snapshot isolation and refresh scope."""
import copy
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from vghks_bot.bot import BotApplication
from vghks_bot.selftest_bot import BotSyntheticSDK, wait_task
from vghks_bot.settings import Settings, timestamp, today


class CurrentSoapSDK(BotSyntheticSDK):
    calls = []

    def visits(self, mrn):
        return [replace(case, visit_date=today()) if case.case_no in {"ONE", "TWO"} else case
                for case in super().visits(mrn)]


class IncrementalReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        CurrentSoapSDK.calls = []
        CurrentSoapSDK.fail_case = ""
        self.app = BotApplication(Settings(), Path(self.temp.name), CurrentSoapSDK)
        self.key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.key)
        wait_task(self.work, self.work.review.start({"kind": "resolve", "identifiers": "TEST001 TEST002 TEST003 TEST004"})["task_id"])
        CurrentSoapSDK.calls.clear()

    def tearDown(self):
        CurrentSoapSDK.fail_case = ""
        self.app.close()
        self.temp.cleanup()

    def run_review(self, *mrns, base=None, **values):
        group = self.work.review.save_set({"mrns": "\n".join(mrns)})
        request = {"set_id": group["id"], **values}
        if base:
            request["base_task_id"] = base["id"]
        return wait_task(self.work, self.work.review.start(request)["task_id"])

    def expire_caches(self):
        expired = today().isoformat() + "T00:00:00+08:00"
        with self.work.store.library.connect() as db:
            for row in db.execute("SELECT id,payload FROM bot_documents WHERE kind='visits'").fetchall():
                value = {**json.loads(row["payload"]), "updated_at": expired}
                db.execute("UPDATE bot_documents SET payload=?,updated_at=? WHERE kind='visits' AND id=?",
                           (json.dumps(value), expired, row["id"]))
            db.execute("UPDATE records SET updated_at=?", (expired,))

    def test_expired_caches_add_only_c_and_copy_independent_notes(self):
        old = self.run_review("TEST001", "TEST002")
        self.assertEqual(old["status"], "completed")
        self.work.review.save_note({"task_id": old["id"], "mrn": "TEST001", "text": "追蹤報告"})
        checked = {row["mrn"]: row["checked_at"] for row in old["items"]}
        original = copy.deepcopy(self.work.review.results({"id": old["id"]}))
        self.expire_caches()
        CurrentSoapSDK.calls.clear()
        new = self.run_review("TEST001", "TEST002", "TEST003", base=old)
        self.assertEqual(new["status"], "completed")
        self.assertEqual((new["new_count"], new["reused_count"], new["pending_count"]), (1, 2, 1))
        self.assertTrue(CurrentSoapSDK.calls)
        self.assertTrue(all(call[2] == "TEST003" for call in CurrentSoapSDK.calls))
        for row in new["items"]:
            if row["mrn"] in checked:
                self.assertEqual(row["checked_at"], checked[row["mrn"]])
                self.assertEqual(row["reuse_label"], "沿用今日結果")
        self.assertEqual(self.work.review.results({"id": old["id"]}), original)
        self.assertEqual(self.work.review.read_notes({"task_id": new["id"]})["patients"][0]["text"], "追蹤報告")
        self.work.review.save_note({"task_id": new["id"], "mrn": "TEST001", "text": "已完成"})
        self.assertEqual(self.work.review.read_notes({"task_id": old["id"]})["patients"][0]["text"], "追蹤報告")

    def test_repeated_inheritance_never_extends_query_timestamp(self):
        first = self.run_review("TEST001")
        second = self.run_review("TEST001", base=first)
        CurrentSoapSDK.calls.clear()
        third = self.run_review("TEST001", base=second)
        self.assertEqual(CurrentSoapSDK.calls, [])
        self.assertEqual(third["items"][0]["checked_at"], first["items"][0]["checked_at"])
        self.assertEqual(third["items"][0]["source_task_id"], first["id"])

    def test_inherited_results_and_notes_are_visible_while_newcomer_is_running(self):
        old = self.run_review("TEST001", "TEST002")
        self.work.review.save_note({"task_id": old["id"], "mrn": "TEST002", "text": "已核對"})
        group = self.work.review.save_set({"mrns": "TEST001 TEST002 TEST003"})
        entered, release = threading.Event(), threading.Event()
        def block(case):
            if case.patient_mrn == "TEST003":
                entered.set()
                release.wait(10)
        with patch.object(CurrentSoapSDK, "on_soap", block):
            key = self.work.review.start({"set_id": group["id"], "base_task_id": old["id"]})["task_id"]
            try:
                self.assertTrue(entered.wait(5))
                value = self.work.review.results({"id": key})
                self.assertEqual(value["incremental"], {"new": 1, "reused": 2, "pending": 1})
                self.assertEqual({row["mrn"] for row in value["patients"]}, {"TEST001", "TEST002", "TEST003"})
                self.assertTrue(all(self.work.review.db.item(key, mrn)["records"] for mrn in ("TEST001", "TEST002")))
                self.assertEqual(self.work.review.read_notes({"task_id": key})["patients"][1]["text"], "已核對")
            finally:
                release.set()
            self.assertEqual(wait_task(self.work, key)["status"], "completed")

    def test_single_refresh_requeries_only_target_and_preserves_scope(self):
        old = self.run_review("TEST001", "TEST002")
        CurrentSoapSDK.calls.clear()
        task = wait_task(self.work, self.work.review.start({
            "base_task_id": old["id"], "refresh_mrn": "TEST002"})["task_id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["cutoff"], old["cutoff"])
        self.assertTrue(all(call[2] == "TEST002" for call in CurrentSoapSDK.calls))
        self.assertIn(("TEST", "index", "TEST002"), CurrentSoapSDK.calls)
        self.assertTrue(any(call[1] == "soap" for call in CurrentSoapSDK.calls))
        self.assertTrue(next(row for row in task["items"] if row["mrn"] == "TEST001")["reused"])
        CurrentSoapSDK.calls.clear()
        resumed = wait_task(self.work, self.work.review.start({"resume": task["id"]})["task_id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(CurrentSoapSDK.calls, [])

    def test_force_refresh_all_requeries_every_patient(self):
        old = self.run_review("TEST001", "TEST002")
        CurrentSoapSDK.calls.clear()
        new = self.run_review("TEST001", "TEST002", base=old, force=True)
        self.assertEqual(new["reused_count"], 0)
        self.assertEqual({call[2] for call in CurrentSoapSDK.calls if call[1] == "index"}, {"TEST001", "TEST002"})
        self.assertEqual({call[2] for call in CurrentSoapSDK.calls if call[1] == "soap"}, {"TEST001", "TEST002"})

    def test_force_refresh_resume_keeps_completed_patient_and_successful_soap(self):
        entered, release = threading.Event(), threading.Event()
        old = self.run_review("TEST001", "TEST002")
        def pause_second(case):
            if case.patient_mrn == "TEST002" and case.case_no == "ONE":
                entered.set()
                release.wait(10)
        group = self.work.review.db.get("set", old["set_id"])
        with patch.object(CurrentSoapSDK, "on_soap", pause_second):
            key = self.work.review.start({"set_id": group["id"], "base_task_id": old["id"], "force": True})["task_id"]
            try:
                self.assertTrue(entered.wait(5))
                self.work.review.stop(key)
            finally:
                release.set()
            self.assertEqual(wait_task(self.work, key)["status"], "paused")
        CurrentSoapSDK.calls.clear()
        self.assertEqual(wait_task(self.work, self.work.review.start({"resume": key})["task_id"])["status"], "completed")
        self.assertTrue(CurrentSoapSDK.calls)
        self.assertTrue(all(call[2] == "TEST002" for call in CurrentSoapSDK.calls))
        self.assertFalse(any(call[1] == "soap" and call[3] == "ONE" for call in CurrentSoapSDK.calls))

    def test_next_taipei_day_rechecks_even_a_fresh_index(self):
        old = self.run_review("TEST001", "TEST002")
        CurrentSoapSDK.calls.clear()
        tomorrow = today() + timedelta(days=1)
        with patch("vghks_bot.review.today", return_value=tomorrow), patch("vghks_bot.review_incremental.today", return_value=tomorrow), patch("vghks_bot.settings.today", return_value=tomorrow):
            new = self.run_review("TEST001", "TEST002", base=old)
        self.assertEqual(new["reused_count"], 0)
        self.assertEqual({call[2] for call in CurrentSoapSDK.calls if call[1] == "index"}, {"TEST001", "TEST002"})

    def test_changed_keywords_and_registration_sources_do_not_inherit(self):
        old = self.run_review("TEST001")
        new = self.run_review("TEST001", base=old, department_keyword="約診")
        self.assertEqual(new["reused_count"], 0)
        group = self.work.review.db.get("set", old["set_id"])
        group["members"][0]["registrations"] = [{"visit_date": today().isoformat(), "section_code": "70"}]
        self.work.review.db.save("set", group)
        old = wait_task(self.work, self.work.review.start({"set_id": group["id"], "mode": "registration"})["task_id"])
        group["members"][0]["registrations"][0]["visit_date"] = (today()-timedelta(days=2)).isoformat()
        self.work.review.db.save("set", group)
        new = wait_task(self.work, self.work.review.start({"set_id": group["id"], "mode": "registration", "base_task_id": old["id"]})["task_id"])
        self.assertEqual(new["reused_count"], 0)

    def test_different_registration_sequence_or_mode_is_not_reused(self):
        old = self.run_review("TEST001")
        group = self.work.review.db.get("set", old["set_id"])
        group["members"][0]["registrations"] = [{"visit_date": today().isoformat(), "section_code": "70", "sequence_no": "001"}]
        self.work.review.db.save("set", group)
        registration = wait_task(self.work, self.work.review.start({"set_id": group["id"], "mode": "registration", "base_task_id": old["id"]})["task_id"])
        self.assertEqual(registration["reused_count"], 0)
        group["members"][0]["registrations"][0]["sequence_no"] = "002"
        self.work.review.db.save("set", group)
        new = wait_task(self.work, self.work.review.start({"set_id": group["id"], "mode": "registration", "base_task_id": registration["id"]})["task_id"])
        self.assertEqual(new["reused_count"], 0)

    def test_confirmed_no_visits_is_reused_but_failed_patient_runs_after_newcomer(self):
        old = self.run_review("TEST004")
        CurrentSoapSDK.calls.clear()
        new = self.run_review("TEST004", base=old)
        self.assertEqual(new["items"][0]["status"], "no_visit")
        self.assertEqual(CurrentSoapSDK.calls, [])
        CurrentSoapSDK.fail_case = "ONE"
        old = self.run_review("TEST001")
        self.assertEqual(old["status"], "partial")
        CurrentSoapSDK.fail_case = ""
        CurrentSoapSDK.calls.clear()
        new = self.run_review("TEST001", "TEST003", base=old)
        self.assertEqual(new["reused_count"], 0)
        self.assertEqual(CurrentSoapSDK.calls[0][2], "TEST003")

    def test_deleted_soap_is_not_implicitly_restored(self):
        old = self.run_review("TEST001")
        keys = [row["id"] for row in old["items"][0]["records"]]
        self.work.delete_records({"ids": keys})
        CurrentSoapSDK.calls.clear()
        new = self.run_review("TEST001", base=old)
        self.assertFalse(new["items"][0]["records"])
        self.assertEqual(CurrentSoapSDK.calls, [])
        refreshed = self.run_review("TEST001", base=new, force=True)
        self.assertEqual(refreshed["items"][0]["status"], "ready")
        self.assertTrue(refreshed["items"][0]["records"])

    def test_without_ancestry_preserves_legacy_new_review_after_deletion(self):
        old = self.run_review("TEST001")
        self.work.delete_records({"ids": [row["id"] for row in old["items"][0]["records"]]})
        CurrentSoapSDK.calls.clear()
        new = self.run_review("TEST001")
        self.assertEqual(new["items"][0]["status"], "ready")
        self.assertTrue(new["items"][0]["records"])
        self.assertTrue(any(call[1] == "soap" for call in CurrentSoapSDK.calls))

    def test_cross_account_and_invalid_target_fail_before_creating_task(self):
        old = self.run_review("TEST001")
        for values in ({"refresh_mrn": "TEST001"},
                       {"base_task_id": old["id"], "refresh_mrn": "TEST002"},
                       {"base_task_id": old["id"], "refresh_mrn": "TEST001", "force": True}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.work.review.start(values)
        other = self.app.login({"username": "OTHER", "password": "synthetic"})["account"]["id"]
        with self.assertRaises(ValueError):
            self.app.workspace(other).review.start({"base_task_id": old["id"], "refresh_mrn": "TEST001"})

    def test_legacy_result_without_query_time_is_rechecked(self):
        old = self.run_review("TEST001")
        row = old["items"][0]
        row.pop("checked_at")
        row.pop("index_checked_at")
        self.work.review.db.item(old["id"], row["mrn"], row)
        new = self.run_review("TEST001", base=old)
        self.assertEqual(new["reused_count"], 0)
        self.assertTrue(new["items"][0]["checked_at"] <= timestamp())
