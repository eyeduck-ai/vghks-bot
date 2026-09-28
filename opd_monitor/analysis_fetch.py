"""Resumable SDK composition; one patient context at a time, shared across modules."""
from __future__ import annotations

from collections import defaultdict
from contextlib import ExitStack
from dataclasses import fields
from datetime import date, timedelta

from vghks_sdk import AuthenticationError
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
from .scanner import Cancelled, safe_failure
from .settings import today
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
            if not connection.auth.check(only=(target,)).ok:
                raise AuthenticationError("Authentication failed", code="AUTH_CHECK_FAILED")
            self.ready.add((key, target))
        return connection

    def close(self):
        self.stack.close()


class PatientCollector:
    def __init__(self, store, state, member, settings, factory, options, sessions=None):
        self.store, self.state, self.member = store, state, member
        self.mrn, self.settings, self.factory = member["mrn"], settings, factory
        self.options = options
        self.stack = ExitStack()
        self.connection = None
        self.ready = set()
        self.seen = {}
        self.issues = []
        self.sessions = sessions

    def sdk(self, target="prq"):
        if self.sessions is not None:
            return self.sessions.get(self.settings, target)
        self.state.check_cancel()
        if self.connection is None:
            if not self.settings.username or not self.settings.password:
                raise ValueError("需要連線取得新資料，請先儲存所選帳號的密碼。")
            self.connection = self.stack.enter_context(self.factory(self.settings))
        if target not in self.ready:
            if not self.connection.auth.check(only=(target,)).ok:
                raise AuthenticationError("Authentication failed", code="AUTH_CHECK_FAILED")
            self.ready.add(target)
        return self.connection

    def query(self, key, kind, operation, *, refresh=False, binary=False, expected_mrn=None, validator=None):
        self.state.check_cancel()
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
                return cached["payload"]
        try:
            self.state.update(stage="analysis", message=f"{self.member.get('name') or self.mrn} · {kind}")
            value = operation()
            # Persist completed requests even when cancellation arrives during I/O.
            if binary:
                value = self.store.save_asset(self.mrn, key, value, self.settings.username)
            else:
                value = clean(value)
                (validator or validate_patient)(value, expected_mrn or self.mrn)
                self.store.save_step(self.mrn, key, kind, value, self.settings.username)
            self.state.count(analysis_fetched=1)
            self.seen[key] = value
            self.state.checkpoint()
            return value
        except (Cancelled, StorageError):
            raise
        except Exception as exc:
            message, code = safe_failure(exc)
            if type(exc) is ValueError:
                message = str(exc)
            self.issues.append({"key": key, "message": message, "code": code})
            self.state.issue(kind, message, code=code, mrn=self.mrn)
            # Errors are not successful cache entries; the next run retries them.
            self.seen[key] = None
            return None

    def run(self):
        try:
            modules = self.options["modules"]
            terms = list(dict.fromkeys(t for m in modules for t in MODULES[m]["orders"]))
            if terms:
                self.examinations(terms)
            if "surgery" in modules:
                self.surgery()
        finally:
            self.stack.close()

    def examinations(self, terms):
        refresh = self.options["refresh"]
        numeric = self.query("numeric-history", "numeric", lambda: self.sdk().records.get_numeric_history(
            self.mrn, NumericHistoryFilter()), refresh=refresh)
        sources = {}
        for category in ("*", "OR"):
            result = self.query("orders-history:" + category, "order_index",
                                lambda category=category: self.sdk().orders.get_order_history(
                                    self.mrn, OrderHistoryFilter(category=category)), refresh=refresh)
            if result is not None:
                sources[category] = result
        visits = self.query("visits", "visits", lambda: self.sdk().records.get_visit_cases(self.mrn),
                            refresh=refresh, validator=validate_visits)
        case_sources = {digest(model(VisitCase, raw).identity): raw["mrn"] for raw in visits or []}
        self.allowed_mrns = {self.mrn, *case_sources.values()}
        numeric_cutoff = (today() - timedelta(days=3650)).isoformat()
        order_cutoff = (today() - timedelta(days=4000)).isoformat()
        older, unsupported, unknown_dates = 0, [], 0
        history_incomplete = len(sources) < 2
        available_dates = [v["visit_date"] for v in visits or [] if v.get("visit_date")]
        for raw in visits or []:
            self.state.check_cancel()
            day = raw.get("visit_date")
            if not day:
                unknown_dates += 1
                continue
            # Date filters limit expensive per-case backfill and assets, while
            # the complete historical index is kept for subsequent analysis.
            if not self.in_range(day):
                continue
            needs_numeric = day < numeric_cutoff or numeric is None
            needs_orders = day < order_cutoff or history_incomplete
            if not needs_numeric and not needs_orders:
                continue
            if raw.get("case_type") != "O":
                unsupported.append({"date": day, "case_type": raw.get("case_type"), "case_no": raw.get("case_no")})
                continue
            if model(VisitCase, raw).patient_mrn != self.mrn:
                self.state.issue("就診清單", "病歷號不符，已略過。", mrn=self.mrn)
                continue
            case = model(VisitCase, raw)
            key = digest(case.identity)
            older += 1
            if needs_numeric:
                self.query("numeric-case:" + key, "numeric",
                           lambda case=case: self.sdk().records.get_numeric_report(case),
                           expected_mrn=case.mrn,
                           validator=lambda value, _, case=case: validate_case_report(value, self.mrn, case))
            if needs_orders:
                result = self.query("orders-case:" + key, "order_index",
                                    lambda case=case: self.sdk().orders.get_case_orders(case), expected_mrn=case.mrn)
                if result is not None:
                    sources[key] = result
        # Include already cached per-case indices when a later run reuses history.
        for saved in self.store.steps(self.mrn, "order_index"):
            if saved["key"] in case_sources:
                sources.setdefault(saved["key"], saved["payload"])
        groups = defaultdict(list)
        for source, orders in sources.items():
            for order in orders:
                expected = self.mrn if source in {"*", "OR"} else case_sources.get(source)
                if not expected or order.get("mrn") != expected or not any(term_match(order.get("name", ""), t) for t in terms):
                    continue
                groups[order_identity(order)].append({"source": source, "order": order})
        for key, variants in groups.items():
            self.state.check_cancel()
            order = min((v["order"] for v in variants),
                        key=lambda o: {"COMPLETED": 0, "UNKNOWN": 1, "NOT_EXECUTED": 2}[
                            classify_order_execution(o.get("status", ""))])
            when = iso_day(order.get("execution_date")) or iso_day(order.get("order_date"))
            if when and not self.in_range(when):
                continue
            cached = self.store.step(self.mrn, "collected-order:" + key)
            fingerprint = digest(unique_refs([v["order"] for v in variants]))
            changed = bool(cached and cached["payload"].get("fingerprint") != fingerprint)
            if cached and cached["payload"].get("status") == "complete" and not changed and not self.options["force"]:
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
            "backfilled_cases": older, "unsupported_cases": unsupported,
            "unknown_date_cases": unknown_dates, "issues": self.issues,
            "requested_start": self.options.get("start", ""), "requested_end": self.options.get("end", ""),
            "note": "涵蓋院方回傳的歷史索引與可補查門診；無法補查的住院／急診另列。",
        }
        self.store.save_step(self.mrn, "coverage", "coverage", coverage, self.settings.username)

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
