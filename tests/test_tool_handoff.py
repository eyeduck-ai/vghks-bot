"""Reviewed patient handoff keeps account scope, fixed tasks and SOAP evidence."""
import copy
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from vghks_bot.bot import BotApplication
from vghks_bot.selftest_bot import BotSyntheticSDK, wait_task
from vghks_bot.settings import Settings
from vghks_bot.workbench import snapshot as workbench_snapshot


class ToolHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        BotSyntheticSDK.calls = []
        BotSyntheticSDK.revision = "original"
        BotSyntheticSDK.fail_case = ""
        BotSyntheticSDK.on_soap = None
        self.app = BotApplication(Settings(), Path(self.temp.name), BotSyntheticSDK)
        self.key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.key)
        resolve = self.work.review.start({"kind": "resolve", "identifiers": "TEST001 TEST002"})["task_id"]
        wait_task(self.work, resolve)
        self.group = self.work.review.save_set({"mrns": "TEST001 TEST002"})
        self.task = self.work.review.start({"set_id": self.group["id"]})["task_id"]
        wait_task(self.work, self.task)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def send(self, mrns=None, module="surgery", **values):
        return self.work.review.handoff({"module": module, "review_task_id": self.task,
                                         "mrns": mrns or ["TEST001"], **values})

    def test_accumulates_without_fetching_or_changing_task_snapshot(self):
        before = copy.deepcopy(self.work.review.db.get("task", self.task))
        calls = list(BotSyntheticSDK.calls)
        first = self.send()
        second = self.send(["TEST002"])
        repeated = self.send()
        self.assertEqual(first["worklist"]["id"], second["worklist"]["id"])
        self.assertEqual(first["cohort"]["id"], second["cohort"]["id"])
        self.assertEqual([m["mrn"] for m in repeated["worklist"]["members"]], ["TEST001", "TEST002"])
        self.assertEqual(first["cohort"]["members"][0]["handoff_sources"],
                         repeated["cohort"]["members"][0]["handoff_sources"])
        self.assertEqual(before, self.work.review.db.get("task", self.task))
        self.assertEqual(calls, BotSyntheticSDK.calls)
        self.assertEqual(self.work.review.cohort({"set_id": second["worklist"]["id"]})["id"], first["cohort"]["id"])
        other = self.send(module="cataract")
        self.assertNotEqual(other["cohort"]["id"], first["cohort"]["id"])
        self.assertEqual(len(other["worklist"]["members"]), 1)

    def test_saves_exact_reviewed_version_not_current_library_head(self):
        patient = self.work.review.results({"id": self.task})["patients"][0]
        record = patient["records"][0]
        current = self.work.store.library.get_record(record["id"])
        self.work.store.library.save_record({**current, "soap": "changed after review"}, "TEST", self.task)
        result = self.send(record_ids=[record["id"]])
        sources = self.work.analysis.store.document("analysis_cohorts", result["cohort"]["id"])["members"][0]["handoff_sources"]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["record"]["soap"], record["soap"])
        self.assertEqual(sources[0]["version"], record["version"])
        self.assertEqual(sources[0]["review_task_id"], self.task)
        self.assertNotIn("highlights", sources[0]["record"])

    def test_rejects_foreign_patient_record_task_and_readonly(self):
        with self.assertRaises(ValueError):
            self.send(["TEST999"])
        other_record = self.work.review.results({"id": self.task})["patients"][1]["records"][0]["id"]
        with self.assertRaises(ValueError):
            self.send(record_ids=[other_record])
        other_key = self.app.login({"username": "SECOND", "password": "synthetic"})["account"]["id"]
        with self.assertRaises(ValueError):
            self.app.workspace(other_key).review.handoff({"module": "surgery", "review_task_id": self.task,
                                                         "mrns": ["TEST001"]})
        self.app.read_only = True
        try:
            with self.assertRaises(ValueError):
                self.send()
        finally:
            self.app.read_only = False

    def test_concurrent_appends_and_transaction_failure(self):
        failures = []

        def send(mrn):
            try:
                self.send([mrn])
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=send, args=(mrn,)) for mrn in ("TEST001", "TEST002")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        state = self.work.review.tool_state()["modules"]["surgery"]
        self.assertEqual(len(self.work.review.db.get("set", state["set_id"])["members"]), 2)
        with patch.object(self.work.analysis.store, "save_document", side_effect=ValueError("synthetic write failure")):
            with self.assertRaises(ValueError):
                self.send(module="retina")
        self.assertNotIn("retina", self.work.review.tool_state()["modules"])
        self.assertIsNone(self.work.review.db.get("draft", "handoff:retina", required=False))

    def test_removal_and_clear_keep_identity_without_resurrecting_patients(self):
        first = self.send(["TEST001", "TEST002"])
        removed = self.work.review.remove_handoff({"module": "surgery", "mrns": ["TEST001"]})
        self.assertEqual([m["mrn"] for m in removed["worklist"]["members"]], ["TEST002"])
        cleared = self.work.review.remove_handoff({"module": "surgery", "mrns": ["TEST002"]})
        self.assertEqual(cleared["worklist"]["members"], [])
        next_value = self.send(["TEST001"])
        self.assertEqual(next_value["cohort"]["id"], first["cohort"]["id"])
        self.assertEqual([m["mrn"] for m in next_value["worklist"]["members"]], ["TEST001"])
        self.assertEqual(len(self.work.review.db.get("task", self.task)["members"]), 2)

    def test_cataract_removal_retires_pending_but_rejects_active_patient(self):
        result = self.send(["TEST001", "TEST002"], module="cataract")
        queue = self.work.analysis.cataract_queue
        with queue.condition:
            queue.cohort_id = result["cohort"]["id"]
            queue.pending = ["TEST001", "TEST002"]
            queue.priority = "TEST001"
            queue.active = "TEST001"
        with self.assertRaises(ValueError):
            self.work.review.remove_handoff({"module": "cataract", "mrns": ["TEST001"]})
        self.assertEqual(len(self.work.review.db.get("set", result["worklist"]["id"])["members"]), 2)
        with queue.condition:
            queue.active = ""
        self.work.review.remove_handoff({"module": "cataract", "mrns": ["TEST001"]})
        self.assertEqual(queue.pending, ["TEST002"])
        self.assertEqual(queue.priority, "")

    def test_public_worklists_omit_soap_but_private_candidates_keep_evidence(self):
        result = self.send()
        private = self.work.analysis.store.document("analysis_cohorts", result["cohort"]["id"])
        source = private["members"][0]["handoff_sources"][0]
        self.assertIn("soap", source["record"])
        documents = [result["worklist"], result["cohort"],
                     self.work.review.cohort({"set_id": result["worklist"]["id"]}),
                     next(group for group in workbench_snapshot(self.work, compact=True)["sets"] if group["id"] == result["worklist"]["id"]),
                     next(cohort for cohort in self.work.analysis.overview({})["cohorts"]
                          if cohort["id"] == private["id"]),
                     self.work.review.save_set({"id": result["worklist"]["id"], "name": "renamed"}),
                     self.work.analysis.save_cohort({"id": private["id"], "name": "renamed cohort"})]
        for document in documents:
            public = next(member for member in document["members"] if member["mrn"] == "TEST001")["handoff_sources"][0]
            self.assertNotIn("record", public)
            self.assertEqual(public["record_id"], source["record_id"])
            self.assertEqual(public["version"], source["version"])
        for result in (self.work.analysis.results({"cohort_id": private["id"], "mrn": "TEST001", "module": "surgery"}),
                       self.work.analysis.raw_data({"cohort_id": private["id"], "mrn": "TEST001"})):
            self.assertNotIn("record", result["member"]["handoff_sources"][0])
        self.assertTrue(self.work.analysis.surgery_candidates({"cohort_id": private["id"]})["candidates"])
        self.assertIn("record", self.work.analysis.store.document("analysis_cohorts", private["id"])["members"][0]["handoff_sources"][0])

    def seed_handoff_copies(self, cohort):
        private = self.work.analysis.store.document("analysis_cohorts", cohort["id"])
        run_id = uuid.uuid4().hex
        self.work.analysis.store.save_document("analysis_runs", {"id": run_id, "cohort_id": private["id"],
            "name": "source copy", "members": copy.deepcopy(private["members"]), "status": "completed",
            "options": {"modules": ["surgery"]}})
        task = copy.deepcopy(self.work.review.db.get("task", self.task))
        task.update(id=uuid.uuid4().hex, members=copy.deepcopy(private["members"]))
        self.work.review.db.save("task", task)
        self.work.review.db.item(task["id"], "TEST001", copy.deepcopy(private["members"][0]))
        return run_id, task["id"]

    def test_record_deletion_removes_all_handoff_copies_at_durable_mark(self):
        result = self.send(["TEST001", "TEST002"])
        run_id, copied_task = self.seed_handoff_copies(result["cohort"])
        private = self.work.analysis.store.document("analysis_cohorts", result["cohort"]["id"])
        source_id = private["members"][0]["handoff_sources"][0]["record_id"]
        binding = self.work.review.db.save("cloud_binding", {"id": "retain", "mrn": "TEST001", "row_sync_id": "stable-cloud-id"})
        # This is the resume-safe transaction before filesystem SOAP deletion.
        self.work.review.db.purge_records([source_id])
        copies = [self.work.review.db.get("set", result["worklist"]["id"]),
                  self.work.analysis.store.document("analysis_cohorts", result["cohort"]["id"]),
                  self.work.analysis.store.document("analysis_runs", run_id),
                  self.work.review.db.get("task", copied_task)]
        for document in copies:
            first = document["members"][0]
            self.assertNotIn(source_id, first["source_records"])
            self.assertTrue(all(source["record_id"] != source_id for source in first["handoff_sources"]))
            self.assertTrue(document["members"][1]["handoff_sources"], "unrelated patient evidence was deleted")
        item = self.work.review.db.items(copied_task)[0]
        self.assertTrue(all(source["record_id"] != source_id for source in item["handoff_sources"]))
        self.assertEqual(binding, self.work.review.db.get("cloud_binding", "retain"))

    def test_analysis_clear_removes_evidence_and_manual_drafts_but_keeps_other_patient(self):
        result = self.send(["TEST001", "TEST002"])
        run_id, copied_task = self.seed_handoff_copies(result["cohort"])
        draft = self.work.analysis.surgery_draft({"cohort_id": result["cohort"]["id"], "mrn": "TEST001"})["candidate"]
        self.work.analysis.surgery_draft({"cohort_id": result["cohort"]["id"], "mrn": "TEST002"})
        values = {"mrns": ["TEST001"], "categories": ["analysis"]}
        preview = self.work.library_data.preview(values)
        self.work.library_data.delete({**values, "fingerprint": preview["fingerprint"]})
        for table, key in (("analysis_cohorts", result["cohort"]["id"]), ("analysis_runs", run_id)):
            members = self.work.analysis.store.document(table, key)["members"]
            self.assertEqual(members[0]["handoff_sources"], [])
            self.assertEqual(members[0]["source_records"], [])
            self.assertTrue(members[1]["handoff_sources"])
        self.assertEqual(self.work.review.db.get("task", copied_task)["members"][0]["handoff_sources"], [])
        self.assertEqual(self.work.review.db.get("set", result["worklist"]["id"])["members"][0]["handoff_sources"], [])
        original_record = self.work.review.results({"id": self.task})["patients"][0]["records"][0]["id"]
        self.assertIsNotNone(self.work.store.library.get_record(original_record))
        self.assertNotIn(draft["id"], [row["id"] for row in self.work.analysis.store.documents("surgery_drafts")])
        self.assertEqual(len(self.work.analysis.store.documents("surgery_drafts")), 1)


if __name__ == "__main__":
    unittest.main()
