"""Resumable SDK composition; one patient context at a time, shared across modules."""
from __future__ import annotations

from collections import defaultdict
from contextlib import ExitStack
from dataclasses import fields
from datetime import date, timedelta

from vghks_sdk import AuthenticationError, SoapRecord, assess_data
from vghks_sdk.models import (
    NumericHistoryFilter,
    OrderDetailRef,
    OrderHistoryFilter,
    OrderReportRef,
    PacsImageRef,
    PacsStudyRef,
    PdfAttachmentRef,
    VisitCase,
    to_jsonable,
)
from vghks_sdk.order_status import classify_order_execution

from .analysis_numeric import MODULES, iso_day, term_match
from .analysis_store import digest
from .connection_state import require_ready, should_pause
from .library import current_cache
from .ophthalmic_orders import (
    SCOPE_KEY,
    eye_order_contexts,
    eye_visit_signature,
    is_eye_visit,
    order_in_eye_context,
)
from .scanner import Cancelled, safe_failure
from .settings import today
from .soap_data import snapshot as soap_snapshot
from .storage import StorageError


def clean(value):
    value = to_jsonable(value)
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if k.casefold() not in {
            "hid", "ssid", "keyone", "keytwo", "keythree", "password", "cookie", "detail_params"}}
    if isinstance(value, (tuple, list)):
        return [clean(v) for v in value]
    return value


def model(cls, value):
    values = {k: v for k, v in value.items() if k in {f.name for f in fields(cls)}}
    if cls is PdfAttachmentRef:
        from .sdk_orders import pdf_reference

        return pdf_reference(**values)
    if cls is VisitCase:
        values["visit_date"] = date.fromisoformat(values["visit_date"]) if values.get("visit_date") else None
    return cls(**values)


def order_identity(order):
    ref = order.get("detail_ref") or order.get("report_ref") or {}
    return digest([order.get("mrn"), order.get("case_type"), order.get("case_no"),
                   ref.get("sequence_no"), order.get("name"), order.get("order_date")])


def unique_refs(refs):
    return list({digest(ref): ref for ref in refs if ref}.values())


def validate_patient(value, mrn):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in {"mrn", "patient_mrn", "hhisnum"} and item and str(item) != mrn:
                raise ValueError("回傳病歷號不符，已略過。")
            validate_patient(item, mrn)
    elif isinstance(value, (list, tuple)):
        for item in value:
            validate_patient(item, mrn)


def validate_visits(value, mrn):
    if not isinstance(value, list):
        raise ValueError("就診清單格式不正確。")
    for raw in value:
        if not isinstance(raw, dict) or model(VisitCase, raw).patient_mrn != mrn:
            raise ValueError("就診清單病人不符，已略過。")


def validate_order_index(value, allowed_mrns):
    if not isinstance(value, list):
        raise ValueError("醫囑索引格式不正確。")
    for order in value:
        if not isinstance(order, dict) or order.get("mrn") not in allowed_mrns:
            raise ValueError("醫囑索引病歷號未經就診清單核對。")
        validate_patient(order, order["mrn"])


def validate_case_orders(value, case):
    validate_order_index(value, {case.mrn})
    contexts = {(case.mrn, case.case_type, case.case_no)}
    if any(not order_in_eye_context(order, contexts) for order in value):
        raise ValueError("單次醫囑索引的就診識別不符。")


def validate_case_report(value, lookup_mrn, case):
    validate_patient(value, case.mrn)
    returned = value.get("case") if isinstance(value, dict) else None
    if not isinstance(returned, dict):
        raise ValueError("單次數值報告缺少就診識別。")
    found = model(VisitCase, returned)
    if found.identity != case.identity or found.patient_mrn != lookup_mrn:
        raise ValueError("單次數值報告就診識別不符。")


