"""Account-scoped, cache-first access to the complete PRQ scan index."""
from __future__ import annotations

from datetime import date

from vghks_sdk import AuthenticationError, VisitCase
from vghks_sdk.models import PdfAttachmentRef

from .analysis_fetch import clean, model
from .analysis_store import digest
from .connection_state import should_pause
from .scanner import safe_failure
from .storage import StorageError


def scan_id(ref):
    return digest([ref.mrn, ref.file_path])


class ScanArchive:
    def __init__(self, app):
        self.app = app
        self.store = app.analysis.store

    def _visits(self, mrn):
        review = getattr(self.app, "review", None)
        if review:
            saved = review.db.get("visits", mrn, required=False)
            if saved:
                return saved["cases"]
        saved = self.store.step(mrn, "visits")
        return saved["payload"] if saved else None

    def _eye_cases(self, mrn):
        cases = []
        for raw in self._visits(mrn) or []:
            case = model(VisitCase, raw)
            if case.patient_mrn != mrn:
                raise ValueError("掃描病歷的就診索引與病人不符。")
            if case.case_type == "O" and "眼科" in case.section_name:
                cases.append(case)
        return sorted({case.identity: case for case in cases}.values(),
                      key=lambda case: (case.visit_date.isoformat() if case.visit_date else "", case.identity), reverse=True)

    def case(self, mrn, reference):
        if not isinstance(reference, str) or len(reference) != 24:
            raise ValueError("掃描病歷就診代碼不正確。")
        from .review import case_key

        for raw in self._visits(mrn) or []:
            case = model(VisitCase, raw)
            if case.patient_mrn == mrn and case_key(case) == reference and case.case_type == "O":
                return case
        raise ValueError("就診不在已核對的病人索引。")

    @staticmethod
    def _refs(records, source_mrn):
        refs = []
        for raw in records:
            ref = model(PdfAttachmentRef, raw["pdf_ref"] if "pdf_ref" in raw else raw)
            if ref.mrn != source_mrn:
                raise ValueError("掃描 PDF 參照的病歷號不符。")
            refs.append(ref)
        return tuple(dict.fromkeys(refs))

    def _case_refs(self, mrn, case):
        key = "scans-case:" + digest(case.identity)
        step = self.store.step(mrn, key)
        if step:
            return self._refs(step["payload"], case.mrn)
        from .review import case_key

        saved = self.app.store.library.get_record(case_key(case))
        structure = saved.get("soap_structure") if saved and saved.get("mrn") == mrn else None
        if isinstance(structure, dict) and isinstance(structure.get("scanned_pdf_refs"), list):
            return self._refs(structure["scanned_pdf_refs"], case.mrn)
        return None

    def case_data(self, mrn, reference):
        case = self.case(mrn, reference)
        refs = self._case_refs(mrn, case)
        try:
            history, _ = self._history_refs(mrn)
        except (ValueError, TypeError, KeyError):
            history = None
        by_ref = {scan_id(ref): row for row, ref in history or []}
        return ({"case": {"date": case.visit_date.isoformat() if case.visit_date else "",
                          "section": case.section_name, "case_no": case.case_no},
                 "scans": [self._public(mrn, ref, by_ref.get(scan_id(ref)), case) for ref in refs]}
                if refs is not None else None)

    def _history_refs(self, mrn):
        step = self.store.step(mrn, "scans-history")
        if not step:
            return None, False
        payload = step["payload"]
        upgraded = isinstance(payload, dict) and payload.get("schema") == 2
        records = payload.get("records") if upgraded else payload
        if not isinstance(records, list):
            raise ValueError("歷年掃描索引格式不正確。")
        rows = []
        for raw in records:
            rows.append((raw, self._validate_history_record(raw, mrn)))
        return rows, upgraded

    @staticmethod
    def _validate_history_record(raw, mrn):
        if not isinstance(raw, dict) or not isinstance(raw.get("pdf_ref"), dict):
            raise ValueError("歷年掃描索引項目不正確。")
        ref = model(PdfAttachmentRef, raw["pdf_ref"])
        if ref.mrn != mrn:
            raise ValueError("歷年掃描索引的病歷號不符。")
        for key in ("record_type", "category_label", "section_label", "record_date"):
            if raw.get(key) is not None and not isinstance(raw[key], str):
                raise ValueError("歷年掃描類別或日期格式不正確。")
        if raw.get("record_date"):
            date.fromisoformat(raw["record_date"])
        return ref

    def _asset(self, mrn, identifier):
        step = self.store.step(mrn, "scan-asset:" + identifier)
        if step:
            try:
                self.store.asset(step["payload"]["digest"])
                return step["payload"]
            except (KeyError, ValueError):
                pass
        return None

    def _public(self, mrn, ref, record=None, case=None):
        record = record or {}
        return {"id": scan_id(ref), "record_type": record.get("record_type") or "",
                "category_label": record.get("category_label") or "",
                "section_label": record.get("section_label") or "",
                "date": record.get("record_date") or (case.visit_date.isoformat() if case and case.visit_date else ""),
                "section": case.section_name if case else "", "case_no": case.case_no if case else "",
                "asset": self._asset(mrn, scan_id(ref))}

    @staticmethod
    def _is_eye(row):
        if row["section_label"] not in {"", "病歷類別"} or not row["category_label"]:
            return False
        return tuple(part.strip() for part in row["category_label"].split("-"))[:3] == (
            "門診", "記錄", "眼科紀錄")

    def history_data(self, mrn):
        cache_error = False
        try:
            history, upgraded = self._history_refs(mrn)
        except (ValueError, TypeError, KeyError):
            history, upgraded, cache_error = None, False, True
        link_error = False
        try:
            cases = self._eye_cases(mrn)
        except (ValueError, TypeError, KeyError):
            cases, link_error = [], True
        links, checked = {}, 0
        for case in cases:
            try:
                refs = self._case_refs(mrn, case)
            except (ValueError, TypeError, KeyError):
                link_error = True
                continue
            if refs is None:
                continue
            checked += 1
            for ref in refs:
                links.setdefault(scan_id(ref), (ref, case))
        eye, pending, other, records, seen = [], [], [], [], set()
        indexed_ids = set()
        for raw, ref in history or []:
            identifier = scan_id(ref)
            linked = links.get(identifier)
            row = self._public(mrn, ref, raw, linked[1] if linked else None)
            key = (identifier, row["section_label"], row["category_label"], row["date"], row["record_type"])
            if key in seen:
                continue
            seen.add(key)
            indexed_ids.add(identifier)
            row["eye"] = self._is_eye(row) or (not row["category_label"] and bool(linked))
            records.append(row)
            (eye if row["eye"]
             else other if row["category_label"] else pending).append(row)
        for identifier, (ref, case) in links.items():
            if identifier not in indexed_ids:
                row = self._public(mrn, ref, case=case)
                row["eye"] = True
                records.append(row)
                eye.append(row)
        for group in (records, eye, pending, other):
            group.sort(key=lambda row: (row["date"], row["id"]), reverse=True)
        categories = {}
        for row in records:
            if not row["category_label"]:
                continue
            key = (row["section_label"], row["category_label"])
            categories[key] = categories.get(key, 0) + 1
        return {"records": records, "scans": eye, "other_history": other,
                "unclassified": pending,
                "categories": [{"section_label": section, "category_label": label, "count": count}
                               for (section, label), count in categories.items()],
                "eye_case_count": len(cases), "checked_case_count": checked,
                "index_loaded": history is not None, "history_loaded": upgraded,
                "cache_error": cache_error, "link_error": link_error,
                "visits_loaded": not link_error and self._visits(mrn) is not None,
                "complete": upgraded,
                "links_complete": not link_error and self._visits(mrn) is not None and checked == len(cases)}

    def index(self, mrn, *, force=False):
        issues = []
        try:
            _, upgraded = self._history_refs(mrn)
        except (ValueError, TypeError, KeyError):
            upgraded = False
        if force or not upgraded:
            try:
                with self.app.sdk_factory(self.app.settings) as sdk:
                    history = sdk.records.get_upload_history(mrn)
                if history.mrn != mrn:
                    raise ValueError("歷年掃描索引病人不符。")
                rows = clean(history.scanned_records)
                for row in rows:
                    self._validate_history_record(row, mrn)
                self.store.save_step(mrn, "scans-history", "scan_index",
                                     {"schema": 2, "records": rows}, self.app.username)
            except AuthenticationError:
                raise
            except StorageError:
                raise
            except Exception as exc:
                if should_pause(exc):
                    raise
                message, code = safe_failure(exc)
                issues.append({"part": "歷年掃描索引", "code": code, "message": message})
        return {"status": "partial" if issues else "ready", "issues": issues}

    def _fetch_visits(self, mrn):
        with self.app.sdk_factory(self.app.settings) as sdk:
            cases = sdk.records.get_visit_cases(mrn)
        if any(not isinstance(case, VisitCase) or case.patient_mrn != mrn for case in cases):
            raise ValueError("眼科就診索引病人不符。")
        payload = clean(cases)
        self.store.save_step(mrn, "visits", "visits", payload, self.app.username)
        review = getattr(self.app, "review", None)
        if review:
            review.db.save("visits", {"id": mrn, "cases": payload})

    def fetch_case(self, mrn, reference):
        case = self.case(mrn, reference)
        with self.app.sdk_factory(self.app.settings) as sdk:
            records = sdk.records.get_case_scanned_records(case)
        rows = clean(records)
        self._refs(rows, case.mrn)
        self.store.save_step(mrn, "scans-case:" + digest(case.identity), "scan_index", rows, self.app.username)
        return {"status": "ready", "count": len(rows)}

    def backfill(self, mrn, *, check_cancel=lambda: None, progress=lambda *_: None):
        initial = self.index(mrn)
        issues = list(initial["issues"])
        needs_visits = self._visits(mrn) is None
        cases = []
        if not needs_visits:
            try:
                cases = self._eye_cases(mrn)
            except (ValueError, TypeError, KeyError):
                needs_visits = True
        if needs_visits:
            try:
                self._fetch_visits(mrn)
                cases = self._eye_cases(mrn)
            except (AuthenticationError, StorageError):
                raise
            except Exception as exc:
                if should_pause(exc):
                    raise
                message, code = safe_failure(exc)
                issues.append({"part": "就診索引", "code": code, "message": message})
        for case in cases:
            check_cancel()
            try:
                saved_refs = self._case_refs(mrn, case)
            except (ValueError, TypeError, KeyError):
                saved_refs = None
            if saved_refs is not None:
                continue
            from .review import case_key

            reference = case_key(case)
            try:
                self.fetch_case(mrn, reference)
                progress(reference, "ready")
            except AuthenticationError:
                raise
            except StorageError:
                raise
            except Exception as exc:
                if should_pause(exc):
                    raise
                message, code = safe_failure(exc)
                issues.append({"part": reference, "code": code, "message": message})
                progress(reference, "error")
        return {"status": "partial" if issues else "ready", "issues": issues}

    def _verified_ref(self, mrn, identifier):
        if not isinstance(identifier, str) or len(identifier) != 64:
            raise ValueError("掃描檔案代碼不正確。")
        try:
            history, _ = self._history_refs(mrn)
        except (ValueError, TypeError, KeyError):
            history = None
        for _, ref in history or []:
            if scan_id(ref) == identifier:
                return ref
        for raw in self._visits(mrn) or []:
            case = model(VisitCase, raw)
            if case.patient_mrn != mrn or case.case_type != "O":
                continue
            for ref in self._case_refs(mrn, case) or ():
                if scan_id(ref) == identifier:
                    return ref
        raise ValueError("掃描檔案不在已核對的病人索引。")

    def asset_data(self, mrn, identifier):
        self._verified_ref(mrn, identifier)
        return self._asset(mrn, identifier)

    def download(self, mrn, identifier):
        ref = self._verified_ref(mrn, identifier)
        cached = self._asset(mrn, identifier)
        if cached:
            return {"status": "ready", "asset": cached, "cached": True}
        with self.app.sdk_factory(self.app.settings) as sdk:
            asset = sdk.orders.download_pdf(ref)
        return {"status": "ready", "asset": self.store.save_asset(
            mrn, "scan-asset:" + identifier, asset, self.app.username), "cached": False}
