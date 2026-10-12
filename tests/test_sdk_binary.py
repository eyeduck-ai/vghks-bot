"""DBR binary regression through the production SDK, platform cache and DEBUG."""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from vghks_sdk.core.errors import AuthExpiredError, ParseError, PasswordChangeRequiredError
from vghks_sdk.core.operations import operation_spec
from vghks_sdk.models import PacsImageRef, PdfAttachmentRef

from vghks_bot.bot import BotApplication
from vghks_bot.debug_evidence import Redactor, response_shape, response_snapshot, save_response
from vghks_bot.selftest_binary import (
    JPEG,
    MRN,
    PDF,
    PDF_REF,
    BinaryScenario,
    check_sdk_binary,
    seed_cataract,
)
from vghks_bot.settings import Settings


class SDKBinaryTests(unittest.TestCase):
    def test_source_and_frozen_selftest_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            check_sdk_binary(Path(directory))

    def test_compatible_adapter_and_clinical_transport_keep_binary_bytes_with_any_mime(self):
        for body, method, ref in ((PDF, "download_pdf", PDF_REF),
                                  (JPEG, "download_pacs_image", PacsImageRef(MRN, "REQ", "", "", "IMG"))):
            for mime in ("application/pdf", "image/jpeg", "text/html", "application/octet-stream", ""):
                scenario = BinaryScenario(body, mime)
                with self.subTest(method=method, mime=mime), scenario.factory(Settings()) as sdk:
                    with (patch.object(sdk._runtime.transport, "text", side_effect=AssertionError("binary decoded")),
                          patch("vghks_sdk.adapters.auth.parse_password_status", side_effect=AssertionError("binary parsed as HTML"))):
                        asset = getattr(sdk.orders, method)(ref)
                    self.assertEqual(asset.content, body)
                    self.assertEqual(len(scenario.calls), 1)

    def test_html_login_and_password_change_cannot_hide_behind_pdf_header(self):
        cases = [(b'<form><input name="muid"><input name="mpassword" type="password"></form>', AuthExpiredError),
                 ("<p>密碼已過期，必須先變更密碼才能登入</p>".encode(), PasswordChangeRequiredError)]
        for body, error in cases:
            with self.subTest(error=error.__name__), BinaryScenario(body).factory(Settings()) as sdk:
                with self.assertRaises(error):
                    sdk._runtime.request_binary(operation_spec("prq.pdf_attachment"),
                        sdk._runtime.settings.prq_base_url + "/Page/JSP/showPDF.jsp", max_bytes=4096)

    def test_http_refusal_and_login_destination_are_still_rejected(self):
        for status, url in ((401, ""), (403, ""), (200, "https://synthetic.invalid/login.do")):
            scenario = BinaryScenario(status=status, final_url=url)
            with self.subTest(status=status, url=url), scenario.factory(Settings()) as sdk:
                with self.assertRaises(AuthExpiredError):
                    sdk._runtime.request_binary(operation_spec("prq.pdf_attachment"),
                        sdk._runtime.settings.prq_base_url + "/Page/JSP/showPDF.jsp", max_bytes=4096)

    def test_incomplete_pdf_retains_validation_code(self):
        with BinaryScenario(PDF.removesuffix(b"%%EOF\n")).factory(Settings()) as sdk:
            with self.assertRaises(ParseError) as error:
                sdk.orders.download_pdf(PDF_REF)
            self.assertEqual(error.exception.info.code, "PDF_BINARY_INVALID")

    def test_binary_evidence_keeps_original_bytes_without_html_decoding(self):
        for body, mime in ((PDF, "text/html"), (JPEG, "")):
            scenario = BinaryScenario(body, mime)
            response = scenario.request("GET", "https://synthetic.invalid/PRQWeb/Page/JSP/showPDF.jsp")
            snapshot = response_snapshot(1, response, .01)
            with self.subTest(mime=mime), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with patch("vghks_bot.debug_evidence.decode_response", side_effect=AssertionError("binary decoded")):
                    shape = response_shape(snapshot, "auto", Redactor())
                    evidence, written = save_response(root, 1, snapshot, Redactor(), "auto", len(body))
                self.assertEqual(shape["kind"], "binary")
                self.assertEqual(shape["decoded_as"], "")
                self.assertNotIn("html_shape", shape)
                self.assertEqual(written, len(body))
                self.assertEqual(len(evidence["files"]), 1)
                self.assertEqual(evidence["binary_encoding"], "original")
                self.assertFalse(evidence["redacted"] or evidence["truncated"])
                self.assertEqual((root / evidence["files"][0]["name"]).read_bytes(), body)
                self.assertFalse(list(root.glob("*.html")))
                truncated, size = save_response(root, 2, snapshot, Redactor(), "auto", 8)
                self.assertTrue(truncated["truncated"])
                self.assertEqual(size, 8)


class CataractBinaryCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scenario = BinaryScenario()
        self.app = BotApplication(Settings(), Path(self.temp.name), self.scenario.factory)
        self.addCleanup(self.app.close)
        account = self.app.login({"username": "SYNTHETIC", "password": "SYNTHETIC"})["account"]["id"]
        self.work = self.app.workspace(account)
        self.history, self.store = self.work.review.history, self.work.analysis.store

    def execute(self, task):
        with self.work.gateway.task_context(task["id"], "history"):
            result = self.history.execute(task)
        report, _ = self.history._stored(MRN, "order_report", task["reference"], cataract=True)
        return {**report, **result}

    def test_partial_report_retries_failed_branch_and_reuses_saved_attachment(self):
        second = PdfAttachmentRef(MRN, f"//nfs01p/EMRU/{MRN}/second.pdf")
        task = seed_cataract(self.work, (PDF_REF, second))
        broken = PDF.removesuffix(b"%%EOF\n")
        self.scenario.files["second.pdf"] = broken
        first = self.execute(task)
        self.assertEqual(first["status"], "partial")
        self.assertEqual(len(first["assets"]), 1)
        self.assertEqual(first["issues"][0]["code"], "PDF_BINARY_INVALID")
        self.assertFalse(self.work.analysis.cataract_orders(MRN)[0]["report_complete"])
        with zipfile.ZipFile(io.BytesIO(self.work.diagnostics.export({"task_id": task["id"]}))) as archive:
            manifest = json.loads(archive.read("debug.json"))
            self.assertFalse(manifest["export"]["missing_files"])
            binaries = [archive.read(name) for name in archive.namelist() if name.endswith(".bin")]
            self.assertEqual(binaries, [broken])
            self.assertFalse(any(name.endswith(".html") for name in archive.namelist()))
        self.scenario.files["second.pdf"] = PDF.replace(b"1 0 obj", b"2 0 obj")
        retried = self.execute(task)
        self.assertEqual(retried["status"], "ready")
        self.assertEqual(len(retried["assets"]), 2)
        self.assertEqual([call["filename"] for call in self.scenario.calls],
                         ["report.pdf", "second.pdf", "second.pdf"])
        self.assertTrue(self.work.analysis.cataract_orders(MRN)[0]["report_complete"])
        self.assertTrue(self.execute(task)["cached"])
        self.assertEqual(len(self.scenario.calls), 3)

    def test_missing_cached_file_downloads_again_without_forcing_other_branches(self):
        task = seed_cataract(self.work)
        result = self.execute(task)
        path, _ = self.store.asset(result["assets"][0]["digest"])
        path.unlink()
        self.assertFalse(self.work.analysis.cataract_orders(MRN)[0]["report_complete"])
        result = self.execute(task)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(self.scenario.calls), 2)
        self.assertTrue(self.store.asset(result["assets"][0]["digest"])[0].is_file())
