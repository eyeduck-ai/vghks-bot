"""Local scope validation and a complete, unpaged approval case view."""
import copy
import tempfile
import unittest
from pathlib import Path

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

    def test_manual_patient_and_recognized_specialty_need_no_extra_confirmation(self):
        before = list(BotSyntheticSDK.calls)
        self.assertFalse(self.work.review.scope(self.options())["needs_confirmation"])
        self.sources(("70", "眼科約診下午"))
        self.assertFalse(self.work.review.scope(self.options())["needs_confirmation"])
        self.assertEqual(before, BotSyntheticSDK.calls)
        task = wait_task(self.work, self.work.review.start(self.options())["task_id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual({r["section"] for r in task["items"][0]["records"]}, {"眼科下午", "眼科約診"})

    def test_unknown_and_mixed_sources_require_confirmation_of_the_exact_scope(self):
        self.sources(("70", "眼科"), ("10", "內科"), ("", ""))
        scope = self.work.review.scope(self.options())
        self.assertTrue(scope["needs_confirmation"])
        self.assertEqual(len(scope["unmatched"]), 2)
        for values in ({}, {"department_confirmed": True},
                       {"department_confirmed": True, "department_confirmation": "stale"}):
            with self.assertRaises(ValueError):
                self.work.review.start(self.options(**values))
        task = wait_task(self.work, self.work.review.start(self.options(
            department_confirmed=True, department_confirmation=scope["confirmation_key"]))["task_id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["department_scope"], scope)
        self.assertTrue(all(r["section_code"] == "70" for r in task["items"][0]["records"]))

    def test_mapping_once_removes_warning_and_changed_rules_invalidate_confirmation(self):
        self.sources(("70", ""))
        scope = self.work.review.scope(self.options())
        self.assertTrue(scope["needs_confirmation"])
        prefs = self.work.review.preferences()
        prefs["departments"][0]["names"].append("眼科晚診")
        self.work.review.preferences(prefs)
        with self.assertRaises(ValueError):
            self.work.review.start(self.options(department_confirmed=True,
                department_confirmation=scope["confirmation_key"]))
        prefs["departments"][0]["codes"].append("70")
        self.work.review.preferences(prefs)
        self.assertFalse(self.work.review.scope(self.options())["needs_confirmation"])
        task = wait_task(self.work, self.work.review.start(self.options())["task_id"])
        prefs["departments"][0]["codes"].append("10")
        self.work.review.preferences(prefs)
        self.assertEqual(self.work.review.task(task["id"])["department"]["codes"], ["70"])

    def test_registration_mode_uses_source_and_does_not_require_specialty_confirmation(self):
        self.sources(("70", ""))
        self.assertFalse(self.work.review.scope(self.options(mode="registration"))["needs_confirmation"])
        task = wait_task(self.work, self.work.review.start(self.options(mode="registration"))["task_id"])
        self.assertIn(task["status"], {"completed", "partial"})

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
