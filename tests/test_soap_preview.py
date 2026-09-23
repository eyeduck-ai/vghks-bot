import tempfile
import unittest
from pathlib import Path

from opd_monitor.library import Library
from opd_monitor.settings import Settings
from opd_monitor.soap_preview import ap_preview


class SoapPreviewTests(unittest.TestCase):
    def test_separate_sections_keep_verbatim_assessment_and_plan(self):
        soap = "S: 主訴\nO: Va 0.4\nA: Cataract OD\n左眼穩定\nP: # Arrange CATA OD\nIOL +21.0 T-0.50\nS: 不應混入"
        self.assertEqual(ap_preview(soap), [
            {"label": "A", "text": "Cataract OD\n左眼穩定"},
            {"label": "P", "text": "# Arrange CATA OD\nIOL +21.0 T-0.50"},
        ])

    def test_explicit_combined_and_multilingual_headers(self):
        for heading in ("A+P:", "A/P：", "A & P:", "Assessment and Plan:", "Assessment/Plan:", "評估與計畫：", "[A+P]", "【Ａ＋Ｐ】"):
            with self.subTest(heading=heading):
                self.assertEqual(ap_preview(heading+"\n觀察 OS；下次回診"), [{"label": "A+P", "text": "觀察 OS；下次回診"}])
        self.assertEqual(ap_preview("【A】\n視網膜穩定\nＰ：依原計畫\nObjective:\n不屬於P"), [
            {"label": "A", "text": "視網膜穩定"}, {"label": "P", "text": "依原計畫"}])

    def test_unlabelled_sdk_blocks_cannot_establish_section_identity(self):
        blocks = ("主訴內容", "Va 0.4", "Cataract OD", "# Arrange CATA OD")
        self.assertEqual(ap_preview("\n\n".join(blocks)), [])
        self.assertEqual(ap_preview(""), [])

    def test_letters_inside_clinical_text_are_not_headers(self):
        for soap in ("Vitamin A: daily", "VA: 0.4\nAP: measurement", "Patient reports P: symptoms", "Plan to review", "# Arrange CATA OD\n# APPLY"):
            with self.subTest(soap=soap):
                self.assertEqual(ap_preview(soap), [])

    def test_repeated_sections_preserve_content_and_end_at_other_soap_headers(self):
        result = ap_preview("A: first\nS: subjective\nA: second\nP:\nO: finding\nPlan:\n  observe\n\nreview")
        self.assertEqual(result, [{"label": "A", "text": "first\nsecond"}, {"label": "P", "text": "observe\n\nreview"}])
        self.assertEqual(ap_preview("A:\nP:\n"), [])

    def test_library_read_derives_preview_without_changing_saved_record_or_version(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Library(Path(directory)/"clinical.sqlite3")
            record = {"id": "synthetic", "mrn": "TEST001", "date": "2026-09-01", "soap": "S: subjective\nA: stable\nP: observe"}
            library.save_record(record, "TEST", "synthetic-task")
            original = library.get_record(record["id"])
            page = library.search({}, Settings())
            self.assertEqual(page["records"][0]["ap_preview"], [{"label": "A", "text": "stable"}, {"label": "P", "text": "observe"}])
            self.assertEqual(library.get_record(record["id"]), original)
            library.save_record(page["records"][0], "TEST", "synthetic-task")
            saved = library.get_record(record["id"])
            self.assertEqual(saved["version_count"], 1)
            self.assertEqual(saved["soap"], record["soap"])
            self.assertNotIn("ap_preview", saved)


if __name__ == "__main__":
    unittest.main()
