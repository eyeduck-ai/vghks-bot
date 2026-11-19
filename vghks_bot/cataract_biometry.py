"""Account-scoped background extraction from verified cataract PDFs."""
from __future__ import annotations

import json
import math
import re
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field

from . import biometry
from .analysis_store import identifier
from .scanner import Cancelled, safe_failure

ACTIVE = {"queued", "running"}
REVIEW_FIELD = re.compile(
    r"(?:eyes\.(?:OD|OS)\.(?:al|acd|lt|cct|wtw|pupil|mean_k|corneal_astigmatism(?:_axis)?|"
    r"(?:k[12]|tk[12])\.(?:power|axis))|iol_calculations\.[0-9]+\."
    r"(?:eye|lens_model|formula|lens_type|keratometry_source|target_refraction|"
    r"constants\.[0-9]+\.value|rows\.[0-9]+\.(?:iol_power|predicted_refraction|toric_power|axis)))")


def review_draft(value):
    if (not isinstance(value, dict) or type(value.get("confirmed")) is not bool
            or not isinstance(edits := value.get("edits"), list) or len(edits) > 4096):
        raise ValueError("核對草稿格式不正確。")
    draft = {}
    for pair in edits:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("核對欄位格式不正確。")
        key, edit = pair
        if (not isinstance(key, str) or len(key) > 120 or not REVIEW_FIELD.fullmatch(key) or key in draft
                or not isinstance(edit, dict) or not isinstance(edit.get("raw"), str) or len(edit["raw"]) > 4096):
            raise ValueError("核對欄位格式不正確。")
        draft[key] = {"raw": edit["raw"]}
        if "value" in edit:
            corrected = edit["value"]
            if (corrected is not None and not (type(corrected) is int and abs(corrected) < 2**53
                    or type(corrected) is float and math.isfinite(corrected)
                    or isinstance(corrected, str) and len(corrected) <= 4096)):
                raise ValueError("核對數值格式不正確。")
            draft[key]["value"] = corrected
        elif value["confirmed"]:
            raise ValueError("請先修正格式有誤的數值，再確認核對。")
    return {"confirmed": value["confirmed"], "edits": [[key, draft[key]] for key in sorted(draft)]}


@dataclass
class _Extraction:
    scope: tuple[str, str, str]
    documents: list[dict]
    category: str
    view: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cancel: threading.Event = field(default_factory=threading.Event)
    state: str = "queued"
    processed: int = 0
    result: dict | None = None
    message: str = ""

    def snapshot(self):
        return {"job_id": self.id, "category": self.category, "view": self.view,
                "state": self.state, "total": len(self.documents),
                "processed": self.processed, "result": self.result, "message": self.message}


