"""Patient input routing and simple SOAP department filtering."""
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

    def test_manual_batch_can_review_without_registration_source(self):
        resolved = self.resolve("000101 000102")
        self.assertEqual(resolved["status"], "completed")
        group = self.work.review.save_set({"mrns": "000101\n000102"})
        self.assertEqual([member["mrn"] for member in group["members"]], ["000101", "000102"])
        self.assertTrue(all(not member["registrations"] for member in group["members"]))
        task = wait_task(self.work, self.work.review.start({
            "set_id": group["id"], "mode": "latest", "department_keywords": ["眼科"],
        })["task_id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual([item["mrn"] for item in task["items"]], ["000101", "000102"])
        self.assertTrue(all(item["records"] for item in task["items"]))

    def test_edited_selection_preserves_source_members_without_changing_saved_set(self):
        group = self.group()
        member = group["members"][0]
        member["registrations"] = [{"visit_date": today().isoformat(), "section_name": "眼科", "sequence_no": "007"}]
        member["source_records"] = ["saved-record"]
        self.work.review.db.save("set", group)
        self.resolve("000101")
        edited = self.work.review.save_set({
            "source_set_id": group["id"], "mrns": "00012345\n000101", "name": "本次選取",
        })
        self.assertNotEqual(edited["id"], group["id"])
        self.assertEqual([row["mrn"] for row in edited["members"]], ["00012345", "000101"])
        self.assertEqual(edited["members"][0]["registrations"][0]["sequence_no"], "007")
        self.assertEqual(edited["members"][0]["source_records"], ["saved-record"])
        self.assertEqual(len(self.work.review.db.get("set", group["id"])["members"]), 1)
        other = self.app.login({"username": "SECOND", "password": "synthetic"})["account"]["id"]
        with self.assertRaises(ValueError):
            self.app.workspace(other).review.save_set({"source_set_id": group["id"], "mrns": "00012345"})

    def test_custom_keyword_uses_newest_matching_outpatient_without_confirmation(self):
        group = self.group()
        group["members"][0]["registrations"] = [{"visit_date": today().isoformat(), "section_code": "", "section_name": "unknown"}]
        self.work.review.db.save("set", group)
        options = {"set_id": group["id"], "department_keyword": "內科"}
        task = wait_task(self.work, self.work.review.start(options)["task_id"])
        self.assertEqual(task["department_keyword"], "內科")
        self.assertEqual([r["case_no"] for r in task["items"][0]["records"]], ["OTHER"])
        self.assertEqual(task["items"][0]["records"][0]["section"], "內科")

    def test_keyword_excludes_missing_department_inpatient_and_future(self):
        group = self.group()
        original = self.work.gateway.connection.records.get_visit_cases

        def visits(mrn):
            cases = original(mrn)
            other = next(c for c in cases if c.case_no == "OTHER")
            return [*cases, replace(other, case_no="NO_SECTION", section_code="", section_name=""),
                    replace(other, case_no="INPATIENT", case_type="I"),
                    replace(other, case_no="FUTURE", visit_date=today()+timedelta(days=1))]

        with patch.object(self.work.gateway.connection.records, "get_visit_cases", side_effect=visits):
            task = wait_task(self.work, self.work.review.start({"set_id": group["id"], "department_keyword": "內科"})["task_id"])
        self.assertEqual({r["case_no"] for r in task["items"][0]["records"]}, {"OTHER"})

    def test_registration_obeys_exact_source_and_department_keyword(self):
        group = self.group()
        group["members"][0]["registrations"] = [{"visit_date": (today()-timedelta(days=1)).isoformat(), "section_code": "70", "section_name": "眼科"}]
        self.work.review.db.save("set", group)
        task = wait_task(self.work, self.work.review.start({"set_id": group["id"], "mode": "registration", "department_keyword": "眼科"})["task_id"])
        self.assertEqual({r["case_no"] for r in task["items"][0]["records"]}, {"ONE", "TWO"})
        other = wait_task(self.work, self.work.review.start({"set_id": group["id"], "mode": "registration", "department_keyword": "內科"})["task_id"])
        self.assertEqual(other["items"][0]["records"], [])

    def test_legacy_group_task_resumes_with_its_saved_department_scope(self):
        group = self.group()
        BotSyntheticSDK.fail_case = "ONE"
        partial = wait_task(self.work, self.work.review.start({"set_id": group["id"]})["task_id"])
        self.assertEqual(partial["status"], "partial")
        saved = self.work.review.db.get("task", partial["id"])
        saved.pop("department_keyword")
        saved.update(department_filter="same", department={"id": "oph", "name": "眼科", "codes": [],
            "names": ["眼科上午", "眼科下午", "眼科約診", "眼科"]})
        self.work.review.db.save("task", saved)
        self.work.review.preferences({"review_options": {"department_keyword": "內科"}})
        BotSyntheticSDK.fail_case = ""
        resumed = wait_task(self.work, self.work.review.start({"resume": partial["id"]})["task_id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual({r["case_no"] for r in resumed["items"][0]["records"]}, {"ONE", "TWO"})

    def test_invalid_keyword_is_rejected_and_another_account_cannot_use_the_set(self):
        group = self.group()
        for value in ("", " ", None, True, [], {}, "眼科\n內科", "x"*101):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.work.review.start({"set_id": group["id"], "department_keyword": value})
        other = self.app.login({"username": "SECOND", "password": "synthetic"})["account"]["id"]
        with self.assertRaises(ValueError):
            self.app.workspace(other).review.start({"set_id": group["id"], "department_keyword": "眼科"})

    def test_review_options_are_saved_per_account_and_validated(self):
        prefs = self.work.review.preferences()
        options = {"department_keyword": " 內科 ", "mode": "latest",
                   "cutoff_mode": "today", "cutoff": today().isoformat(), "retrieval": "refresh"}
        self.work.review.preferences({**prefs, "review_options": options})
        self.assertEqual(self.work.review.preferences()["review_options"], {**options, "department_keyword": "內科"})
        for invalid in ({"department_keyword": []}, {"mode": "unknown"}, {"cutoff_mode": "date", "cutoff": "bad"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.work.review.preferences({**prefs, "review_options": invalid})
        other = self.app.login({"username": "SECOND", "password": "synthetic"})["account"]["id"]
        self.assertEqual(self.app.workspace(other).review.preferences()["review_options"]["department_keyword"], "眼科")
        self.assertEqual(self.work.review.preferences()["review_options"]["department_keyword"], "內科")
