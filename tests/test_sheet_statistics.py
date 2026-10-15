"""Eye counts and report provenance from synthetic monthly and archived cells."""
import copy
import unittest

import test_sheet_management as management_tests
from analysis_fixtures import literal
from test_sheet_archive import prepared

from vghks_bot.sheet_archive_lock import acquire_request, archive_lock
from vghks_bot.sheet_schema import cancelled_cell
from vghks_bot.sheet_statistics import procedure_components, statistics


def row(**values):
    return {"date": "2026-09-15", "hospital": "高榮", "mrn": "TEST001", "name": "合成病人",
            "side": "OD", "procedure": "Phaco-IOL", "iol": "SN60WF", "sync_id": "a" * 32,
            "position": "202609!3", "_cancelled": False, **values}


class StatisticsTests(unittest.TestCase):
    def test_displayed_cancellation_includes_conditional_and_rich_text_overrides(self):
        cell = {"userEnteredValue": {"stringValue": "TEST001"}, "effectiveFormat": {"textFormat": {"strikethrough": True}}}
        self.assertTrue(cancelled_cell(cell))
        cell["textFormatRuns"] = [{"startIndex": 0, "format": {"strikethrough": False}}]
        self.assertFalse(cancelled_cell(cell))
        cell["textFormatRuns"].append({"startIndex": 4, "format": {"strikethrough": True}})
        self.assertTrue(cancelled_cell(cell))

    def test_overlapping_aliases_and_combination_components_are_counted_once_per_eye(self):
        report = statistics([row(side="OU", procedure="LenSx-Phaco-IOL+CATA+Phaco-IOL"),
                             row(procedure="VT+MP+VT", iol="", sync_id="b" * 32)], {"sheets": []})
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["eye_count"], 3)
        self.assertEqual({x["procedure"]: x["eye_count"] for x in report["procedures"]},
                         {"CATA": 2, "LenSx": 2, "VT": 1, "MP": 1})
        self.assertEqual(report["monthly"][0]["lensx_eye_count"], 2)
        self.assertEqual(report["monthly"][0]["cata_eye_count"], 2)
        self.assertEqual(procedure_components("MSICS-IOL"), ["CATA"])
        self.assertEqual(procedure_components("Suture IOL"), ["SUTURE IOL"])
        self.assertEqual(procedure_components("CATASTROPHE"), ["CATASTROPHE"])

    def test_cancelled_unknown_side_and_ambiguous_lenses_are_separate(self):
        report = statistics([row(side="OU", _cancelled=True), row(side=""),
                             row(side="OU", iol="SN60WF OD / DIB00 OS")], {"sheets": []})
        self.assertEqual((report["total"], report["eye_count"], report["excluded_cancelled"],
                          report["excluded_unknown_side"]), (2, 2, 1, 1))
        self.assertTrue(all(item["eye_count"] == 0 for item in report["models"]))
        self.assertEqual({item["code"] for item in report["statistics_issues"]}, {"unknown_side", "ambiguous_iol"})

    def test_iol_model_normalization_preserves_rows_and_separates_similar_models(self):
        report = statistics([row(side="OU", iol=" ｓｎ６０ｗｆ "), row(iol="sn60wf"), row(iol="SN60WF-A")], {"sheets": []})
        self.assertEqual(report["models"], [{"model": "SN60WF", "count": 2, "eye_count": 3},
                                            {"model": "SN60WF-A", "count": 1, "eye_count": 1}])

    def test_absent_month_is_distinct_from_a_known_empty_month(self):
        book = {"sheets": [{"properties": {"title": "202609"}}]}
        report = statistics([], book, "2026-09-01", "2026-10-31")
        self.assertTrue(report["monthly"][0]["available"])
        self.assertFalse(report["monthly"][1]["available"])
        self.assertEqual(report["statistics_issues"][0]["code"], "missing_report_month")

    def test_real_report_keeps_eye_totals_through_copy_and_finalization(self):
        env = management_tests.SheetManagementTests()
        env.setUp()
        preview = env.prepare_archive()
        env.client.book["sheets"][0]["rows"][2][6] = {"userEnteredValue": {"stringValue": "OU"}}
        earlier = prepared("202608", 102)["sheets"][0]
        earlier["rows"][0].append(literal("合成人工欄"))
        earlier["rows"][2].append(literal("保留人工內容"))
        earlier["properties"]["gridProperties"]["columnCount"] += 1
        earlier["column_widths"].append({"pixelSize": 110})
        env.client.book["sheets"].append(earlier)
        preview = env.preview("archive", months=["202608", "202609"])
        before = env.manager.report({})
        self.assertEqual(env.manager.apply({"id": preview["id"]})["status"], "copied")
        during = env.manager.report({})
        self.assertEqual((during["eye_count"], during["models"]), (before["eye_count"], before["models"]))
        self.assertFalse(during["complete"])
        self.assertEqual(env.manager.apply({"id": preview["id"]})["status"], "applied")
        after = env.manager.report({})
        self.assertEqual((after["eye_count"], after["models"]), (before["eye_count"], before["models"]))
        self.assertEqual(archive_lock(env.client.book), "")

    def test_foreign_archive_blocks_management_but_reports_remain_readable(self):
        env = management_tests.SheetManagementTests()
        env.setUp()
        env.client.book["developerMetadata"] = [copy.deepcopy(acquire_request("GAS-TEST")["createDeveloperMetadata"]["developerMetadata"])]
        preview = env.preview("create_month", month="202611")
        self.assertEqual(preview["status"], "blocked")
        report = env.manager.report({})
        self.assertEqual(report["total"], 3)
        self.assertTrue(any(issue["code"] == "archive_in_progress" for issue in report["issues"]))
        self.assertEqual(env.client.writes, 0)
