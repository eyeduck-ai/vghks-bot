import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from analysis_fixtures import ExamSDK, MemorySheets, book_fixture, proposal, soap_record

from opd_monitor.analysis_numeric import exam_name, extract_tables, numeric
from opd_monitor.analysis_store import AnalysisStore
from opd_monitor.google_sheets import SheetError
from opd_monitor.jobs import Application
from opd_monitor.settings import Settings
from opd_monitor.sheet_plan import Planner, cell_text, entries, fingerprint, verify
from opd_monitor.surgery_candidates import candidates


class AnalysisTests(unittest.TestCase):
    def test_eye_numeric_names_do_not_merge_vacc_into_va(self):
        self.assertEqual(exam_name("VacC"), "VacC")
        self.assertEqual(exam_name("Va"), "Va")
        self.assertEqual(exam_name("IOP-pneumo"), "IOP-pneumo")
        rows = extract_tables({"tables": [
            {"title": "VacC", "headers": ["日期", "OD"], "rows": [["2026-01-02", "0.8"]]},
            {"title": "IOP-pneumo", "headers": ["日期", "OS"], "rows": [["2026-01-02", "17"]]},
        ]}, "synthetic")
        self.assertEqual([row["exams"] for row in rows], [["VacC"], ["IOP-pneumo"]])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        ExamSDK.calls = []
        ExamSDK.fail_pdf = ExamSDK.fail_numeric = False
        ExamSDK.cancel_callback = None
        ExamSDK.generation = 0
        self.app = Application(Settings(), self.path, ExamSDK)
        self.key = next(iter(self.app.accounts))
        self.app.save_accounts(
            {"accounts": [{"id": self.key, "username": "TEST", "password": "private-test"}]}
        )
        self.record = soap_record()
        self.app.store.library.save_record(self.record, "TEST", "synthetic")
        self.app.store.library.save_record(soap_record(day="2026-09-17"), "TEST", "synthetic")
        self.cohort = self.app.analysis.save_cohort(
            {"source": "library", "all": True, "name": "synthetic", "filters": {"tag": "surgery"}}
        )

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def run_analysis(self, **options):
        reply = self.app.analysis.start(
            {
                "cohort_id": self.cohort["id"],
                "modules": ["retina", "cataract", "surgery"],
                **options,
            }
        )
        self.assertTrue(self.app.idle.wait(15))
        return self.app.analysis.store.document("analysis_runs", reply["run_ids"][0])

    def test_serial_patients_share_login_and_shared_content_survives_one_patient_delete(self):
        self.app.store.library.save_record(soap_record("0000002"), "TEST", "synthetic")
        self.cohort = self.app.analysis.save_cohort({"source": "library", "all": True, "name": "two"})
        self.assertEqual(self.run_analysis()["status"], "completed")
        self.assertEqual(sum(c[0] == "login" for c in ExamSDK.calls), 1)
        mrns = [call[1] for call in ExamSDK.calls if call[0] == "numeric"]
        self.assertEqual(len(set(mrns)), 2)
        store = self.app.analysis.store
        assets = list(store.assets.iterdir())
        self.app.analysis.delete_data({"cohort_id": self.cohort["id"], "mrn": "0000001"})
        self.assertEqual(list(store.assets.iterdir()), assets)
        self.assertTrue(store.steps("0000002"))
        store.raw_data("0000002")

    def test_raw_versions_and_orphan_cleanup_after_restart(self):
        self.run_analysis()
        store = self.app.analysis.store
        before = store.step("0000001", "numeric-history")
        store.save_step("0000001", "numeric-history", "numeric", {"mrn": "0000001", "tables": []}, "TEST")
        raw = store.raw_data("0000001")
        numeric_versions = [v["payload"] for v in raw["versions"] if v["key"] == "numeric-history"]
        self.assertIn(before["payload"], numeric_versions)
        self.assertEqual(len(numeric_versions), 2)
        orphan = store.assets / ("a" * 64)
        orphan.write_bytes(b"%PDF-1.4 synthetic interrupted deletion")
        AnalysisStore(self.app.store.library)
        self.assertFalse(orphan.exists())

    def test_cohort_deduplicates_mrns_retains_sources_and_cross_page_all(self):
        self.assertEqual(len(self.cohort["members"]), 1)
        self.assertEqual(len(self.cohort["members"][0]["source_records"]), 2)
        for i in range(1, 205):
            self.app.store.library.save_record(soap_record(str(i).zfill(7)), "TEST", "synthetic")
        cohort = self.app.analysis.save_cohort(
            {"source": "library", "all": True, "name": "all", "filters": {"limit": 1}}
        )
        self.assertEqual(len(cohort["members"]), 204)

    def test_manual_mrns_deduplicate_preserve_zeroes_and_analyze_without_registration(self):
        ExamSDK.calls.clear()
        cohort = self.app.analysis.save_cohort({"source": "manual", "account_id": self.key,
            "name": "直接分析", "mrns": "００００００１，0000002\n0000001;0000002"})
        self.assertEqual(ExamSDK.calls, [])
        self.assertEqual([m["mrn"] for m in cohort["members"]], ["0000001", "0000002"])
        self.assertEqual(len(cohort["members"][0]["source_records"]), 2)
        self.assertEqual(cohort["members"][1]["source_records"], [])
        self.assertTrue(all(m["account_id"] == self.key and m["origin"] == "manual" for m in cohort["members"]))
        self.cohort = cohort
        self.assertEqual(self.run_analysis(modules=["retina"])["status"], "completed")
        self.assertEqual({c[1] for c in ExamSDK.calls if c[0] == "numeric"}, {"0000001", "0000002"})
        self.assertFalse(any(c[0] in {"soap", "list"} for c in ExamSDK.calls))
        self.app.close()
        self.app = Application(Settings(), self.path, ExamSDK)
        ExamSDK.calls.clear()
        self.assertEqual(self.run_analysis(modules=["retina"])["status"], "completed")
        self.assertEqual(ExamSDK.calls, [])

    def test_manual_bad_input_is_rejected_without_partial_cohort_or_requests(self):
        before = len(self.app.analysis.store.documents("analysis_cohorts"))
        for raw in (None, 12345, ["000001"], "", "0000001 病人姓名", "../settings", "1/2", "1e+6",
                    "1" * 33, "x" * 20001, "\n".join(str(i) for i in range(501))):
            with self.subTest(raw=str(raw)[:40]), self.assertRaises(ValueError):
                self.app.analysis.save_cohort({"source": "manual", "account_id": self.key, "name": "bad", "mrns": raw})
        self.assertEqual(len(self.app.analysis.store.documents("analysis_cohorts")), before)
        self.assertEqual(ExamSDK.calls, [])

    def test_google_connection_probe_closes_client_on_success_and_failure(self):
        client = Mock()
        client.connection_info.return_value = {"ok": True, "title": "Synthetic"}
        self.app.analysis.client_factory = lambda: client
        self.assertTrue(self.app.analysis.handle("google/test", {})["ok"])
        client.close.assert_called_once()
        client.reset_mock()
        client.connection_info.side_effect = SheetError("permission")
        with self.assertRaises(SheetError):
            self.app.analysis.handle("google/test", {})
        client.close.assert_called_once()

    def test_all_modules_share_history_preserve_special_va_and_old_cases(self):
        run = self.run_analysis()
        self.assertEqual(run["status"], "completed", run["issues"])
        self.assertEqual(sum(c[0] == "numeric" for c in ExamSDK.calls), 1)
        self.assertEqual(sum(c[0] == "pdf" for c in ExamSDK.calls), 1)
        self.assertIn(("case_numeric", "0000001", "2010-01-01"), ExamSDK.calls)
        self.assertFalse(any(c[0] == "detail" and c[-1] == "99" for c in ExamSDK.calls))
        data = self.app.analysis.results(
            {"cohort_id": self.cohort["id"], "mrn": "0000001", "module": "retina"}
        )
        values = [c for r in data["numeric"] for c in r["cells"]]
        self.assertIn("HM", [c["raw"] for c in values])
        self.assertTrue(all(c["value"] is None for c in values if c["raw"] in {"HM", "CF"}))
        self.assertEqual(sum(r["date"] == "2025-01-01" for r in data["numeric"]), 2)
        self.assertEqual(len(data["coverage"]["unsupported_cases"]), 1)
        self.assertTrue(any(o["assets"] for o in data["orders"]))
        self.assertEqual(
            self.app.analysis.store.document("analysis_cohorts", self.cohort["id"])["members"][0][
                "account_id"
            ],
            self.key,
        )

    def test_rerun_restart_cache_and_refresh_only_indices(self):
        self.assertEqual(self.run_analysis()["status"], "completed")
        ExamSDK.calls.clear()
        self.app.close()
        self.app = Application(Settings(), self.path, ExamSDK)
        self.assertEqual(self.run_analysis()["status"], "completed")
        self.assertEqual(ExamSDK.calls, [])
        self.app.save_accounts({"accounts": [{"id": self.key, "password": "private-test"}]})
        self.assertEqual(self.run_analysis(refresh=True)["status"], "completed")
        self.assertTrue(any(c[0] == "numeric" for c in ExamSDK.calls))
        self.assertFalse(any(c[0] in {"pdf", "image", "detail", "report"} for c in ExamSDK.calls))

    def test_pdf_failure_keeps_all_images_and_resume_only_retries_failure(self):
        ExamSDK.fail_pdf = True
        run = self.run_analysis()
        self.assertEqual(run["status"], "partial")
        calls_before = list(ExamSDK.calls)
        self.assertGreater(sum(c[0] == "image" for c in calls_before), 1)
        ExamSDK.fail_pdf = False
        ExamSDK.calls.clear()
        resumed = self.run_analysis(resume=run["id"])
        self.assertEqual(resumed["status"], "completed", resumed["issues"])
        self.assertEqual(sum(c[0] == "pdf" for c in ExamSDK.calls), 1)
        self.assertFalse(any(c[0] == "image" for c in ExamSDK.calls))

    def test_empty_reports_refresh_and_foreign_patient_references_are_rejected(self):
        from vghks_sdk.models import OrderDetail, OrderReportRef

        with patch.object(ExamSDK, "detail", lambda _, ref: OrderDetail(ref, {})):
            run = self.run_analysis()
            self.assertEqual(run["status"], "completed")
        ExamSDK.calls.clear()
        self.assertEqual(self.run_analysis(refresh=True)["status"], "completed")
        self.assertTrue(any(c[0] == "pdf" for c in ExamSDK.calls))
        with patch.object(
            ExamSDK,
            "detail",
            lambda _, ref: OrderDetail(
                ref,
                {},
                report_refs=(
                    OrderReportRef("FOREIGN", ref.case_no, ref.case_type, ref.sequence_no),
                ),
            ),
        ):
            run = self.run_analysis(force=True)
            self.assertEqual(run["status"], "partial")
        self.assertFalse(any("FOREIGN" in call for call in ExamSDK.calls))

    def test_linked_old_mrn_is_backfilled_under_the_queried_patient(self):
        original = ExamSDK.visits
        def linked(sdk, mrn):
            return [replace(case, mrn="OLD0001", lookup_mrn=mrn) if case.case_no == "OLD" else case
                    for case in original(sdk, mrn)]
        with patch.object(ExamSDK, "visits", linked):
            run = self.run_analysis()
        self.assertEqual(run["status"], "completed", run["issues"])
        self.assertIn(("case_numeric", "OLD0001", "2010-01-01"), ExamSDK.calls)

    def test_account_change_on_resume_reuses_successful_downloads(self):
        ExamSDK.fail_pdf = True
        run = self.run_analysis()
        self.app.save_accounts({"accounts": [{"username": "OTHER", "password": "second-secret"}]})
        key = next(k for k, a in self.app.accounts.items() if a.username == "OTHER")
        self.app.analysis.save_cohort(
            {"id": self.cohort["id"], "name": "synthetic", "assignments": {"0000001": key}}
        )
        ExamSDK.fail_pdf = False
        ExamSDK.calls.clear()
        resumed = self.run_analysis(resume=run["id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertIn(("login", "OTHER"), ExamSDK.calls)
        self.assertFalse(any(c[0] in {"numeric", "orders", "image"} for c in ExamSDK.calls))

    def test_cancel_during_query_preserves_complete_step(self):
        def cancel():
            for state in self.app.states.values():
                state.cancel.set()

        ExamSDK.cancel_callback = cancel
        run = self.run_analysis()
        self.assertEqual(run["status"], "cancelled")
        self.assertIsNotNone(self.app.analysis.store.step("0000001", "numeric-history"))
        ExamSDK.cancel_callback = None
        ExamSDK.calls.clear()
        resumed = self.run_analysis(resume=run["id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertFalse(any(c[0] == "numeric" for c in ExamSDK.calls))

    def test_deletion_removes_versions_assets_and_does_not_resurrect_on_restart(self):
        self.run_analysis()
        store = self.app.analysis.store
        self.assertTrue(list(store.assets.iterdir()))
        self.app.analysis.delete_data({"cohort_id": self.cohort["id"], "mrn": "0000001"})
        self.assertFalse(store.steps("0000001"))
        self.assertFalse(list(store.assets.iterdir()))
        self.app.close()
        self.app = Application(Settings(), self.path, ExamSDK)
        self.assertFalse(self.app.analysis.store.steps("0000001"))
        self.assertEqual(self.app.library_search({})["total"], 2)

    def test_no_credentials_in_sqlite_or_run_journals(self):
        self.run_analysis()
        for path in self.path.rglob("*"):
            if path.is_file() and path.name != ".lock":
                self.assertNotIn(b"private-test", path.read_bytes(), str(path))

    def test_candidate_repeated_tags_both_eyes_and_latest_changes(self):
        records = [
            soap_record(day="2026-09-17"),
            soap_record(target="-1.00"),
            soap_record(side="OS"),
        ]
        rows = candidates(records)
        self.assertEqual(len(rows), 2)
        od = next(r for r in rows if r["fields"]["side"] == "OD")
        self.assertEqual(len(od["sources"]), 2)
        self.assertEqual(od["fields"]["target"], "")
        self.assertEqual(od["fields"]["diagnosis"], "CATA")
        self.assertEqual(od["fields"]["ga"], "GA")
        self.assertEqual(od["differences"]["target"], ["-0.50", "-1.00"])

    def prepare_sheet(self):
        self.sheets = MemorySheets()
        self.app.analysis.client_factory = lambda: self.sheets
        row = self.app.analysis.surgery_candidates({"cohort_id": self.cohort["id"]})["candidates"][
            0
        ]
        return self.app.analysis.preview(
            {
                "cohort_id": self.cohort["id"],
                "proposals": [
                    {"id": row["id"], "fields": {}, "selected_fields": ["iol", "target"]}
                ],
            }
        )

    def test_sheet_apply_timeout_is_idempotent_and_verifiable(self):
        preview = self.prepare_sheet()
        self.sheets.timeout_after_commit = True
        value = self.app.analysis.apply({"id": preview["id"]})
        self.assertEqual(value["status"], "uncertain")
        self.sheets.timeout_after_commit = False
        value = self.app.analysis.apply({"id": preview["id"]})
        self.assertEqual(value["status"], "applied")
        self.assertEqual(self.sheets.writes, 1)

    def test_sheet_timeout_before_commit_retries_once_and_permission_failure_is_preserved(self):
        preview = self.prepare_sheet()
        self.sheets.timeout_before_commit = True
        value = self.app.analysis.apply({"id": preview["id"]})
        self.assertEqual(value["status"], "uncertain")
        self.sheets.timeout_before_commit = False
        self.assertEqual(self.app.analysis.apply({"id": preview["id"]})["status"], "applied")
        preview = self.prepare_sheet()
        with patch.object(self.sheets, "write", side_effect=SheetError("permission")):
            with self.assertRaises(SheetError):
                self.app.analysis.apply({"id": preview["id"]})
        self.assertEqual(self.app.analysis.store.document("sheet_previews", preview["id"])["status"], "failed")

    def test_sheet_preview_rejects_concurrent_edit_and_deleted_source(self):
        preview = self.prepare_sheet()
        self.sheets.book["sheets"][0]["rows"][5][15]["note"] = "changed manually"
        with self.assertRaisesRegex(ValueError, "刀表內容"):
            self.app.analysis.apply({"id": preview["id"]})
        self.assertEqual(self.sheets.writes, 0)
        self.app.delete_records({"ids": [self.record["id"]]})
        with self.assertRaises(ValueError):
            self.app.analysis.apply({"id": preview["id"]})

    def test_restart_keeps_existing_records_and_marks_analysis_interrupted(self):
        store = self.app.analysis.store
        doc = {"id": "a" * 32, "status": "running", "members": [], "cohort_id": self.cohort["id"]}
        store.save_document("analysis_runs", doc)
        recovered = AnalysisStore(self.app.store.library)
        self.assertEqual(recovered.document("analysis_runs", doc["id"])["status"], "interrupted")
        self.assertEqual(self.app.library_search({})["total"], 2)
        with self.app.store.library.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)


class SheetPlannerTests(unittest.TestCase):
    def execute(self, proposals, book=None):
        client = MemorySheets(book)
        preview = Planner(client.read(), client.key, proposals).build()
        self.assertTrue(preview["can_apply"])
        client.write(preview["requests"])
        self.assertEqual(verify(client.read(), preview), "verified")
        return client, preview

    def test_update_preserves_other_hospital_manual_cells_formula_and_arrival(self):
        original = book_fixture()
        client, _ = self.execute([proposal()])
        rows = client.book["sheets"][0]["rows"]
        self.assertEqual(rows[2], original["sheets"][0]["rows"][2])
        self.assertEqual(rows[0], original["sheets"][0]["rows"][0])
        for index in (0, 12, 13, 14, 15, 18, 22):
            self.assertEqual(rows[5][index], original["sheets"][0]["rows"][5][index])
        self.assertEqual(cell_text(rows[5][10]), "SN60WF")
        self.assertEqual(cell_text(rows[5][11]), "-0.50")

    def test_new_patient_uses_blank_row_without_cloning_patient_specific_content(self):
        client, preview = self.execute([proposal(mrn="0000003", side="OS", name="新增病人")])
        row = client.book["sheets"][0]["rows"][6]
        self.assertEqual(cell_text(row[2]), "0000003")
        for index in (0, 12, 13, 15, 18, 22):
            self.assertFalse(row[index].get("userEnteredValue"))
            self.assertFalse(row[index].get("note"))
        self.assertEqual(row[6]["dataValidation"]["strict"], True)
        self.assertEqual(preview["items"][0]["action"], "insert")

    def test_create_month_and_day_and_duplicate_metadata_rejects_entire_batch(self):
        p = proposal(date="2026-10-09", mrn="0000003", side="OS")
        client, preview = self.execute([p])
        self.assertEqual(len(client.book["sheets"]), 2)
        new = client.book["sheets"][1]
        self.assertEqual(new["properties"]["title"], "202610")
        patients = [e for e in entries(new) if e["kind"] == "patient"]
        self.assertEqual(patients[0]["fields"]["date"], "2026-10-09")
        snapshot = fingerprint(client.read())
        with self.assertRaises(SheetError):
            client.write(preview["requests"])
        self.assertEqual(fingerprint(client.read()), snapshot)

    def test_reschedule_requires_matching_event_then_moves_complete_row(self):
        p = proposal(date="2026-10-09")
        preview = Planner(book_fixture(), MemorySheets.key, [p]).build()
        self.assertFalse(preview["can_apply"])
        self.assertEqual(preview["items"][0]["status"], "conflict")
        p["target"] = "202609!6"
        client, preview = self.execute([p])
        old = client.book["sheets"][0]
        self.assertFalse(
            any(
                e["kind"] == "patient"
                and e["fields"]["mrn"] == "1"
                and e["fields"]["hospital"] == "高榮"
                for e in entries(old)
            )
        )
        new = client.book["sheets"][1]
        row = next(e["row"] for e in entries(new) if e["kind"] == "patient")
        self.assertEqual(cell_text(row[0]), "08:15")
        self.assertEqual(cell_text(row[22]), "event-keep")
        self.assertEqual(row[15]["note"], "manual cell note")

    def test_same_month_moves_in_both_directions_and_final_positions_after_inserts(self):
        for when in ("2026-09-29", "2026-09-28"):
            with self.subTest(day=when):
                p = proposal(date=when)
                p["target"] = "202609!6"
                self.execute([p, proposal(mrn="0000003", date="2026-09-27")])

    def test_duplicate_identity_and_strict_validation_fail_closed(self):
        with self.assertRaises(ValueError):
            Planner(book_fixture(), MemorySheets.key, [proposal(), proposal()]).build()
        p = proposal(side="INVALID")
        p["target"] = "202609!6"
        p["selected_fields"] = ["side"]
        with self.assertRaisesRegex(ValueError, "需為"):
            Planner(book_fixture(), MemorySheets.key, [p]).build()

    def test_two_candidates_cannot_overwrite_the_same_manual_row(self):
        first, second = proposal(), proposal(side="OS")
        first["target"] = second["target"] = "202609!6"
        with self.assertRaisesRegex(ValueError, "同一病人列"):
            Planner(book_fixture(), MemorySheets.key, [first, second]).build()

    def test_explicit_match_cannot_create_duplicate_event_and_merged_row_is_blocked(self):
        book = book_fixture()
        sheet = book["sheets"][0]
        sheet["rows"][6] = copy.deepcopy(sheet["rows"][5])
        sheet["rows"][6][6] = {"userEnteredValue": {"stringValue": "OS"}}
        p = proposal()
        p["target"] = "202609!7"
        with self.assertRaisesRegex(ValueError, "重複排程"):
            Planner(book, MemorySheets.key, [p]).build()
        book = book_fixture()
        book["sheets"][0]["merges"] = [
            {"startRowIndex": 5, "endRowIndex": 6, "startColumnIndex": 15, "endColumnIndex": 17}
        ]
        with self.assertRaisesRegex(ValueError, "合併儲存格"):
            Planner(book, MemorySheets.key, [proposal()]).build()


class NumericTests(unittest.TestCase):
    def test_sdk_column_paths_keep_eye_order_and_unaligned_tables_raw(self):
        report = {"case": {"visit_date": "2026-09-01"}, "tables": [{
            "title": "Va", "headers": ["日期", "Va", "OS", "OD"],
            "header_rows": [["日期", "Va"], ["OS", "OD"]],
            "column_paths": [["日期"], ["Va", "OS"], ["Va", "OD"]],
            "rows": [["115/9/1", "error", "0.7"], ["115/9/1", "", "0.8"]],
            "parsing_issues": ["NUMERIC_HEADER_SPAN_MISMATCH"],
        }]}
        rows = extract_tables(report, "case")
        self.assertEqual(rows[0]["headers"], ["日期", "Va / OS", "Va / OD"])
        self.assertEqual([(c["side"], c["raw"]) for c in rows[0]["cells"]],
                         [("OS", "error"), ("OD", "0.7")])
        self.assertEqual([(c["side"], c["raw"]) for c in rows[1]["cells"]], [("OD", "0.8")])
        report["tables"][0]["column_paths"] = []
        unaligned = extract_tables(report, "case")
        self.assertTrue(all(row["unparsed"] and not row["cells"] for row in unaligned))
        self.assertEqual(unaligned[0]["values"], ["115/9/1", "error", "0.7"])

    def test_explicit_units_eye_date_and_unparseable_values(self):
        report = {
            "tables": [
                {
                    "title": "Va",
                    "headers": ["日期", "OD", "OS"],
                    "rows": [["115/9/1", "0.4", "HM"], ["115/9/1", "0.5", "CF"]],
                }
            ]
        }
        rows = extract_tables(report, "history")
        self.assertEqual(rows[0]["date"], "2026-09-01")
        self.assertEqual(rows[0]["cells"][0]["side"], "OD")
        self.assertIsNone(rows[0]["cells"][1]["value"])
        self.assertEqual(len(rows), 2)
        for value in ("CF", "HM", "<0.1", "6/60", "nan", "inf", "0.3(+2)"):
            self.assertIsNone(numeric(value))

    def test_transposed_dates_keep_same_trend_and_types_do_not_mix(self):
        rows = extract_tables(
            {
                "tables": [
                    {
                        "title": "Va",
                        "headers": ["項目", "2024-01-01", "2025-01-01"],
                        "rows": [["Va OD BCVA", "0.3", "0.5"]],
                    }
                ]
            },
            "history",
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["cells"][0]["trend"], rows[1]["cells"][0]["trend"])
        self.assertEqual(rows[0]["cells"][0]["side"], "OD")
        self.assertEqual([r["date"] for r in rows], ["2024-01-01", "2025-01-01"])

    def test_mixed_exam_header_does_not_classify_unrelated_numbers_as_va(self):
        rows = extract_tables(
            {
                "tables": [
                    {
                        "title": "Va",
                        "headers": ["日期", "Va OD", "KM OD", "序號"],
                        "rows": [["2025-01-01", "0.4", "43", "123"]],
                    }
                ]
            },
            "history",
        )
        self.assertEqual([c["raw"] for c in rows[0]["cells"]], ["0.4", "43"])
        a, b = extract_tables(
            {
                "tables": [
                    {
                        "title": "KM",
                        "headers": ["日期", "側別", "單位", "K1"],
                        "rows": [
                            ["2025-01-01", "OD", "D", "43"],
                            ["2025-01-02", "OD", "mm", "7.8"],
                        ],
                    }
                ]
            },
            "history",
        )
        self.assertNotEqual(a["cells"][0]["trend"], b["cells"][0]["trend"])
