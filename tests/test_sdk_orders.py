"""Synthetic regressions for PRQ phototherapy order-index responses."""
import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote

from vghks_sdk import ConfigurationError, ParseError, VisitCase
from vghks_sdk.models import OrderHistoryFilter, PdfAttachmentRef, to_jsonable
from vghks_sdk.parsing.clinical import parse_clinical_orders

from vghks_bot.analysis_fetch import model
from vghks_bot.scanner import create_sdk
from vghks_bot.sdk_orders import (
    CompatibleOrderAdapter,
    PhototherapyPdfRef,
    parse_order_index,
    pdf_reference,
)
from vghks_bot.settings import Settings

MRN = "TEST001"
LIGHT_PDF = r"\\hfs01_1A0.vghks.gov.tw\SECTORD\lightrep\TEST001_O_DOCUMENT1.pdf"


def order_page(*, photo=True, path=LIGHT_PDF, other_mrn=MRN):
    button = ""
    if photo:
        button = ("var filepath = " + json.dumps(path) + ";\n"
                  + r'''qrcodeStr += '<button class="attachBtn" path=\''+filepath+'\'>光照治療紀錄</button>';''')
    return """<script>
var orderStr = '<a href="/PRQWeb/QueryOrderDetail.do?caseNo=EYE1&caseType=O&hhisnum=MRN&seqNo=1">NAME</a>';
var mydate = '2026-09-01'; var rcpDt = '2026-09-02'; var orspDept = ''; var qrcodeStr = '';
BUTTON
new KSCase('', orderStr, mydate, rcpDt, '合成醫師'+orspDept, '已執行', qrcodeStr);
</script>""".replace("MRN", other_mrn).replace("NAME", "光照治療" if photo else "DBR, free charge").replace("BUTTON", button)


class OrderCompatibilityTests(unittest.TestCase):
    def test_phototherapy_keeps_all_orders_and_a_validated_pdf(self):
        page = order_page() + order_page(photo=False)
        with self.assertRaises(ParseError) as failed:
            parse_clinical_orders(page, mrn=MRN)
        self.assertEqual(failed.exception.info.code, "JS_EXPRESSION_UNSUPPORTED")
        orders = parse_order_index(page, mrn=MRN)
        self.assertEqual({order.name for order in orders}, {"光照治療", "DBR, free charge"})
        photo = next(order for order in orders if order.name == "光照治療")
        self.assertEqual(photo.case_no, "EYE1")
        self.assertEqual(photo.attachment, "光照治療紀錄")
        self.assertEqual(len(photo.pdf_refs), 1)
        self.assertIsInstance(photo.pdf_refs[0], PhototherapyPdfRef)
        self.assertEqual(model(PdfAttachmentRef, to_jsonable(photo.pdf_refs[0])), photo.pdf_refs[0])

    def test_existing_order_format_matches_the_sdk(self):
        page = order_page(photo=False)
        self.assertEqual(parse_order_index(page, mrn=MRN), parse_clinical_orders(page, mrn=MRN))
        ref = pdf_reference(MRN, r"\\HFS01_1A0\EMRU\TEST001\report.pdf")
        self.assertIs(type(ref), PdfAttachmentRef)
        self.assertEqual(pdf_reference(MRN, quote(LIGHT_PDF, safe="")).file_path, LIGHT_PDF)

    def test_unknown_javascript_and_patient_mismatch_still_fail(self):
        pages = [order_page().replace(json.dumps(LIGHT_PDF), "getFilePath()"),
                 order_page().replace("qrcodeStr +=", "if (dynamicFlag) qrcodeStr +="),
                 order_page(other_mrn="OTHER001"),
                 order_page(path=LIGHT_PDF.replace(MRN, "OTHER001"))]
        for page in pages:
            with self.subTest(page=pages.index(page)), self.assertRaises(ParseError):
                parse_order_index(page, mrn=MRN)

    def test_pdf_extension_rejects_external_paths_and_traversal(self):
        for path in (LIGHT_PDF.replace("hfs01_1A0.vghks.gov.tw", "external.example"),
                     LIGHT_PDF.replace("lightrep", "other"),
                     LIGHT_PDF.replace("lightrep\\", "lightrep\\..\\"),
                     LIGHT_PDF + "?token=secret", LIGHT_PDF.replace(".pdf", ".exe"),
                     LIGHT_PDF.replace(MRN, "OTHER001")):
            with self.subTest(path=path), self.assertRaises(ConfigurationError):
                pdf_reference(MRN, path)

    def test_factory_and_order_requests_use_the_compatible_adapter(self):
        sdk = create_sdk(Settings(username="TEST", password="synthetic"))
        try:
            self.assertIsInstance(sdk.orders._adapter, CompatibleOrderAdapter)
            self.assertIs(sdk.orders._adapter.runtime, sdk._runtime)
            self.assertIs(sdk.records._adapter.runtime, sdk._runtime)
        finally:
            sdk.close()
        runtime = SimpleNamespace(settings=SimpleNamespace(prq_base_url="https://hospital.test/PRQWeb"),
            auth=SimpleNamespace(hid_for=lambda _: "synthetic"),
            request_text=Mock(return_value=order_page()),
            execute=lambda spec, operation, **kwargs: operation())
        adapter = CompatibleOrderAdapter(runtime)
        adapter._history_context_raw = Mock()
        adapter._get_case_detail_raw = Mock()
        adapter._prime_key_raw = Mock()
        self.assertEqual(len(adapter.get_order_history(MRN, OrderHistoryFilter(category="OR"))), 1)
        adapter._history_context_raw.assert_called_once_with(MRN)
        self.assertEqual(runtime.request_text.call_args.kwargs["data"]["ordertype"], "OR")
        self.assertEqual(len(adapter.get_order_history(MRN, OrderHistoryFilter(order_date=date(2025, 1, 1)))), 0)
        case = VisitCase(MRN, date(2026, 9, 1), "O", "EYE1", "70", "眼科")
        self.assertEqual(len(adapter.get_case_orders(case)), 1)
        adapter._get_case_detail_raw.assert_called_once_with(case)
        adapter._prime_key_raw.assert_called_once_with()
        self.assertEqual(runtime.request_text.call_args.kwargs["data"]["section"], "70")
        runtime.request_binary = Mock(return_value=(b"%PDF-1.4\n% synthetic offline test\n%%EOF\n", "application/pdf"))
        asset = adapter.download_pdf(pdf_reference(MRN, LIGHT_PDF))
        self.assertTrue(asset.content.startswith(b"%PDF-"))
        self.assertEqual(runtime.request_binary.call_args.kwargs["params"]["hhisnum"], MRN)


if __name__ == "__main__":
    unittest.main()
