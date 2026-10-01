import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from vghks_sdk import OutpatientPatient, SoapRecord

from vghks_bot.jobs import Application, BusyError
from vghks_bot.library import current_cache, registration_id
from vghks_bot.selftest import SyntheticSDK
from vghks_bot.settings import Settings, today
from vghks_bot.storage import Journal, StorageError


class CountingSDK(SyntheticSDK):
    calls = []
    suffix = ""

    def __init__(self, settings):
        super().__init__(settings)
        self.calls.append(("login", self.card))

    def patients(self, card, day):
        self.calls.append(("list", card, str(day)))
        return super().patients(card, day)

    def visits(self, mrn):
        self.calls.append(("visits", self.card, mrn))
        return super().visits(mrn)

    def soap(self, case):
        self.calls.append(("soap", self.card, case.mrn, str(case.visit_date)))
        value = super().soap(case)
        return replace(value, blocks=(*value.blocks, self.suffix))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        CountingSDK.calls = []
        CountingSDK.suffix = ""
        self.app = Application(Settings(), self.path, CountingSDK)
        self.key = next(iter(self.app.accounts))
        self.app.save_accounts({"accounts": [{"id": self.key, "username": "DOC1", "password": "private-test-password", "start": "2026-09-18"}]})

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def wait(self, reply):
        self.assertTrue(self.app.idle.wait(10))
        value = self.app.snapshot(reply["run_ids"][0])
        self.assertEqual(value["status"], "completed", value.get("issues"))
        return value

    def browse(self, **kwargs):
        return self.wait(self.app.browse({"account_ids": [self.key], **kwargs}))

    def rows(self):
        data = self.app.patient_list({"account_id": self.key})
        return [r for day in data["days"] for r in day["rows"]]

    def fetch(self, rows=None, **kwargs):
        return self.wait(self.app.fetch_selected({"selections": [{"account_id": self.key, "rows": [{"day": r["day"], "id": r["id"]} for r in (self.rows() if rows is None else rows)]}], **kwargs}))

    def test_list_stage_never_reads_soap_and_revisit_uses_no_network(self):
        self.browse()
        self.assertEqual([r[0] for r in CountingSDK.calls], ["login", "list"])
        self.assertEqual(len(self.rows()), 4)
        self.assertEqual(self.app.library_search({})["total"], 0)
        CountingSDK.calls.clear()
        cached = self.browse()
        self.assertEqual(cached["counts"]["days_cached"], 1)
        self.assertEqual(CountingSDK.calls, [])

    def test_outpatient_sequence_is_cached_and_distinguishes_same_patient_registrations(self):
        def two_registrations(_, card, day):
            CountingSDK.calls.append(("list", card, str(day)))
            return [OutpatientPatient("TEST001", "測試病人", day, section_code="70", room="02",
                doctor_card=card, doctor_label_present=True, sequence_no=number)
                for number in ("003", "004")]

        with patch.object(CountingSDK, "patients", two_registrations):
            self.browse()
        rows = self.rows()
        self.assertEqual([row["sequence_no"] for row in rows], ["003", "004"])
        self.assertEqual(len({row["id"] for row in rows}), 2)
        self.assertEqual([call[0] for call in CountingSDK.calls], ["login", "list"])
        legacy = dict(rows[0])
        legacy.pop("sequence_no")
        self.assertEqual(registration_id(legacy), registration_id({**legacy, "sequence_no": ""}))

    def test_future_list_can_select_historical_analysis_but_cannot_fetch_future_soap(self):
        from analysis_fixtures import ExamSDK

        day = (today() + timedelta(days=7)).isoformat()
        self.app.save_accounts({"accounts": [{"id": self.key, "start": day}]})
        self.browse()
        rows = self.rows()
        self.assertTrue(rows)
        self.assertTrue(all(row["eligible"] and not row["soap_eligible"] for row in rows))
        self.assertEqual([call[0] for call in CountingSDK.calls], ["login", "list"])
        with self.assertRaisesRegex(ValueError, "未來掛號尚無當日 SOAP"):
            self.fetch(rows[:1])
        self.assertFalse(any(c[0] in {"soap", "visits"} for c in CountingSDK.calls))
        cohort = self.app.analysis.save_cohort({"source": "list", "account_id": self.key, "name": "future",
            "rows": [{"day": row["day"], "id": row["id"]} for row in rows[:1]]})
        self.assertEqual(cohort["members"][0]["source_registrations"][0]["day"], day)
        self.app.sdk_factory = ExamSDK
        ExamSDK.calls, ExamSDK.fail_numeric, ExamSDK.fail_pdf = [], False, False
        ExamSDK.cancel_callback = None
        run = self.wait(self.app.analysis.start({"cohort_id": cohort["id"], "modules": ["retina"]}))
        self.assertEqual(run["status"], "completed")
        result = self.app.analysis.results({"cohort_id": cohort["id"], "mrn": rows[0]["mrn"], "module": "retina"})
        self.assertTrue(result["numeric"])
        self.assertTrue(all(r["date"] < day for r in result["numeric"] if r["date"]))
        self.assertEqual(result["coverage"]["numeric_history_start"], (today() - timedelta(days=3650)).isoformat())
        self.assertEqual(self.app.delete_list({"account_id": self.key, "start": day})["deleted"], 1)

    def test_future_cache_expires_and_requires_final_refresh_after_clinic_date(self):
        with patch("vghks_bot.library.today", return_value=date(2026, 9, 24)), patch(
            "vghks_bot.library.timestamp", return_value="2026-09-24T12:01:00+08:00"
        ):
            self.assertTrue(current_cache("2026-09-30", "2026-09-24T12:00:30+08:00"))
            self.assertFalse(current_cache("2026-09-30", "2026-09-24T12:00:00+08:00"))
            self.assertFalse(current_cache("2026-09-30", "2026-09-24T12:02:00+08:00"))
        with patch("vghks_bot.library.today", return_value=date(2026, 10, 1)):
            self.assertFalse(current_cache("2026-09-30", "2026-09-24T12:00:00+08:00"))
            self.assertTrue(current_cache("2026-09-30", "2026-10-01T12:00:00+08:00"))

    def test_selection_reads_only_selected_patient_and_keeps_untagged(self):
        self.browse()
        result = self.fetch([r for r in self.rows() if r["mrn"] == "TEST003"])
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["records"][0]["matches"], [])
        self.assertEqual([r[2] for r in CountingSDK.calls if r[0] == "visits"], ["TEST003"])
        self.assertEqual(self.app.library_search({"q": "追蹤視力", "tag": "__untagged"})["total"], 1)

    def test_repeat_fetch_deduplicates_and_uses_zero_requests_including_no_visit(self):
        self.browse()
        self.fetch()
        CountingSDK.calls.clear()
        cached = self.fetch()
        self.assertEqual(cached["counts"]["soap_cached"], 3)
        self.assertEqual(CountingSDK.calls, [])
        self.assertEqual(self.app.library_search({})["total"], 3)
        self.assertEqual(self.app.library_search({})["patients"], 3)

    def test_multiple_dates_keep_encounters_and_group_by_patient_without_splitting_pages(self):
        self.app.save_accounts({"accounts": [{"id": self.key, "mode": "range", "start": "2026-09-17", "end": "2026-09-18"}]})
        self.browse()
        self.fetch()
        result = self.app.library_search({"group": "patient", "limit": 1})
        self.assertEqual((result["total"], result["patients"], len(result["records"])), (6, 3, 2))
        self.assertEqual(len({r["mrn"] for r in result["records"]}), 1)
        self.assertEqual(self.app.library_search({"tag": "surgery"})["total"], 4)

    def test_tags_and_fulltext_change_offline(self):
        self.browse()
        self.fetch()
        CountingSDK.calls.clear()
        self.app.save_settings({"categories": [{"id": "follow", "name": "追蹤", "keywords": ["Routine follow-up"], "parser": "none"}]})
        self.assertEqual(self.app.library_search({"tag": "follow"})["records"][0]["mrn"], "TEST003")
        self.assertEqual(self.app.library_search({"q": "SN60wf"})["total"], 1)
        self.assertEqual(self.app.library_search({"q": "%"})["total"], 0)
        self.assertEqual(CountingSDK.calls, [])

    def test_restart_keeps_cache_and_library_without_passwords(self):
        self.browse()
        self.fetch()
        self.app.close()
        self.app = Application(Settings(), self.path, CountingSDK)
        CountingSDK.calls.clear()
        self.browse()
        self.fetch()
        self.assertEqual(CountingSDK.calls, [])
        self.assertEqual(self.app.library_search({})["total"], 3)
        self.assertFalse(self.app.accounts[self.key].password)
        for file in self.path.rglob("*"):
            if file.is_file() and file.name != ".lock":
                self.assertNotIn(b"private-test-password", file.read_bytes())

    def test_refresh_preserves_old_soap_version(self):
        self.browse()
        self.fetch()
        record = self.app.library_search({"mrn": "TEST001"})["records"][0]
        CountingSDK.suffix = "Amended note"
        self.fetch(force=True)
        result = self.app.library_record(record["id"])
        self.assertEqual(result["version_count"], 2)
        self.assertIn("Amended note", result["soap"])
        self.assertTrue(any("Amended note" not in v["soap"] for v in result["versions"]))
        self.assertEqual(self.app.library_search({})["total"], 3)

    def test_delete_removes_all_versions_journal_copies_and_does_not_resurrect(self):
        self.browse()
        self.fetch()
        self.fetch()
        record = self.app.library_search({"mrn": "TEST001"})["records"][0]
        self.app.delete_records({"ids": [record["id"]]})
        self.assertEqual(self.app.library_search({})["total"], 2)
        for summary in self.app.history()["runs"]:
            self.assertNotIn(record["id"], [r["id"] for r in self.app.snapshot(summary["id"])["records"]])
        self.app.close()
        self.app = Application(Settings(), self.path, CountingSDK)
        self.assertEqual(self.app.library_search({})["total"], 2)
        self.assertIsNone(self.app.store.library.get_record(record["id"]))
        self.assertFalse(next(r for r in self.rows() if r["mrn"] == "TEST001")["saved"])

    def test_pending_delete_resumes_after_filesystem_failure(self):
        self.browse()
        self.fetch()
        record = self.app.library_search({})["records"][0]
        with patch("vghks_bot.storage.os.replace", side_effect=OSError("test disk failure")), self.assertRaises(StorageError):
            self.app.delete_records({"ids": [record["id"]]})
        self.assertEqual(self.app.library_search({})["total"], 2)
        self.app.close()
        self.app = Application(Settings(), self.path, CountingSDK)
        self.assertEqual(self.app.library_search({})["total"], 2)
        self.assertFalse(self.app.store.library.pending_deletions())

    def test_clear_list_cache_keeps_soap(self):
        self.browse()
        self.fetch()
        self.app.delete_list({"account_id": self.key, "start": "2026-09-18"})
        self.assertFalse(self.rows())
        self.assertEqual(self.app.library_search({})["total"], 3)

    def test_todays_list_expires_but_historical_list_does_not(self):
        self.browse()
        self.app.save_accounts({"accounts": [{"id": self.key, "start": today().isoformat()}]})
        self.browse()
        with self.app.store.library.connect() as db:
            db.execute("UPDATE list_cache SET fetched_at='2026-09-19T00:00:00+08:00'")
        CountingSDK.calls.clear()
        self.browse()
        self.assertEqual([r[0] for r in CountingSDK.calls], ["login", "list"])
        self.app.save_accounts({"accounts": [{"id": self.key, "start": "2026-09-18"}]})
        CountingSDK.calls.clear()
        self.browse()
        self.assertFalse(CountingSDK.calls)

    def test_account_cache_isolation_and_untrusted_selection_rejected(self):
        self.browse()
        refs = self.rows()
        self.app.save_accounts({"accounts": [{"id": self.key, "username": "DOC2", "password": "different"}]})
        self.assertFalse(self.rows())
        with self.assertRaises(ValueError):
            self.fetch(refs)
        self.assertEqual(self.app.library_search({})["total"], 0)

    def test_failed_and_wrong_date_lists_never_cached_empty_success_is_cached(self):
        with patch.object(CountingSDK, "patients", side_effect=RuntimeError("failed")):
            reply = self.app.browse({"account_ids": [self.key]})
            self.assertTrue(self.app.idle.wait(10))
            self.assertEqual(self.app.snapshot(reply["run_ids"][0])["status"], "partial")
        self.assertFalse(self.rows())
        with patch.object(CountingSDK, "patients", return_value=[]):
            self.browse()
        self.assertTrue(self.app.patient_list({"account_id": self.key})["days"][0]["cached"])
        CountingSDK.calls.clear()
        self.browse()
        self.assertFalse(CountingSDK.calls)

    def test_empty_soap_can_be_retried(self):
        self.browse()
        with patch.object(CountingSDK, "soap", side_effect=lambda case: SoapRecord(case, ("",))):
            result = self.app.fetch_selected({"selections": [{"account_id": self.key, "rows": [{"day": r["day"], "id": r["id"]} for r in self.rows()]}]})
            self.assertTrue(self.app.idle.wait(10))
            self.assertEqual(self.app.snapshot(result["run_ids"][0])["status"], "partial")
        self.assertFalse(next(r for r in self.rows() if r["mrn"] == "TEST001")["saved"])
        self.fetch()
        self.assertEqual(self.app.library_search({})["total"], 3)

    def test_delete_rejected_during_work_and_invalid_ids(self):
        self.app.idle.clear()
        with self.assertRaises(BusyError):
            self.app.delete_records({"ids": ["a" * 24]})
        self.app.idle.set()
        with self.assertRaises(ValueError):
            self.app.delete_records({"ids": ["../settings"]})

    def test_recover_current_journal_is_repeatable_and_preserves_untagged(self):
        self.browse()
        run = self.fetch()
        summary = {k: v for k, v in run.items() if k not in {"records", "issues"}}
        self.app.close()
        # Simulate a v2-only data folder: the journals remain authoritative input.
        (self.path / "clinical.sqlite3").unlink()
        Journal(self.path / "runs" / run["id"]).checkpoint(summary)
        self.app = Application(Settings(), self.path, CountingSDK)
        self.assertEqual(self.app.library_search({"tag": "__untagged"})["total"], 1)
        self.assertEqual(self.app.library_search({})["total"], 3)
        self.assertEqual(json.loads((self.path / "runs" / run["id"] / "run.json").read_text(encoding="utf-8"))["id"], run["id"])

    def test_cross_account_same_encounter_shares_soap_but_keeps_source_filters(self):
        self.browse()
        self.fetch()
        self.app.save_accounts({"accounts": [{"id": self.key, "username": "DOC2", "password": "different"}]})
        self.browse()
        CountingSDK.calls.clear()
        self.fetch()
        self.assertFalse([c for c in CountingSDK.calls if c[0] == "soap"])
        self.assertEqual(self.app.library_search({})["total"], 3)
        self.assertEqual(self.app.library_search({"account": "DOC1"})["total"], 3)
        self.assertEqual(self.app.library_search({"account": "DOC2"})["total"], 3)

    def test_browse_range_override_does_not_change_account_range(self):
        result = self.browse(ranges={self.key: {"start": "2026-09-16"}})
        self.assertEqual(result["start"], "2026-09-16")
        self.assertEqual(self.app.accounts[self.key].start, "2026-09-18")


    def test_previous_clinic_day_cache_refreshes_once_after_midnight(self):
        self.assertFalse(current_cache("2026-09-18", "2026-09-18T12:00:00+08:00"))
        self.assertTrue(current_cache("2026-09-18", "2026-09-19T12:00:00+08:00"))

    def test_database_failure_stops_list_requests(self):
        self.app.save_accounts({"accounts": [{"id": self.key, "mode": "range", "start": "2026-09-17", "end": "2026-09-18"}]})
        with patch.object(self.app.store.library, "save_list", side_effect=StorageError("test storage failure")):
            reply = self.app.browse({"account_ids": [self.key]})
            self.assertTrue(self.app.idle.wait(10))
            self.assertEqual(self.app.snapshot(reply["run_ids"][0])["status"], "failed")
        self.assertEqual(len([c for c in CountingSDK.calls if c[0] == "list"]), 1)