class CataractBiometry:
    def __init__(self, analysis):
        self.analysis, self.app = analysis, analysis.app
        self.lock = threading.Lock()
        self.pending = deque()
        self.latest = {}
        self.worker = None
        self.closed = False

    def _scope(self, values):
        cohort = identifier(values.get("cohort_id"))
        mrn, source = values.get("mrn"), values.get("source")
        if not isinstance(mrn, str) or source not in ("orders", "scans"):
            raise ValueError("請指定病人與 PDF 來源。")
        if self.analysis.cohort_member(values).get("account_id") != getattr(self.app, "account_id", None):
            raise ValueError("此病人未指定給目前抓取帳號。")
        return cohort, mrn, source

    def _catalog(self, scope, values):
        _, mrn, source = scope
        documents = {}
        if source == "orders":
            bounds = self.analysis.options({"modules": ["cataract"],
                "start": values.get("start", ""), "end": values.get("end", "")})
            for order in self.analysis.cataract_orders(mrn, bounds["start"], bounds["end"]):
                for asset in order["attachments"]:
                    if asset["available"] and asset["mime"] == "application/pdf":
                        documents[order["id"], asset["digest"]] = {
                            "id": order["id"], "digest": asset["digest"], "date": order["date"],
                            "label": f'{order["name"]} · {asset["name"]}', "pages": None, "_slot": asset["name"]}
        else:
            history, references = self.app.scans.history_catalog(mrn)
            if values.get("action") == "start" and values.get("mode") == "all" and not history["complete"]:
                raise ValueError("請先取得完整的歷年掃描索引，或勾選已列出的 PDF。")
            for row in history["records"]:
                asset = row["asset"]
                if asset and asset["mime"] != "application/pdf":
                    continue
                documents.setdefault((row["id"], None), {
                    "id": row["id"], "date": row["date"], "pages": None,
                    "_scan_ref": references[row["id"]], "_slot": "",
                    **({"digest": asset["digest"]} if asset else {}),
                    "label": row["category_label"] or row["record_type"] or "掃描病歷"})
        return documents

    @staticmethod
    def _selection(source, catalog, values, *, one=False):
        if not one and values.get("mode") == "all":
            return list(catalog.values())
        selection = values.get("documents")
        if (not one and values.get("mode") != "selected" or not isinstance(selection, list)
                or not selection or (one and len(selection) != 1)):
            raise ValueError("請選擇要處理的 PDF。")
        picked = {}
        for item in selection:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ValueError("PDF 選取格式不正確。")
            sha = item.get("digest") if source == "orders" else None
            if source == "orders" and not isinstance(sha, str):
                raise ValueError("PDF 附件代碼不正確。")
            key = item["id"], sha
            if key not in catalog or key in picked:
                raise ValueError("PDF 重複或不在此病人的可用附件中。")
            pages = item.get("pages")
            if pages is not None and (not isinstance(pages, list) or not pages or
                    any(type(n) is not int or n < 1 for n in pages)):
                raise ValueError("請至少選擇一個有效頁碼。")
            doc = {**catalog[key], "pages": sorted(set(pages)) if pages else None}
            if source == "scans" and (expected := item.get("digest")) is not None:
                if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
                    raise ValueError("PDF 附件代碼不正確。")
                doc["_expected_digest"] = expected
            picked[key] = doc
        return list(picked.values())

    def handle(self, values):
        if biometry.biometry_variant() == "basic":
            raise ValueError("基本版提供 PDF 預覽與比較。擷取眼科測量請使用文字 PDF 版或離線 OCR 版。")
        action = values.get("action")
        if action not in ("start", "status", "pages", "load", "save_review"):
            raise ValueError("擷取操作不正確。")
        library = self.analysis.store.library
        with self.app.lock, (library.batch() if action == "save_review" else library.read_snapshot()):
            if not getattr(self.app, "entered", False):
                raise ValueError("請先登入或進入離線檢閱。")
            scope = self._scope(values)
            if action == "status":
                return self._status(scope, values.get("job_id"))
            if action == "start" and self.app.root.read_only:
                raise ValueError("目前是唯讀檢閱，無法開始擷取。")
            if action == "save_review" and self.app.root.read_only:
                raise ValueError("目前是唯讀檢閱，無法儲存核對內容。")
            category = values.get("category", "all")
            view = values.get("view", "batch")
            if action == "start" and category not in biometry.EXTRACTION_CATEGORIES:
                raise ValueError("請選擇有效的 PDF 擷取類型。")
            if action == "start" and view not in ("inline", "batch"):
                raise ValueError("PDF 擷取檢視方式不正確。")
            reviewing = action in ("load", "save_review")
            catalog = self._catalog(scope, {} if reviewing else values)
            try:
                selection = self._selection(scope[2], catalog, values, one=action != "start")
            except ValueError:
                if reviewing and self._removed_pdf(scope, catalog, values.get("documents")):
                    return {"state": "changed"}
                raise
            if action == "start":
                if view == "inline" and (values.get("mode") != "selected" or len(selection) != 1):
                    raise ValueError("預覽擷取只能選取目前的一份 PDF。")
                return self._start(scope, selection, category=category, view=view)
            if action in ("load", "save_review"):
                return self._saved(scope, selection[0], catalog, values)
        path, digest = self._pdf(scope, selection[0], download=False)
        result = {"page_count": biometry.pdf_page_count(path)}
        if scope[2] == "scans":
            result["digest"] = digest
        return result

    def _removed_pdf(self, scope, catalog, documents):
        _, mrn, source = scope
        if not isinstance(documents, list) or len(documents) != 1 or not isinstance(ref := documents[0], dict):
            return False
        document_id, sha = ref.get("id"), ref.get("digest")
        return (isinstance(document_id, str) and isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{64}", sha)
                and (document_id, sha if source == "orders" else None) not in catalog
                and bool(self.analysis.store.cataract_pdf_review(mrn, source, document_id, sha, draft_only=True)))

    def _saved(self, scope, doc, catalog, values):
        _, mrn, source = scope
        store, sha = self.analysis.store, doc.get("digest")
        if source == "scans" and doc.get("_expected_digest", sha) != sha:
            return {"state": "changed"}
        if values["action"] == "save_review":
            capture_id, revision = values.get("capture_id"), values.get("revision")
            if (not isinstance(capture_id, str) or not re.fullmatch(r"[a-f0-9]{32}", capture_id)
                    or type(revision) is not int or not 0 <= revision < 2**53 - 1):
                raise ValueError("核對版本不正確。")
            return store.save_cataract_pdf_draft(mrn, source, doc["id"], sha, capture_id, revision,
                                                   review_draft(values.get("review")))
        saved = store.cataract_pdf_review(mrn, source, doc["id"], sha)
        if saved:
            document = {**json.loads(saved["document"]), "label": doc["label"], "date": doc["date"],
                        "capture_id": saved["capture_id"], "saved_review": store.cataract_review_draft(saved)}
            return {"state": "saved", "category": saved["category"], "document": document}
        previous = store.cataract_pdf_previous(mrn, source, doc["id"], doc["_slot"])
        current = {item.get("digest") for item in catalog.values() if item["id"] == doc["id"]}
        return {"state": "changed" if sha and previous - current else "missing"}

    def _status(self, scope, job_id):
        with self.lock:
            job = self.latest.get(scope)
            if job_id and (job is None or job.id != job_id):
                raise ValueError("找不到此病人的擷取工作。")
            snapshot = job.snapshot() if job else {"state": "idle"}
        if result := snapshot.get("result"):
            _, mrn, source = scope
            store = self.analysis.store
            rows = []
            for row in result["documents"]:
                if row.get("capture_id"):
                    saved = store.cataract_pdf_review(mrn, source, row["id"], row["digest"], draft_only=True)
                    if saved and saved["capture_id"] == row["capture_id"]:
                        row = {**row, "saved_review": store.cataract_review_draft(saved)}
                rows.append(row)
            snapshot["result"] = {**result, "documents": rows}
        return snapshot

    def _start(self, scope, selection, *, category, view):
        from .jobs import BusyError

        if not selection:
            raise ValueError("此病人目前沒有可擷取的 PDF。")
        with self.lock:
            if self.closed:
                raise BusyError("工作區正在關閉。")
            existing = self.latest.get(scope)
            if existing and existing.state in ACTIVE:
                if (existing.category, existing.view, existing.documents) != (category, view, selection):
                    raise BusyError("此病人的同來源 PDF 正在擷取，請等候目前的工作完成。")
                return existing.snapshot()
            if len(self.pending) >= 8:
                raise BusyError("擷取佇列已滿，請稍後重試。")
            self.latest.pop(scope, None)
            if len(self.latest) >= 20:
                oldest = next(key for key, job in self.latest.items() if job.state not in ACTIVE)
                del self.latest[oldest]
            job = _Extraction(scope, selection, category, view)
            self.latest[scope] = job
            self.pending.append(job)
            if self.worker is None:
                self.worker = threading.Thread(target=self._work, name="cataract-biometry", daemon=True)
                self.worker.start()
            return job.snapshot()

    def _pdf(self, scope, doc, *, download):
        _, mrn, source = scope
        if source == "orders":
            sha = doc["digest"]
        else:
            asset = self.app.scans._asset(mrn, doc["id"])
            if not asset:
                if not download or not self.app.gateway.online or self.app.root.read_only:
                    raise ValueError("此掃描 PDF 尚未保存，請先取得附件。")
                asset = self.app.scans.download_verified(mrn, doc["_scan_ref"])["asset"]
            sha = asset["digest"]
            if doc.get("_expected_digest", sha) != sha:
                raise ValueError("PDF 內容已變更，請重新選擇頁碼。")
        path, mime = self.analysis.store.asset(sha)
        if mime != "application/pdf":
            raise ValueError("附件不是 PDF。")
        return path, sha

    def _run(self, job):
        _, _, source = job.scope
        rows = []
        parsed = {}
        failures = partial = 0
        with self.app.gateway.task_context(job.id, "cataract_biometry", job.cancel):
            for doc in job.documents:
                if job.cancel.is_set():
                    return
                row = {key: value for key, value in doc.items() if not key.startswith("_")}
                try:
                    path, row["digest"] = self._pdf(job.scope, doc, download=True)
                    if job.cancel.is_set():
                        return
                    key = row["digest"], tuple(doc["pages"] or ())
                    if key not in parsed:
                        parsed[key] = biometry.extract_for_review(path, doc["pages"], job.category)
                    data, review = parsed[key]
                    row["data"] = data
                    if review:
                        row["review"] = review
                    if data.get("metadata", {}).get("processing_status") not in {"failed", "unavailable", "not_run"}:
                        with self.app.lock:
                            if job.cancel.is_set():
                                return
                            row["saved_review"] = self.analysis.store.save_cataract_pdf_capture(
                                job.scope[1], source, doc["_slot"], job.id, job.category, row)
                            row["capture_id"] = job.id
                except Cancelled:
                    job.cancel.set()
                    return
                except biometry.BiometryError as exc:
                    row["error"] = str(exc)
                except ValueError as exc:
                    row["error"] = str(exc) if type(exc) is ValueError else "無法擷取此 PDF。"
                except Exception as exc:
                    row["error"] = safe_failure(exc)[0]
                processing = row.get("data", {}).get("metadata", {}).get("processing_status")
                failures += "error" in row or processing in {"failed", "unavailable", "not_run"}
                partial += processing == "partial"
                rows.append(row)
                with self.lock:
                    job.processed = len(rows)
        with self.lock:
            if not job.cancel.is_set():
                if failures == len(rows):
                    job.state = "failed"
                elif failures or partial:
                    job.state = "partial"
                else:
                    job.state = "completed"
                if failures or partial:
                    job.message = f"{failures + partial} 份 PDF 擷取未完成，請查看擷取結果與原因。"
                job.result = {"schema_version": 1, "source": source, "category": job.category, "documents": rows}

    def _work(self):
        while True:
            with self.lock:
                if not self.pending:
                    self.worker = None
                    return
                job = self.pending.popleft()
                job.state = "running"
            try:
                self._run(job)
            except Exception:
                with self.lock:
                    job.state = "failed"
                    job.message = "擷取工作發生錯誤，請重新執行。"
            with self.lock:
                if job.cancel.is_set():
                    job.state = "cancelled"

    def _invalidate(self, *, mrns=None, sources=None, cohort=None):
        for key, job in list(self.latest.items()):
            cohort_id, mrn, source = key
            if (mrns is None or mrn in mrns) and (sources is None or source in sources) and (
                    cohort is None or cohort_id == cohort):
                job.cancel.set()
                del self.latest[key]
        self.pending = deque(job for job in self.pending if not job.cancel.is_set())

    def invalidate(self, *, mrns=None, sources=None, cohort=None):
        with self.lock:
            self._invalidate(mrns=mrns, sources=sources, cohort=cohort)

    def close(self, *, wait=False):
        with self.lock:
            self.closed = True
            self._invalidate()
            worker = self.worker
        if wait and worker:
            # Let an active parse release its temporary PDF and page images.
            worker.join()
