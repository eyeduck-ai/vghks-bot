"""SOAP department substring matching and a complete, unpaged approval case view."""
import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from opd_monitor.analysis_fetch import clean
from opd_monitor.bot import BotApplication
from opd_monitor.selftest_bot import BotSyntheticSDK, wait_task
from opd_monitor.settings import Settings, today


class ReviewScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = BotApplication(Settings(), Path(self.temp.name), BotSyntheticSDK)
        key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(key)
        run = self.work.review.start({"kind": "resolve", "identifiers": "TEST001"})["task_id"]
        wait_task(self.work, run)
        self.group = self.work.review.save_set({"mrns": "TEST001"})

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def sources(self, *sources):
        group = copy.deepcopy(self.group)
        group["members"][0]["registrations"] = [{"visit_date": today().isoformat(),
            "section_code": code, "section_name": name} for code, name in sources]
        self.work.review.db.save("set", group)

    def options(self, **values):
        return {"set_id": self.group["id"], **values}

    def test_default_keyword_matches_each_visit_department_without_confirmation(self):
        before = list(BotSyntheticSDK.calls)
        self.sources(("70", "眼科約診下午"), ("10", "內科"), ("", ""))
        self.assertEqual(before, BotSyntheticSDK.calls)
        task = wait_task(self.work, self.work.review.start(self.options())["task_id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["department_keyword"], "眼科")
        self.assertEqual({r["section"] for r in task["items"][0]["records"]}, {"眼科下午", "眼科約診"})

    def test_custom_keyword_is_a_substring_of_visit_name(self):
        self.sources(("10", "內科"))
        task = wait_task(self.work, self.work.review.start(self.options(department_keyword=" 約診 "))["task_id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["department_keyword"], "約診")
        self.assertEqual([r["case_no"] for r in task["items"][0]["records"]], ["ONE"])

    def test_multiple_keywords_match_either_visit_name_or_code(self):
        original = self.work.gateway.connection.records.get_visit_cases
        def visits(mrn):
            return [replace(case, section_name="病例計酬") if case.case_no == "ONE" else
                replace(case, section_name="外科", section_code="病例計酬") if case.case_no == "TWO" else case
                for case in original(mrn)]
        with patch.object(self.work.gateway.connection.records, "get_visit_cases", side_effect=visits):
            task = wait_task(self.work, self.work.review.start(self.options(
                department_keywords=["眼科", "病例計酬"]))["task_id"])
        self.assertEqual(task["department_keywords"], ["眼科", "病例計酬"])
        self.assertEqual({r["case_no"] for r in task["items"][0]["records"]}, {"ONE", "TWO"})
        self.assertNotIn("OTHER", [r["case_no"] for r in task["items"][0]["records"]])

    def test_multiple_keywords_preference_and_registration_mode(self):
        from datetime import timedelta
        options = self.work.review.preferences({"review_options": {
            "department_keywords": [" 眼科 ", "眼科", "病例計酬", ""], "mode": "registration"}})
        self.assertEqual(options["review_options"]["department_keywords"], ["眼科", "病例計酬"])
        self.sources(("70", "眼科"))
        group = self.work.review.db.get("set", self.group["id"])
        group["members"][0]["registrations"][0]["visit_date"] = (today()-timedelta(days=1)).isoformat()
        self.work.review.db.save("set", group)
        original = self.work.gateway.connection.records.get_visit_cases
        with patch.object(self.work.gateway.connection.records, "get_visit_cases", side_effect=lambda mrn: [
            replace(case, section_name="病例計酬") if case.case_no == "ONE" else case for case in original(mrn)]):
            task = wait_task(self.work, self.work.review.start(self.options(mode="registration"))["task_id"])
        self.assertEqual(task["department_keywords"], ["眼科", "病例計酬"])
        self.assertIn("ONE", [r["case_no"] for r in task["items"][0]["records"]])
        with self.assertRaisesRegex(ValueError, "至少一個"):
            self.work.review.start(self.options(department_keywords=[" ", ""]))
        with self.assertRaisesRegex(ValueError, "最多十個"):
            self.work.review.start(self.options(department_keywords=[str(i) for i in range(11)]))

    def test_registration_mode_uses_source_visit_and_keyword(self):
        from datetime import timedelta
        group = copy.deepcopy(self.group)
        group["members"][0]["registrations"] = [{"visit_date": (today()-timedelta(days=1)).isoformat(),
            "section_code": "70", "section_name": "眼科"}]
        self.work.review.db.save("set", group)
        task = wait_task(self.work, self.work.review.start(self.options(mode="registration", department_keyword="約診"))["task_id"])
        self.assertEqual([r["case_no"] for r in task["items"][0]["records"]], ["ONE"])

    def test_linked_old_mrn_is_used_for_soap_and_numeric_report(self):
        original = self.work.gateway.connection.records.get_visit_cases
        def linked(mrn):
            return [replace(case, mrn="OLD001", lookup_mrn=mrn) if case.case_no == "ONE" else case
                    for case in original(mrn)]
        with patch.object(self.work.gateway.connection.records, "get_visit_cases", side_effect=linked):
            task = wait_task(self.work, self.work.review.start(self.options())["task_id"])
            self.assertEqual(task["status"], "completed")
            record = next(record for record in task["items"][0]["records"] if record["case_no"] == "ONE")
            self.assertEqual(record["mrn"], "TEST001")
            self.assertEqual(record["case"]["mrn"], "OLD001")
            saved = self.work.store.library.get_record(record["id"])
            self.assertEqual(saved["source_mrn"], "OLD001")
            numeric = wait_task(self.work, self.work.review.start({"kind": "numeric", "mrn": "TEST001",
                "record_id": record["id"]})["task_id"])
            self.assertEqual(numeric["status"], "completed")
            self.assertEqual(numeric["items"][0]["payload"]["case"]["mrn"], "OLD001")
        self.assertIn(("TEST", "numeric", "OLD001"), BotSyntheticSDK.calls)
        self.assertFalse(any(call[1:3] == ("index", "OLD001") for call in BotSyntheticSDK.calls))

    def test_approval_view_includes_every_case_and_filters_locally(self):
        base = clean(self.work.gateway.connection._review_rows()[0])
        for index in range(245):
            case = copy.deepcopy(base)
            case["reference"]["apply_seq"] = str(5000+index)
            case["fields"]["ApplySeq"] = str(5000+index)
            case["mrn"] = "TEST001" if index % 2 else "TEST002"
            self.work.approvals.tracker.observe(case)
        before = list(BotSyntheticSDK.calls)
        all_cases = self.work.approvals.cases()
        self.assertEqual(len(all_cases["rows"]), 245)
        filtered = self.work.approvals.cases({"mrn": "TEST001"})
        self.assertEqual(len(filtered["rows"]), 122)
        self.assertEqual(filtered["total"], len(filtered["rows"]))
        self.assertEqual(before, BotSyntheticSDK.calls)


if __name__ == "__main__":
    unittest.main()
