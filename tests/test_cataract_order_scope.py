"""Cataract requests and cached results stay within verified eye encounters."""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from vghks_sdk import ParseError, VisitCase
from vghks_sdk.models import (
    BinaryAsset,
    ClinicalOrder,
    OrderDetail,
    OrderDetailRef,
    OrderReport,
    OrderReportRef,
    PdfAttachmentRef,
)

from vghks_bot.analysis_fetch import clean, model, order_identity
from vghks_bot.analysis_store import digest
from vghks_bot.bot import BotApplication
from vghks_bot.ophthalmic_orders import SCOPE_KEY
from vghks_bot.selftest_bot import BotSyntheticSDK
from vghks_bot.settings import Settings, today

MRN, ALIAS = "TEST001", "LEGACY001"


class MixedOrdersSDK(BotSyntheticSDK):
    fail_history = False
    fail_case = ""
    no_eye = False
    wrong_case = False

    def __init__(self, settings):
        super().__init__(settings)
        self.orders = SimpleNamespace(get_order_history=self.order_history, get_case_orders=self.case_orders,
                                      get_order_detail=self.detail, get_order_report=self.report, download_pdf=self.pdf)

    def visits(self, mrn):
        cases = super().visits(mrn)
        cases += [VisitCase(ALIAS, today() - timedelta(days=5000), "O", "LEGACY", "70", "眼科", lookup_mrn=mrn),
                  VisitCase(mrn, today() - timedelta(days=5000), "O", "OLD_DERM", "71", "皮膚科"),
                  VisitCase(mrn, today() - timedelta(days=1), "O", "DERM", "71", "皮膚科"),
                  VisitCase(mrn, today() + timedelta(days=1), "O", "FUTURE", "70", "眼科")]
        return [case for case in cases if "眼科" not in case.section_name] if type(self).no_eye else cases

    @staticmethod
    def order(mrn, case_no):
        return ClinicalOrder(mrn, case_no, "O", "DBR, free charge", "2026-09-01", "2026-09-02",
                             detail_ref=OrderDetailRef(mrn, case_no, "O", "1"))

    def detail(self, ref):
        self.calls.append((self.card, "order-detail", ref.case_no))
        return OrderDetail(ref, {"項目": "合成檢查"}, (OrderReportRef(ref.mrn, ref.case_no, ref.case_type, ref.sequence_no),))

    def report(self, ref):
        self.calls.append((self.card, "order-report", ref.case_no))
        return OrderReport(ref, {"結果": "合成資料"},
                           pdf_refs=(PdfAttachmentRef(ref.mrn, "//HFS01_1A0/EMRU/" + ref.mrn + "/" + ref.case_no + ".pdf"),))

    def pdf(self, ref):
        self.calls.append((self.card, "pdf", ref.mrn))
        return BinaryAsset(b"%PDF-1.4\n% synthetic offline test\n%%EOF\n", "application/pdf")

    def order_history(self, mrn, filters):
        self.calls.append((self.card, "orders-history", mrn, filters.category))
        if type(self).fail_history:
            raise ParseError("synthetic phototherapy expression", code="JS_EXPRESSION_UNSUPPORTED")
        return [self.order(mrn, "ONE"), self.order(mrn, "DERM")]

    def case_orders(self, case):
        self.calls.append((self.card, "case-orders", case.case_no))
        if case.case_no == type(self).fail_case:
            type(self).fail_case = ""
            raise ParseError("synthetic unknown format", code="JS_EXPRESSION_UNSUPPORTED")
        if type(self).wrong_case and case.case_no == "LEGACY":
            return [self.order(case.mrn, "DERM")]
        return [self.order(case.mrn, case.case_no)] if case.case_no in {"ONE", "LEGACY", "DERM", "OLD_DERM", "OTHER"} else []


class CataractOrderScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        MixedOrdersSDK.calls = []
        MixedOrdersSDK.fail_case = ""
        MixedOrdersSDK.fail_history = MixedOrdersSDK.no_eye = MixedOrdersSDK.wrong_case = False
        self.app = BotApplication(Settings(), Path(self.temp.name), MixedOrdersSDK)
        self.addCleanup(self.app.close)
        self.account = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.account)
        self.analysis, self.store = self.work.analysis, self.work.analysis.store
        self.cohort = self.analysis.save_cohort({"source": "manual", "account_id": self.account,
                                               "name": "合成眼科範圍驗證", "mrns": MRN})
        self.values = {"cohort_id": self.cohort["id"], "mrn": MRN, "module": "cataract"}
        MixedOrdersSDK.calls.clear()

    def run_analysis(self, **options):
        run_id = self.analysis.start({"cohort_id": self.cohort["id"], "modules": ["cataract"], "mrn": MRN,
                                      **options})["run_ids"][0]
        self.assertTrue(self.work.idle.wait(15))
        return self.store.document("analysis_runs", run_id)

    def test_fresh_cataract_uses_one_history_query_and_only_backfills_old_eye_cases(self):
        run = self.run_analysis()
        self.assertEqual(run["status"], "completed", run["issues"])
        cases = {call[2] for call in MixedOrdersSDK.calls if call[1] == "case-orders"}
        self.assertEqual(cases, {"LEGACY"})
        self.assertEqual({call[2] for call in MixedOrdersSDK.calls if call[1] in {"order-detail", "order-report"}},
                         {"ONE", "LEGACY"})
        self.assertEqual({call[2] for call in MixedOrdersSDK.calls if call[1] == "pdf"}, {MRN, ALIAS})
        self.assertEqual([call[3] for call in MixedOrdersSDK.calls if call[1] == "orders-history"], ["*"])
        self.assertIsNotNone(self.store.step(MRN, "orders-history:*"))
        self.assertIsNone(self.store.step(MRN, "orders-history:OR"))
        self.assertTrue(self.analysis.cataract_status(self.values)["ready"])
        orders = self.analysis.results(self.values)["orders"]
        self.assertEqual({order["case_no"] for order in orders}, {"ONE", "LEGACY"})
        self.assertEqual(self.analysis.results(self.values)["coverage"]["order_scope"], "ophthalmology")
        MixedOrdersSDK.calls.clear()
        self.assertEqual(self.run_analysis()["status"], "completed")
        self.assertFalse(any(call[1] in {"orders-history", "case-orders"} for call in MixedOrdersSDK.calls))

    def test_shared_history_is_retained_but_other_departments_are_hidden(self):
        with self.work.sdk_factory(self.work.settings) as sdk:
            visits = clean(sdk.records.get_visit_cases(MRN))
        self.store.save_step(MRN, "visits", "visits", visits, self.work.username)
        orders = clean([MixedOrdersSDK.order(MRN, "ONE"), MixedOrdersSDK.order(MRN, "DERM"),
                        MixedOrdersSDK.order(MRN, "UNKNOWN"), MixedOrdersSDK.order(MRN, "LEGACY")])
        for category in ("*", "OR"):
            self.store.save_step(MRN, "orders-history:" + category, "order_index", orders, self.work.username)
        MixedOrdersSDK.calls.clear()
        self.assertEqual(self.run_analysis()["status"], "completed")
        cases = [call[2] for call in MixedOrdersSDK.calls if call[1] == "case-orders"]
        self.assertEqual(cases, ["LEGACY"])
        self.assertEqual({row["case_no"] for row in self.analysis.results(self.values)["orders"]}, {"ONE", "LEGACY"})
        self.assertEqual(len(self.store.step(MRN, "orders-history:*")["payload"]), 4)
        self.assertIn("DERM", {order["case_no"] for order in self.work.review.history._order_sources(MRN)})

    def test_new_history_scope_ignores_stale_category_and_recent_case_indices(self):
        self.assertEqual(self.run_analysis()["status"], "completed")
        stale = clean(MixedOrdersSDK.order(MRN, "TWO"))
        self.store.save_step(MRN, "orders-history:OR", "order_index", [stale], self.work.username)
        case = next(row for row in self.store.step(MRN, "visits")["payload"] if row["case_no"] == "TWO")
        self.store.save_step(MRN, "orders-case:" + digest(model(VisitCase, case).identity),
                             "order_index", [stale], self.work.username)
        self.assertNotIn("TWO", {row["case_no"] for row in self.analysis.results(self.values)["orders"]})
        self.assertIn("TWO", {row["case_no"] for row in self.work.review.history._order_sources(MRN)})
        with self.assertRaises(ValueError):
            self.work.review.history.scope({"cohort_id": self.cohort["id"], "mrn": MRN,
                                            "resource": "order_report", "reference": order_identity(stale)})

    def test_failed_eye_case_is_partial_and_resume_only_fetches_the_missing_case(self):
        MixedOrdersSDK.fail_case = "LEGACY"
        first = self.run_analysis()
        self.assertEqual(first["status"], "partial")
        self.assertFalse(self.store.step(MRN, SCOPE_KEY)["payload"]["complete"])
        self.assertFalse(self.analysis.cataract_status(self.values)["ready"])
        self.assertTrue(self.analysis.results(self.values)["orders"])
        MixedOrdersSDK.calls.clear()
        second = self.run_analysis(resume=first["id"])
        self.assertEqual(second["status"], "completed", second["issues"])
        self.assertEqual([call[2] for call in MixedOrdersSDK.calls if call[1] == "case-orders"], ["LEGACY"])
        self.assertTrue(self.analysis.cataract_status(self.values)["ready"])

    def test_changed_eye_visit_list_invalidates_scope_until_new_case_is_read(self):
        self.assertEqual(self.run_analysis()["status"], "completed")
        visits = self.store.step(MRN, "visits")["payload"]
        visits.append(clean(VisitCase(MRN, today(), "O", "NEW_EYE", "70", "眼科")))
        self.store.save_step(MRN, "visits", "visits", visits, self.work.username)
        self.assertFalse(self.analysis.cataract_status(self.values)["ready"])
        MixedOrdersSDK.calls.clear()
        self.assertEqual(self.run_analysis()["status"], "completed")
        self.assertEqual([call[3] for call in MixedOrdersSDK.calls if call[1] == "orders-history"], ["*"])
        self.assertEqual([call[2] for call in MixedOrdersSDK.calls if call[1] == "case-orders"], [])
        self.assertTrue(self.analysis.cataract_status(self.values)["ready"])

    def test_unknown_history_error_keeps_other_results_and_falls_back_only_to_eye_visits(self):
        MixedOrdersSDK.fail_history = True
        first = self.run_analysis()
        self.assertEqual(first["status"], "partial")
        self.assertEqual({call[2] for call in MixedOrdersSDK.calls if call[1] == "case-orders"},
                         {"EMPTY", "ONE", "TWO", "OLD", "LEGACY"})
        self.assertEqual({row["case_no"] for row in self.analysis.results(self.values)["orders"]}, {"ONE", "LEGACY"})
        MixedOrdersSDK.fail_history = False
        MixedOrdersSDK.calls.clear()
        self.assertEqual(self.run_analysis(resume=first["id"])["status"], "completed")
        self.assertEqual([call[3] for call in MixedOrdersSDK.calls if call[1] == "orders-history"], ["*"])
        self.assertFalse(any(call[1] == "case-orders" for call in MixedOrdersSDK.calls))

    def test_wrong_encounter_is_rejected_and_empty_eye_scope_is_complete(self):
        MixedOrdersSDK.wrong_case = True
        self.assertEqual(self.run_analysis()["status"], "partial")
        self.assertFalse(self.analysis.cataract_status(self.values)["ready"])
        self.assertNotIn("DERM", {order["case_no"] for order in self.analysis.results(self.values)["orders"]})
        MixedOrdersSDK.no_eye = True
        MixedOrdersSDK.wrong_case = False
        MixedOrdersSDK.calls.clear()
        self.assertEqual(self.run_analysis(refresh=True)["status"], "completed")
        self.assertFalse(any(call[1] in {"case-orders", "orders-history"} for call in MixedOrdersSDK.calls))
        self.assertEqual(self.analysis.results(self.values)["orders"], [])
        self.assertTrue(self.analysis.cataract_status(self.values)["ready"])


if __name__ == "__main__":
    unittest.main()
