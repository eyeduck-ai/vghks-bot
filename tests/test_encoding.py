import json
import threading
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import requests
from vghks_sdk import RequestPolicy
from vghks_sdk.parsing.prq import parse_opd_patients

from vghks_bot.encoding import (
    ClinicalTransport,
    decode_response,
    observe_prq_decoding,
    report_attachment_downloads,
)
from vghks_bot.scanner import ScanState, _collect, create_sdk
from vghks_bot.settings import Settings


class EncodingTests(unittest.TestCase):
    def test_big5_patient_fields_survive_invalid_byte_elsewhere(self):
        html = """<meta charset=big5><script>
        aryOpdSec[0]='70';aryOpdRoom[0]='02';aryOpdDoc[0]='DOC1F';
        if(aryOpdSec[0]=='70'){new KSCase('','1','TEST001','測試姓名','女','68歲');}
        </script>"""
        raw = html.encode("cp950") + b"\xff"
        decoded = decode_response(raw,"text/html; charset=big5")
        patients = parse_opd_patients(decoded,visit_date=date(2026,9,19),doctor_card="DOC1")
        self.assertEqual(len(patients),1)
        self.assertEqual((patients[0].name,patients[0].sex,patients[0].age),("測試姓名","女","68歲"))
        self.assertEqual(patients[0].sequence_no, "1")
        # The same warning can occur when the source already carries a replacement character.
        day = date(2026, 9, 19)
        source = decode_response(html.replace("測試姓名", "合成\ufffd").encode())
        rows = parse_opd_patients(source, visit_date=day, doctor_card="DOC1")
        state = ScanState(day, day)
        sdk = SimpleNamespace(opd=SimpleNamespace(get_doctor_patients=lambda *_: rows))
        _collect(sdk, state, Settings(username="DOC1"), day, day)
        self.assertEqual(state.data["counts"]["registrations"], 1)
        warning, = state.data["issues"]
        self.assertEqual(warning["code"], "TEXT_DECODE_INCOMPLETE")
        self.assertIn("姓名", warning["message"])
        self.assertIn("更新門診清單", warning["message"])
        self.assertNotIn("合成", warning["message"])

    def test_utf8_big5_headers_meta_and_overrides(self):
        for header in ("","text/html; charset=iso-8859-1","text/html; charset=utf-8","text/html; charset=big5"):
            with self.subTest(header=header):
                self.assertEqual(decode_response("姓名 女 68歲".encode("cp950"),header),"姓名 女 68歲")
                self.assertEqual(decode_response("姓名 女 68歲".encode(),header),"姓名 女 68歲")
        self.assertEqual(decode_response("姓名".encode("utf-8-sig")),"姓名")
        self.assertEqual(decode_response("姓名".encode("cp950"),encoding="big5"),"姓名")
        self.assertEqual(decode_response("姓名".encode(),encoding="utf-8"),"姓名")

    def test_unrecoverable_replacement_is_not_fabricated(self):
        self.assertEqual(decode_response("���J�q�@�@".encode()),"���J�q�@�@")

    def test_sdk_uses_transport_without_network(self):
        with create_sdk(Settings(username="DOC1",password="synthetic")) as sdk:
            self.assertIsInstance(sdk._runtime.transport,ClinicalTransport)
            response = SimpleNamespace(content="女".encode("cp950"),headers={"Content-Type":"text/html"})
            self.assertEqual(sdk._runtime.transport.text(response),"女")


