"""Synthetic birthdays survive manual resolution, reviews and cataract cohorts."""
import copy
import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from vghks_bot.bot import BotApplication
from vghks_bot.demographics import birthday_iso, merge_demographics
from vghks_bot.selftest_bot import BotSyntheticSDK, wait_task
from vghks_bot.settings import Settings


class BirthdayTests(unittest.TestCase):
    def test_common_roc_and_gregorian_formats_and_source_preservation(self):
        for raw in ("047/01/01", "47-1-1", "0470101", "470101", "19580101", "1958-01-01",
                    "1958/1/1", "1958.01.01", "民國47年1月1日", "西元1958年1月1日",
                    "１９５８／０１／０１", "1958-01-01T00:00:00+08:00", "1958-01-01 00:00:00",
                    date(1958, 1, 1), datetime(1958, 1, 1)):
            with self.subTest(raw=raw):
                self.assertEqual(birthday_iso(raw), "1958-01-01")
                original = {"mrn": "00012345", "birthday": raw}
                normalized = merge_demographics(original, on_date=date(2026, 10, 3))
                self.assertEqual(normalized["age"], "68")
                self.assertEqual(normalized["birthday_raw"], str(raw))
                self.assertEqual(original, {"mrn": "00012345", "birthday": raw})

    def test_invalid_missing_future_and_leap_dates_are_not_guessed(self):
        for raw in (None, "", "0000101", "0470230", "19580229", "19581301", "01/01/1958",
                    "1958", "unknown", "1958-01-01T25:00:00"):
            with self.subTest(raw=raw):
                self.assertEqual(birthday_iso(raw), "")
        self.assertEqual(birthday_iso("0890229"), "2000-02-29")
        for birthday in ("2026-10-04", "1880-01-01"):
            self.assertEqual(merge_demographics({"mrn": "X", "birthday": birthday, "age": "68"},
                                               on_date=date(2026, 10, 3))["age"], "")
        self.assertEqual(merge_demographics({"mrn": "X", "birthday": ""})["age"], "")

    def test_birthday_boundary_zero_and_registration_fallback(self):
        for birthday, age in (("1958-10-04", "67"), ("1958-10-03", "68"), ("2026-10-03", "0")):
            self.assertEqual(merge_demographics({"mrn": "X", "birthday": birthday, "age": "stale"},
                                               on_date=date(2026, 10, 3))["age"], age)
        value = {"mrn": "X", "registrations": [{"mrn": "X", "sex": "女", "age": "68歲"}]}
        self.assertEqual(merge_demographics(value)["age"], "68歲")
        self.assertEqual(merge_demographics(value)["sex"], "女")
        self.assertEqual(merge_demographics({"mrn": "X", "age": 0})["age"], 0)
        unrelated = {"mrn": "X", "records": [{"mrn": "Y", "name": "other", "birthday": "1958-01-01"}]}
        self.assertEqual(merge_demographics(unrelated)["birthday"], "")
        self.assertEqual(merge_demographics(unrelated)["name"], "")

    def test_shared_review_and_cataract_age_display(self):
        subprocess.run(["node", "tests/demographics_ui.js"], cwd=Path(__file__).resolve().parents[1],
                       check=True, capture_output=True, text=True, encoding="utf-8")


class RocSDK(BotSyntheticSDK):
    def demographics(self, mrn):
        return replace(super().demographics(mrn), birthday="047/01/01" if self.card == "TEST" else "0700101")


class DemographicFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        BotSyntheticSDK.calls = []
        self.app = BotApplication(Settings(), Path(self.temp.name), RocSDK)
        self.key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.key)
        self.mrn = "00012345"
        self.clock = patch("vghks_bot.demographics.today", return_value=date(2026, 10, 3))
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.app.close()
        self.temp.cleanup()

    def group(self):
        run = self.work.review.start({"kind": "resolve", "identifier_kind": "auto", "identifiers": self.mrn})["task_id"]
        resolved = wait_task(self.work, run)["items"][0]
        self.assertEqual(resolved["mrn"], self.mrn)
        self.assertEqual(resolved["birthday"], "1958-01-01")
        self.assertEqual(resolved["birthday_raw"], "047/01/01")
        self.assertEqual(resolved["age"], "68")
        return self.work.review.save_set({"mrns": self.mrn})

    def test_manual_profile_set_soap_review_and_cataract_bridge(self):
        group = self.group()
        self.assertEqual(group["members"][0]["age"], "68")
        run = self.work.review.start({"set_id": group["id"]})["task_id"]
        task = wait_task(self.work, run)
        self.assertEqual(task["members"][0]["birthday"], "1958-01-01")
        result = self.work.review.results({"id": run})["patients"][0]
        self.assertEqual(result["age"], "68")
        self.assertEqual(result["sex"], "女")
        self.assertTrue(result["records"])
        self.assertTrue(all(record["birthday"] == "1958-01-01" for record in result["records"]))
        cohort = self.work.review.cohort({"set_id": group["id"]})
        self.assertEqual(cohort["members"][0]["birthday"], "1958-01-01")
        self.assertEqual(cohort["members"][0]["sex"], "女")
        displayed = self.work.analysis.results({"cohort_id": cohort["id"], "mrn": self.mrn, "module": "cataract"})
        self.assertEqual(displayed["member"]["age"], "68")
        profile_calls = [call for call in BotSyntheticSDK.calls if call[1] == "profile"]
        self.work.review.profile(self.mrn)
        self.assertEqual([call for call in BotSyntheticSDK.calls if call[1] == "profile"], profile_calls)

    def test_legacy_raw_profile_and_empty_snapshots_recover_offline_without_writes(self):
        group = self.group()
        for key in ("name", "sex", "age", "birthday", "birthday_raw"):
            group["members"][0][key] = ""
        self.work.review.db.save("set", group)
        profile = self.work.review.db.get("profile", self.mrn)
        self.work.review.db.save("profile", {**profile, "birthday": "0470101", "birthday_raw": "", "age": ""})
        run = self.work.review.start({"set_id": group["id"]})["task_id"]
        wait_task(self.work, run)
        item = self.work.review.db.item(run, self.mrn)
        for key in ("name", "sex", "age", "birthday", "birthday_raw"):
            item[key] = ""
        self.work.review.db.item(run, self.mrn, item)
        cohort = self.work.review.cohort({"set_id": group["id"]})
        for key in ("name", "sex", "age", "birthday", "birthday_raw"):
            cohort["members"][0].pop(key, None)
        self.work.analysis.store.save_document("analysis_cohorts", cohort)
        self.app.offline(self.key)
        self.app.read_only = True
        calls = copy.deepcopy(BotSyntheticSDK.calls)
        with self.work.store.library.connect() as db:
            before = list(db.iterdump())
        with patch("vghks_bot.library.sqlite3.connect", wraps=sqlite3.connect) as connections:
            patient = self.work.review.results({"id": run})["patients"][0]
        self.assertEqual(connections.call_count, 1)
        self.assertEqual(patient["birthday"], "1958-01-01")
        self.assertEqual(patient["age"], "68")
        self.assertEqual(patient["sex"], "女")
        self.assertEqual(patient["name"], "合成病人 " + self.mrn)
        values = {"cohort_id": cohort["id"], "mrn": self.mrn, "module": "cataract"}
        self.assertEqual(self.work.analysis.results(values)["member"]["age"], "68")
        self.assertEqual(self.work.analysis.overview({})["cohorts"][0]["members"][0]["age"], "68")
        self.assertEqual(self.work.review.profile(self.mrn)["age"], "68")
        with self.work.store.library.connect() as db:
            self.assertEqual(list(db.iterdump()), before)
        self.assertEqual(BotSyntheticSDK.calls, calls)

    def test_copy_saved_set_and_library_review_sources_keep_demographics(self):
        group = self.group()
        group["members"][0].update(age="", birthday="", birthday_raw="", sex="")
        self.work.review.db.save("set", group)
        copied = self.work.review.save_set({"mrns": self.mrn, "source_set_id": group["id"]})
        self.assertEqual(copied["members"][0]["age"], "68")
        run = self.work.review.start({"set_id": copied["id"]})["task_id"]
        wait_task(self.work, run)
        with self.work.store.library.connect() as db:
            db.execute("DELETE FROM bot_documents WHERE kind='profile' AND id=?", (self.mrn,))
        review_group = self.work.review.save_set({"source": "review", "task_id": run, "all": True})
        self.assertEqual(review_group["members"][0]["birthday"], "1958-01-01")
        library_group = self.work.review.save_set({"source": "library", "filters": {"mrn": self.mrn}, "all": True})
        self.assertEqual(library_group["members"][0]["age"], "68")
        self.assertEqual(self.work.review.cohort({"set_id": library_group["id"]})["members"][0]["sex"], "女")

    def test_profile_update_invalidates_only_affected_cataract_patient(self):
        group = self.group()
        cohort = self.work.review.cohort({"set_id": group["id"]})
        before = self.work.analysis.store.patient_revision(self.mrn)
        unrelated = self.work.analysis.store.patient_revision("OTHER")
        profile = self.work.review.db.get("profile", self.mrn)
        self.work.review.db.save("profile", {**profile, "birthday": "1968-01-01", "birthday_raw": "0570101"})
        self.assertNotEqual(self.work.analysis.store.patient_revision(self.mrn), before)
        self.assertEqual(self.work.analysis.store.patient_revision("OTHER"), unrelated)
        member = self.work.analysis.results({"cohort_id": cohort["id"], "mrn": self.mrn, "module": "cataract"})["member"]
        self.assertEqual(member["birthday"], "1968-01-01")
        self.assertEqual(member["age"], "58")

    def test_accounts_and_mismatched_cached_profiles_do_not_cross(self):
        group = self.group()
        other_key = self.app.login({"username": "SECOND", "password": "synthetic"})["account"]["id"]
        other = self.app.workspace(other_key)
        other.review.profile(self.mrn)
        self.assertEqual(other.review.member_demographics({"mrn": self.mrn})["age"], "45")
        self.assertEqual(self.work.review.member_demographics({"mrn": self.mrn})["age"], "68")
        with self.assertRaises(ValueError):
            other.review.cohort({"set_id": group["id"]})
        self.work.review.db.save("profile", {"id": "BROKEN", "mrn": "UNRELATED", "status": "resolved",
                                            "name": "other", "birthday": "1958-01-01", "sex": "男"})
        self.assertEqual(self.work.review.member_demographics({"mrn": "BROKEN"})["birthday"], "")
        with self.assertRaises(ValueError):
            self.work.review.save_set({"mrns": "BROKEN"})
