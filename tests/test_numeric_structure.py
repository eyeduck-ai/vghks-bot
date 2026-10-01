"""Compound measurements retain their raw source and durable, versioned decimal derivations."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vghks_bot.analysis_export import _csv
from vghks_bot.analysis_numeric import CATARACT_NUMERIC_EXAMS, extract_tables
from vghks_bot.analysis_store import AnalysisStore
from vghks_bot.bot import BotApplication
from vghks_bot.numeric_measurements import enrich_rows
from vghks_bot.selftest_bot import BotSyntheticSDK
from vghks_bot.settings import Settings


def report(mrn="TEST001"):
    return {"mrn": mrn, "tables": [
        {"title": "驗光-散瞳前", "headers": ["日期", "OD", "OS"],
         "rows": [["2026-09-11", "-1.25 -1.00 X 170", "-2.25 -1.00 X 175"]]},
        {"title": "KM", "headers": ["日期", "OD", "OS"], "rows": [["2026-09-11",
         "K1 41.25 8.20 X 160 K2 42.50 7.96 X 70 CYL -1.25 X 160",
         "K1 41.50 8.14 X 45 K2 42.25 7.98 X 135 CYL -0.75 X 45"]]},
    ]}


class NumericStructureTests(unittest.TestCase):
    def test_supplied_samples_decimal_derivations_and_raw_preserved(self):
        rows = enrich_rows(extract_tables(report(), "synthetic"))
        values = [m["values"] for row in rows for m in row["measurements"]]
        self.assertEqual([values[0]["se"], values[1]["se"]], [-1.75, -2.75])
        self.assertEqual([values[2]["kavg"], values[3]["kavg"]], [41.875, 41.875])
        self.assertEqual(values[2]["axis1"], 160)
        self.assertEqual(values[3]["r2"], 7.98)
        self.assertEqual(rows[1]["measurements"][0]["exact"]["kavg"], "41.875")
        self.assertEqual(rows[0]["values"], report()["tables"][0]["rows"][0])
        self.assertEqual(len(rows), 2)
        csv = _csv(rows)
        self.assertIn("sph,cyl,axis,se,k1,r1,axis1,k2,r2,axis2,cyl_axis,kavg", csv)
        self.assertIn("41.875", csv)

    def test_decimal_storage_does_not_round_long_source_precision(self):
        data = {"tables": [{"title": "配鏡", "headers": ["日期", "OD"],
                            "rows": [["2026-09-11", "-1.000000000000000000000000000001 -1 X 90"]]}]}
        measurement = enrich_rows(extract_tables(data, "synthetic"))[0]["measurements"][0]
        self.assertEqual(measurement["exact"]["se"], "-1.500000000000000000000000000001")

    def test_split_fields_nulls_texts_and_conflicts(self):
        data = {"tables": [
            {"title": "驗光-散瞳後", "headers": ["日期", "眼別", "SPH (D)", "CYL (D)", "Axis (°)"],
             "rows": [["2026-09-11", "OD", "-1.25", "-0.25", "90"]]},
            {"title": "KM", "headers": ["日期", "OD K1 (D)", "OD K2 (D)"],
             "rows": [["2026-09-11", "41.50", "42.25"]]},
            {"title": "配鏡", "headers": ["日期", "OS SPH (D)"], "rows": [["2026-09-11", "0"]]},
            {"title": "Va", "headers": ["日期", "OD", "OS"], "rows": [["2026-09-11", "HM", "0"]]},
            {"title": "驗光-散瞳前", "headers": ["日期", "OD", "OD SPH"],
             "rows": [["2026-09-11", "-1.25 -1.00 X 170", "-3"]]},
        ]}
        rows = enrich_rows(extract_tables(data, "synthetic"))
        self.assertEqual(rows[0]["measurements"][0]["values"]["se"], -1.375)
        self.assertEqual(rows[1]["measurements"][0]["values"]["kavg"], 41.875)
        self.assertIsNone(rows[1]["measurements"][0]["values"]["r1"])
        self.assertIsNone(rows[2]["measurements"][0]["values"]["se"])
        self.assertEqual(rows[3]["measurements"][0]["status"], "text")
        self.assertEqual(rows[3]["measurements"][1]["values"]["value"], 0)
        self.assertEqual(rows[4]["measurements"][0]["status"], "unparsed")
        self.assertTrue(all(value is None for value in rows[4]["measurements"][0]["values"].values()))

    def test_normalization_and_bad_formats_do_not_guess_or_merge(self):
        data = report()
        data["tables"][0]["rows"] = [
            ["2026-09-11", "　−１．２５ −１．００ × １７０　", "0 -0.25 x 0"],
            ["2026-09-11", "-1.25 -1.00 X 181", "0.8(-1.25 -1.00 X 170)"],
        ]
        rows = enrich_rows(extract_tables(data, "synthetic"))
        self.assertEqual(rows[0]["measurements"][0]["values"]["se"], -1.75)
        self.assertEqual(rows[0]["measurements"][1]["values"]["se"], -.125)
        self.assertTrue(all(m["status"] == "unparsed" for m in rows[1]["measurements"]))
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(CATARACT_NUMERIC_EXAMS), 15)

    def test_projection_persist_restart_legacy_readonly_version_refresh_and_delete(self):
        with tempfile.TemporaryDirectory() as folder:
            app = BotApplication(Settings(), Path(folder), BotSyntheticSDK)
            try:
                key = app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
                work = app.workspace(key)
                cohort = work.analysis.save_cohort({"source": "manual", "account_id": key, "name": "numeric", "mrns": "TEST001"})
                store = work.analysis.store
                store.save_step("TEST001", "numeric-history", "numeric", report(), "TEST")
                projection = store.step("TEST001", "numeric-structured:numeric-history")
                self.assertTrue(projection["payload"]["source_digest"])
                self.assertEqual(AnalysisStore(store.library).numeric_rows(store.step("TEST001", "numeric-history"))[1]["measurements"][0]["values"]["kavg"], 41.875)
                with store.library.connect() as db:
                    db.execute("DELETE FROM analysis_steps WHERE kind='numeric_structured'")
                app.read_only = True
                data = work.analysis.results({"cohort_id": cohort["id"], "mrn": "TEST001", "module": "cataract"})
                self.assertEqual(data["numeric"][0]["measurements"][0]["status"], "parsed")
                self.assertIsNone(store.step("TEST001", "numeric-structured:numeric-history"))
                app.read_only = False
                work.analysis.results({"cohort_id": cohort["id"], "mrn": "TEST001", "module": "cataract"})
                self.assertIsNotNone(store.step("TEST001", "numeric-structured:numeric-history"))
                with patch("vghks_bot.numeric_measurements.PARSER_VERSION", 2):
                    rows = store.numeric_rows(store.step("TEST001", "numeric-history"))
                    self.assertEqual(rows[0]["parser_version"], 2)
                old = store.numeric_rows(store.step("TEST001", "numeric-history"))
                changed = report()
                changed["tables"][0]["rows"][0][1] = "-1.00 -1.00 X 170"
                store.save_step("TEST001", "numeric-history", "numeric", changed, "TEST")
                self.assertNotEqual(old, store.numeric_rows(store.step("TEST001", "numeric-history")))
                count = work.library_data.read({})["patients"][0]["categories"]["numeric"]["count"]
                self.assertEqual(count, 1)
                request = {"mrns": ["TEST001"], "categories": ["numeric"]}
                preview = work.library_data.preview(request)
                self.assertEqual(preview["records"], 1)
                work.library_data.delete({**request, "fingerprint": preview["fingerprint"]})
                self.assertIsNone(store.step("TEST001", "numeric-structured:numeric-history"))
                self.assertFalse(store.raw_data("TEST001")["versions"])
            finally:
                app.close()

    def test_projection_in_api_is_json_numeric_and_no_account_bleed(self):
        with tempfile.TemporaryDirectory() as folder:
            app = BotApplication(Settings(), Path(folder), BotSyntheticSDK)
            try:
                one = app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
                two = app.login({"username": "OTHER", "password": "synthetic"})["account"]["id"]
                work = app.workspace(one)
                work.analysis.store.save_step("TEST001", "numeric-history", "numeric", report(), "TEST")
                rows = work.analysis.store.numeric_rows(work.analysis.store.step("TEST001", "numeric-history"))
                decoded = json.loads(json.dumps(rows))
                self.assertIsInstance(decoded[1]["measurements"][0]["values"]["kavg"], float)
                self.assertFalse(app.workspace(two).analysis.store.steps("TEST001"))
                self.assertEqual(rows, decoded)
            finally:
                app.close()
