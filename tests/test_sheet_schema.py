"""The planner, migration and monitor must interpret the same typed workbook."""
import copy
import unittest
from datetime import date

from analysis_fixtures import book_fixture, literal, proposal

from vghks_bot.calendar_sync import full_day, headers, resource, scan
from vghks_bot.sheet_identity import EVENT_ID_HEADER, header_columns, plan_identity_setup
from vghks_bot.sheet_plan import FUPlanner, Planner, columns, entries, native_cell, typed_day
from vghks_bot.sheet_schema import (
    DATE_HEADER,
    DIAGNOSIS_FORMULA_MARKER,
    canonical_header,
    full_date,
)


def source_book():
    book = book_fixture()
    # The old planner fixture intentionally shares an event value to check cell
    # preservation; a cross-consumer fixture needs independently valid events.
    for row in book["sheets"][0]["rows"][1:]:
        if len(row) > 22:
            row[22] = {}
    return book


class HeaderContractTests(unittest.TestCase):
    def test_aliases_are_shared_and_legacy_event_column_is_renamed_in_place(self):
        for alias in (DATE_HEADER, "日期/報到時間", "時間"):
            for event_alias in (EVENT_ID_HEADER, "日曆eventID", "日曆apiEventID"):
                with self.subTest(date=alias, event=event_alias):
                    book = source_book()
                    sheet = book["sheets"][0]
                    sheet["rows"][0][0] = literal(alias)
                    sheet["rows"][0][22] = literal(event_alias)
                    sheet["rows"][2][22] = literal("keep-event")
                    setup = plan_identity_setup(book)
                    self.assertFalse(setup["issues"])
                    target = setup["book"]["sheets"][0]
                    self.assertEqual(header_columns(target)[DATE_HEADER], 0)
                    self.assertEqual(columns(target)["date"], 0)
                    self.assertEqual(headers(target)[1][DATE_HEADER], 0)
                    self.assertEqual(header_columns(target)[EVENT_ID_HEADER], 22)
                    self.assertEqual(target["rows"][0][22], literal(EVENT_ID_HEADER))
                    self.assertEqual(target["rows"][2][22], literal("keep-event"))
                    self.assertFalse(scan(setup["book"])["issues"])

    def test_real_duplicate_alias_columns_are_rejected_by_all_consumers(self):
        for label in ("時間", "日曆apiEventID", "診斷｜重複"):
            with self.subTest(label=label):
                book = source_book()
                sheet = book["sheets"][0]
                sheet["rows"][0].append(literal(label))
                self.assertTrue(plan_identity_setup(book)["issues"])
                self.assertTrue(scan(book)["issues"])
                with self.assertRaisesRegex(ValueError, "重複"):
                    columns(sheet)

    def test_diagnosis_only_accepts_exact_summary_prefix_or_known_formula(self):
        accepted = [literal("診斷"), literal("診斷｜CATA 2"),
                    {"userEnteredValue": {"formulaValue": f'=N("{DIAGNOSIS_FORMULA_MARKER}")&"診斷"'}}]
        rejected = [literal("診斷1"), literal("診斷註記"),
                    {"userEnteredValue": {"formulaValue": '="診斷"&COUNTIF(H2:H99,"CATA")'}}]
        for cell in accepted + rejected:
            with self.subTest(cell=cell):
                sheet = source_book()["sheets"][0]
                sheet["rows"][0][7] = copy.deepcopy(cell)
                recognized = cell in accepted
                self.assertEqual(canonical_header(cell) == "診斷", recognized)
                self.assertEqual("診斷" in header_columns(sheet), recognized)
                self.assertEqual("診斷" in headers(sheet)[1], recognized)
                if recognized:
                    self.assertEqual(columns(sheet)["diagnosis"], 7)
                else:
                    with self.assertRaisesRegex(ValueError, "診斷"):
                        columns(sheet)

    def test_fu_date_is_not_a_monthly_header_alias(self):
        self.assertEqual(canonical_header("日期"), "日期")
        self.assertEqual(canonical_header({}), "")