class DecodeEvidenceTests(unittest.TestCase):
    base = "https://synthetic.invalid/PRQWeb"

    def setUp(self):
        self.transport = ClinicalTransport(policy=RequestPolicy(), attachment_base_url=self.base)
        self.addCleanup(self.transport.close)

    def observe(self, content, header="text/html", *, operation="opd"):
        method, endpoint = ("POST", "QueryOPDPatList.do") if operation == "opd" else ("GET", "QueryBillingSOAP.do")
        with observe_prq_decoding(operation) as evidence:
            text = self.response(content, header, method, self.base + "/" + endpoint)
        return text, evidence

    def response(self, content, header, method, url):
        return self.transport.text(SimpleNamespace(
            content=content, headers={"Content-Type": header}, url=url, request=SimpleNamespace(method=method),
        ))

    def test_source_markers_emit_only_the_exact_fixed_original_bytes(self):
        cases = {
            "utf8_literal": b"\xef\xbf\xbd",
            "html_numeric_entity": b"&#xFfFd;",
            "html_null_entity": b"&#0;",
            "javascript_escape": br"\uFFFD",
        }
        for marker, sample in cases.items():
            with self.subTest(marker=marker):
                content = b'<meta charset="utf-8">SYNTHETIC-NAME' + sample + b"TOKEN-MRN"
                _, evidence = self.observe(content, "text/html; charset=BIG5")
                self.assertEqual(evidence["http_charset"], "big5")
                self.assertEqual(evidence["meta_charset"], "utf-8")
                self.assertEqual(evidence["configured_encoding"], "auto")
                self.assertEqual(evidence["decoded_with"], "utf-8")
                self.assertFalse(evidence["decoder_replacement"])
                self.assertEqual(evidence["source_markers"], [marker])
                self.assertEqual(evidence["source_byte_samples"], [{"marker": marker, "hex": sample.hex()}])
                self.assertEqual(evidence["masked_byte_samples"], [])
                self.assertNotIn(b"SYNTHETIC-NAME".hex(), json.dumps(evidence))

    def test_malformed_bytes_are_masked_and_unknown_labels_are_suppressed(self):
        self.transport.response_encoding = "utf-8"
        invalid = b"\xe2\x80"
        content = b'<meta charset="SECRET-META">SYNTHETIC-NAME' + (invalid + b"/TOKEN-MRN") * 5
        _, evidence = self.observe(content, "text/html; charset=SECRET-HEADER")
        self.assertEqual((evidence["http_charset"], evidence["meta_charset"]), ("other", "other"))
        self.assertEqual(evidence["decoded_with"], "utf-8")
        self.assertEqual(evidence["configured_encoding"], "utf-8")
        self.assertTrue(evidence["decoder_replacement"])
        self.assertEqual(evidence["masked_byte_samples"], ["XXXX"] * 3)
        self.assertEqual(evidence["source_byte_samples"], [])
        serialized = json.dumps(evidence)
        for sensitive in ("SECRET", "SYNTHETIC", "TOKEN", "MRN", invalid.hex(), content.hex()):
            self.assertNotIn(sensitive, serialized)

    def test_bom_and_decoder_replacement_can_coexist_with_original_marker(self):
        self.transport.response_encoding = "big5"
        text, evidence = self.observe(b"\xef\xbb\xbf\xef\xbf\xbd\xff", "text/html", operation="soap")
        self.assertEqual(text, "\ufffd\ufffd")
        self.assertEqual(evidence["configured_encoding"], "big5")
        self.assertEqual(evidence["decoded_with"], "utf-8-sig")
        self.assertTrue(evidence["decoder_replacement"])
        self.assertEqual(evidence["masked_byte_samples"], ["XX"])
        self.assertEqual(evidence["source_byte_samples"], [{"marker": "utf8_literal", "hex": "efbfbd"}])

    def test_scope_excludes_the_opd_landing_other_endpoints_and_other_origins(self):
        content = b"&#65533;"
        header = "text/html"
        with observe_prq_decoding("opd") as evidence:
            self.response(content, header, "GET", self.base + "/QueryOPDPatList.do")
            self.response(content, header, "POST", self.base + "/OtherQuery.do")
            self.response(content, header, "POST", "https://other.invalid/PRQWeb/QueryOPDPatList.do")
            self.assertEqual(evidence, {})
        self.response(content, header, "POST", self.base + "/QueryOPDPatList.do")
        self.assertEqual(evidence, {})