class RunSessions:
    """Lazy login once per account, used serially for all patients in this run."""
    def __init__(self, factory, state):
        self.factory, self.state = factory, state
        self.stack = ExitStack()
        self.connections, self.ready = {}, set()

    def get(self, settings, target):
        self.state.check_cancel()
        key = id(settings)
        if key not in self.connections:
            if not settings.username or not settings.password:
                raise ValueError("需要連線取得新資料，請先儲存所選帳號的密碼。")
            self.connections[key] = self.stack.enter_context(self.factory(settings))
        connection = self.connections[key]
        if (key, target) not in self.ready:
            require_ready(connection.auth.check(only=(target,)))
            self.ready.add((key, target))
        return connection

    def close(self):
        self.stack.close()


class AnalysisYield(Exception):
    """Cooperative pause after a saved SDK read."""


class PatientCollector:
    def __init__(self, store, state, member, settings, factory, options, sessions=None, review=None,
                 checkpoint=None):
        self.store, self.state, self.member = store, state, member
        self.mrn, self.settings, self.factory = member["mrn"], settings, factory
        self.options = options
        self.stack = ExitStack()
        self.connection = None
        self.ready = set()
        self.seen = {}
        self.issues = []
        self.sessions = sessions
        self.review = review
        self.control = checkpoint

    def check(self):
        self.state.check_cancel()
        if self.control:
            self.control()

    def sdk(self, target="prq"):
        self.check()
        if self.sessions is not None:
            return self.sessions.get(self.settings, target)
        self.state.check_cancel()
        if self.connection is None:
            if not self.settings.username or not self.settings.password:
                raise ValueError("需要連線取得新資料，請先儲存所選帳號的密碼。")
            self.connection = self.stack.enter_context(self.factory(self.settings))
        if target not in self.ready:
            require_ready(self.connection.auth.check(only=(target,)))
            self.ready.add(target)
        return self.connection

    def query(self, key, kind, operation, *, refresh=False, binary=False, expected_mrn=None, validator=None):
        self.check()
        if key in self.seen:
            return self.seen[key]
        cached = self.store.step(self.mrn, key)
        if cached and kind == "asset":
            try:
                self.store.asset(cached["payload"]["digest"])
            except ValueError:
                cached = None
        if cached and not (refresh or self.options["force"]):
            if not binary:
                try:
                    (validator or validate_patient)(cached["payload"], expected_mrn or self.mrn)
                except (TypeError, ValueError):
                    cached = None
            if cached:
                self.state.count(analysis_cached=1)
                self.seen[key] = cached["payload"]
                self.note_assessment(key, kind, cached.get("assessment") or {})
                return cached["payload"]
        try:
            self.state.update(stage="analysis", message=f"{self.member.get('name') or self.mrn} · {kind}")
            value = operation()
            assessment = assess_data(value)
            # Persist completed requests even when cancellation arrives during I/O.
            if binary:
                value = self.store.save_asset(self.mrn, key, value, self.settings.username)
            else:
                value = clean(value)
                (validator or validate_patient)(value, expected_mrn or self.mrn)
                self.store.save_step(self.mrn, key, kind, value, self.settings.username,
                                     assessment=to_jsonable(assessment))
            self.state.count(analysis_fetched=1)
            self.seen[key] = value
            self.note_assessment(key, kind, to_jsonable(assessment))
            self.state.checkpoint()
            return value
        except (Cancelled, StorageError, AnalysisYield):
            raise
        except Exception as exc:
            if should_pause(exc):
                raise
            message, code = safe_failure(exc)
            if type(exc) is ValueError:
                message = str(exc)
            from .diagnostics import analysis_query_context

            query = analysis_query_context(key, kind)
            self.issues.append({"key": key, "message": message, "code": code, "query": query})
            self.state.issue(kind, message, code=code, mrn=self.mrn, key=key, query=query)
            # Errors are not successful cache entries; the next run retries them.
            self.seen[key] = None
            return None

    def note_assessment(self, key, kind, assessment):
        if assessment.get("complete") is not False:
            return
        from .diagnostics import analysis_query_context

        query = analysis_query_context(key, kind)
        for issue in assessment.get("issues", []):
            self.issues.append({"key": key, "message": "已保留資料；部分內容解析未完成。",
                                "code": issue["code"], "query": query})
            self.state.issue(kind, "已保留資料；部分內容解析未完成。", code=issue["code"],
                             mrn=self.mrn, key=key, query=query)

    def run(self):
        try:
            modules = self.options["modules"]
            terms = list(dict.fromkeys(t for m in modules for t in MODULES[m]["orders"]))
            if terms:
                self.examinations(terms, prioritize_soap=modules == ["cataract"] and bool(self.review))
            if "surgery" in modules:
                self.surgery()
        finally:
            self.stack.close()

    def examinations(self, terms, *, prioritize_soap=False):
        refresh = self.options["refresh"]
        eye_only = self.options["modules"] == ["cataract"]
        visits = self.query("visits", "visits", lambda: self.sdk().records.get_visit_cases(self.mrn),
                            refresh=refresh, validator=validate_visits)
        cases = [model(VisitCase, raw) for raw in visits or []]
        case_models = {digest(case.identity): case for case in cases}
        case_sources = {key: case.mrn for key, case in case_models.items()}
        self.allowed_mrns = {self.mrn, *case_sources.values()}
        eye_contexts = eye_order_contexts(visits, self.mrn)
        numeric = self.query("numeric-history", "numeric", lambda: self.sdk().records.get_numeric_history(
            self.mrn, NumericHistoryFilter()), refresh=refresh)
        if prioritize_soap:
            # The first view uses SOAP and numeric history. Save them before
            # requesting any order index, report, or attachment.
            self.latest_eye_soap(visits)
        numeric_cutoff = (today() - timedelta(days=3650)).isoformat()
        order_cutoff = (today() - timedelta(days=4000)).isoformat()
        backfilled = set()
        unsupported = {}
        rejected = set()
        unknown_dates = 0
        available_dates = [v["visit_date"] for v in visits or [] if v.get("visit_date")]
        eligible = []
        for raw in visits or []:
            self.state.check_cancel()
            day = raw.get("visit_date")
            if not day:
                unknown_dates += 1
            elif self.in_range(day) and day <= today().isoformat():
                eligible.append(raw)

        def backfill_case(raw):
            case = model(VisitCase, raw)
            key = digest(case.identity)
            if case.case_type != "O":
                unsupported.setdefault(key, {"date": raw["visit_date"],
                                             "case_type": raw.get("case_type"),
                                             "case_no": raw.get("case_no")})
                return None
            if case.patient_mrn != self.mrn:
                if key not in rejected:
                    self.state.issue("就診清單", "病歷號不符，已略過。", mrn=self.mrn)
                    rejected.add(key)
                return None
            backfilled.add(key)
            return case, key

        # Older encounters beyond the SDK history window are also numeric
        # data for the opening view; finish them before order work begins.
        for raw in eligible:
            self.state.check_cancel()
            if raw["visit_date"] >= numeric_cutoff and numeric is not None:
                continue
            selected = backfill_case(raw)
            if selected is None:
                continue
            case, key = selected
            self.query("numeric-case:" + key, "numeric",
                       lambda case=case: self.sdk().records.get_numeric_report(case),
                       expected_mrn=case.mrn,
                       validator=lambda value, _, case=case: validate_case_report(value, self.mrn, case))

        sources = {}
        categories = ("*",) if eye_only else ("*", "OR")
        for category in categories:
            if eye_only and not eye_contexts:
                continue
            saved = self.store.step(self.mrn, "orders-history:" + category)
            visit_step = self.store.step(self.mrn, "visits")
            stale = bool(eye_only and saved and visit_step and saved["saved_at"] < visit_step["saved_at"])
            result = self.query("orders-history:" + category, "order_index",
                                lambda category=category: self.sdk().orders.get_order_history(
                                    self.mrn, OrderHistoryFilter(category=category)), refresh=refresh or stale,
                                validator=lambda value, _: validate_order_index(value, self.allowed_mrns))
            if result is not None:
                sources[category] = result
        history_incomplete = len(sources) < len(categories)
        order_indices_complete = visits is not None
        order_cases = set()
        for raw in eligible:
            self.state.check_cancel()
            if eye_only and not is_eye_visit(raw):
                continue
            if raw["visit_date"] >= order_cutoff and not history_incomplete:
                continue
            selected = backfill_case(raw)
            if selected is None:
                continue
            case, key = selected
            order_cases.add(key)
            result = self.query("orders-case:" + key, "order_index",
                                lambda case=case: self.sdk().orders.get_case_orders(case), expected_mrn=case.mrn, refresh=refresh,
                                validator=lambda value, _, case=case: validate_case_orders(value, case))
            if result is not None:
                sources[key] = result
            else:
                order_indices_complete = False
        # Include already cached per-case indices when a later run reuses history.
        for saved in self.store.steps(self.mrn, "order_index"):
            source = saved["key"].removeprefix("orders-case:")
            if source in order_cases:
                case = case_models[source]
                try:
                    validate_case_orders(saved["payload"], case)
                    sources.setdefault(source, saved["payload"])
                except (TypeError, ValueError):
                    pass
        if eye_only:
            self.store.save_step(self.mrn, SCOPE_KEY, "order_scope", {
                "scope": "ophthalmology", "visit_signature": eye_visit_signature(visits, self.mrn),
                "history_complete": "*" in sources, "case_keys": sorted(order_cases),
                "complete": order_indices_complete and not (self.options.get("start") or self.options.get("end")),
            }, self.settings.username)
        groups = defaultdict(list)
        for source, orders in sources.items():
            for order in orders:
                if eye_only and not order_in_eye_context(order, eye_contexts):
                    continue
                expected = ((order.get("mrn") if order.get("mrn") in self.allowed_mrns else None)
                            if source in {"*", "OR"} else case_sources.get(source))
                if not expected or order.get("mrn") != expected or not any(term_match(order.get("name", ""), t) for t in terms):
                    continue
                groups[order_identity(order)].append({"source": source, "order": order})
        for key, variants in groups.items():
            self.state.check_cancel()
            if self.options.get("lazy_cataract_orders"):
                continue
            order = min((v["order"] for v in variants),
                        key=lambda o: {"COMPLETED": 0, "UNKNOWN": 1, "NOT_EXECUTED": 2}[
                            classify_order_execution(o.get("status", ""))])
            when = iso_day(order.get("execution_date")) or iso_day(order.get("order_date"))
            if when and not self.in_range(when):
                continue
            cached = self.store.step(self.mrn, "collected-order:" + key)
            reviewed = self.store.step(self.mrn, "history-order:" + key)
            def assets_available(step):
                if not step:
                    return False
                try:
                    for asset in step["payload"].get("assets") or []:
                        self.store.asset(asset["digest"])
                except (KeyError, ValueError):
                    return False
                return True
            if (reviewed and reviewed["payload"].get("status") in {"ready", "missing", "not_executed"}
                    and assets_available(reviewed) and not (self.options["force"] or refresh)):
                self.state.count(analysis_cached=1)
                continue
            fingerprint = digest(unique_refs([v["order"] for v in variants]))
            changed = bool(cached and cached["payload"].get("fingerprint") != fingerprint)
            if (cached and (cached["payload"].get("status") == "complete" or
                            (not refresh and cached["payload"].get("status") in {"no_data", "no_links"}))
                    and assets_available(cached) and not changed and not self.options["force"]):
                self.state.count(analysis_cached=1)
                continue
            value = self.collect_order(variants, key, changed or refresh)
            value.update(fingerprint=fingerprint, date=when, name=order.get("name", ""),
                         id=key, order=order, sources=variants)
            self.store.save_step(self.mrn, "collected-order:" + key, "order", value, self.settings.username)
        coverage = {
            "numeric_history_start": numeric_cutoff, "order_history_start": order_cutoff,
            "earliest_visit": min(available_dates) if available_dates else "",
            "latest_visit": max(available_dates) if available_dates else "",
            "backfilled_cases": len(backfilled), "unsupported_cases": list(unsupported.values()),
            "unknown_date_cases": unknown_dates, "issues": self.issues,
            "requested_start": self.options.get("start", ""), "requested_end": self.options.get("end", ""),
            "order_scope": "ophthalmology" if eye_only else "all",
            "note": ("批次取得歷年醫囑索引，僅抓取已核對眼科就診的相關報告與附件；超出歷年範圍或索引失敗時補查眼科門診，無法補查的住院／急診另列。"
                     if eye_only else "涵蓋院方回傳的歷史索引與可補查門診；無法補查的住院／急診另列。"),
        }
        self.store.save_step(self.mrn, "coverage", "coverage", coverage, self.settings.username)
        return visits

    def latest_eye_soap(self, visits):
        """Save the newest readable eye SOAP without changing a review task or its tags."""
        if visits is None:
            return
        from .review import case_key

        candidates = []
        for raw in visits:
            case = model(VisitCase, raw)
            if (case.patient_mrn == self.mrn and case.case_type == "O" and case.visit_date
                    and case.visit_date <= today()
                    and "眼科" in (case.section_name + " " + case.section_code)):
                candidates.append(case)
        candidates = sorted({case.identity: case for case in candidates}.values(),
                            key=lambda case: case.visit_date, reverse=True)
        failed = False
        for case in candidates:
            self.check()
            key = case_key(case)
            if self.seen.get("cataract-soap:" + key, True) is None:
                failed = True
                continue
            if self.review and self.review.db.deleted_since(key, ""):
                continue
            if (self.options.get("mrn") and not (self.options["force"] or self.options["refresh"])
                    and self.store.step(self.mrn, "cataract-soap-empty:" + key)):
                continue
            cached = self.store.library.get_record(key)
            if (cached and cached.get("mrn") == self.mrn
                    and not (self.options["force"] or self.options["refresh"])
                    and (self.options.get("mrn") or current_cache(case.visit_date.isoformat(), cached["updated_at"]))):
                self.store.save_step(self.mrn, "cataract-soap", "cataract_soap",
                                     {"status": "ready", "record_id": key}, self.settings.username)
                self.state.count(analysis_cached=1)
                return
            try:
                self.state.update(stage="analysis", message=f"{self.member.get('name') or self.mrn} · 最新眼科 SOAP")
                soap = self.sdk().records.get_soap(case)
                if (not isinstance(soap, SoapRecord) or soap.case.identity != case.identity
                        or soap.case.patient_mrn != self.mrn):
                    raise ValueError("眼科 SOAP 就診識別不符。")
                if not soap.full_text.strip():
                    self.store.save_step(self.mrn, "cataract-soap-empty:" + key, "cataract_soap",
                                         {"status": "empty", "record_id": key}, self.settings.username)
                    continue
                record = {"id": key, "mrn": self.mrn, "name": self.member.get("name", ""),
                          "date": case.visit_date.isoformat(), "section": case.section_name,
                          "section_code": case.section_code, "case_no": case.case_no,
                          "source_mrn": case.mrn, "case_index": case.index,
                          "doctor": case.doctor_name, "doctor_card": case.doctor_card,
                          **soap_snapshot(soap)}
                self.store.library.save_record(record, self.settings.username, self.state.data["id"])
                self.store.save_step(self.mrn, "cataract-soap", "cataract_soap",
                                     {"status": "ready", "record_id": key}, self.settings.username)
                self.state.count(analysis_fetched=1)
                self.state.checkpoint()
                return
            except (Cancelled, StorageError, AuthenticationError, AnalysisYield):
                raise
            except Exception as exc:
                if should_pause(exc):
                    raise
                failed = True
                self.seen["cataract-soap:" + key] = None
                message, code = safe_failure(exc)
                self.issues.append({"key": "cataract-soap:" + key, "message": message, "code": code})
                self.state.issue("最新眼科 SOAP", message, code=code, mrn=self.mrn)
        if not failed:
            self.store.save_step(self.mrn, "cataract-soap", "cataract_soap",
                                 {"status": "missing" if candidates else "no_visit", "record_id": ""},
                                 self.settings.username)

    def in_range(self, day):
        return not ((self.options.get("start") and day < self.options["start"])
                    or (self.options.get("end") and day > self.options["end"]))

    def collect_order(self, variants, order_key, changed):
        row = {"texts": [], "assets": [], "branches": [], "status": "complete"}
        orders = [v["order"] for v in variants]
        if all(classify_order_execution(o.get("status", "")) == "NOT_EXECUTED" for o in orders):
            return {**row, "status": "not_executed"}
        details, reports, pdfs, studies, images = [], [], [], [], []
        for order in orders:
            details.extend([order.get("detail_ref")])
            reports.extend([order.get("report_ref")])
            pdfs.extend(order.get("pdf_refs") or [])
            studies.extend([order.get("pacs_ref")])

        def fetch(cls, method, reference, *, binary=False):
            key = method + ":" + digest(reference)
            def operation():
                source_mrn = reference.get("mrn", "")
                if source_mrn not in self.allowed_mrns:
                    raise ValueError("報告參照病歷號未經就診清單核對。")
                validate_patient(reference, source_mrn)
                return getattr(self.sdk().orders, method)(model(cls, reference))
            # Retry metadata-only / empty report branches when explicitly refreshing.
            value = self.query(key, "asset" if binary else "report",
                               operation,
                               refresh=changed and not binary, binary=binary,
                               expected_mrn=reference.get("mrn", ""))
            row["branches"].append({"key": key, "status": "ok" if value is not None else "failed"})
            if value is None:
                row["status"] = "partial"
            return value

        for ref in unique_refs(details):
            detail = fetch(OrderDetailRef, "get_order_detail", ref)
            if detail:
                reports.extend(detail.get("report_refs") or [])
                pdfs.extend(detail.get("pdf_refs") or [])
                studies.extend(detail.get("pacs_refs") or [])
        for ref in unique_refs(reports):
            report = fetch(OrderReportRef, "get_order_report", ref)
            if report:
                if report.get("report_text") or report.get("fields"):
                    row["texts"].append({"text": report.get("report_text", ""), "fields": report.get("fields", {}),
                                         "status": report.get("report_data_status", ""), "reference": ref})
                pdfs.extend(report.get("pdf_refs") or [])
                studies.extend(report.get("pacs_refs") or [])
        for ref in unique_refs(studies):
            study = fetch(PacsStudyRef, "get_pacs_study", ref)
            if study:
                images.extend(study.get("images") or [])
                if not study.get("images"):
                    row.setdefault("empty_reasons", []).append(study.get("empty_reason") or "PACS 未提供影像")
        for cls, method, refs in ((PdfAttachmentRef, "download_pdf", pdfs),
                                  (PacsImageRef, "download_pacs_image", images)):
            for ref in unique_refs(refs):
                asset = fetch(cls, method, ref, binary=True)
                if asset:
                    row["assets"].append({**asset, "reference": ref})
        if row["status"] == "complete" and not row["assets"] and not row["texts"]:
            row["status"] = "no_data" if row["branches"] else "no_links"
        return row

    def surgery(self):
        # Derive every time from the live SOAP library so deletions cannot resurrect tags.
        from .surgery_candidates import candidates

        rows = candidates(self.store.records(self.mrn))
        self.query("surgery-patient", "surgery_patient",
                   lambda: self.sdk("oppl").surgery.get_patient_info(self.mrn), refresh=self.options["refresh"])
        dates = [r["fields"]["date"] for r in rows if r["fields"]["date"]]
        start = date.fromisoformat(min(dates)) if dates else today()
        end = date.fromisoformat(max(dates)) if dates else today()
        # Search around proposed dates so nearby reschedules are visible.
        start -= timedelta(days=90)
        end += timedelta(days=180)
        key = "schedule:" + digest([self.settings.username, start.isoformat(), end.isoformat()])
        self.query(key, "surgery_schedule",
                   lambda: self.sdk("oppl").surgery.get_schedule(self.settings.username, start, end, mrn=self.mrn),
                   refresh=self.options["refresh"])
