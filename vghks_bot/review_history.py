"""Cached, account-scoped history browsing for a SOAP review patient."""
from __future__ import annotations

import copy
import uuid
from datetime import timedelta

from vghks_sdk import AuthenticationError, SoapRecord, VisitCase
from vghks_sdk.core.errors import error_info
from vghks_sdk.models import (
    NumericHistoryFilter,
    OrderDetailRef,
    OrderHistoryFilter,
    OrderReportRef,
    PacsImageRef,
    PacsStudyRef,
    PdfAttachmentRef,
    to_jsonable,
)

from .analysis_fetch import clean, model, order_identity, unique_refs, validate_patient
from .analysis_store import digest
from .connection_state import should_pause
from .encoding import report_attachment_downloads
from .order_display import display_group
from .progress import progress
from .report_cache import branch_complete, result_complete
from .scanner import Cancelled, safe_failure
from .sdk_orders import assess_order_index, order_index_complete
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

    def _save_order_index(self, mrn, key, orders, *, issues=()):
        assessment = to_jsonable(assess_order_index(orders))
        assessment["issues"].extend(issues)
        allowed = self._allowed_mrns(mrn)
        payload = []
        for order in clean(orders):
            try:
                if not isinstance(order, dict) or order.get("mrn") not in allowed:
                    raise ValueError("unverified patient")
                validate_patient(order, order["mrn"])
            except (TypeError, ValueError):
                assessment["issues"].append({"code": "ORDER_REF_MRN_MISMATCH", "category": "PARSE"})
            else:
                payload.append(order)
        if assessment["issues"]:
            assessment["complete"] = False
            previous = self.store.step(mrn, key)
            # An incomplete refresh must not erase previously confirmed rows.
            retained = {}
            for order in previous["payload"] if previous else []:
                try:
                    if not isinstance(order, dict) or order.get("mrn") not in allowed:
                        raise ValueError("unverified patient")
                    validate_patient(order, order["mrn"])
                except (TypeError, ValueError):
                    continue
                retained[self._order_key(order)] = order
            retained.update({self._order_key(order): order for order in payload})
            payload = list(retained.values())
        assessment["item_count"] = len(payload)
        assessment["availability"] = "AVAILABLE" if payload else "EMPTY"
        self.store.save_step(mrn, key, "order_index", payload, self.app.username, assessment=assessment)
        return assessment["issues"]

    def _index_steps(self, mrn, resource, reference=""):
        if resource == "visits":
            values = [self.store.step(mrn, "visits"), self.review.db.get("visits", mrn, required=False)]
            return [value for value in values if value] or [None]
        if resource == "numeric":
            return [self.store.step(mrn, "numeric-history")]
        if resource == "case_numeric":
            from .review import case_key

            case = self.case(mrn, reference)
            values = [self.store.step(mrn, "numeric-case:" + digest(case.identity)),
                      self.review.db.get("numeric", case_key(case), required=False)]
            return [value for value in values if value] or [None]
        if resource == "orders":
            return [self.store.step(mrn, "orders-history:" + category) for category in ("*", "OR")]
        if resource == "case_orders":
            return [self.store.step(mrn, "orders-case:" + digest(self.case(mrn, reference).identity))]
        return []

    @staticmethod
    def _public_orders(groups):
        rows = []
        for key, variants in groups.items():
            order = max(variants, key=lambda item: sum(bool(item.get(name)) for name in
                         ("detail_ref", "report_ref", "pacs_ref", "pdf_refs")))
            rows.append({"id": key, "name": order.get("name", ""), "date": order.get("execution_date") or order.get("order_date") or "",
                         "execution_date": order.get("execution_date", ""), "order_date": order.get("order_date", ""),
                         "case_no": order.get("case_no", ""), "case_type": order.get("case_type", ""),
                         "status": order.get("status", ""), "requester": order.get("requester", ""),
                         **display_group(order.get("name", ""))})
        rows.sort(key=lambda row: (row["date"], row["name"], row["id"]), reverse=True)
        return sorted(rows, key=lambda row: not row["ophthalmic"])

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
            if resource == "case_numeric" and getattr(self.app, "resources", None):
                from .clinical_resources import ClinicalResources

                value = ClinicalResources.cached_numeric(self.app, mrn, case, allow_stale=True, allow_partial=True)
                return (value.value, value.saved_at) if value else (None, "")
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
                return bool(saved and result_complete(saved["payload"], self.store))

            if step and (step["payload"].get("data_revision") or complete(step) or not complete(old)):
                return step["payload"], step["saved_at"]
            if old:
                value = old["payload"]
                return {"id": reference, "order": self._public_orders({reference: self.order(mrn, reference, cataract=cataract)})[0],
                        "details": [], "texts": [{"text": row.get("text", ""), "fields": row.get("fields", {}),
                                                  "status": row.get("status", ""), "text_extraction_notes": row.get("text_extraction_notes", [])}
                                                 for row in value.get("texts", [])],
                        "assets": [{key: asset[key] for key in ("digest", "mime", "size") if key in asset}
                                   for asset in value.get("assets", [])],
                        "issues": ([] if complete(old) else [{"branch": "report", "code": "BOT_REPORT_INCOMPLETE", "message": "已存報告未完整，仍需補查。"}]),
                        "status": value.get("status", "ready"),
                        "report_cache_revision": value.get("report_cache_revision", 0)}, old["saved_at"]
            return None, ""
        record = self.app.store.library.get_record(reference)
        return (record, record["updated_at"]) if record and record["mrn"] == mrn else (None, "")

    def read(self, values):
        with self.app.store.library.read_snapshot():
            return self._read(values)

    def _read(self, values):
        _, mrn, resource, reference = self.scope(values)
        data, updated = self._stored(mrn, resource, reference, cataract=bool(values.get("cohort_id")))
        index_steps = self._index_steps(mrn, resource, reference)
        issues = [issue for step in index_steps if step for issue in step.get("assessment", {}).get("issues", [])]
        complete = (all(order_index_complete(step) for step in index_steps) if index_steps else
                    data["history_loaded"] if resource == "scans" else
                    bool(data is not None and result_complete(data, self.store)) if resource == "order_report" else
                    data is not None)
        return {"data": data, "updated_at": updated, "resource": resource,
                "complete": complete, "issues": issues,
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
            steps = self._index_steps(mrn, resource, reference)
            complete = not steps or all(order_index_complete(step) for step in steps)
            if resource == "order_report":
                complete = data is not None and result_complete(data, self.store)
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
            visits, _ = self._visits(mrn)
            verified_cases = [model(VisitCase, raw) for raw in visits or []]
            failures = []
            with self.app.sdk_factory(self.app.settings) as sdk:
                for category in ("*", "OR"):
                    if state:
                        state.check_cancel()
                    key = "orders-history:" + category
                    if order_index_complete(self.store.step(mrn, key)) and not task.get("force"):
                        continue
                    try:
                        orders = sdk.orders.get_order_history(mrn, OrderHistoryFilter(category=category),
                                                             verified_cases=verified_cases)
                    except (Cancelled, StorageError):
                        raise
                    except Exception as exc:
                        if should_pause(exc):
                            raise
                        issues = self._save_order_index(mrn, key, [], issues=[to_jsonable(error_info(exc))])
                    else:
                        issues = self._save_order_index(mrn, key, orders)
                    failures.extend(issues)
                    # Publish each saved category, so the browser can display it
                    # while the remaining category is still being fetched.
                    if state:
                        self.review.report(state, task, stage="已保存歷年醫囑 · " + ("全部類別" if category == "*" else "OR"))
            return {"status": "partial" if failures else "ready", "cached": False,
                    "resource": resource, "reference": reference, "issues": failures}
        elif resource in CASE_RESOURCES:
            case = self.case(mrn, reference)
            resources = getattr(self.app, "resources", None)
            if resources and resource == "case_numeric":
                resources.case_numeric(mrn, case, force=task.get("force", False))
                return {"status": "ready", "cached": False, "resource": resource, "reference": reference}
            if resources and resource == "visit_soap":
                if self.review.db.deleted_since(reference, "") and not task.get("force"):
                    raise ValueError("這份病歷已刪除；如需重新取得，請明確按更新資料。")
                member = next(m for m in self.review.db.get("task", task["review_task_id"])["members"] if m["mrn"] == mrn)
                acquired = resources.soap(mrn, case, member, force=task.get("force", False))
                return {"status": "missing" if acquired.value["empty"] else "ready", "cached": acquired.source != "network",
                        "resource": resource, "reference": reference}
            with self.app.sdk_factory(self.app.settings) as sdk:
                if resource == "case_numeric":
                    report = sdk.records.get_numeric_report(case)
                    if report.case.identity != case.identity or report.case.patient_mrn != mrn:
                        raise ValueError("單次數值報告就診不符。")
                    self.store.save_step(mrn, "numeric-case:" + digest(case.identity), "numeric", clean(report), self.app.username)
                elif resource == "case_orders":
                    orders = sdk.orders.get_case_orders(case)
                    issues = self._save_order_index(mrn, "orders-case:" + digest(case.identity), orders)
                    return {"status": "partial" if issues else "ready", "cached": False,
                            "resource": resource, "reference": reference, "issues": issues}
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
            result = self._collect_order_report(task, state)
            return {"status": result["status"], "cached": False, "resource": resource, "reference": reference}
        return {"status": "ready", "cached": False, "resource": resource, "reference": reference}

    def _publish_order_report(self, task, value, state=None, *, start=False):
        """Publish only durable files, merging late checkpoints under one transaction."""
        mrn, key = task["mrn"], "history-order:" + task["reference"]
        with self.app.store.library.batch():
            saved = self.store.step(mrn, key)
            previous = saved["payload"] if saved else {}
            owns = start or previous.get("processing_run") in {None, value["processing_run"]}
            snapshot = copy.deepcopy(value if owns else previous)
            retain = not owns or snapshot.get("processing") or snapshot.get("status") == "partial"
            incoming = {item["ref_key"]: copy.deepcopy(item) for item in value.get("attachment_items", [])}
            old_items = {item["ref_key"]: copy.deepcopy(item) for item in previous.get("attachment_items", [])}
            items = incoming if owns else old_items
            assets = {asset.get("ref_key") or "legacy:" + asset["digest"]: copy.deepcopy(asset)
                      for asset in (previous.get("assets", []) if retain else [])}
            for asset in value.get("assets", []):
                identity = asset.get("ref_key") or "legacy:" + asset["digest"]
                if not owns and (not previous.get("processing") or identity not in old_items):
                    # The current attempt alone owns its manifest. An older
                    # collector cannot restore files retired by a newer result.
                    continue
                if owns or identity not in assets:
                    assets[identity] = copy.deepcopy(asset)
            if retain:
                for identity, asset in list(assets.items()):
                    if start:
                        try:
                            self.store.asset(asset["digest"])
                        except (KeyError, ValueError):
                            assets.pop(identity)
                            continue
                    if identity not in items:
                        items[identity] = {"id": identity, "ref_key": identity,
                            "ordinal": asset.get("ordinal") or len(items) + 1,
                            "kind": "pdf" if asset.get("mime") == "application/pdf" else "image",
                            "status": "ready", "retained": True, **asset}
                    else:
                        for field in ("digest", "mime", "size"):
                            if field not in items[identity] and field in asset:
                                items[identity][field] = asset[field]
                        if items[identity].get("status") != "ready":
                            items[identity]["retained"] = True
                        elif owns:
                            items[identity].pop("retained", None)
                for field in ("details", "texts"):
                    if owns and not snapshot.get(field):
                        snapshot[field] = copy.deepcopy(previous.get(field, []))
            # A legacy composition has no stable reference IDs. Once a matching
            # file is observed, replace its legacy tile with the reference tile.
            stable_digests = {asset["digest"] for identity, asset in assets.items()
                              if not identity.startswith("legacy:")}
            for identity in list(assets):
                if identity.startswith("legacy:") and assets[identity]["digest"] in stable_digests:
                    assets.pop(identity)
                    items.pop(identity, None)
            snapshot["attachment_items"] = sorted(items.values(), key=lambda item: (item["ordinal"], item["id"]))
            snapshot["assets"] = sorted(assets.values(), key=lambda asset: (asset.get("ordinal", 0), asset.get("ref_key", "")))
            snapshot["data_revision"] = previous.get("data_revision", 0) + 1
            self.store.save_step(mrn, key, "order_report", snapshot, self.app.username,
                                 version=owns and not value.get("processing"))
            value["data_revision"] = snapshot["data_revision"]
            if state:
                task["progress"] = copy.deepcopy(value["progress"])
                self.review.db.update_task(task["id"], {"progress": value["progress"],
                    "message": value["progress"]["stage"]})
        if state:
            state.update(progress=value["progress"], message=value["progress"]["stage"])

    def _collect_order_report(self, task, state=None):
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
        previous, _ = self._stored(mrn, "order_report", reference)
        previous = previous or {}
        old_ordinals = {item["ref_key"]: item["ordinal"] for item in previous.get("attachment_items", [])}
        result = {"id": reference, "order": self._public_orders({reference: variants})[0],
                  "details": [], "texts": [], "assets": [], "attachment_items": [], "issues": [],
                  "status": "partial", "processing": True, "processing_run": uuid.uuid4().hex,
                  "report_cache_revision": 2}
        details = [order.get("detail_ref") for order in variants]
        reports = [order.get("report_ref") for order in variants]
        pdfs = [ref for order in variants for ref in order.get("pdf_refs") or []]
        studies = [order.get("pacs_ref") for order in variants]
        discovered = False

        def check_cancel():
            if state:
                state.check_cancel()

        def discover_attachments():
            known = {item["id"] for item in result["attachment_items"]}
            for method, refs, kind in (("download_pdf", pdfs, "pdf"), ("download_pacs_image", images, "image")):
                for ref in unique_refs(refs):
                    identity = digest([method, ref])
                    if identity in known:
                        continue
                    ordinal = old_ordinals.get(identity) or max([*old_ordinals.values(),
                        *(item["ordinal"] for item in result["attachment_items"]), 0]) + 1
                    result["attachment_items"].append({"id": identity, "ref_key": identity,
                        "ordinal": ordinal, "kind": kind, "status": "pending"})
                    known.add(identity)

        def publish(phase="discovery", *, start=False):
            discover_attachments()
            items = result["attachment_items"]
            completed = sum(item["status"] == "ready" for item in items)
            failed = sum(item["status"] == "failed" for item in items)
            total = len(items) if discovered else None
            stage = ("取得醫囑報告與附件清單" if phase == "discovery" else
                     "已暫停，保留已取得附件" if phase == "paused" else
                     f"已取得 {completed} / {total} 份附件" + (f"，{failed} 份未取得" if failed else ""))
            result["progress"] = {**progress(completed + failed, total, unit="份附件", failed=failed, stage=stage),
                                  "phase": phase, "discovered": len(items), "completed": completed}
            self._publish_order_report(task, result, state, start=start)

        def fetch(cls, method, ref, *, binary=False, item=None):
            check_cancel()
            source = ref.get("mrn", "")
            if source not in allowed:
                raise ValueError("醫囑參照病歷號未經就診清單核對。")
            validate_patient(ref, source)
            key = "history-asset:" + method + ":" + digest(ref)
            saved = self.store.step(mrn, key)
            if saved and not task.get("force"):
                if not binary and branch_complete(saved["payload"], saved.get("assessment")):
                    return saved["payload"]
                if binary:
                    try:
                        self.store.asset(saved["payload"]["digest"])
                        return saved["payload"]
                    except (KeyError, ValueError):
                        pass
            if binary:
                item["status"] = "downloading"
                publish("attachments")
            if resources := getattr(self.app, "resources", None):
                proof = visits if visits is not None else self._visits(mrn)[0] or []
                return resources.branch(mrn, cls, method, ref, verified_cases=proof,
                    force=task.get("force", False), fast_attachment=binary).value
            if binary:
                with report_attachment_downloads(), self.app.sdk_factory(self.app.settings) as sdk:
                    value = getattr(sdk.orders, method)(model(cls, ref))
            else:
                with self.app.sdk_factory(self.app.settings) as sdk:
                    value = getattr(sdk.orders, method)(model(cls, ref))
            if binary:
                return self.store.save_asset(mrn, key, value, self.app.username)
            from vghks_sdk import assess_data

            assessment = clean(assess_data(value))
            payload = clean(value)
            validate_patient(payload, source)
            self.store.save_step(mrn, key, "report", payload, self.app.username, assessment=assessment)
            return payload

        def collect(cls, method, refs, accept, *, binary=False):
            for ref in unique_refs(refs):
                check_cancel()
                item = None
                if binary:
                    identity = digest([method, ref])
                    item = next(item for item in result["attachment_items"] if item["id"] == identity)
                try:
                    value = fetch(cls, method, ref, binary=binary, item=item)
                    if value:
                        if binary:
                            asset = {**value, "ref_key": item["id"], "ordinal": item["ordinal"], "kind": item["kind"]}
                            item.update(asset, status="ready")
                            result["assets"].append(asset)
                        else:
                            accept(value)
                except (Cancelled, AuthenticationError, StorageError):
                    raise
                except Exception as exc:
                    if should_pause(exc):
                        raise
                    message, code = safe_failure(exc)
                    result["issues"].append({"branch": method, "code": code, "message": message})
                    if item is not None:
                        item.update(status="failed", message=message)
                publish("attachments" if binary else "discovery")

        def accept_detail(detail):
            reports.extend(detail.get("report_refs") or [])
            pdfs.extend(detail.get("pdf_refs") or [])
            studies.extend(detail.get("pacs_refs") or [])
            result["details"].append({"fields": detail.get("fields", {})})
        def accept_report(report):
            if report.get("report_text") or report.get("fields"):
                result["texts"].append({"text": report.get("report_text", ""), "fields": report.get("fields", {}),
                                        "status": report.get("report_data_status", ""),
                                        "text_extraction_notes": report.get("text_extraction_notes", [])})
            if not branch_complete(report):
                result["issues"].append({"branch": "get_order_report", "code": "BOT_REPORT_TEXT_PARTIAL",
                                         "message": "已保留可讀內容；部分文字尚未完整解析。"})
            pdfs.extend(report.get("pdf_refs") or [])
            studies.extend(report.get("pacs_refs") or [])
        images = []
        check_cancel()
        for field in ("details", "texts", "assets"):
            result[field] = copy.deepcopy(previous.get(field, []))
        publish(start=True)
        for field in ("details", "texts", "assets"):
            result[field] = []
        try:
            collect(OrderDetailRef, "get_order_detail", details, accept_detail)
            collect(OrderReportRef, "get_order_report", reports, accept_report)
            collect(PacsStudyRef, "get_pacs_study", studies, lambda study: images.extend(study.get("images") or []))
            discovered = True
            publish("attachments")
            collect(PdfAttachmentRef, "download_pdf", pdfs, None, binary=True)
            collect(PacsImageRef, "download_pacs_image", images, None, binary=True)
        except StorageError:
            raise
        except Exception:
            result.update(processing=False, status="partial")
            for item in result["attachment_items"]:
                if item["status"] == "downloading":
                    item["status"] = "pending"
            publish("paused")
            raise
        result["processing"] = False
        if result["issues"]:
            result["status"] = "partial"
        elif not (result["details"] or result["texts"] or result["assets"]):
            result["status"] = "missing"
        else:
            result["status"] = "ready"
        publish("completed")
        return result
