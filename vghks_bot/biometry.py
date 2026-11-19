"""Local biometry-parser adapter for saved cataract report PDFs."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from biometry_parser.models import ExtractionResult, FieldResult

OCR_MODEL_NAME = "ppocr-v6-small"
BUNDLED_OCR_MODELS = "biometry_models"
BIOMETRY_VARIANT_FILE = "biometry_variant.txt"
BIOMETRY_EXECUTABLES = {"basic": "VGHKS-bot.exe", "lite": "VGHKS-bot-pdf.exe", "full": "VGHKS-bot-ocr.exe"}
_COUNT_MODE = "--internal-biometry-page-count"
EXTRACTION_CATEGORIES = ("biometry", "iol", "all")

# PDFium is process-wide and does not allow concurrent calls, even across PDFs.
_PDFIUM_LOCK = Lock()
_BASIC_EXTRACTION_ERROR = "基本版提供 PDF 預覽與比較。請使用文字 PDF 版或離線 OCR 版擷取 PDF 數據。"
_OCR_NOT_INCLUDED_ERROR = f"請使用離線 OCR 版 {BIOMETRY_EXECUTABLES['full']} 辨識掃描 PDF。"
_BUNDLED_MODEL_ERROR = f"EXE 內建的本機 OCR 模型缺失或損毀。請更換完整的 {BIOMETRY_EXECUTABLES['full']} 後再重試。"
_OCR_SETUP_ISSUES = {
    "model_unavailable": "尚未安裝本機 OCR 模型 PP-OCRv6 small。",
    "model_hash_mismatch": "本機 OCR 模型未通過完整性檢查。",
    "dependency_unavailable": "本機 OCR 元件無法使用。請重新安裝本程式，再擷取掃描頁。",
}


class BiometryError(ValueError):
    """An input or preparation failure safe to display in the cataract workspace."""


def biometry_variant() -> str:
    """Identify the build from its embedded marker, independently of its filename."""
    if not getattr(sys, "frozen", False):
        return "source"
    message = "無法辨識 EXE 的封裝版本。請更換完整的 VGHKS-bot EXE 後再重試。"
    try:
        marker = Path(__file__).resolve().with_name(BIOMETRY_VARIANT_FILE).read_bytes()
    except OSError as exc:
        raise BiometryError(message) from exc
    for variant in BIOMETRY_EXECUTABLES:
        if marker == f"{variant}\n".encode("ascii"):
            return variant
    raise BiometryError(message)


def _bundled_model_cache(variant: str) -> Path | None:
    if variant == "full":
        return Path(__file__).resolve().with_name(BUNDLED_OCR_MODELS)
    return None


def _ocr_setup_message(code: str, variant: str) -> str:
    message = _OCR_SETUP_ISSUES[code]
    if code == "dependency_unavailable":
        return message
    if variant == "full":
        return _BUNDLED_MODEL_ERROR
    return f"{message} 請執行 python run.py --install-biometry-models，再擷取掃描頁。"


def _pdf_page_count(pdf: bytes | Path) -> int:
    try:
        from pypdfium2 import PdfDocument, PdfiumError
    except (ImportError, OSError) as exc:
        raise BiometryError("PDF 擷取元件無法使用。請重新安裝本程式。") from exc
    try:
        with PdfDocument(pdf) as document:
            count = len(document)
    except (PdfiumError, ValueError, OSError) as exc:
        raise BiometryError("無法開啟 PDF。請確認報告完整且未加密。") from exc
    if not count:
        raise BiometryError("這份 PDF 沒有可擷取的頁面。")
    return count


def pdf_page_count(pdf: bytes | Path) -> int:
    """Keep page selection responsive while another PDF is using PDFium for OCR."""
    if biometry_variant() == "basic":
        raise BiometryError(_BASIC_EXTRACTION_ERROR)
    if _PDFIUM_LOCK.acquire(blocking=False):
        try:
            return _pdf_page_count(pdf)
        finally:
            _PDFIUM_LOCK.release()

    # PDFium calls in the parser and this server cannot overlap in one process.
    # Start a short-lived reader only when an extraction already holds the lock.
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.extend(("-m", "vghks_bot.biometry"))
    path = str(pdf.resolve()) if isinstance(pdf, Path) else "-"
    try:
        child = subprocess.run([*command, _COUNT_MODE, path], input=pdf if path == "-" else b"",
                               cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, check=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        count = int(child.stdout)
    except subprocess.TimeoutExpired as exc:
        raise BiometryError("讀取 PDF 頁數逾時，請稍後再試。") from exc
    except (subprocess.CalledProcessError, ValueError, OSError) as exc:
        raise BiometryError("無法讀取 PDF 頁數，請確認報告完整且未加密。") from exc
    if count < 1:
        raise BiometryError("這份 PDF 沒有可擷取的頁面。")
    return count


def count_worker_main(arguments: list[str]) -> int | None:
    if len(arguments) != 2 or arguments[0] != _COUNT_MODE:
        return None
    try:
        if biometry_variant() == "basic":
            raise BiometryError(_BASIC_EXTRACTION_ERROR)
        source = arguments[1]
        if source == "-":
            with open(0, "rb", closefd=False) as stream:
                pdf = stream.read()
        else:
            pdf = Path(source)
        count = _pdf_page_count(pdf)
        with open(1, "w", encoding="ascii", closefd=False) as stream:
            stream.write(f"{count}\n")
        return 0
    except (BiometryError, OSError):
        return 1


def _has_measurements(result) -> bool:
    return bool(result.iol_calculations or any(
        observation.eye and observation.value is not None
        for run in result.runs for observation in run.observations
    ))


def _report_image_only_pages(result) -> None:
    from biometry_parser.models import Issue

    image_pages = sorted({
        decision["page_number"]
        for run in result.runs for decision in run.metadata.get("routing_decisions", [])
        if decision["report_template"]["native_quality"]["positioned_span_count"] == 0
    })
    if image_pages:
        result.issues.extend(Issue(
            code="ocr_required", message=f"此頁沒有可讀的 PDF 文字層。{_OCR_NOT_INCLUDED_ERROR}", page_number=page,
        ) for page in image_pages)
        result.metadata["processing_status"] = "partial" if _has_measurements(result) else "unavailable"


def _parse_biometry(pdf: bytes | Path, pages: list[int] | None) -> ExtractionResult:
    variant = biometry_variant()
    if variant == "basic":
        raise BiometryError(_BASIC_EXTRACTION_ERROR)
    try:
        from biometry_parser import BiometryParser
        from pypdfium2 import PdfiumError
    except (ImportError, OSError) as exc:
        raise BiometryError("生物量測擷取元件無法使用。請重新安裝本程式。") from exc
    model_cache = _bundled_model_cache(variant)
    try:
        with TemporaryDirectory(prefix="vghks-biometry-") as folder:
            source = Path(folder) / "report.pdf"
            if isinstance(pdf, Path):
                copyfile(pdf, source)
            else:
                source.write_bytes(pdf)
            with _PDFIUM_LOCK:
                count = _pdf_page_count(source)
                if pages is not None and (
                    not isinstance(pages, list) or not pages
                    or any(type(page) is not int or not 1 <= page <= count for page in pages)
                    or len(set(pages)) != len(pages)
                ):
                    raise BiometryError(f"請選取有效的 PDF 頁碼（1 至 {count}），且不可重複。")
                if variant == "lite":
                    result = BiometryParser(backend="native", allow_cloud=False).parse(source, pages=pages)
                    _report_image_only_pages(result)
                else:
                    result = BiometryParser(backend="local", allow_cloud=False, model=OCR_MODEL_NAME,
                                            model_cache=model_cache).parse(source, pages=pages)
                    setup_issues = [issue for issue in result.issues if issue.code in _OCR_SETUP_ISSUES]
                    if setup_issues:
                        # Retain text-based pages even if an earlier scanned page failed OCR setup.
                        result = BiometryParser(backend="native", allow_cloud=False).parse(source, pages=pages)
                        result.issues.extend(issue.model_copy(update={"message": _ocr_setup_message(issue.code, variant)})
                                             for issue in setup_issues)
                        result.metadata["processing_status"] = "partial" if _has_measurements(result) else "unavailable"
                result.metadata["source_page_count"] = count
    except BiometryError:
        raise
    except (PdfiumError, ValueError, OSError) as exc:
        raise BiometryError("無法擷取這份 PDF。請確認選取的頁面可正常開啟，再重試。") from exc
    return result


def extract_biometry(pdf: bytes | Path, pages: list[int] | None = None) -> dict:
    """Extract selected 1-based pages and preserve the parser's exchange contract."""
    result = _parse_biometry(pdf, pages)
    from biometry_parser import to_exchange_document

    return to_exchange_document(result)


