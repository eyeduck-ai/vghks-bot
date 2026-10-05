"""Cached, account-scoped history browsing for a SOAP review patient."""
from __future__ import annotations

from datetime import timedelta

from vghks_sdk import AuthenticationError, SoapRecord, VisitCase
from vghks_sdk.models import (
    NumericHistoryFilter,
    OrderDetailRef,
    OrderHistoryFilter,
    OrderReportRef,
    PacsImageRef,
    PacsStudyRef,
    PdfAttachmentRef,
)

from .analysis_fetch import clean, model, order_identity, unique_refs, validate_patient
from .analysis_store import digest
from .connection_state import should_pause
from .scanner import safe_failure
from .settings import today
from .soap_data import snapshot as soap_snapshot
from .storage import StorageError

RESOURCES = {"numeric", "orders", "visits", "case_numeric", "case_orders", "order_report", "visit_soap",
             "scans", "case_scans", "scan_asset"}
CASE_RESOURCES = {"case_numeric", "case_orders", "visit_soap", "case_scans"}
NAMES = {"numeric": "歷年數值類報告", "orders": "歷年醫囑清單", "visits": "歷次就診紀錄",
         "case_numeric": "該次數值類報告", "case_orders": "該次醫囑清單",
         "order_report": "醫囑報告內容", "visit_soap": "歷史 SOAP",
         "scans": "歷年掃描病歷", "case_scans": "該次掃描病歷", "scan_asset": "掃描病歷 PDF"}


