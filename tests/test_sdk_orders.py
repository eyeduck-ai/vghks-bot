"""Synthetic regressions for PRQ phototherapy order-index responses."""
import html
import json
import unittest
from dataclasses import replace as replace_case
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import quote, urlencode

from vghks_sdk import ConfigurationError, ParseError, VisitCase, assess_data
from vghks_sdk.models import (
    OrderHistoryFilter,
    OrderReportRef,
    PacsImageRef,
    PacsStudyRef,
    PdfAttachmentRef,
    to_jsonable,
)
from vghks_sdk.parsing.clinical import parse_clinical_orders, parse_order_report, parse_pacs_study

from vghks_bot.analysis_fetch import model
from vghks_bot.scanner import create_sdk
from vghks_bot.sdk_orders import (
    CompatibleOrderAdapter,
    OrderIndex,
    PhototherapyPdfRef,
    assess_order_index,
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
    def test_history_accepts_only_verified_old_patient_encounters(self):
        legacy = order_page(photo=False, other_mrn="OLD001")
        verified = VisitCase("OLD001", date(2025, 1, 1), "O", "EYE1", "70", "眼科", lookup_mrn=MRN)
        result = parse_order_index(order_page() + legacy, mrn=MRN, verified_cases=[verified], allow_partial=True)
        self.assertEqual({order.mrn for order in result}, {MRN, "OLD001"})
        self.assertTrue(assess_order_index(result).complete)
        old = next(order for order in result if order.mrn == "OLD001")
        self.assertEqual(old.detail_ref.mrn, "OLD001")
        for cases in ([], [replace_case(verified, lookup_mrn="OTHER001")], [replace_case(verified, case_no="OTHER")]):
            with self.subTest(cases=cases):
                rejected = parse_order_index(order_page() + legacy, mrn=MRN, verified_cases=cases, allow_partial=True)
                self.assertEqual([order.mrn for order in rejected], [MRN])
                self.assertFalse(assess_order_index(rejected).complete)
                self.assertEqual(len(rejected.issues), 1)

    def test_partial_rows_survive_bad_expressions_and_unsafe_attachments(self):
        before = order_page(photo=False)
        after = before.replace("caseNo=EYE1", "caseNo=EYE2")
        broken = order_page().replace(json.dumps(LIGHT_PDF), "getFilePath()")
        unsafe = order_page(path=LIGHT_PDF.replace(MRN, "OTHER001"))
        foreign = order_page(photo=False, other_mrn="OTHER001")
        result = parse_order_index(before + broken + unsafe + foreign + after, mrn=MRN, allow_partial=True)
        self.assertEqual({order.case_no for order in result}, {"EYE1", "EYE2"})
        self.assertEqual(len(result.issues), 3)
        self.assertFalse(assess_order_index(result).complete)
        self.assertTrue(all(order.detail_ref.mrn == MRN for order in result))
        failed = parse_order_index(broken + foreign, mrn=MRN, allow_partial=True)
        self.assertEqual(failed, [])
        self.assertFalse(assess_order_index(failed).complete)

    def test_multiple_constructors_keep_their_own_references_and_partial_rows(self):
        first = order_page(photo=False).removeprefix("<script>").removesuffix("</script>")
        second = first.replace("caseNo=EYE1", "caseNo=EYE2").replace("seqNo=1", "seqNo=2")
        broken = "new KSCase('', readUnknownName(), '2026-09-01', '', '', '', '');"
        result = parse_order_index("<script>" + first + broken + second + "</script>", mrn=MRN, allow_partial=True)
        self.assertEqual({order.case_no for order in result}, {"EYE1", "EYE2"})
        self.assertEqual({order.detail_ref.sequence_no for order in result}, {"1", "2"})
        self.assertEqual(len(result.issues), 1)

    def test_single_case_does_not_accept_other_encounters(self):
        case = VisitCase(MRN, date(2026, 9, 1), "O", "EYE1", "70", "眼科")
        page = order_page(photo=False)
        result = parse_order_index(page + page.replace("caseNo=EYE1", "caseNo=OTHER"),
                                   mrn=MRN, case=case, allow_partial=True)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.issues[0].code, "ORDER_REF_CASE_MISMATCH")

    def test_date_filter_keeps_partial_metadata_and_attachment_parsers_are_lazy(self):
        runtime = SimpleNamespace(settings=SimpleNamespace(prq_base_url="https://hospital.test/PRQWeb"),
            auth=SimpleNamespace(hid_for=lambda _: "synthetic"), request_text=Mock(return_value=
                order_page(photo=False) + order_page(other_mrn="OTHER001")),
            execute=lambda spec, operation, **kwargs: operation())
        adapter = CompatibleOrderAdapter(runtime)
        adapter._history_context_raw = Mock()
        result = adapter.get_order_history(MRN, OrderHistoryFilter(order_date=date(2025, 1, 1)))
        self.assertIsInstance(result, OrderIndex)
        self.assertEqual(result, [])
        self.assertFalse(assess_order_index(result).complete)
        with patch("vghks_bot.sdk_orders._pdf_candidates") as pdfs, patch("vghks_bot.sdk_orders._pacs_refs_from_source") as pacs:
            parse_order_index(order_page(photo=False), mrn=MRN)
        pdfs.assert_not_called()
        pacs.assert_not_called()
        escaped_pdf = order_page().replace(".pdf", r"\x2epdf")
        self.assertEqual(parse_order_index(escaped_pdf, mrn=MRN)[0].pdf_refs[0].file_path, LIGHT_PDF)
        with self.assertRaises(ParseError):
            parse_order_index(escaped_pdf.replace("lightrep\\\\TEST001", "lightrep\\\\OTHER001"), mrn=MRN)
    def test_union_and_legacy_images_round_trip_through_the_compatible_adapter(self):
        image_url = "http://192.168.20.137:800/xwado/PushWADO?" + urlencode({
            "requestType": "WADO", "contentType": "image/jpeg", "rows": "512", "objectUID": "1.2.3"})
        source = "/PRQWeb/Page/JSP/showPACSPic.jsp?" + urlencode({
            "hhisnum": MRN, "hid": "OLD", "reqno": "SYNTHETIC", "imgUrl": image_url})
        runtime = SimpleNamespace(settings=SimpleNamespace(prq_base_url="https://hospital.test/PRQWeb"),
            auth=SimpleNamespace(hid_for=lambda _: "CURRENT"),
            request_text=Mock(return_value='<img src="' + html.escape(source) + '">'),
            request_binary=Mock(return_value=(b"\xff\xd8synthetic\xff\xd9", "image/jpeg")),
            execute=lambda spec, operation, **kwargs: operation())
        adapter = CompatibleOrderAdapter(runtime)
        study = adapter.get_pacs_study(PacsStudyRef(MRN, "SYNTHETIC"))
        reference = model(PacsImageRef, json.loads(json.dumps(to_jsonable(study.images[0]))))
        self.assertEqual(reference.img_url, image_url)
        for ref in (reference, PacsImageRef(MRN, "SYNTHETIC", "SERIES", "STUDY", "1.2.3")):
            self.assertEqual(adapter.download_pacs_image(ref).media_type, "image/jpeg")
            request = runtime.request_binary.call_args
            self.assertTrue(request.args[1].endswith("/PRQWeb/Page/JSP/showPACSPic.jsp"))
            self.assertEqual(request.kwargs["params"]["hid"], "CURRENT")
            self.assertEqual(request.kwargs["params"].get("imgUrl"), ref.img_url or None)
            self.assertEqual(request.kwargs["params"].get("uid"), ref.uid or None)

    def test_pacs_empty_is_not_an_unknown_or_login_page(self):
        reference = PacsStudyRef(MRN, "SYNTHETIC")
        for content in ("查無資料!", "查無資料!!"):
            study = parse_pacs_study(content, reference=reference)
            self.assertEqual(study.images, ())
            self.assertEqual(study.data_status, "EMPTY")
        for content in ("", "<script>var x='查無資料!!';</script>", "<form>登入</form>"):
            with self.subTest(content=content), self.assertRaises(ParseError):
                parse_pacs_study(content, reference=reference)

    def test_resval_text_and_unknown_templates_keep_sdk_completeness(self):
        source = r'''resVal="First line\n第二行";
while(resVal.indexOf("\n")>0){resVal=resVal.replace("\n","<br/>");}
document.write(resVal);'''
        reference = OrderReportRef(MRN, "SYNTHETIC", "O", "1")
        def parse(script):
            return parse_order_report('<table><tr><th>報告內容</th><td><pre><script>' + script +
                                      '</script></pre></td></tr></table>', reference=reference)
        report = parse(source)
        self.assertEqual(report.report_text, "First line\n第二行")
        self.assertTrue(assess_data(report).complete)
        unknown = parse(source.replace('>0', '>=0'))
        self.assertEqual(unknown.report_text, "")
        self.assertFalse(assess_data(unknown).complete)

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
