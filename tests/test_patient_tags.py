import tempfile
import unittest
from pathlib import Path

from analysis_fixtures import ExamSDK, soap_record

from opd_monitor.jobs import Application
from opd_monitor.settings import Settings
from opd_monitor.tags import extract_tags


class PatientTagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        ExamSDK.calls = []
        ExamSDK.fail_numeric = ExamSDK.fail_pdf = False
        ExamSDK.cancel_callback = None
        self.app = Application(Settings(), self.path, ExamSDK)
        self.account = next(iter(self.app.accounts))
        self.app.save_accounts({"accounts": [{"id": self.account, "username": "TEST", "password": "synthetic"}]})

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def add_group(self, mrns="0000001", name="追蹤群組"):
        reply = self.app.patient_tags_update({"mrns": mrns, "new_tag": name})
        return reply["settings"]["categories"][-1]["id"]

    def test_manual_only_definition_does_not_match_text(self):
        settings = Settings().update({"categories": [{"id": "follow", "name": "追蹤", "keywords": []}]})
        self.assertEqual(extract_tags("追蹤 # Arrange", settings), [])

    def test_patients_without_soap_can_join_multiple_groups_and_deduplicate(self):
        tag = self.add_group("００００００１，0000001\n0000002")
        other = self.add_group("0000001", "研究候選")
        self.assertEqual(ExamSDK.calls, [])
        self.assertEqual(self.app.library_search({})["total"], 0)
        value = self.app.tag_patients({"tag": tag})
        self.assertEqual(value["total"], 2)
        self.assertEqual([p["mrn"] for p in value["patients"]], ["0000001", "0000002"])
        first = value["patients"][0]
        self.assertEqual({t["id"] for t in first["manual_tags"]}, {tag, other})
        self.assertEqual(first["auto_tags"], [])
        added_at = first["manual_tags"][0]["added_at"]
        self.assertEqual(self.app.patient_tags_update({"mrns": "0000001", "tag_ids": [tag]})["changed"], 0)
        self.assertEqual(self.app.tag_patients({"tag": tag})["patients"][0]["manual_tags"][0]["added_at"], added_at)

    def test_manual_tag_applies_to_all_encounters_and_survives_refresh_reclassification_restart(self):
        tag = self.add_group()
        for day in ("2026-09-17", "2026-09-18"):
            record = {**soap_record(day=day), "soap": "Routine follow up",
                      "soap_structure": {"assessment_plan": "Routine follow up", "present_sections": ["AP"]}}
            self.app.store.library.save_record(record, "TEST", "test")
        result = self.app.library_search({"tag": tag})
        self.assertEqual((result["patients"], result["total"]), (1, 2))
        self.assertTrue(all(not r["matches"] and r["manual_tags"][0]["id"] == tag for r in result["records"]))
        self.assertEqual(self.app.library_search({"tag": "__untagged"})["total"], 0)
        self.app.store.library.save_record({**result["records"][0], "soap": "updated",
            "soap_structure": {"assessment_plan": "updated", "present_sections": ["AP"]}}, "TEST", "refresh")
        self.assertNotIn("manual_tags", self.app.store.library.get_record(result["records"][0]["id"]))
        categories = self.app.settings.public()["categories"]
        categories[-1].update(name="改名追蹤", keywords=["not in SOAP"])
        self.app.save_settings({"categories": categories})
        self.app.close()
        self.app = Application(Settings(), self.path, ExamSDK)
        result = self.app.library_search({"tag": tag})
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["records"][0]["manual_tags"][0]["name"], "改名追蹤")
        self.assertEqual(ExamSDK.calls, [])

    def test_manual_removal_preserves_automatic_match_and_other_groups(self):
        self.app.store.library.save_record(soap_record(), "TEST", "test")
        tag = self.add_group()
        self.app.patient_tags_update({"mrns": "0000001", "tag_ids": ["surgery"]})
        result = self.app.tag_patients({"tag": "surgery"})
        self.assertEqual(result["tag_counts"]["surgery"], 1)
        self.assertEqual(result["total"], 1)
        self.app.patient_tags_update({"mrns": "0000001", "tag_ids": ["surgery"], "operation": "remove"})
        self.assertEqual(self.app.tag_patients({"tag": "surgery", "source": "manual"})["total"], 0)
        self.assertEqual(self.app.tag_patients({"tag": "surgery", "source": "auto"})["total"], 1)
        self.assertEqual(self.app.tag_patients({"tag": tag})["total"], 1)
        self.assertEqual(self.app.library_search({"tag": "surgery"})["total"], 1)

    def test_group_cross_page_analysis_snapshot_and_no_soap_surgery_candidate(self):
        tag = self.add_group("\n".join(str(i).zfill(7) for i in range(1, 206)))
        page = self.app.tag_patients({"tag": tag, "offset": 200, "limit": 40})
        self.assertEqual((page["total"], len(page["patients"])), (205, 5))
        cohort = self.app.analysis.save_cohort({"source": "tags", "all": True, "account_id": self.account,
            "filters": {"tag": tag, "offset": 200, "limit": 1}, "name": "snapshot"})
        self.assertEqual(len(cohort["members"]), 205)
        self.app.patient_tags_update({"mrns": "0000001", "tag_ids": [tag], "operation": "remove"})
        self.assertEqual(len(self.app.analysis.store.document("analysis_cohorts", cohort["id"])["members"]), 205)
        cohort = self.app.analysis.save_cohort({"source": "tags", "account_id": self.account,
            "filters": {"tag": tag}, "mrns": "0000002", "name": "one"})
        self.assertEqual(self.app.analysis.surgery_candidates({"cohort_id": cohort["id"]})["candidates"], [])
        run = self.app.analysis.start({"cohort_id": cohort["id"], "modules": ["retina"]})["run_ids"][0]
        self.assertTrue(self.app.idle.wait(10))
        self.assertEqual(self.app.snapshot(run)["status"], "completed")
        with self.assertRaisesRegex(ValueError, "群組已變更"):
            self.app.analysis.save_cohort({"source": "tags", "filters": {"tag": tag}, "mrns": "0000001", "name": "stale"})

    def test_soap_and_analysis_deletion_keep_manual_membership_but_explicit_tag_removal_does_not_revive(self):
        record = soap_record()
        self.app.store.library.save_record(record, "TEST", "test")
        tag = self.add_group()
        cohort = self.app.analysis.save_cohort({"source": "manual", "account_id": self.account, "mrns": "0000001", "name": "one"})
        self.app.analysis.delete_data({"cohort_id": cohort["id"], "mrn": "0000001"})
        self.app.delete_records({"ids": [record["id"]]})
        self.assertEqual(self.app.tag_patients({"tag": tag})["total"], 1)
        self.app.patient_tags_update({"mrns": "0000001", "tag_ids": [tag], "operation": "remove"})
        self.app.close()
        self.app = Application(Settings(), self.path, ExamSDK)
        self.app.store.library.save_record(record, "TEST", "test-refresh")
        self.assertEqual(self.app.tag_patients({"tag": tag})["total"], 0)
        self.assertEqual(self.app.library_search({})["records"][0]["manual_tags"], [])

    def test_deleting_tag_definition_removes_only_its_memberships_and_cannot_revive(self):
        tag, other = self.add_group(), self.add_group("0000001", "其他")
        definitions = self.app.settings.public()["categories"]
        self.app.save_settings({"categories": [c for c in definitions if c["id"] != tag]})
        self.app.save_settings({"categories": definitions})
        self.assertEqual(self.app.tag_patients({"tag": tag})["total"], 0)
        self.assertEqual(self.app.tag_patients({"tag": other})["total"], 1)

    def test_invalid_bulk_inputs_do_not_partially_save(self):
        tag = self.add_group()
        initial = self.app.store.library.manual_tags(self.app.settings)
        for payload in ({"mrns": "0000002 姓名", "new_tag": "invalid"},
                        {"mrns": "0000002", "tag_ids": [tag, "missing"]},
                        {"mrns": "0000002", "new_tag": "追蹤群組"},
                        {"mrns": "0000002", "new_tag": "remove-new", "operation": "remove"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.app.patient_tags_update(payload)
        self.assertEqual(self.app.store.library.manual_tags(self.app.settings), initial)
        self.assertEqual(len(self.app.settings.categories), 3)
        self.assertEqual(ExamSDK.calls, [])