def _field_review(field: FieldResult) -> dict:
    observations = [item for item in field.candidates if item.role == "summary"]
    if field.candidate_value is not None:
        observations = [item for item in observations
                        if item.status == "present" and item.value == field.candidate_value]
    if not observations:
        return {}
    evidence = {"pages": sorted({item.page_number for item in observations})}
    # The pinned local OCR reports 0..1 uncalibrated scores. Native readings
    # have no score. Scores for an unrelated value are excluded above.
    scored = [item for item in observations if item.confidence is not None
              and 0 <= item.confidence <= 1 and item.confidence_scale == "0..1 (uncalibrated)"]
    if scored:
        lowest = min(scored, key=lambda item: item.confidence)
        evidence.update(confidence=lowest.confidence, confidence_scale=lowest.confidence_scale)
    return evidence


def extract_for_review(pdf: bytes | Path, pages: list[int] | None, category: str) -> tuple[dict, dict]:
    """Return the requested fields with separate evidence for source review."""
    if category not in EXTRACTION_CATEGORIES:
        raise BiometryError("請選擇有效的 PDF 擷取類型。")
    result = _parse_biometry(pdf, pages)
    from biometry_parser import to_exchange_document
    from biometry_parser.models import EYE_FIELDS

    # The pinned parser recognizes both record types in one pass. Project its
    # exchange document without revisiting the PDF or promoting candidates.
    data = to_exchange_document(result)
    if category == "biometry":
        data.pop("iol_calculations")
        data["issues"] = [item for item in data["issues"]
                          if not item["code"].startswith("iol_") and item["code"] != "invalid_iol_cell"]
    elif category == "iol":
        for key in ("eyes", "completeness", "verification_state"):
            data.pop(key)
        for key in ("merge_policy_version", "source_state_counts"):
            data["metadata"].pop(key, None)
        measurement_fields = {*EYE_FIELDS, "k1", "k2", "tk1", "tk2"}
        data["issues"] = [item for item in data["issues"]
                          if item["code"] not in ("no_biometry", "unpaired_keratometry")
                          and item["field"] not in measurement_fields]
        return data, {}
    eyes = {eye: {name: evidence for name in EYE_FIELDS
                  if (evidence := _field_review(result.scalar(name, eye)))} for eye in result.eyes}
    return data, {"eyes": eyes}


def install_biometry_models() -> None:
    """Prepare the local model cache or verify the full build's bundled weights."""
    variant = biometry_variant()
    if variant in {"basic", "lite"}:
        raise BiometryError(_OCR_NOT_INCLUDED_ERROR)
    try:
        from biometry_parser.model_store import ModelStoreError, download_model, resolve_model
    except ImportError as exc:
        raise BiometryError("生物量測擷取元件無法使用。請重新安裝本程式。") from exc
    model_cache = _bundled_model_cache(variant)
    try:
        if model_cache is None:
            download_model(OCR_MODEL_NAME)
        else:
            resolve_model(OCR_MODEL_NAME, model_cache)
    except (ModelStoreError, OSError) as exc:
        if model_cache is not None:
            raise BiometryError(_BUNDLED_MODEL_ERROR) from exc
        raise BiometryError("本機 OCR 模型安裝失敗。請確認網路與資料夾權限後重新執行。") from exc


if __name__ == "__main__":
    raise SystemExit(count_worker_main(sys.argv[1:]))
