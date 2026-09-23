import unittest

from opd_monitor.keywords import extract_keywords
from opd_monitor.settings import Settings
from opd_monitor.surgery import parse_surgery

FULL_TEXT_SETTINGS = Settings().update({"categories": [
    {**tag, "scope": "all"} for tag in Settings().public()["categories"]
]})


class KeywordTests(unittest.TestCase):
    def test_default_categories_only_match_their_complete_literal_keywords(self):
        ap = "# arbitrary\n# APPLY Cataract\n# Arrange CATA OD\narrange CATA"
        outside = "# Arrange outside AP\n# APPLY outside AP"
        record = {"soap": outside + "\n" + ap,
                  "soap_structure": {"subjective": outside, "objective": outside,
                                     "assessment_plan": ap, "present_sections": ["S", "O", "AP"]}}
        matches = extract_keywords(record, Settings())
        self.assertEqual([m["category"] for m in matches], ["review", "surgery"])
        self.assertTrue(all(m["source_field"] == "assessment_plan" for m in matches))
        self.assertEqual(extract_keywords(outside, Settings()), [])

    def test_arbitrary_keywords_need_no_prefix_and_regex_is_literal(self):
        settings = Settings().update({"categories":[{"id":"custom","name":"追蹤","keywords":["回診提醒", ".*", "[REVIEW]"]}]})
        text = "回診提醒：下次\n\nnot a regex\n.* literal\n[review] lower case"
        self.assertEqual(len(extract_keywords(text, settings)), 3)
        self.assertEqual(extract_keywords("anything else # APPLY", settings), [])

    def test_continuation_lines_and_source_offsets_including_emoji(self):
        text = "🙂 P:\n# Arrange CATA OD\n- TEL: test\n- explain\n# APPLY Cataract\n\nUnrelated\nO: exam"
        matches = extract_keywords(text, FULL_TEXT_SETTINGS)
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0]["excerpt"], "# Arrange CATA OD\n- TEL: test\n- explain")
        self.assertEqual(matches[1]["excerpt"], "# APPLY Cataract")
        for match in matches:
            self.assertEqual(text[match["start"]:match["end"]], match["excerpt"])

    def test_same_keyword_can_belong_to_two_categories(self):
        settings = Settings().update({"categories":[{"id":"a","name":"甲","keywords":["keyword", "key"]},{"id":"b","name":"乙","keywords":["keyword"]}]})
        matches = extract_keywords("keyword text", settings)
        self.assertEqual(len(matches), 2)
        self.assertTrue(all(m["excerpt"] == "keyword text" for m in matches))

    def test_html_kept_as_inert_source_text(self):
        text = '# APPLY <img src=x onerror=alert(1)>'
        self.assertEqual(extract_keywords(text,FULL_TEXT_SETTINGS)[0]["excerpt"],text)

    def test_additional_field_category_does_not_truncate_surgery_details(self):
        settings = FULL_TEXT_SETTINGS.update({"categories":[*FULL_TEXT_SETTINGS.public()["categories"],{"id":"phone","name":"聯絡","keywords":["TEL","CATA"]}]})
        text = "# Arrange CATA OD (IOL SN60WF T-0.5) on 20260930\n- TEL: 0900-000-001\n# APPLY Cataract"
        surgery = next(m for m in extract_keywords(text,settings) if m["category"] == "surgery")
        self.assertEqual(surgery["surgery"]["tel"],"0900-000-001")
        self.assertNotIn("# APPLY",surgery["excerpt"])


class SurgeryTests(unittest.TestCase):
    def test_filled_cataract_template(self):
        parsed = parse_surgery("# Arrange LENSX+CATA OS (IOL SN60WF +21.0 T-0.50) on 20260930\n- TEL: 0900-000-001 / 07-0000000 #123")
        self.assertEqual(parsed, {"procedure":"LENSX+CATA", "laterality":"OS", "iol":"SN60WF +21.0", "target":"-0.50", "scheduled_date":"20260930", "date_iso":"2026-09-30", "tel":"0900-000-001 / 07-0000000 #123"})

    def test_missing_values_are_not_inferred(self):
        parsed = parse_surgery("# Arrange CATA OD/OS/OU (IOL? T?) on {{today}}\n- TEL:\n- love wearing GL()")
        self.assertEqual(parsed["procedure"], "CATA")
        self.assertTrue(all(not value for key,value in parsed.items() if key != "procedure"))

    def test_reference_procedures_and_accessories_not_iol(self):
        for kind in ("CATA", "LENSX+CATA", "LMR", "VT", "VT+MP"):
            with self.subTest(kind=kind):
                parsed = parse_surgery(f"# Arrange {kind} OU (sharkskin+TWIN) on 2026/10/01")
                self.assertEqual(parsed["procedure"],kind)
                self.assertEqual(parsed["laterality"],"OU")
                self.assertEqual(parsed["date_iso"],"2026-10-01")
                self.assertEqual(parsed["iol"],"")

    def test_bare_lens_target_fullwidth_and_roc_date(self):
        parsed = parse_surgery("# Arrange CATA OD （ZCB00 +20.0 Target: −0.25） on 115/09/30")
        self.assertEqual(parsed["iol"],"ZCB00 +20.0")
        self.assertEqual(parsed["target"],"-0.25")
        self.assertEqual(parsed["date_iso"],"2026-09-30")
        self.assertEqual(parse_surgery("# Arrange CATA OS (ZCB00 T+0.25) on 20261001")["target"],"+0.25")

    def test_invalid_date_and_placeholder_target_remain_unparsed(self):
        parsed = parse_surgery("# Arrange CATA OD? (IOL? T-0.50?) on 20260230")
        self.assertEqual(parsed["date_iso"],"")
        self.assertEqual(parsed["scheduled_date"],"20260230")
        self.assertEqual(parsed["laterality"],"")
        self.assertEqual(parsed["target"],"")
