import io
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pypdfium2 as pdfium
from biometry_parser import BiometryParser
from biometry_parser.models import FieldResult, Observation

from vghks_bot import biometry
from vghks_bot.biometry import (
    BiometryError,
    extract_biometry,
    install_biometry_models,
    pdf_page_count,
)
from vghks_bot.selftest_biometry import rasterized_copy, synthetic_biometry_pdf


class BiometryParserTests(unittest.TestCase):
    def test_original_page_scope_and_parser_json_contract(self):
        pdf = synthetic_biometry_pdf("25.58", "23.62")
        self.assertEqual(pdf_page_count(pdf), 2)
        parsed = extract_biometry(pdf, [2])
        self.assertEqual(parsed["metadata"]["selected_pages"], [2])
        self.assertEqual(parsed["metadata"]["source_page_count"], 2)
        self.assertEqual(parsed["eyes"]["OD"]["al"]["candidate_value"], 23.62)
        self.assertIsNone(parsed["eyes"]["OD"]["al"]["value"])
        self.assertEqual(parsed["eyes"]["OS"]["al"]["candidate_value"], 23.41)
        self.assertEqual(json.loads(json.dumps(parsed)), parsed)

    def test_review_evidence_uses_scores_for_the_chosen_source_value(self):
        def observed(page, value, confidence=None, **kwargs):
            return Observation(field="al", eye="OD", page_number=page, value=Decimal(value),
                               confidence=confidence, confidence_scale="0..1 (uncalibrated)" if confidence is not None else None,
                               **kwargs)

        chosen = FieldResult(unit="mm", status="unverified", data_status="present", candidate_value=Decimal("23.62"),
                             candidates=[observed(1, "23.62"), observed(2, "23.62", 0.73),
                                         observed(3, "24.62", 0.35), observed(4, "23.62", 0.1, role="individual")])
        self.assertEqual(biometry._field_review(chosen), {
            "pages": [1, 2], "confidence": 0.73, "confidence_scale": "0..1 (uncalibrated)"})

    def test_all_keeps_both_result_types_for_original_selected_page(self):
        pdf = synthetic_biometry_pdf("25.58", "23.62", include_iol=True)
        result, review = biometry.extract_for_review(pdf, [2], "all")
        self.assertEqual(result["eyes"]["OD"]["al"]["candidate_value"], 23.62)
        self.assertIsNone(result["eyes"]["OD"]["al"]["value"])
        self.assertEqual(review["eyes"]["OD"]["al"], {"pages": [2]})
        self.assertEqual([(table["page_number"], table["rows"][0]["predicted_refraction"])
                          for table in result["iol_calculations"]], [(2, -0.25)])

    def test_invalid_pages_and_malformed_pdf_fail_safely(self):
        pdf = synthetic_biometry_pdf("23.62")
        for pages in ([], [2], [True], [1, 1]):
            with self.subTest(pages=pages), self.assertRaises(BiometryError):
                extract_biometry(pdf, pages)
        with self.assertRaises(BiometryError):
            pdf_page_count(b"%PDF-1.4 invalid")

    def test_cached_pdf_can_be_counted_while_extraction_holds_pdfium(self):
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "digest-without-file-extension"
            asset.write_bytes(synthetic_biometry_pdf("25.58", "23.62"))
            with biometry._PDFIUM_LOCK:
                self.assertEqual(pdf_page_count(asset), 2)
            parsed = extract_biometry(asset, [2])
        self.assertEqual(parsed["eyes"]["OD"]["al"]["candidate_value"], 23.62)

    def test_scanned_page_reports_missing_local_model(self):
        scanned = rasterized_copy(synthetic_biometry_pdf("23.62"))
        with tempfile.TemporaryDirectory() as cache, patch.dict(os.environ, {"BIOMETRY_PARSER_MODEL_CACHE": cache}):
            parsed = extract_biometry(scanned, [1])
        self.assertEqual(parsed["metadata"]["processing_status"], "unavailable")
        self.assertIn("model_unavailable", {issue["code"] for issue in parsed["issues"]})

    def test_frozen_runtime_and_setup_command_use_the_bundled_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            expected = package / biometry.BUNDLED_OCR_MODELS
            (package / biometry.BIOMETRY_VARIANT_FILE).write_bytes(b"full\n")
            with (patch.object(biometry, "__file__", str(package / "biometry.py")),
                  patch("vghks_bot.biometry.sys.frozen", True, create=True),
                  patch.dict(os.environ, {"BIOMETRY_PARSER_MODEL_CACHE": "unrelated-external-cache"}),
                  patch("biometry_parser.BiometryParser", wraps=BiometryParser) as parser,
                  patch("biometry_parser.model_store.resolve_model") as verify,
                  patch("biometry_parser.model_store.download_model") as download):
                extract_biometry(synthetic_biometry_pdf("23.62"), [1])
                install_biometry_models()
        parser.assert_called_once_with(backend="local", allow_cloud=False, model=biometry.OCR_MODEL_NAME, model_cache=expected)
        verify.assert_called_once_with(biometry.OCR_MODEL_NAME, expected)
        download.assert_not_called()

    def test_lite_reads_text_without_ocr_packages_and_labels_image_only_pages(self):
        pdf = synthetic_biometry_pdf("23.62")
        scanned = rasterized_copy(pdf)
        blocked = {package: None for package in ("rapidocr", "onnxruntime", "cv2", "numpy", "shapely")}
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / biometry.BIOMETRY_VARIANT_FILE).write_bytes(b"lite\n")
            with (patch.object(biometry, "__file__", str(package / "biometry.py")),
                  patch("vghks_bot.biometry.sys.frozen", True, create=True),
                  patch.dict(sys.modules, blocked),
                  patch("biometry_parser.BiometryParser", wraps=BiometryParser) as parser):
                native = extract_biometry(pdf, [1])
                parsed = extract_biometry(scanned, [1])
                with self.assertRaisesRegex(BiometryError, r"VGHKS-bot-ocr\.exe"):
                    install_biometry_models()
        self.assertEqual([call.kwargs for call in parser.call_args_list],
            [{"backend": "native", "allow_cloud": False}] * 2)
        self.assertEqual(native["eyes"]["OD"]["al"]["candidate_value"], 23.62)
        self.assertEqual(parsed["metadata"]["processing_status"], "unavailable")
        self.assertTrue(any(issue["code"] == "ocr_required" and issue["page_number"] == 1
                            and "VGHKS-bot-ocr.exe" in issue["message"]
                            for issue in parsed["issues"]), parsed["issues"])

    def test_basic_disables_parser_page_counts_and_model_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / biometry.BIOMETRY_VARIANT_FILE).write_bytes(b"basic\n")
            with (patch.object(biometry, "__file__", str(package / "biometry.py")),
                  patch("vghks_bot.biometry.sys.frozen", True, create=True),
                  patch.dict(sys.modules, {"biometry_parser": None, "pypdfium2": None})):
                for operation in (pdf_page_count, extract_biometry):
                    with self.assertRaisesRegex(BiometryError, "基本版"):
                        operation(b"")
                self.assertEqual(biometry.count_worker_main([biometry._COUNT_MODE, "-"]), 1)
                with self.assertRaisesRegex(BiometryError, "離線 OCR 版"):
                    install_biometry_models()

    def test_source_installation_downloads_model(self):
        with patch("biometry_parser.model_store.download_model") as download:
            install_biometry_models()
        download.assert_called_once_with(biometry.OCR_MODEL_NAME)

    def test_frozen_runtime_rejects_missing_or_invalid_variant_marker(self):
        with (tempfile.TemporaryDirectory() as directory,
              patch("vghks_bot.biometry.sys.frozen", True, create=True),
              patch.object(biometry, "__file__", str(Path(directory) / "biometry.py"))):
            with self.assertRaisesRegex(BiometryError, "封裝版本"):
                biometry.biometry_variant()
            (Path(directory) / biometry.BIOMETRY_VARIANT_FILE).write_bytes(b"other\n")
            with self.assertRaisesRegex(BiometryError, "封裝版本"):
                biometry.biometry_variant()

    def test_missing_model_keeps_native_pages_after_a_scanned_page(self):
        native = synthetic_biometry_pdf("23.62")
        mixed = io.BytesIO()
        with (pdfium.PdfDocument.new() as combined, pdfium.PdfDocument(rasterized_copy(native)) as scanned,
              pdfium.PdfDocument(native) as text):
            combined.import_pages(scanned)
            combined.import_pages(text)
            combined.save(mixed)
        with tempfile.TemporaryDirectory() as cache, patch.dict(os.environ, {"BIOMETRY_PARSER_MODEL_CACHE": cache}):
            parsed = extract_biometry(mixed.getvalue(), [1, 2])
        self.assertEqual(parsed["metadata"]["processing_status"], "partial")
        self.assertEqual(parsed["metadata"]["selected_pages"], [1, 2])
        self.assertEqual(parsed["eyes"]["OD"]["al"]["candidate_value"], 23.62)
        self.assertIn("model_unavailable", {issue["code"] for issue in parsed["issues"]})

        with patch("vghks_bot.biometry.biometry_variant", return_value="lite"):
            text_only = extract_biometry(mixed.getvalue(), [1, 2])
        self.assertEqual(text_only["metadata"]["processing_status"], "partial")
        self.assertEqual(text_only["eyes"]["OD"]["al"]["candidate_value"], 23.62)
        self.assertEqual([issue["page_number"] for issue in text_only["issues"] if issue["code"] == "ocr_required"], [1])