class ReviewHistory:
    def __init__(self, review):
        self.review = review
        self.app = review.app
        self.store = self.app.analysis.store

    def scope(self, values):
        mrn = values.get("mrn")
        if not isinstance(mrn, str):
            raise ValueError("病歷號不正確。")
        resource = values.get("resource")
        if resource not in RESOURCES:
            raise ValueError("歷史資料類型不正確。")
        if bool(values.get("review_task_id")) == bool(values.get("cohort_id")):
            raise ValueError("請指定一個病歷檢閱或分析清單。")
        if values.get("review_task_id"):
            task = self.review.db.get("task", values["review_task_id"])
            if task.get("kind") != "review" or mrn not in {member["mrn"] for member in task["members"]}:
                raise ValueError("病人不在本次檢閱清單。")
        else:
            if resource not in {"scans", "case_scans", "scan_asset", "order_report"}:
                raise ValueError("分析清單僅能開啟掃描病歷或醫囑報告。")
            member = self.app.analysis.member(values)
            if member.get("account_id") != self.app.account_id:
                raise ValueError("此病人未指定給目前抓取帳號。")
            task = None
        reference = values.get("reference", "")
        if not isinstance(reference, str) or len(reference) > 128:
            raise ValueError("歷史資料明細代碼不正確。")
        if resource == "case_scans":
            self.app.scans.case(mrn, reference)
        elif resource in CASE_RESOURCES:
            self.case(mrn, reference)
        elif resource == "order_report":
            self.order(mrn, reference, cataract=bool(values.get("cohort_id")))
        elif resource == "scan_asset":
            self.app.scans._verified_ref(mrn, reference)
        elif reference:
            raise ValueError("此歷史查詢不需要明細代碼。")
        return task, mrn, resource, reference

    def prepare(self, values):
        _, mrn, resource, reference = self.scope(values)
        result = {"mrn": mrn, "resource": resource, "reference": reference, "name": NAMES[resource]}
        result["review_task_id" if values.get("review_task_id") else "cohort_id"] = (
            values.get("review_task_id") or values.get("cohort_id"))
        if resource == "scans":
            result["backfill"] = values.get("backfill") is True
        return result

    @staticmethod
    def _case_key(case):
        from .review import case_key
        return case_key(case)

    @staticmethod
    def _order_key(order):
        return order_identity(order)

    def _visits(self, mrn):
        saved = self.review.db.get("visits", mrn, required=False)
        if saved:
            return saved["cases"], saved["updated_at"]
        step = self.store.step(mrn, "visits")
        return (step["payload"], step["saved_at"]) if step else (None, "")

    def case(self, mrn, reference):
        cases, _ = self._visits(mrn)
        for raw in cases or []:
            case = model(VisitCase, raw)
            if case.patient_mrn == mrn and self._case_key(case) == reference:
                if case.case_type != "O" or not case.visit_date or case.visit_date > today():
                    raise ValueError("此就診目前無法查看門診 SOAP 或補查報告。")
                return case
        raise ValueError("就診不在已核對的病人索引。")

    def _order_sources(self, mrn, *, visits=None, categories=("*", "OR"), case_keys=None):
        sources = []
        for category in categories:
            step = self.store.step(mrn, "orders-history:" + category)
            if step:
                sources.extend(step["payload"])
        if visits is None:
            visits, _ = self._visits(mrn)
        for raw in visits or []:
            case = model(VisitCase, raw)
            if case.patient_mrn != mrn or case.case_type != "O":
                continue
            key = digest(case.identity)
            if case_keys is not None and key not in case_keys:
                continue
            step = self.store.step(mrn, "orders-case:" + key)
            if step:
                sources.extend(step["payload"])
        return sources

    def _allowed_mrns(self, mrn, *, visits=None):
        if visits is None:
            visits, _ = self._visits(mrn)
        allowed = {mrn}
        for raw in visits or []:
            case = model(VisitCase, raw)
            if case.patient_mrn == mrn:
                allowed.add(case.mrn)
        return allowed

    def _groups(self, mrn, orders, *, visits=None):
        allowed = self._allowed_mrns(mrn, visits=visits)
        groups = {}
        for order in orders:
            if not isinstance(order, dict) or order.get("mrn") not in allowed:
                raise ValueError("醫囑索引的病歷號不在已核對的病人範圍。")
            validate_patient(order, order["mrn"])
            groups.setdefault(self._order_key(order), []).append(order)
        return groups

    @staticmethod
    def _public_orders(groups):
        rows = []
        for key, variants in groups.items():
            order = max(variants, key=lambda item: sum(bool(item.get(name)) for name in
                         ("detail_ref", "report_ref", "pacs_ref", "pdf_refs")))
            rows.append({"id": key, "name": order.get("name", ""), "date": order.get("execution_date") or order.get("order_date") or "",
                         "execution_date": order.get("execution_date", ""), "order_date": order.get("order_date", ""),
                         "case_no": order.get("case_no", ""), "case_type": order.get("case_type", ""),
                         "status": order.get("status", ""), "requester": order.get("requester", "")})
        return sorted(rows, key=lambda row: (row["date"], row["name"], row["id"]), reverse=True)

    def order(self, mrn, reference, *, cataract=False):
        if cataract:
            sources, visits = self.app.analysis.cataract_sources(mrn)
            groups = self._groups(mrn, sources, visits=visits)
        else:
            groups = self._groups(mrn, self._order_sources(mrn))
        if reference not in groups:
            raise ValueError("醫囑不在已核對的病人索引。")
        return groups[reference]

    def _stored(self, mrn, resource, reference, *, cataract=False):
        if resource == "scans":
            step = self.store.step(mrn, "scans-history")
            return self.app.scans.history_data(mrn), step["saved_at"] if step else ""
        if resource == "case_scans":
            value = self.app.scans.case_data(mrn, reference)
            return value, ""
        if resource == "scan_asset":
            return self.app.scans.asset_data(mrn, reference), ""
        if resource == "numeric":
            step = self.store.step(mrn, "numeric-history")
            return (step["payload"], step["saved_at"]) if step else (None, "")
        if resource == "orders":
            steps = [self.store.step(mrn, "orders-history:" + category) for category in ("*", "OR")]
            if not any(steps):
                return None, ""
            orders = [order for step in steps if step for order in step["payload"]]
            return self._public_orders(self._groups(mrn, orders)), max(step["saved_at"] for step in steps if step)
        if resource == "visits":
            cases, saved = self._visits(mrn)
            if cases is None:
                return None, ""
            rows = []
            for raw in cases:
                case = model(VisitCase, raw)
                if case.patient_mrn != mrn:
                    continue
                rows.append({"id": self._case_key(case), "date": case.visit_date.isoformat() if case.visit_date else "",
                             "section": case.section_name or case.section_code, "case_type": case.case_type,
                             "case_no": case.case_no, "doctor": case.doctor_name,
                             "soap_available": case.case_type == "O" and bool(case.visit_date and case.visit_date <= today())})
            return sorted(rows, key=lambda row: (row["date"], row["id"]), reverse=True), saved
        if resource in {"case_numeric", "case_orders"}:
            case = self.case(mrn, reference)
            key = ("numeric-case:" if resource == "case_numeric" else "orders-case:") + digest(case.identity)
            step = self.store.step(mrn, key)
            if not step:
                return None, ""
            if resource == "case_orders":
                return self._public_orders(self._groups(mrn, step["payload"])), step["saved_at"]
            return step["payload"], step["saved_at"]
        if resource == "order_report":
            step = self.store.step(mrn, "history-order:" + reference)
            old = self.store.step(mrn, "collected-order:" + reference)
            def complete(saved):
                if not saved or saved["payload"].get("status") not in {
                    "ready", "complete", "missing", "not_executed", "no_data", "no_links"
                }:
                    return False
                try:
                    for asset in saved["payload"].get("assets") or []:
                        self.store.asset(asset["digest"])
                except (KeyError, ValueError):
                    return False
                return True

            if step and (complete(step) or not complete(old)):
                return step["payload"], step["saved_at"]
            if old:
                value = old["payload"]
                return {"id": reference, "order": self._public_orders({reference: self.order(mrn, reference, cataract=cataract)})[0],
                        "details": [], "texts": [{"text": row.get("text", ""), "fields": row.get("fields", {})}
                                                 for row in value.get("texts", [])],
                        "assets": [{key: asset[key] for key in ("digest", "mime", "size") if key in asset}
                                   for asset in value.get("assets", [])],
                        "issues": [], "status": value.get("status", "ready")}, old["saved_at"]
            return None, ""
        record = self.app.store.library.get_record(reference)
        return (record, record["updated_at"]) if record and record["mrn"] == mrn else (None, "")

    def read(self, values):
        _, mrn, resource, reference = self.scope(values)
        data, updated = self._stored(mrn, resource, reference, cataract=bool(values.get("cohort_id")))
        complete = (all(self.store.step(mrn, "orders-history:" + category) is not None
                        for category in ("*", "OR")) if resource == "orders" else
                    data["history_loaded"] if resource == "scans" else data is not None)
        return {"data": data, "updated_at": updated, "resource": resource,
                "complete": complete,
                "coverage": {"numeric_start": (today() - timedelta(days=3650)).isoformat(),
                             "orders_start": (today() - timedelta(days=4000)).isoformat()}}

    def execute(self, task, state=None):
        mrn, resource, reference = task["mrn"], task["resource"], task.get("reference", "")
        if resource == "scans":
            if task.get("backfill"):
                from .review import case_key
                def progress(reference, status):
                    self.review.db.item(task["id"], reference, {"status": status, "reference": reference})
                    if state:
                        self.review.report(state, task, stage="核對眼科就診掃描病歷")
                result = self.app.scans.backfill(mrn, check_cancel=state.check_cancel if state else lambda: None,
                                                  progress=progress)
                try:
                    checked = [case_key(c) for c in self.app.scans._eye_cases(mrn)]
                except (ValueError, TypeError, KeyError):
                    checked = []
                return {**result, "resource": resource, "reference": reference,
                        "checked": checked}
            result = self.app.scans.index(mrn, force=task.get("force", False))
            return {**result, "resource": resource, "reference": reference}
        if resource == "case_scans":
            return {**self.app.scans.fetch_case(mrn, reference), "resource": resource, "reference": reference}
        if resource == "scan_asset":
            return {**self.app.scans.download(mrn, reference), "resource": resource, "reference": reference}
        if not task.get("force"):
            data, _ = self._stored(mrn, resource, reference, cataract=bool(task.get("cohort_id")))
            complete = resource != "orders" or all(
                self.store.step(mrn, "orders-history:" + category) is not None for category in ("*", "OR"))
            if data is not None and complete:
                return {"status": "ready", "cached": True, "resource": resource, "reference": reference}
        if resource == "visits":
            cases, _ = self.review.cases(mrn, force=True)
            self.store.save_step(mrn, "visits", "visits", clean(cases), self.app.username)
        elif resource == "numeric":
            with self.app.sdk_factory(self.app.settings) as sdk:
                report = sdk.records.get_numeric_history(mrn, NumericHistoryFilter())
            if report.mrn != mrn:
                raise ValueError("歷年數值報告病人不符。")
            self.store.save_step(mrn, "numeric-history", "numeric", clean(report), self.app.username)
        elif resource == "orders":
            if not self._visits(mrn)[0]:
                self.review.cases(mrn)
            with self.app.sdk_factory(self.app.settings) as sdk:
                for category in ("*", "OR"):
                    key = "orders-history:" + category
                    if self.store.step(mrn, key) and not task.get("force"):
                        continue
                    orders = sdk.orders.get_order_history(mrn, OrderHistoryFilter(category=category))
                    payload = clean(orders)
                    self._groups(mrn, payload)
                    self.store.save_step(mrn, key, "order_index", payload, self.app.username)
        elif resource in CASE_RESOURCES:
            case = self.case(mrn, reference)
            with self.app.sdk_factory(self.app.settings) as sdk:
                if resource == "case_numeric":
                    report = sdk.records.get_numeric_report(case)
                    if report.case.identity != case.identity or report.case.patient_mrn != mrn:
                        raise ValueError("單次數值報告就診不符。")
                    self.store.save_step(mrn, "numeric-case:" + digest(case.identity), "numeric", clean(report), self.app.username)
                elif resource == "case_orders":
                    orders = sdk.orders.get_case_orders(case)
                    payload = clean(orders)
                    self._groups(mrn, payload)
                    self.store.save_step(mrn, "orders-case:" + digest(case.identity), "order_index", payload, self.app.username)
                else:
                    if self.review.db.deleted_since(reference, "") and not task.get("force"):
                        raise ValueError("這份病歷已刪除；如需重新取得，請明確按更新資料。")
                    soap = sdk.records.get_soap(case)
                    if not isinstance(soap, SoapRecord) or soap.case.identity != case.identity or soap.case.patient_mrn != mrn:
                        raise ValueError("歷史 SOAP 就診不符。")
                    if not soap.full_text.strip():
                        return {"status": "missing", "resource": resource, "reference": reference}
                    member = next(m for m in self.review.db.get("task", task["review_task_id"])["members"] if m["mrn"] == mrn)
                    record = {"id": reference, "mrn": mrn, "name": member.get("name", ""),
                              "sex": member.get("sex", ""), "age": member.get("age", ""),
                              "date": case.visit_date.isoformat(), "section": case.section_name,
                              "section_code": case.section_code, "case_no": case.case_no,
                              "source_mrn": case.mrn, "case_index": case.index,
                              "doctor": case.doctor_name, "doctor_card": case.doctor_card, **soap_snapshot(soap)}
                    self.app.store.library.save_record(record, self.app.username, task["id"])
        else:
            result = self._collect_order_report(task)
            return {"status": result["status"], "cached": False, "resource": resource, "reference": reference}
        return {"status": "ready", "cached": False, "resource": resource, "reference": reference}

    def _collect_order_report(self, task):
        mrn, reference = task["mrn"], task["reference"]
        if task.get("cohort_id"):
            sources, visits = self.app.analysis.cataract_sources(mrn)
            variants = self._groups(mrn, sources, visits=visits).get(reference)
            if not variants:
                raise ValueError("醫囑不在已核對的白內障檢查索引。")
        else:
            visits = None
            variants = self.order(mrn, reference)
        allowed = self._allowed_mrns(mrn, visits=visits)
        result = {"id": reference, "order": self._public_orders({reference: variants})[0],
                  "details": [], "texts": [], "assets": [], "issues": [], "status": "ready"}
        details = [order.get("detail_ref") for order in variants]
        reports = [order.get("report_ref") for order in variants]
        pdfs = [ref for order in variants for ref in order.get("pdf_refs") or []]
        studies = [order.get("pacs_ref") for order in variants]

        def fetch(cls, method, ref, *, binary=False):
            source = ref.get("mrn", "")
            if source not in allowed:
                raise ValueError("醫囑參照病歷號未經就診清單核對。")
            validate_patient(ref, source)
            key = "history-asset:" + method + ":" + digest(ref)
            saved = self.store.step(mrn, key)
            if saved and not task.get("force"):
                if not binary:
                    return saved["payload"]
                try:
                    self.store.asset(saved["payload"]["digest"])
                    return saved["payload"]
                except ValueError:
                    pass
            with self.app.sdk_factory(self.app.settings) as sdk:
                value = getattr(sdk.orders, method)(model(cls, ref))
            if binary:
                return self.store.save_asset(mrn, key, value, self.app.username)
            payload = clean(value)
            validate_patient(payload, source)
            self.store.save_step(mrn, key, "report", payload, self.app.username)
            return payload

        def collect(cls, method, refs, target, *, binary=False):
            for ref in unique_refs(refs):
                try:
                    value = fetch(cls, method, ref, binary=binary)
                    if value:
                        target.append(value)
                except (AuthenticationError, StorageError):
                    raise
                except Exception as exc:
                    if should_pause(exc):
                        raise
                    message, code = safe_failure(exc)
                    result["issues"].append({"branch": method, "code": code, "message": message})

        collected_details = []
        collect(OrderDetailRef, "get_order_detail", details, collected_details)
        for detail in collected_details:
            reports.extend(detail.get("report_refs") or [])
            pdfs.extend(detail.get("pdf_refs") or [])
            studies.extend(detail.get("pacs_refs") or [])
            result["details"].append({"fields": detail.get("fields", {})})
        collected_reports = []
        collect(OrderReportRef, "get_order_report", reports, collected_reports)
        for report in collected_reports:
            if report.get("report_text") or report.get("fields"):
                result["texts"].append({"text": report.get("report_text", ""), "fields": report.get("fields", {}),
                                        "status": report.get("report_data_status", "")})
            pdfs.extend(report.get("pdf_refs") or [])
            studies.extend(report.get("pacs_refs") or [])
        collected_studies = []
        collect(PacsStudyRef, "get_pacs_study", studies, collected_studies)
        images = [image for study in collected_studies for image in study.get("images") or []]
        collect(PdfAttachmentRef, "download_pdf", pdfs, result["assets"], binary=True)
        collect(PacsImageRef, "download_pacs_image", images, result["assets"], binary=True)
        if result["issues"]:
            result["status"] = "partial"
        elif not (result["details"] or result["texts"] or result["assets"]):
            result["status"] = "missing"
        self.store.save_step(mrn, "history-order:" + reference, "order_report", result, self.app.username)
        return result
