"""Patient input routing and explicit all-specialty review behavior."""
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from opd_monitor.bot import BotApplication
from opd_monitor.selftest_bot import BotSyntheticSDK, wait_task
from opd_monitor.settings import Settings, today


class ToolExperienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        BotSyntheticSDK.calls = []
        BotSyntheticSDK.fail_case = ""
        BotSyntheticSDK.on_soap = None
        self.app = BotApplication(Settings(), Path(self.temp.name), BotSyntheticSDK)
        self.key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.key)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def resolve(self, text, **options):
        return wait_task(self.work, self.work.review.start({"kind": "resolve", "identifier_kind": "auto",
            "identifiers": text, **options})["task_id"])

    def group(self):
        self.resolve("00012345")
        return self.work.review.save_set({"mrns": "00012345"})

    def test_auto_routes_numeric_and_letter_inputs_and_retains_leading_zeros(self):
        task = self.resolve("０００１２３４５\na123456789")
        rows = {r["input"]: r for r in task["items"]}
        self.assertEqual(rows["00012345"]["mrn"], "00012345")
        self.assertEqual(rows["00012345"]["identifier_kind"], "mrn")
        self.assertEqual(rows["a123456789"]["mrn"], "NEW000")
        self.assertEqual(rows["a123456789"]["identifier_kind"], "national_id")
        self.assertIn(("TEST", "identity", "A123456789"), BotSyntheticSDK.calls)
        self.assertFalse(any(c[1] == "index" for c in BotSyntheticSDK.calls))

    def test_auto_identity_failure_is_not_retried_as_mrn_and_can_resume(self):
        with patch.object(self.work.gateway.connection.patients, "resolve_identity", side_effect=ValueError("synthetic lookup failure")):
            task = self.resolve("A123456789\n00012345")
        self.assertEqual(task["status"], "partial")
        self.assertFalse(any(c[1:3] == ("profile", "A123456789") for c in BotSyntheticSDK.calls))
        with self.assertRaises(ValueError):
            self.work.review.save_set({"mrns": "A123456789"})
        before = BotSyntheticSDK.calls.count(("TEST", "profile", "00012345"))
        retried = wait_task(self.work, self.work.review.start({"resume": task["id"]})["task_id"])
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(BotSyntheticSDK.calls.count(("TEST", "profile", "00012345")), before)

    def test_all_departments_uses_newest_outpatient_without_confirmation(self):
        group = self.group()
        group["members"][0]["registrations"] = [{"visit_date": today().isoformat(), "section_code": "", "section_name": "unknown"}]
        self.work.review.db.save("set", group)
        self.assertTrue(self.work.review.scope({"set_id": group["id"]})["needs_confirmation"])
        options = {"set_id": group["id"], "department_filter": "all"}
        self.assertFalse(self.work.review.scope(options)["needs_confirmation"])
        task = wait_task(self.work, self.work.review.start(options)["task_id"])
        self.assertEqual(task["department_filter"], "all")
        self.assertEqual([r["case_no"] for r in task["items"][0]["records"]], ["OTHER"])
        self.assertEqual(task["items"][0]["records"][0]["section"], "內科")

    def test_all_departments_keeps_same_day_records_but_excludes_inpatient_and_future(self):
        group = self.group()
        original = self.work.gateway.connection.records.get_visit_cases

        def visits(mrn):
            cases = original(mrn)
            other = next(c for c in cases if c.case_no == "OTHER")
            return [*cases, replace(other, case_no="NO_SECTION", section_code="", section_name=""),
                    replace(other, case_no="INPATIENT", case_type="I"),
                    replace(other, case_no="FUTURE", visit_date=today()+timedelta(days=1))]

        with patch.object(self.work.gateway.connection.records, "get_visit_cases", side_effect=visits):
            task = wait_task(self.work, self.work.review.start({"set_id": group["id"], "department_filter": "all"})["task_id"])
        self.assertEqual({r["case_no"] for r in task["items"][0]["records"]}, {"OTHER", "NO_SECTION"})

    def test_all_departments_registration_still_obeys_exact_registration_source(self):
        group = self.group()
        group["members"][0]["registrations"] = [{"visit_date": (today()-timedelta(days=1)).isoformat(), "section_code": "70", "section_name": "眼科"}]
        self.work.review.db.save("set", group)
        task = wait_task(self.work, self.work.review.start({"set_id": group["id"], "mode": "registration", "department_filter": "all"})["task_id"])
        self.assertEqual({r["case_no"] for r in task["items"][0]["records"]}, {"ONE", "TWO"})

    def test_invalid_scope_is_rejected_and_another_account_cannot_use_the_set(self):
        group = self.group()
        for value in ("unknown", None, True, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.work.review.start({"set_id": group["id"], "department_filter": value})
        other = self.app.login({"username": "SECOND", "password": "synthetic"})["account"]["id"]
        with self.assertRaises(ValueError):
            self.app.workspace(other).review.start({"set_id": group["id"], "department_filter": "all"})

    def test_review_options_are_saved_per_account_and_validated(self):
        prefs = self.work.review.preferences()
        options = {"department_filter": "all", "department": "oph", "mode": "latest",
                   "cutoff_mode": "today", "cutoff": today().isoformat(), "retrieval": "refresh"}
        self.work.review.preferences({**prefs, "review_options": options})
        self.assertEqual(self.work.review.preferences()["review_options"], options)
        for invalid in ({"department_filter": []}, {"mode": "unknown"}, {"cutoff_mode": "date", "cutoff": "bad"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.work.review.preferences({**prefs, "review_options": invalid})
        other = self.app.login({"username": "SECOND", "password": "synthetic"})["account"]["id"]
        self.assertNotIn("review_options", self.app.workspace(other).review.preferences())
        self.assertEqual(self.work.review.preferences()["review_options"], options)