class AttachmentTransportTests(unittest.TestCase):
    base = "https://synthetic.invalid/PRQWeb"

    def transport(self, outcomes=()):
        class Session(requests.Session):
            def __init__(self):
                super().__init__()
                self.calls, self.outcomes, self.closes = [], list(outcomes), 0

            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                outcome = self.outcomes.pop(0) if self.outcomes else 200
                if isinstance(outcome, Exception):
                    raise outcome
                response = requests.Response()
                response.status_code = outcome
                response.url, response._content, response._content_consumed = url, b"synthetic", True
                return response

            def close(self):
                self.closes += 1
                super().close()

        session, sleeps = Session(), []
        policy = RequestPolicy(min_delay_seconds=1.5, max_delay_seconds=1.5, max_attempts=2)
        transport = ClinicalTransport(policy=policy, session=session, sleeper=sleeps.append,
            rng=SimpleNamespace(uniform=lambda low, high: (low + high) / 2), attachment_base_url=self.base)
        return transport, session, sleeps

    def test_scope_is_limited_to_exact_origin_binary_gets_and_resets(self):
        transport, session, sleeps = self.transport()
        image = self.base + "/Page/JSP/showPACSPic.jsp"
        transport.request("GET", image)
        self.assertEqual(sleeps, [1.5])
        original = transport.policy
        normal_requests = (("GET", self.base + "/QueryOrderDetail.do"),
                           ("GET", self.base + "/QueryReportByOrder.do"),
                           ("GET", self.base + "/QueryCaseList.do"),
                           ("POST", "https://synthetic.invalid/portal/login.do"),
                           ("POST", self.base + "/QueryPatientRecord.do"),
                           ("GET", self.base + "/Page/JSP/KS_Patient.jsp"),
                           ("POST", image), ("GET", image + "/other"),
                           ("GET", image.replace("synthetic.invalid", "other.invalid")),
                           ("GET", image.replace("https://", "http://")))
        with report_attachment_downloads():
            for path in ("showPACSPic.jsp", "showPDF.jsp"):
                transport.request("GET", self.base + "/Page/JSP/" + path)
            for method, url in normal_requests:
                transport.request(method, url)
        self.assertEqual(sleeps, [1.5] * (1 + len(normal_requests)),
                         "attachment scope changed text/index/login/identity pacing")
        self.assertIs(transport.policy, original)
        self.assertIs(transport.attachment_transport.session, session)
        self.assertEqual(transport.attachment_transport.policy.max_attempts, original.max_attempts)
        with self.assertRaisesRegex(ValueError, "synthetic scope failure"):
            with report_attachment_downloads():
                raise ValueError("synthetic scope failure")
        transport.request("GET", image)
        self.assertEqual(sleeps, [1.5] * (2 + len(normal_requests)))

    def test_scope_does_not_affect_other_threads_and_missing_origin_fails_closed(self):
        transport, _, sleeps = self.transport()
        image = self.base + "/Page/JSP/showPACSPic.jsp"
        with report_attachment_downloads():
            thread = threading.Thread(target=lambda: transport.request("GET", image))
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sleeps, [1.5])
        transport.attachment_origin = None
        with report_attachment_downloads():
            transport.request("GET", image)
        self.assertEqual(sleeps, [1.5, 1.5])

    def test_attachment_retry_preserves_backoff_timeouts_tls_and_late_capture(self):
        for outcome in (503, requests.ConnectionError("synthetic disconnected")):
            with self.subTest(outcome=type(outcome).__name__):
                transport, session, sleeps = self.transport([outcome, 200])
                tls = SimpleNamespace(apply_all=Mock(), mark_response=Mock())
                transport.connections = tls
                transport.diagnostics, transport.raw_capture = Mock(), Mock()
                with report_attachment_downloads():
                    transport.request("GET", self.base + "/Page/JSP/showPDF.jsp")
                self.assertEqual(sleeps, [1.25], "retry backoff was skipped or attachment jitter returned")
                self.assertEqual(len(session.calls), 2)
                self.assertEqual(session.calls[0][2]["timeout"], (10.0, 45.0))
                self.assertEqual(tls.apply_all.call_count, 2)
                self.assertEqual(transport.diagnostics.record_http_request.call_count, 2)
                self.assertEqual(transport.raw_capture.record_response.call_count +
                                 transport.raw_capture.record_network_error.call_count, 2)
                transport.diagnostics.record_retry.assert_called_once()
                transport.close()
                transport.close()
                self.assertEqual(session.closes, 1, "shared authenticated session was closed more than once")

    def test_normal_query_retry_keeps_original_jitter_inside_attachment_scope(self):
        transport, session, sleeps = self.transport([503, 200])
        with report_attachment_downloads():
            transport.request("GET", self.base + "/QueryCaseList.do")
        self.assertEqual(sleeps, [1.5, 1.25, 1.5])
        self.assertEqual(len(session.calls), 2)

    def test_attachment_retry_after_is_preserved(self):
        for status in (429, 503):
            with self.subTest(status=status):
                transport, session, sleeps = self.transport([status, 200])
                request = session.request

                def limited(method, url, _request=request, _status=status, **kwargs):
                    response = _request(method, url, **kwargs)
                    if response.status_code == _status:
                        response.headers["Retry-After"] = "4"
                    return response

                session.request = limited
                with report_attachment_downloads():
                    transport.request("GET", self.base + "/Page/JSP/showPDF.jsp")
                self.assertEqual(sleeps, [4])
                self.assertEqual(len(session.calls), 2)

    def test_shared_session_is_serialized_across_normal_and_attachment_paths(self):
        transport, session, _ = self.transport()
        started, release, second = threading.Event(), threading.Event(), threading.Event()
        request = session.request
        active, maximum = 0, 0

        def blocked(method, url, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            if url.endswith("QueryCaseList.do"):
                started.set()
                if not release.wait(3):
                    raise AssertionError("synthetic session was not released")
            response = request(method, url, **kwargs)
            active -= 1
            return response

        session.request = blocked
        normal = threading.Thread(target=lambda: transport.request("GET", self.base + "/QueryCaseList.do"))

        def download():
            with report_attachment_downloads():
                second.set()
                transport.request("GET", self.base + "/Page/JSP/showPDF.jsp")

        attachment = threading.Thread(target=download)
        normal.start()
        try:
            self.assertTrue(started.wait(2))
            attachment.start()
            self.assertTrue(second.wait(2))
        finally:
            release.set()
            normal.join(3)
            if attachment.ident is not None:
                attachment.join(3)
        self.assertFalse(normal.is_alive() or attachment.is_alive())
        self.assertEqual(maximum, 1)
