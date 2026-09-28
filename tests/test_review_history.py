"""History browsing keeps patient identity, cached data and auth recovery bounded."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vghks_sdk import AuthenticationError, VisitCase
from vghks_sdk.models import (
    BinaryAsset,
    ClinicalOrder,
    OrderDetail,
    OrderDetailRef,
    OrderReport,
    OrderReportRef,
    PacsImageRef,
    PacsStudy,
    PacsStudyRef,
    PdfAttachmentRef,
    ScannedRecord,
    UploadHistory,
)
from vghks_sdk.models.documents import HtmlDocument

from opd_monitor.analysis_fetch import clean
from opd_monitor.analysis_store import digest
from opd_monitor.bot import BotApplication
from opd_monitor.scanned_records import scan_id
from opd_monitor.selftest_bot import BotSyntheticSDK, wait_task
from opd_monitor.settings import Settings, today

MRN = "TEST001"
ALIAS = "ALIAS001"
PDF = PdfAttachmentRef(ALIAS, r"\\HFS01_1A0\EMRU\ALIAS001\report.pdf")
SCAN_OPG = PdfAttachmentRef(MRN, r"\\HFS01_1A0\OPG\TEST001\opg.pdf")
SCAN_LINKED = PdfAttachmentRef(MRN, r"\\HFS01_1A0\EMRU\TEST001\linked.pdf")
SCAN_PENDING = PdfAttachmentRef(MRN, r"\\HFS01_1A0\EMRU\TEST001\pending.pdf")
SCAN_OTHER = PdfAttachmentRef(MRN, r"\\HFS01_1A0\EMRU\TEST001\other.pdf")
EYE_CATEGORY = "門診-記錄-眼科紀錄-空白紀錄單"
OTHER_CATEGORY = "門診-記錄-內科紀錄"
CONSENT_CATEGORY = "同意書-手術/麻醉-術前標示(OPH)"
DETAIL = OrderDetailRef(ALIAS, "LEGACY", "O", "1")
REPORT = OrderReportRef(ALIAS, "LEGACY", "O", "1")
STUDY = PacsStudyRef(ALIAS, "REQUEST1")
IMAGE = PacsImageRef(ALIAS, "REQUEST1", "SERIES1", "STUDY1", "IMAGE1")


class HistorySDK(BotSyntheticSDK):
    calls = []
    logins = 0
    expire_once = False
    expire_pdf_once = False
    reject_new_sessions = False
    fail_scan_case = ""
    fail_scan_history = False
    mismatched_scan_history = False
    fail_visits = False

    def __init__(self, settings):
        super().__init__(settings)
        type(self).logins += 1
        self.auth = SimpleNamespace(check=lambda **_: SimpleNamespace(ok=not type(self).reject_new_sessions))
        self.orders = SimpleNamespace(
            get_order_history=self.order_history,
            get_case_orders=self.case_orders,
            get_order_detail=self.order_detail,
            get_order_report=self.order_report,
            get_pacs_study=self.pacs_study,
            download_pdf=self.download_pdf,
            download_pacs_image=self.download_image,
        )
        self.records.get_upload_history = self.upload_history
        self.records.get_case_scanned_records = self.case_scans

    def soap(self, case):
        value = super().soap(case)
        return replace(value, scanned_pdf_refs=(SCAN_LINKED,)) if case.case_no == "ONE" else value

    def upload_history(self, mrn):
        self.calls.append((self.card, "scan-history", mrn))
        if type(self).fail_scan_history:
            type(self).fail_scan_history = False
            raise ValueError("synthetic scan index failure")
        if type(self).mismatched_scan_history:
            return UploadHistory(mrn, HtmlDocument((), "", ""), (),
                (ScannedRecord("RECORD", PDF, category_label=EYE_CATEGORY),))
        return UploadHistory(mrn, HtmlDocument((), "", ""), (),
            (ScannedRecord("OPG", SCAN_OPG, category_label=CONSENT_CATEGORY,
                           record_date=date(2026, 2, 1), section_label="病歷類別"),
             ScannedRecord("RECORD", SCAN_LINKED, category_label=EYE_CATEGORY,
                           record_date=date(2025, 1, 15), section_label="病歷類別"),
             ScannedRecord("RECORD", SCAN_OTHER, category_label=OTHER_CATEGORY,
                           record_date=date(2024, 1, 1), section_label="病歷類別"),
             ScannedRecord("RECORD", SCAN_PENDING),
             ScannedRecord("RECORD", SCAN_LINKED, category_label=EYE_CATEGORY,
                           record_date=date(2025, 1, 15), section_label="病歷類別")))

    def case_scans(self, case):
        self.calls.append((self.card, "scan-case", case.case_no))
        if case.case_no == type(self).fail_scan_case:
            type(self).fail_scan_case = ""
            raise ValueError("synthetic scan case failure")
        refs = (PDF,) if case.case_no == "LEGACY" else (SCAN_LINKED,) if case.case_no == "ONE" else ()
        return tuple(ScannedRecord(None, ref) for ref in refs)

    def visits(self, mrn):
        if type(self).fail_visits:
            type(self).fail_visits = False
            raise ValueError("synthetic visit index failure")
        cases = super().visits(mrn)
        if mrn == MRN:
            cases += [
                VisitCase(ALIAS, today() - timedelta(days=5000), "O", "LEGACY", "70", "眼科", lookup_mrn=mrn),
                VisitCase(mrn, today() - timedelta(days=5), "A", "ADMIT", "20", "內科"),
                VisitCase(mrn, today() - timedelta(days=4), "E", "ER", "30", "急診"),
            ]
        return cases

    def history(self, mrn, options):
        if type(self).expire_once:
            type(self).expire_once = False
            raise AuthenticationError("expired synthetic session", code="AUTH_EXPIRED")
        return super().history(mrn, options)

    def order_history(self, mrn, options):
        self.calls.append((self.card, "orders-history", mrn, options.category))
        if mrn != MRN:
            return []
        return [ClinicalOrder(ALIAS, "LEGACY", "O", "跨科影像", "2024-01-01", "2024-01-02",
                              detail_ref=DETAIL, report_ref=REPORT, pacs_ref=STUDY, pdf_refs=(PDF,))]

    def case_orders(self, case):
        self.calls.append((self.card, "case-orders", case.case_no))
        return [ClinicalOrder(case.mrn, case.case_no, "O", "舊門診檢驗", "2012-01-01")]

    def order_detail(self, reference):
        self.calls.append((self.card, "order-detail", reference.case_no))
        return OrderDetail(reference, {"項目": "影像檢閱"}, (REPORT,), (STUDY,), (PDF,))

    def order_report(self, reference):
        self.calls.append((self.card, "order-report", reference.case_no))
        return OrderReport(reference, {"結果": "正常"}, (PDF,), (STUDY,), "合成文字報告")

    def pacs_study(self, reference):
        self.calls.append((self.card, "pacs-study", reference.request_no))
        return PacsStudy(reference, (IMAGE,))

    def download_pdf(self, reference):
        if type(self).expire_pdf_once:
            type(self).expire_pdf_once = False
            raise AuthenticationError("expired before attachment", code="AUTH_EXPIRED")
        self.calls.append((self.card, "pdf", reference.mrn))
        return BinaryAsset(b"%PDF-1.4\n%synthetic\n", "application/pdf")

    def download_image(self, reference):
        self.calls.append((self.card, "image", reference.mrn))
        return BinaryAsset(b"\x89PNG\r\n\x1a\nsynthetic", "image/png")


class ReviewHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        HistorySDK.calls = []
        HistorySDK.logins = 0
        HistorySDK.expire_once = False
        HistorySDK.expire_pdf_once = False
        HistorySDK.reject_new_sessions = False
        HistorySDK.fail_scan_case = ""
        HistorySDK.fail_scan_history = False
        HistorySDK.mismatched_scan_history = False
        HistorySDK.fail_visits = False
        self.app = BotApplication(Settings(), Path(self.temp.name), HistorySDK)
        self.key = self.app.login({"username": "TEST", "password": "memory-only", "remember": False})["account"]["id"]
        self.work = self.app.workspace(self.key)
        resolved = self.run_task(kind="resolve", identifiers=MRN)
        self.assertEqual(resolved["status"], "completed")
        group = self.work.review.save_set({"mrns": MRN})
        self.review = self.run_task(kind="review", set_id=group["id"])
        self.history = self.work.review.history
        self.assertEqual(self.review["status"], "completed")

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def run_task(self, **values):
        return wait_task(self.work, self.work.review.start(values)["task_id"])

    def request(self, resource, reference=""):
        return {"review_task_id": self.review["id"], "mrn": MRN,
                "resource": resource, "reference": reference}

    def fetch(self, resource, reference="", **options):
        return self.run_task(kind="history", **self.request(resource, reference), **options)

    def test_visits_and_soap_preserve_current_review_and_cache_offline(self):
        original = self.work.review.results({"id": self.review["id"]})["patients"]
        self.assertIsNotNone(self.history.read(self.request("visits"))["data"])
        before_index = sum(call[1] == "index" for call in HistorySDK.calls)
        self.assertEqual(self.fetch("visits")["status"], "completed")
        self.assertEqual(sum(call[1] == "index" for call in HistorySDK.calls), before_index)
        visits = self.history.read(self.request("visits"))["data"]
        self.assertEqual({row["case_type"] for row in visits}, {"O", "A", "E"})
        legacy = next(row for row in visits if row["case_no"] == "LEGACY")
        self.assertTrue(legacy["soap_available"])
        self.assertFalse(next(row for row in visits if row["case_no"] == "ADMIT")["soap_available"])
        with self.assertRaises(ValueError):
            self.history.read(self.request("visit_soap", "unverified"))
        with self.assertRaises(ValueError):
            self.fetch("visit_soap", next(row["id"] for row in visits if row["case_no"] == "ER"))
        self.assertEqual(self.fetch("visit_soap", legacy["id"])["status"], "completed")
        saved = self.history.read(self.request("visit_soap", legacy["id"]))["data"]
        self.assertEqual(saved["source_mrn"], ALIAS)
        self.assertEqual(saved["mrn"], MRN)
        self.assertEqual(original, self.work.review.results({"id": self.review["id"]})["patients"])
        self.app.logout(self.key)
        before = list(HistorySDK.calls)
        self.assertEqual(self.history.read(self.request("visit_soap", legacy["id"]))["data"]["soap"], saved["soap"])
        self.assertEqual(before, HistorySDK.calls)

    def test_numeric_orders_dedup_lazy_assets_and_account_scope(self):
        self.assertEqual(self.fetch("numeric")["status"], "completed")
        numeric = self.history.read(self.request("numeric"))["data"]
        self.assertEqual(numeric["tables"][0]["rows"][0][0], "2025-01-01")
        self.assertEqual(self.fetch("orders")["status"], "completed")
        orders = self.history.read(self.request("orders"))
        self.assertTrue(orders["complete"])
        self.assertEqual(len(orders["data"]), 1)
        self.assertEqual(orders["data"][0]["name"], "跨科影像")
        self.assertFalse(any(call[1] in {"order-detail", "pdf", "image"} for call in HistorySDK.calls))
        ref = orders["data"][0]["id"]
        with self.assertRaises(ValueError):
            self.history.read(self.request("order_report", "unverified"))
        self.assertEqual(self.fetch("order_report", ref)["status"], "completed")
        report = self.history.read(self.request("order_report", ref))["data"]
        self.assertEqual(report["texts"][0]["text"], "合成文字報告")
        self.assertEqual({asset["mime"] for asset in report["assets"]}, {"application/pdf", "image/png"})
        self.assertEqual(sum(call[1] == "pdf" for call in HistorySDK.calls), 1)
        self.assertEqual(sum(call[1] == "image" for call in HistorySDK.calls), 1)
        for asset in report["assets"]:
            self.assertTrue(self.work.analysis.store.asset(asset["digest"])[0].is_file())
        self.assertEqual(self.fetch("order_report", ref)["items"][0]["cached"], True)
        other_key = self.app.login({"username": "OTHER", "password": "other"})["account"]["id"]
        other = self.app.workspace(other_key)
        with self.assertRaises(ValueError):
            other.review.history.read(self.request("order_report", ref))

    def test_current_review_case_opens_numeric_and_orders_from_verified_index(self):
        current = self.work.review.results({"id": self.review["id"]})["patients"][0]["records"][0]
        reference = current["id"]
        self.assertEqual(self.fetch("case_numeric", reference)["status"], "completed")
        numeric = self.history.read(self.request("case_numeric", reference))
        self.assertTrue(numeric["data"]["tables"])
        self.assertEqual(self.fetch("case_orders", reference)["status"], "completed")
        orders = self.history.read(self.request("case_orders", reference))
        self.assertEqual(orders["data"][0]["name"], "舊門診檢驗")
        self.assertIn(("TEST", "case-orders", current["case_no"]), HistorySDK.calls)
        self.app.logout(self.key)
        before = list(HistorySDK.calls)
        self.assertEqual(self.history.read(self.request("case_numeric", reference))["data"], numeric["data"])
        self.assertEqual(self.history.read(self.request("case_orders", reference))["data"], orders["data"])
        self.assertEqual(HistorySDK.calls, before)

    def test_missing_order_branch_is_completed_without_reloading_existing_index(self):
        self.work.analysis.store.save_step(MRN, "orders-history:*", "order_index",
                                           [], self.work.username)
        partial = self.history.read(self.request("orders"))
        self.assertEqual(partial["data"], [])
        self.assertFalse(partial["complete"])
        self.assertEqual(self.fetch("orders")["status"], "completed")
        self.assertEqual([call[3] for call in HistorySDK.calls if call[1] == "orders-history"], ["OR"])
        self.assertTrue(self.history.read(self.request("orders"))["complete"])

    def test_expired_session_recovers_once_with_only_memory_password(self):
        self.assertEqual(self.app.registry.password(self.key), "")
        HistorySDK.expire_once = True
        before = HistorySDK.logins
        task = self.fetch("numeric")
        self.assertEqual(task["status"], "completed")
        self.assertEqual(HistorySDK.logins, before + 1)
        self.assertTrue(self.work.gateway.online)
        events = self.work.diagnostics.query({"task_id": task["id"]})["sdk_events"]
        self.assertTrue(any(row["method"] == "reconnect" and row["status"] == "ok" for row in events))
        self.assertNotIn("memory-only", str(events))

    def test_expired_pdf_read_reconnects_and_finishes_the_selected_report(self):
        self.fetch("orders")
        ref = self.history.read(self.request("orders"))["data"][0]["id"]
        HistorySDK.expire_pdf_once = True
        before = HistorySDK.logins
        self.assertEqual(self.fetch("order_report", ref)["status"], "completed")
        self.assertEqual(HistorySDK.logins, before + 1)
        self.assertEqual(len(self.history.read(self.request("order_report", ref))["data"]["assets"]), 2)

    def test_failed_recovery_pauses_and_manual_login_resumes_same_task(self):
        HistorySDK.expire_once = True
        HistorySDK.reject_new_sessions = True
        task = self.fetch("numeric")
        self.assertEqual(task["status"], "paused")
        self.assertTrue(task["error_code"].startswith("AUTH_"))
        self.assertFalse(self.work.gateway.online)
        self.assertIsNone(self.history.read(self.request("numeric"))["data"])
        HistorySDK.reject_new_sessions = False
        self.app.login({"id": self.key, "password": "updated-memory-only", "remember": False})
        resumed = self.run_task(resume=task["id"])
        self.assertEqual(resumed["id"], task["id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertIsNotNone(self.history.read(self.request("numeric"))["data"])

    def test_auth_check_false_reconnects_but_non_read_is_not_replayed(self):
        first = self.work.gateway.connection
        with patch.object(first.auth, "check", return_value=SimpleNamespace(ok=False)):
            self.assertTrue(self.work.gateway.invoke("auth", "check", only=("prq",)).ok)
        self.assertEqual(HistorySDK.logins, 2)
        before = HistorySDK.logins
        self.work.gateway.connection.reviews.submit_case = lambda: (_ for _ in ()).throw(
            AuthenticationError("expired", code="AUTH_EXPIRED"))
        try:
            with self.assertRaises(AuthenticationError):
                self.work.gateway.invoke("reviews", "submit_case")
        finally:
            del self.work.gateway.connection.reviews.submit_case
        self.assertEqual(HistorySDK.logins, before)

    def test_scans_reuse_soap_classify_history_and_download_lazily(self):
        current = next(record for record in self.work.review.results({"id": self.review["id"]})["patients"][0]["records"]
                       if record["case_no"] == "ONE")
        case = self.history.read(self.request("case_scans", current["id"]))
        self.assertTrue(case["complete"])
        self.assertEqual(case["data"]["scans"][0]["id"], scan_id(SCAN_LINKED))
        self.assertFalse(any(call[1] == "scan-case" for call in HistorySDK.calls))
        self.assertEqual(self.fetch("scans")["status"], "completed")
        data = self.history.read(self.request("scans"))["data"]
        self.assertEqual(len(data["scans"]), 1)
        self.assertEqual(len(data["unclassified"]), 1)
        self.assertEqual(len(data["other_history"]), 2)
        self.assertEqual(len(data["records"]), 4)
        self.assertEqual(len(data["categories"]), 3)
        self.assertNotIn(scan_id(SCAN_OPG), {row["id"] for row in data["scans"]})
        self.assertIn(scan_id(SCAN_LINKED), {row["id"] for row in data["scans"]})
        self.assertEqual(data["scans"][0]["category_label"], EYE_CATEGORY)
        self.assertEqual(data["scans"][0]["date"], "2025-01-15")
        self.assertEqual(data["checked_case_count"], 2)
        self.assertTrue(data["complete"])
        self.assertFalse(data["links_complete"])
        self.assertTrue(self.history.read(self.request("scans"))["complete"])
        self.assertFalse(any(call[1] == "pdf" for call in HistorySDK.calls))
        with self.assertRaises(ValueError):
            self.history.read(self.request("scan_asset", "f" * 64))
        self.assertEqual(self.fetch("scan_asset", scan_id(SCAN_OPG))["status"], "completed")
        asset = self.history.read(self.request("scan_asset", scan_id(SCAN_OPG)))["data"]
        self.assertEqual(asset["mime"], "application/pdf")
        self.assertEqual(sum(call[1] == "pdf" for call in HistorySDK.calls), 1)
        self.app.logout(self.key)
        before = list(HistorySDK.calls)
        self.assertEqual(self.history.read(self.request("scan_asset", scan_id(SCAN_OPG)))["data"], asset)
        self.assertEqual(before, HistorySDK.calls)

    def test_scans_backfill_partial_resumes_and_old_mrn_stays_verified(self):
        self.fetch("scans")
        HistorySDK.fail_scan_case = "LEGACY"
        task = self.fetch("scans", backfill=True)
        self.assertEqual(task["status"], "partial")
        self.assertLess(self.history.read(self.request("scans"))["data"]["checked_case_count"],
                        self.history.read(self.request("scans"))["data"]["eye_case_count"])
        resumed = self.run_task(resume=task["id"])
        self.assertEqual(resumed["status"], "completed")
        data = self.history.read(self.request("scans"))["data"]
        self.assertTrue(data["complete"])
        self.assertTrue(data["links_complete"])
        self.assertEqual(len(data["unclassified"]), 1)
        self.assertIn(scan_id(PDF), {row["id"] for row in data["scans"]})
        self.assertEqual(self.fetch("scan_asset", scan_id(PDF))["status"], "completed")
        self.assertIn(("TEST", "pdf", ALIAS), HistorySDK.calls)

    def test_old_scan_cache_is_visible_then_upgraded_to_full_category_index(self):
        legacy = clean([ScannedRecord("OPG", SCAN_OPG), ScannedRecord("RECORD", SCAN_LINKED)])
        self.work.analysis.store.save_step(MRN, "scans-history", "scan_index", legacy, self.work.username)
        before = self.history.read(self.request("scans"))
        self.assertFalse(before["complete"])
        self.assertTrue(before["data"]["index_loaded"])
        self.assertEqual(len(before["data"]["records"]), 2)
        self.assertEqual(before["data"]["categories"], [])
        self.assertEqual(self.fetch("scans")["status"], "completed")
        upgraded = self.history.read(self.request("scans"))
        self.assertTrue(upgraded["complete"])
        self.assertEqual(len(upgraded["data"]["records"]), 4)
        self.assertEqual(len([call for call in HistorySDK.calls if call[1] == "scan-history"]), 1)
        self.assertEqual(self.fetch("scans")["status"], "completed")
        self.assertEqual(len([call for call in HistorySDK.calls if call[1] == "scan-history"]), 1)

    def test_failed_scan_refresh_retains_cache_and_rejects_other_patient_ref(self):
        self.assertEqual(self.fetch("scans")["status"], "completed")
        saved = self.history.read(self.request("scans"))["data"]
        HistorySDK.fail_scan_history = True
        self.assertEqual(self.fetch("scans", force=True)["status"], "partial")
        self.assertEqual(self.history.read(self.request("scans"))["data"], saved)
        HistorySDK.mismatched_scan_history = True
        self.assertEqual(self.fetch("scans", force=True)["status"], "partial")
        self.assertEqual(self.history.read(self.request("scans"))["data"], saved)

    def test_scan_category_and_table_section_override_pdf_subtype(self):
        records = clean([
            ScannedRecord(None, SCAN_OPG, category_label=EYE_CATEGORY,
                          section_label="病歷類別", record_date=date(2026, 3, 1)),
            ScannedRecord("RECORD", SCAN_OTHER, category_label=EYE_CATEGORY,
                          section_label="病歷類別(E化表單)", record_date=date(2026, 2, 1)),
            ScannedRecord("RECORD", SCAN_OTHER, category_label=OTHER_CATEGORY,
                          section_label="病歷類別", record_date=date(2026, 2, 1)),
        ])
        self.work.analysis.store.save_step(MRN, "scans-history", "scan_index",
                                           {"schema": 2, "records": records}, self.work.username)
        data = self.history.read(self.request("scans"))["data"]
        self.assertEqual(len(data["records"]), 4)
        self.assertEqual(len(data["categories"]), 3)
        self.assertEqual({row["id"] for row in data["scans"]}, {scan_id(SCAN_OPG), scan_id(SCAN_LINKED)})
        self.assertEqual(len(data["other_history"]), 2)
        self.assertEqual(next(row for row in data["scans"] if row["id"] == scan_id(SCAN_OPG))["record_type"], "")

    def test_corrupt_scan_cache_does_not_block_refresh_or_verified_case_asset(self):
        broken = {"schema": 2, "records": [{"record_type": "RECORD", "pdf_ref": clean(PDF)}]}
        self.work.analysis.store.save_step(MRN, "scans-history", "scan_index", broken, self.work.username)
        before = self.history.read(self.request("scans"))
        self.assertFalse(before["complete"])
        self.assertTrue(before["data"]["cache_error"])
        self.assertNotIn(scan_id(PDF), {row["id"] for row in before["data"]["records"]})
        self.assertEqual(self.fetch("scan_asset", scan_id(SCAN_LINKED))["status"], "completed")
        self.assertEqual(self.fetch("scans")["status"], "completed")
        self.assertTrue(self.history.read(self.request("scans"))["complete"])

    def test_corrupt_visit_links_do_not_hide_full_history(self):
        self.assertEqual(self.fetch("scans")["status"], "completed")
        wrong = VisitCase("OTHER", today(), "O", "WRONG", "70", "眼科")
        self.work.review.db.save("visits", {"id": MRN, "cases": clean([wrong])})
        data = self.history.read(self.request("scans"))["data"]
        self.assertTrue(data["history_loaded"])
        self.assertTrue(data["link_error"])
        self.assertEqual(len(data["records"]), 4)
        self.assertEqual(len(data["scans"]), 1)
        HistorySDK.fail_visits = True
        task = self.fetch("scans", backfill=True)
        self.assertEqual(task["status"], "partial")
        self.assertTrue(self.history.read(self.request("scans"))["data"]["link_error"])
        self.assertEqual(self.run_task(resume=task["id"])["status"], "completed")
        self.assertTrue(self.history.read(self.request("scans"))["data"]["links_complete"])

    def test_corrupt_case_scan_refs_are_replaced_during_explicit_backfill(self):
        self.assertEqual(self.fetch("scans")["status"], "completed")
        case = next(case for case in self.work.scans._eye_cases(MRN) if case.case_no == "ONE")
        key = "scans-case:" + digest(case.identity)
        self.work.analysis.store.save_step(MRN, key, "scan_index",
                                           clean([ScannedRecord(None, PDF)]), self.work.username)
        self.assertTrue(self.history.read(self.request("scans"))["data"]["link_error"])
        self.assertEqual(self.fetch("scans", backfill=True)["status"], "completed")
        self.assertIn(("TEST", "scan-case", "ONE"), HistorySDK.calls)
        self.assertTrue(self.history.read(self.request("scans"))["data"]["links_complete"])

    def test_scan_legacy_soap_cache_and_cataract_cohort_scope(self):
        current = next(record for record in self.work.review.results({"id": self.review["id"]})["patients"][0]["records"]
                       if record["case_no"] == "ONE")
        record = self.work.store.library.get_record(current["id"])
        record["soap_structure"].pop("scanned_pdf_refs")
        self.work.store.library.save_record(record, self.work.username, "synthetic-old-cache")
        self.assertFalse(self.history.read(self.request("case_scans", current["id"]))["complete"])
        self.assertEqual(self.fetch("case_scans", current["id"])["status"], "completed")
        self.assertIn(("TEST", "scan-case", "ONE"), HistorySDK.calls)
        cohort = self.work.analysis.save_cohort({"source": "manual", "account_id": self.key,
                                                 "name": "白內障合成", "mrns": MRN})
        scoped = {"cohort_id": cohort["id"], "mrn": MRN, "resource": "scans"}
        self.assertIsNotNone(self.history.read(scoped)["data"])
        self.assertEqual(self.run_task(kind="history", **scoped)["status"], "completed")
        with self.assertRaises(ValueError):
            self.history.read({**scoped, "mrn": "OTHER"})
        with self.assertRaises(ValueError):
            self.history.read({**scoped, "resource": "orders"})
        wrong = self.work.analysis.store.document("analysis_cohorts", cohort["id"])
        wrong["members"][0]["account_id"] = "f" * 32
        self.work.analysis.store.save_document("analysis_cohorts", wrong)
        with self.assertRaises(ValueError):
            self.history.read(scoped)


if __name__ == "__main__":
    unittest.main()
