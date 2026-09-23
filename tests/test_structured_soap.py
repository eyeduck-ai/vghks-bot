import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from vghks_sdk import SoapRecord, VisitCase

from opd_monitor.bot import BotApplication
from opd_monitor.selftest_bot import BotSyntheticSDK, wait_task
from opd_monitor.selftest_soap import synthetic_soap
from opd_monitor.settings import Settings
from opd_monitor.soap_data import snapshot
from opd_monitor.soap_preview import ap_preview
from opd_monitor.tags import classification, extract_tags

CASE = VisitCase("TEST001", date(2026, 9, 21), "O", "ONE", "70", "眼科",
                 detail_params={"hid": "transient-not-clinical"})


def rule(scope, keyword):
    return Settings().update({"categories": [{"id": "test", "name": "測試", "keywords": [keyword], "scope": scope}]})


class StructuredSoapTests(unittest.TestCase):
    def setUp(self):
        self.soap = synthetic_soap(CASE)
        self.record = snapshot(self.soap)

    def test_sdk_snapshot_keeps_fields_unknown_text_and_printed_values(self):
        data = self.record["soap_structure"]
        self.assertEqual(self.record["soap"], self.soap.full_text)
        self.assertIn("Cataract OD", data["assessment_plan"])
        self.assertIsNone(data["assessment"])
        self.assertIsNone(data["plan"])
        self.assertEqual(data["medications"][0]["total_quantity"], "1.00")
        self.assertEqual(data["orders"][0]["quantity"], "1.00")
        self.assertEqual(data["diagnoses"][0]["coding_system"], "ICD")
        self.assertEqual(data["chronic_prescription_periods"][0]["start_date"], "2026-09-21")
        self.assertIn("其他合成原文", data["unclassified_blocks"])
        self.assertEqual(data["parsing_issues"], [])
        self.assertNotIn("transient-not-clinical", json.dumps(data))
        self.assertNotIn("case", data)

    def test_each_tag_scope_only_reads_its_own_source(self):
        pairs = {"s": "主訴文字", "o": "Va OD", "ap": "# Arrange",
                 "medications": "SYNTHETIC DROP", "orders": "SYNTHETIC FUNDUS"}
        for scope, keyword in pairs.items():
            with self.subTest(scope=scope):
                hit = extract_tags(self.record, rule(scope, keyword))[0]
                self.assertEqual(hit["scope"], scope)
                self.assertEqual(len(extract_tags(self.record, rule("all", keyword))), 1)
                for other in pairs:
                    if other != scope:
                        self.assertEqual(extract_tags(self.record, rule(other, keyword)), [])
                self.assertEqual(self.record["soap"][hit["start"]:hit["keyword_end"]], keyword)

    def test_duplicate_text_does_not_guess_global_offset_or_tag_other_sections(self):
        soap = SoapRecord(CASE, ("SAME", "SAME"), subjective="SAME", assessment_plan="SAME", present_sections=("S", "AP"))
        hit, = extract_tags(snapshot(soap), rule("ap", "SAME"))
        self.assertEqual(hit["source_field"], "assessment_plan")
        self.assertEqual((hit["source_start"], hit["source_keyword_end"]), (0, 4))
        self.assertIsNone(hit["start"])
        self.assertEqual(len(extract_tags(snapshot(soap), rule("all", "SAME"))), 2)

    def test_unicode_offsets_and_literal_keywords_remain_exact(self):
        soap = SoapRecord(CASE, ("🙂 中文 [.*] EXTRA",), assessment_plan="🙂 中文 [.*] EXTRA", present_sections=("AP",))
        hit, = extract_tags(snapshot(soap), rule("ap", "[.*]"))
        self.assertEqual((hit["start"], hit["keyword_end"]), (5, 9))
        self.assertEqual(hit["excerpt"], "[.*] EXTRA")

    def test_ap_includes_explicit_separate_sections_without_cross_section_matching(self):
        soap = SoapRecord(CASE, ("A: same", "P: same"), assessment="same", plan="same", present_sections=("A", "P"))
        hits = extract_tags(snapshot(soap), rule("ap", "same"))
        self.assertEqual({hit["source_field"] for hit in hits}, {"assessment", "plan"})
        self.assertEqual(extract_tags(snapshot(soap), rule("ap", "same same")), [])
        self.assertEqual([part["label"] for part in ap_preview(snapshot(soap))], ["A", "P"])

    def test_partial_summaries_keep_valid_hits_and_flag_incomplete_scope(self):
        soap = replace(self.soap, parsing_issues=("SOAP_MEDICATION_ROW_UNRECOGNIZED",))
        result = classification(snapshot(soap), rule("medications", "SYNTHETIC DROP"))
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["tag_scope_issues"][0]["status"], "partial")
        self.assertEqual(classification(snapshot(soap), rule("orders", "SYNTHETIC FUNDUS"))["tag_scope_issues"], [])

    def test_missing_and_explicit_empty_sections_are_different(self):
        for scope, field, code in (("s", "subjective", "S"), ("ap", "assessment_plan", "AP")):
            empty = snapshot(SoapRecord(CASE, ("P: source",), **{field: ""}, present_sections=(code,)))
            absent = snapshot(SoapRecord(CASE, ("P: source",)))
            self.assertEqual(classification(empty, rule(scope, "source"))["tag_scope_issues"], [])
            self.assertEqual(classification(absent, rule(scope, "source"))["tag_scope_issues"][0]["status"], "unavailable")
        empty_ap = snapshot(SoapRecord(CASE, ("other",), assessment_plan="", present_sections=("AP",)))
        self.assertEqual(ap_preview(empty_ap), [{"label": "A+P", "text": "", "empty": True}])

    def test_empty_and_missing_medication_table_do_not_mean_the_same(self):
        empty = snapshot(SoapRecord(CASE, (), present_sections=("MEDICATIONS",)))
        missing = snapshot(SoapRecord(CASE, ()))
        self.assertEqual(classification(empty, rule("medications", "drug"))["tag_scope_issues"], [])
        self.assertEqual(classification(missing, rule("medications", "drug"))["tag_scope_issues"][0]["status"], "unavailable")

    def test_unclassified_text_is_not_included_in_medication_scope(self):
        soap = SoapRecord(CASE, ("SYNTHETIC DROP",), unclassified_blocks=("SYNTHETIC DROP",),
                          present_sections=("MEDICATIONS",), parsing_issues=("SOAP_MEDICATION_HEADER_UNRECOGNIZED",))
        result = classification(snapshot(soap), rule("medications", "SYNTHETIC DROP"))
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["tag_scope_issues"][0]["status"], "partial")
        self.assertEqual(len(extract_tags(snapshot(soap), rule("all", "SYNTHETIC DROP"))), 1)

    def test_old_record_scope_is_unknown_not_a_full_text_fallback(self):
        legacy = {"soap": "A: # Arrange CATA OD"}
        self.assertEqual(len(extract_tags(legacy, rule("all", "# Arrange"))), 1)
        result = classification(legacy, rule("ap", "# Arrange"))
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["tag_scope_issues"][0]["status"], "unavailable")
        self.assertEqual(ap_preview(legacy), [{"label": "A", "text": "# Arrange CATA OD"}])

    def test_preview_uses_structured_ap_before_plain_text_heading_heuristics(self):
        soap = SoapRecord(CASE, ("A: misleading text", "actual AP"), assessment_plan="actual AP", present_sections=("AP",))
        self.assertEqual(ap_preview(snapshot(soap)), [{"label": "A+P", "text": "actual AP", "empty": False}])
        self.assertEqual(ap_preview(snapshot(replace(soap, assessment_plan=None, present_sections=()))), [])

    def test_scope_round_trip_and_invalid_values(self):
        self.assertTrue(all(tag.scope == "ap" for tag in Settings().categories))
        for scope in ("all", "s", "o", "ap", "medications", "orders"):
            settings = rule(scope, "sample")
            self.assertEqual(Settings().update(settings.public()).categories[0].scope, scope)
        for scope in ("unknown", True, None, [], {}):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                rule(scope, "sample")


class StructuredSoapWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        BotSyntheticSDK.calls = []
        BotSyntheticSDK.fail_case = ""
        BotSyntheticSDK.on_soap = None
        BotSyntheticSDK.revision = "original"
        self.app = BotApplication(Settings(), self.path, BotSyntheticSDK)
        self.key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.key)
        run = self.work.review.start({"kind": "resolve", "identifiers": "TEST001"})["task_id"]
        wait_task(self.work, run)
        self.group = self.work.review.save_set({"mrns": "TEST001"})

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()
        BotSyntheticSDK.revision = "original"

    def review(self, **options):
        run = self.work.review.start({"set_id": self.group["id"], **options})["task_id"]
        task = wait_task(self.work, run)
        self.assertEqual(task["status"], "completed")
        return task

    def test_fetch_persist_local_reclassify_preview_and_offline_restart(self):
        task = self.review()
        record = task["items"][0]["records"][0]
        original = self.work.library_record(record["id"])
        before = list(BotSyntheticSDK.calls)
        self.work.save_settings(rule("medications", "SYNTHETIC DROP").public())
        self.work.review.reclassify(task["id"])
        result = self.work.review.results({"id": task["id"], "tag": "test"})
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["scope_issue_count"], 0)
        self.assertEqual(result["patients"][0]["records"][0]["matches"][0]["source_field"], "medications")
        saved = self.work.library_search({"tag": "test"})
        self.assertEqual(saved["total"], 2)
        self.assertEqual(saved["records"][0]["ap_preview"][0]["text"], record["soap_structure"]["assessment_plan"])
        self.assertEqual(self.work.library_record(record["id"])["version_count"], original["version_count"])
        self.assertEqual(BotSyntheticSDK.calls, before)
        self.app.close()
        self.app = BotApplication(Settings(), self.path, BotSyntheticSDK)
        self.app.offline(self.key)
        self.work = self.app.workspace(self.key)
        reread = self.work.library_record(record["id"])
        self.assertEqual(reread["soap_structure"], record["soap_structure"])
        self.assertEqual(self.work.settings.categories[0].scope, "medications")
        self.assertEqual(BotSyntheticSDK.calls, before)

    def test_cached_structure_reused_and_force_refresh_preserves_old_task_snapshot(self):
        first = self.review()
        record = first["items"][0]["records"][0]
        count = len([call for call in BotSyntheticSDK.calls if call[1] == "soap" and call[-1] in {"ONE", "TWO"}])
        reused = self.review()
        self.assertTrue(all(r["cached"] for r in reused["items"][0]["records"]))
        self.assertEqual(len([call for call in BotSyntheticSDK.calls if call[1] == "soap" and call[-1] in {"ONE", "TWO"}]), count)
        BotSyntheticSDK.revision = "updated"
        self.review(force=True)
        saved = self.work.library_record(record["id"])
        self.assertEqual(saved["version_count"], 2)
        self.assertIn("updated", saved["soap_structure"]["subjective"])
        old = self.work.review.results({"id": first["id"]})["patients"][0]["records"][0]
        self.assertIn("original", old["soap_structure"]["subjective"])
        self.work.delete_records({"ids": [record["id"]]})
        self.assertIsNone(self.work.store.library.get_record(record["id"]))
        self.assertNotIn(record["id"], [r["id"] for p in self.work.review.results({"id": first["id"]})["patients"] for r in p["records"]])


if __name__ == "__main__":
    unittest.main()