class DateContractTests(unittest.TestCase):
    def test_exact_date_boundaries_formats_and_weekday_match_all_paths(self):
        serial = (date(2026, 10, 12) - date(1899, 12, 30)).days
        examples = [(literal(serial), "2026-10-12"), (literal(float(serial)), "2026-10-12"),
                    (literal("2026/10/12 Mon"), "2026-10-12"),
                    (literal("2026.10.12 mon"), "2026-10-12"),
                    (literal("20261012"), "2026-10-12"),
                    (literal("2000-1-1 Sat"), "2000-01-01"),
                    (literal("2099-12-31 Thu"), "2099-12-31"),
                    (literal(serial + 0.5), ""), (literal("2026-10-12 Tue"), ""),
                    (literal("2026-10-12 Monday"), ""), (literal("2026-10-12 09:30"), ""),
                    (literal("115/10/12"), ""), (literal("1999-12-31"), ""),
                    (literal("2100-01-01"), ""), (literal("2026-02-30"), ""),
                    (literal("on 2026-10-12"), ""), (literal(0.5), ""),
                    (literal("２０２６/１０/１２"), ""), (literal("2026／10／12"), "")]
        for cell, expected in examples:
            with self.subTest(cell=cell):
                self.assertEqual(full_date(cell), expected)
                self.assertEqual(typed_day(cell), expected)
                self.assertEqual(full_day(cell), expected)
        self.assertEqual(full_date(serial), "2026-10-12")
        self.assertEqual(full_date(20261012), "")

    def test_formula_effective_date_cannot_hide_fractional_time(self):
        serial = (date(2026, 10, 12) - date(1899, 12, 30)).days
        cell = {"userEnteredValue": {"formulaValue": "=DATE(2026,10,12)+0.5"},
                "effectiveValue": {"numberValue": serial + 0.5}, "formattedValue": "2026/10/12"}
        self.assertEqual(full_date(cell), "")
        cell["effectiveValue"]["numberValue"] = serial
        self.assertEqual(full_date(cell), "2026-10-12")


