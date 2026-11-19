"""Synthetic optical measurements for parser and packaged OCR checks."""
from __future__ import annotations

import ctypes
import io
import os
from contextlib import closing, contextmanager
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw

from .biometry import _PDFIUM_LOCK, extract_biometry, pdf_page_count


def synthetic_biometry_pdf(*right_axial_lengths, include_iol=False):
    """Make independent reports with conflicting values if page scope is lost."""
    output = io.BytesIO()
    with pdfium.PdfDocument.new() as document:
        for axial_length in right_axial_lengths:
            with closing(document.new_page(595, 842)) as page:
                labels = (
                    (40, 790, "ZEISS IOLMaster 700"), (40, 760, "Patient ID: SYNTHETIC"),
                    (40, 730, "Date of measurement: 11/18/2026"),
                    (220, 680, "Biometric values"), (60, 650, "OD"), (380, 650, "OS"),
                    (60, 600, f"AL: {axial_length} mm"), (380, 600, "AL: 23.41 mm"),
                    (60, 575, "ACD: 3.01 mm"), (380, 575, "ACD: 3.11 mm"),
                )
                if include_iol:
                    labels += (
                        (60, 515, "IOL Calculation"), (60, 490, "OD"),
                        (60, 459, "SYNTHETIC-LENS"), (60, 435, "Barrett Universal II K"),
                        (60, 405, "IOL (D)"), (155, 405, "Ref (D)"),
                        (60, 378, "+20.50"), (155, 378, "-0.25"),
                        (60, 355, "+20.00"), (155, 355, "+0.12"),
                    )
                for x, y, label in labels:
                    text = pdfium_raw.FPDFPageObj_NewTextObj(document, b"Helvetica", 14)
                    utf16 = (ctypes.c_ushort * (len(label) + 1)).from_buffer_copy(label.encode("utf-16-le") + b"\0\0")
                    assert pdfium_raw.FPDFText_SetText(text, utf16)
                    pdfium_raw.FPDFPageObj_Transform(text, 1, 0, 0, 1, x, y)
                    pdfium_raw.FPDFPage_InsertObject(page, text)
                page.gen_content()
        document.save(output)
    return output.getvalue()


def rasterized_copy(pdf):
    output = io.BytesIO()
    with pdfium.PdfDocument(pdf) as document, closing(document[0]) as page, closing(page.render(scale=2)) as bitmap:
        with bitmap.to_pil() as image, image.convert("RGB") as raster:
            raster.save(output, format="PDF", resolution=144)
    return output.getvalue()


@contextmanager
def _empty_model_cache():
    key = "BIOMETRY_PARSER_MODEL_CACHE"
    original = os.environ.get(key)
    with TemporaryDirectory() as empty_cache:
        os.environ[key] = empty_cache
        try:
            yield
        finally:
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original


def check_bundled_ocr():
    """Recognize an image-only PDF with an empty external model cache."""
    scan = rasterized_copy(synthetic_biometry_pdf("23.62"))
    with _empty_model_cache():
        result = extract_biometry(scan, [1])
    assert result["metadata"]["processing_status"] == "succeeded", result["issues"]
    assert result["eyes"]["OD"]["al"]["candidate_value"] == 23.62, result["issues"]


def check_concurrent_page_count():
    """Count a cached PDF through the frozen worker while the parser lock is held."""
    with TemporaryDirectory() as directory:
        asset = Path(directory) / "digest-without-file-extension"
        asset.write_bytes(synthetic_biometry_pdf("25.58", "23.62"))
        with _PDFIUM_LOCK:
            assert pdf_page_count(asset) == 2


def check_lite_text_pdf():
    """Parse native PDF text with the OCR runtime excluded from the EXE."""
    for package in ("rapidocr", "onnxruntime", "cv2", "numpy", "shapely"):
        assert find_spec(package) is None, f"Unexpected OCR dependency: {package}"
    pdf = synthetic_biometry_pdf("23.62")
    scan = rasterized_copy(pdf)
    native = extract_biometry(pdf, [1])
    scanned = extract_biometry(scan, [1])
    assert native["metadata"]["processing_status"] == "succeeded", native["issues"]
    assert native["eyes"]["OD"]["al"]["candidate_value"] == 23.62, native["issues"]
    assert scanned["metadata"]["processing_status"] == "unavailable", scanned["issues"]
    assert any(issue["code"] == "ocr_required" and issue["page_number"] == 1
               for issue in scanned["issues"]), scanned["issues"]