class BlockContractTests(unittest.TestCase):
    def assert_block_rejected(self, mutate):
        book = source_book()
        mutate(book["sheets"][0])
        setup = plan_identity_setup(book)
        self.assertTrue(setup["issues"])
        self.assertTrue(scan(setup["book"])["issues"])
        with self.assertRaises(ValueError):
            entries(setup["book"]["sheets"][0])
        with self.assertRaises(ValueError):
            Planner(setup["book"], "synthetic", [proposal()]).build()
        return setup

    def test_missing_wrong_or_conflicting_header_fields_reject_all_paths(self):
        mutations = [lambda s: s["rows"][1].__setitem__(0, {}),
                     lambda s: s["rows"][1].__setitem__(0, literal("2026-09-30 Thu")),
                     lambda s: s["rows"][1][0]["userEnteredValue"].__setitem__("numberValue", 46295.5),
                     lambda s: s["rows"][1].__setitem__(1, {}),
                     lambda s: s["rows"][1].__setitem__(1, literal("高雄榮總")),
                     lambda s: s["rows"][1].__setitem__(2, {}),
                     lambda s: s["rows"][1].__setitem__(2, literal("◆ 非刀日")),
                     lambda s: s["rows"][1].__setitem__(22, literal("unexpected-event"))]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.assert_block_rejected(mutate)

    def test_malformed_second_header_never_reuses_preceding_valid_day(self):
        setup = self.assert_block_rejected(lambda s: s["rows"][4].__setitem__(2, {}))
        patients = scan(setup["book"])["patients"]
        self.assertFalse(any(patient["row"] == 5 for patient in patients))

    def test_patient_full_date_in_arrival_cell_is_invalid_not_another_patient(self):
        for cell in (literal("2026/9/30"), literal("2026-09-30 08:30"), literal(46295.5)):
            with self.subTest(cell=cell):
                self.assert_block_rejected(lambda sheet, c=cell: sheet["rows"][2].__setitem__(0, c))

    def test_marker_whitespace_and_fractional_arrival_preserve_block_route(self):
        book = source_book()
        sheet = book["sheets"][0]
        sheet["rows"][1][2] = literal(" ◆　 刀日 ")
        sheet["rows"][2][0] = literal(0.5)
        setup = plan_identity_setup(book)
        self.assertFalse(setup["issues"])
        result = scan(setup["book"])
        self.assertFalse(result["issues"])
        main = entries(setup["book"]["sheets"][0])[1]
        self.assertEqual(main["route"]["hospital"], "聯醫")
        self.assertEqual(main["route"]["date"], result["patients"][0]["date"])
        self.assertIn("T12:00:00+08:00", resource(result["patients"][0], "synthetic")["start"]["dateTime"])

    def test_plain_numeric_hhmm_arrival_is_not_mistaken_for_a_date_serial(self):
        for cell in (literal(830), literal(1230), {"userEnteredValue": {"formulaValue": "=TIME(8,30,0)"},
                     "effectiveValue": {"numberValue": 8.5 / 24}, "formattedValue": "8:30 AM"}):
            with self.subTest(cell=cell):
                book = source_book()
                book["sheets"][0]["rows"][2][0] = cell
                setup = plan_identity_setup(book)
                self.assertFalse(setup["issues"])
                result = scan(setup["book"])
                self.assertFalse(result["issues"])
                self.assertEqual(entries(setup["book"]["sheets"][0])[1]["kind"], "patient")
                self.assertIn("dateTime", resource(result["patients"][0], "synthetic")["start"])

    def test_destination_hospital_is_limited_even_without_a_dropdown(self):
        setup = plan_identity_setup(source_book())
        draft = proposal(mrn="99")
        draft["destination"] = {"date": "2026-10-12", "hospital": "高雄榮總"}
        with self.assertRaisesRegex(ValueError, "高榮或聯醫"):
            Planner(setup["book"], "synthetic", [draft]).build()

    def test_only_op_followup_tab_requires_explicit_manual_rename(self):
        book = source_book()
        book["sheets"].append({"properties": {"title": "OP", "sheetId": 900}, "rows": []})
        draft = {"id": "fu", "fields": {"mrn": "3", "tag": "FU"}}
        with self.assertRaisesRegex(ValueError, "重新命名為 FU"):
            FUPlanner(book, "synthetic", [draft]).build()
        self.assertFalse(any(sheet["properties"]["title"] == "FU" for sheet in book["sheets"]))

    def test_archive_missing_type_fails_before_migration(self):
        book = source_book()
        book["sheets"].append({"properties": {"title": "刀表封存_2026", "sheetId": 900,
            "gridProperties": {"rowCount": 100, "columnCount": 4}}, "merges": [],
            "rows": [[], [], [], [], [], list(map(literal, ("病歷號", "姓名", "CalendarEventId", "ScheduleSyncId"))),
                     list(map(literal, ("3", "封存患者", "", "a" * 32)))]})
        setup = plan_identity_setup(book)
        self.assertTrue(any(issue["code"] == "missing_archive_type" for issue in setup["issues"]))
        self.assertTrue(any("ArchiveRowType" in issue for issue in scan(setup["book"])["issues"]))

    def test_new_month_refuses_drift_in_any_month_including_manual_columns(self):
        book = plan_identity_setup(source_book())["book"]
        second = copy.deepcopy(book["sheets"][0])
        second["properties"].update(title="202610", sheetId=901)
        second["rows"] = [second["rows"][0]]
        second["rows"][0][12] = literal("人工欄異動")
        book["sheets"].append(second)
        with self.assertRaisesRegex(ValueError, "所有月表"):
            Planner(book, "synthetic", [proposal(date="2026-11-02", mrn="99")]).build()
        self.assertEqual(len(book["sheets"]), 2)

    def test_new_month_semantic_headers_ignore_aliases_and_trailing_grid_blanks(self):
        book = plan_identity_setup(source_book())["book"]
        second = copy.deepcopy(book["sheets"][0])
        second["properties"].update(title="202610", sheetId=901)
        second["rows"] = [second["rows"][0]]
        second["rows"][0][0] = literal("時間")
        second["rows"][0][22] = literal("日曆apiEventID")
        second["rows"][0].extend([{}, {}, {}])
        second["properties"]["gridProperties"]["columnCount"] += 3
        book["sheets"].append(second)
        planner = Planner(book, "synthetic", [proposal(date="2026-11-02", mrn="99")])
        preview = planner.build()
        self.assertTrue(preview["can_apply"])
        self.assertEqual(planner.book["sheets"][-1]["properties"]["title"], "202611")

    def test_new_month_formula_header_snapshot_supports_move_without_writing_derived_fields(self):
        book = plan_identity_setup(source_book())["book"]
        source = book["sheets"][0]
        source["rows"][0][16] = {"userEnteredValue": {"formulaValue": '="Manual Header"'},
                                 "effectiveValue": {"stringValue": "Manual Header"},
                                 "formattedValue": "Manual Header", "note": "keep header note"}
        draft = proposal()
        draft["target_sync_id"] = entries(source)[3]["sync_id"]
        draft["destination"] = {"date": "2026-10-12", "hospital": "高榮"}
        planner = Planner(book, "synthetic", [draft])
        preview = planner.build()
        self.assertTrue(preview["can_apply"])
        self.assertEqual(preview["items"][0]["action"], "move")
        created = planner.book["sheets"][-1]
        self.assertEqual(created["rows"][0], source["rows"][0])
        header_request = next(request["updateCells"] for request in preview["requests"]
                              if "updateCells" in request
                              and request["updateCells"]["range"]["sheetId"] == created["properties"]["sheetId"]
                              and request["updateCells"]["range"]["startRowIndex"] == 0)
        self.assertEqual(header_request["rows"][0]["values"], [native_cell(cell) for cell in source["rows"][0]])
        for request in preview["requests"]:
            for row in request.get("updateCells", {}).get("rows", []):
                for cell in row["values"]:
                    self.assertNotIn("formattedValue", cell)
                    self.assertNotIn("effectiveValue", cell)

    def test_new_month_refuses_unnamed_manual_body_columns_missing_in_another_month(self):
        for content in (literal("manual data"), {"note": "manual note"},
                        {"textFormatRuns": [{"startIndex": 0, "format": {"bold": True}}]}):
            with self.subTest(content=content):
                book = plan_identity_setup(source_book())["book"]
                source = book["sheets"][0]
                second = copy.deepcopy(source)
                second["properties"].update(title="202610", sheetId=901)
                second["rows"] = [second["rows"][0]]
                source["rows"][2].extend([{}, {}, copy.deepcopy(content)])
                source["properties"]["gridProperties"]["columnCount"] += 3
                book["sheets"].append(second)
                with self.assertRaisesRegex(ValueError, "所有月表"):
                    Planner(book, "synthetic", [proposal(date="2026-11-02", mrn="99")]).build()

    def test_new_month_keeps_unnamed_manual_column_contract_until_whole_row_move(self):
        book = plan_identity_setup(source_book())["book"]
        source = book["sheets"][0]
        source["rows"][5].extend([{}, {}, literal("keep unnamed manual data")])
        source["properties"]["gridProperties"]["columnCount"] += 3
        expected_signature = Planner.header_signature(source)
        draft = proposal()
        draft["target_sync_id"] = entries(source)[3]["sync_id"]
        draft["destination"] = {"date": "2026-10-12", "hospital": "高榮"}
        planner = Planner(book, "synthetic", [draft])
        preview = planner.build()
        self.assertTrue(preview["can_apply"])
        created = planner.book["sheets"][-1]
        self.assertEqual(Planner.header_signature(created), expected_signature)
        moved = next(entry for entry in entries(created) if entry["kind"] == "patient")
        self.assertEqual(moved["row"][-1], literal("keep unnamed manual data"))
        self.assertEqual(planner._signature(planner.book["sheets"][0]), expected_signature)

    def test_new_month_ignores_format_only_and_empty_string_tail_columns(self):
        book = plan_identity_setup(source_book())["book"]
        source = book["sheets"][0]
        signature = Planner.header_signature(source)
        source["rows"][0].extend([literal(""), {"userEnteredFormat": {"backgroundColor": {"red": 1}}}])
        source["rows"][2].extend([literal(""), {"userEnteredFormat": {"backgroundColor": {"red": 1}}}])
        source["properties"]["gridProperties"]["columnCount"] += 2
        self.assertEqual(Planner.header_signature(source), signature)
        planner = Planner(book, "synthetic", [proposal(date="2026-11-02", mrn="99")])
        self.assertTrue(planner.build()["can_apply"])


if __name__ == "__main__":
    unittest.main()
