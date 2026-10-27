"""Account-local clinical acquisitions shared after scope checks, never HTTP/session state."""
from __future__ import annotations

import copy
import threading
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from time import monotonic

from vghks_sdk import ParseError, SoapRecord, VisitCase
from vghks_sdk.core.errors import SDKError
from vghks_sdk.models import to_jsonable

from .analysis_store import digest
from .bot_store import fresh
from .library import current_cache
from .scanner import Cancelled
from .settings import timestamp


@dataclass(frozen=True)
class Acquisition:
    value: object
    source: str
    saved_at: str
    assessment: dict = field(default_factory=dict)
    acquisition_id: str = ""
    producer_task_id: str = ""


@dataclass(eq=False)
class _Flight:
    key: tuple
    resource: tuple
    mrn: str
    category: str
    epoch: tuple
    generation: int
    load: object
    fetch: object
    save: object
    task: dict
    force: bool
    order: int
    cancel: threading.Event = field(default_factory=threading.Event)
    subscribers: dict = field(default_factory=dict)
    defer_probes: dict = field(default_factory=dict)
    priority: bool = False
    started: bool = False
    done: bool = False
    value: object = None
    error: BaseException | None = None
    saved_at: str = ""
    source: str = "network"
    assessment: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    diagnostic_emitted: bool = False


class ClinicalResources:
    """One bounded worker preserves account serial I/O and foreground queue priority.

    Registry/fence locks are held only for short cache reads or atomic saves,
    never while waiting for the gateway or another acquisition. The worker owns
    cancellation independently of the task that happened to ask first.
    """

    def __init__(self, app, *, pending_limit=64):
        self.app, self.gateway = app, app.gateway
        self.condition = threading.Condition(threading.RLock())
        self.pending_limit = pending_limit
        self.flights, self.pending = {}, []
        self.epochs, self.generations = {}, {}
        self.promoted = {}
        self.login_epoch = self.sequence = 0
        self.closed = False
        self.worker = None
        self.stats = {"network": 0, "shared": 0, "cache": 0}

    def _epoch(self, mrn, category):
        return self.login_epoch, self.epochs.get((mrn, category), 0)

    @contextmanager
    def invalidation(self, mrns=(), categories=()):
        # Same order as promotion: registry/fence first, then library transaction.
        with self.condition:
            selected, kinds = set(mrns), set(categories)
            for mrn in selected:
                for category in kinds:
                    self.epochs[mrn, category] = self.epochs.get((mrn, category), 0) + 1
            for flight in self.flights.values():
                if flight.mrn in selected and flight.category in kinds:
                    flight.cancel.set()
            self.condition.notify_all()
            yield

    def reset(self):
        with self.condition:
            self.login_epoch += 1
            for flight in self.flights.values():
                flight.cancel.set()
            self.condition.notify_all()

    def close(self, *, wait=False):
        with self.condition:
            self.closed = True
            self.login_epoch += 1
            for flight in self.flights.values():
                flight.cancel.set()
            self.condition.notify_all()
            worker = self.worker
        if wait and worker and worker is not threading.current_thread():
            worker.join()

    def _should_defer(self, flight):
        with self.condition:
            pending_foreground = any(value.priority and not value.cancel.is_set() for value in self.pending)
            probes = [flight.defer_probes.get(key) for key in flight.subscribers]
            priority = flight.priority
        # An idle producer must not impose its policy on an independent consumer.
        # Evaluate arbitrary scheduler callbacks outside the registry lock.
        return pending_foreground or bool(not priority and probes and all(probe and probe() for probe in probes))

    def _run(self):
        from .bot_gateway import AcquisitionDeferred

        while True:
            with self.condition:
                while not self.pending and not self.closed:
                    self.condition.wait()
                if not self.pending:
                    return
                flight = min(self.pending, key=lambda value: (not value.priority, value.order))
                self.pending.remove(flight)
                flight.started = True
            deferred = False
            self.gateway.local.diagnostic_emitted = False
            try:
                if flight.cancel.is_set():
                    raise Cancelled()
                with self.condition:
                    if flight.epoch != self._epoch(flight.mrn, flight.category):
                        raise Cancelled()
                    cached = None if flight.force else flight.load()
                if cached is not None:
                    value, saved = cached[:2]
                    assessment = cached[2] if len(cached) > 2 else {}
                    source = "cache"
                else:
                    with self.gateway.task_context(flight.task.get("task_id", ""),
                            flight.task.get("task_kind", "clinical_resource"), flight.cancel, acquisition_id=flight.id), \
                            self.gateway.query_priority(lambda flight=flight: flight.priority,
                                lambda flight=flight: self._should_defer(flight)):
                        value = flight.fetch()
                    assessment = value.assessment if isinstance(value, Acquisition) else {}
                    if isinstance(value, Acquisition):
                        value = value.value
                    source, saved = "network", timestamp()
                    assessment = {**assessment, "acquisition_ref": {"id": flight.id,
                        "producer_task_id": flight.task.get("task_id", "")}}
                    with self.condition:
                        if self.closed or flight.epoch != self._epoch(flight.mrn, flight.category):
                            raise Cancelled()
                        # Never replace a newer completed refresh. A queued
                        # force still allows an earlier success to be retained.
                        if flight.generation >= self.promoted.get(flight.resource, 0):
                            value, saved = flight.save(value, saved, assessment)
                            self.promoted[flight.resource] = flight.generation
                        self.stats["network"] += 1
                with self.condition:
                    flight.value, flight.saved_at, flight.source = copy.deepcopy(value), saved, source
                    flight.assessment = copy.deepcopy(assessment)
            except AcquisitionDeferred:
                with self.condition:
                    deferred = True
                    flight.started = False
                    self.pending.append(flight)
                    self.condition.wait(.05)
            except BaseException as exc:
                with self.condition:
                    flight.error = exc
                    flight.diagnostic_emitted = getattr(self.gateway.local, "diagnostic_emitted", False)
            finally:
                with self.condition:
                    if not deferred:
                        flight.done = True
                        if self.flights.get(flight.key) is flight:
                            self.flights.pop(flight.key)
                    self.condition.notify_all()

    def _receipt(self, value, resource, *, failed=False):
        task_id = getattr(self.gateway.local, "task", {}).get("task_id", "")
        if task_id:
            receipt = {"logical": True, "acquisition_id": value.acquisition_id or uuid.uuid4().hex,
                       "source": value.source, "producer_task_id": value.producer_task_id}
            try:
                # This is a logical consumption receipt, not another SDK/HTTP read.
                self.app.review.db.sdk_event(session_id=self.gateway.session_id, task_id=task_id,
                    service="clinical_resources", method=resource[0], status="error" if failed else "ok",
                    assessment=receipt)
            except Exception:
                pass  # Audit storage cannot turn a verified acquisition into a failure.

    def acquire(self, resource, mrn, category, **options):
        try:
            value = self._acquire(resource, mrn, category, **options)
        except Exception as exc:
            receipt = getattr(exc, "acquisition_receipt", {})
            self._receipt(Acquisition(None, receipt.get("source", "cancelled"), "", {},
                receipt.get("id", ""), receipt.get("producer_task_id", "")), resource, failed=True)
            raise
        self._receipt(value, resource)
        return value

    def _acquire(self, resource, mrn, category, *, load, fetch, save, force=False, pacing="normal"):
        from .jobs import BusyError

        cancel = getattr(self.gateway.local, "cancel", None)
        foreground = bool(getattr(self.gateway.local, "foreground", False))
        subscriber = uuid.uuid4().hex
        with self.condition:
            if self.closed or cancel is not None and cancel.is_set():
                raise Cancelled()
            # Cached values never establish or restore PRQ patient context.
            if not force and (cached := load()) is not None:
                self.stats["cache"] += 1
                assessment = copy.deepcopy(cached[2]) if len(cached) > 2 else {}
                receipt = assessment.get("acquisition_ref", {})
                return Acquisition(copy.deepcopy(cached[0]), "cache", cached[1],
                                   assessment, receipt.get("id", ""), receipt.get("producer_task_id", ""))
            resource = (mrn, *resource)
            if force:
                self.generations[resource] = self.generations.get(resource, 0) + 1
            generation = self.generations.get(resource, 0)
            key = (resource, self._epoch(mrn, category), self.gateway.session_id,
                   self.gateway.auth_generation, generation, pacing, subscriber if force else "normal")
            flight = self.flights.get(key)
            if flight is not None and flight.cancel.is_set():
                flight = None
            shared = flight is not None
            if flight is None:
                if len(self.pending) >= self.pending_limit:
                    raise BusyError("此帳號的資料取得佇列已額滿，請稍後重試。")
                self.sequence += 1
                flight = _Flight(key, resource, mrn, category, self._epoch(mrn, category), generation,
                                 load, fetch, save, dict(getattr(self.gateway.local, "task", {})),
                                 force, self.sequence)
                self.flights[key] = flight
                self.pending.append(flight)
            flight.subscribers[subscriber] = foreground
            flight.defer_probes[subscriber] = getattr(self.gateway.local, "defer_probe", None)
            flight.priority = any(flight.subscribers.values())
            if self.worker is None:
                self.worker = threading.Thread(target=self._run, name="clinical-resources", daemon=True)
                self.worker.start()
            self.condition.notify_all()
        deadline = monotonic() + self.gateway.wait_timeout
        try:
            with self.condition:
                while not flight.done:
                    if cancel is not None and cancel.is_set():
                        flight.subscribers.pop(subscriber, None)
                        flight.defer_probes.pop(subscriber, None)
                        flight.priority = any(flight.subscribers.values())
                        if not flight.subscribers:
                            flight.cancel.set()
                        if not flight.started:
                            raise Cancelled()
                    # Once I/O starts its original SDK timeouts/backoff apply.
                    # Pending subscriptions retain the existing bounded wait.
                    if not flight.started and monotonic() >= deadline:
                        raise BusyError("等待共用資料取得逾時，原查詢仍在執行。")
                    self.condition.wait(.1)
                if flight.error is not None:
                    if flight.diagnostic_emitted:
                        self.gateway.local.diagnostic_emitted = True
                    error = copy.copy(flight.error)
                    error.acquisition_receipt = {"id": flight.id, "source": "shared" if shared else "network",
                                                "producer_task_id": flight.task.get("task_id", "")}
                    raise error
                source = "shared" if shared and flight.source == "network" else flight.source
                self.stats[source] += int(source != "network")
                return Acquisition(copy.deepcopy(flight.value), source, flight.saved_at, copy.deepcopy(flight.assessment),
                                   flight.id, flight.task.get("task_id", ""))
        finally:
            with self.condition:
                flight.subscribers.pop(subscriber, None)
                flight.defer_probes.pop(subscriber, None)
                flight.priority = any(flight.subscribers.values())
                if not flight.subscribers and not flight.done:
                    flight.cancel.set()
                self.condition.notify_all()

    def visits(self, mrn, *, force=False, readiness=None):
        from .analysis_fetch import clean, model, validate_visits

        store, db = self.app.analysis.store, self.app.review.db
        def load():
            values = []
            document = db.get("visits", mrn, required=False)
            step = store.step(mrn, "visits")
            if any(saved and saved.get("assessment", {}).get("complete") is False for saved in (document, step)):
                return None
            for value, saved in ((document.get("cases"), document.get("updated_at")) if document else (None, ""),
                                 (step["payload"], step["saved_at"]) if step else (None, "")):
                if value is not None and fresh(saved, 300):
                    if step and value is step["payload"] and step.get("assessment", {}).get("complete") is False:
                        continue
                    validate_visits(value, mrn)
                    assessment = document.get("assessment", {}) if document and value is document.get("cases") else step.get("assessment", {}) if step else {}
                    values.append((value, saved, assessment))
            return max(values, key=lambda value: value[1]) if values else None
        def fetch():
            from vghks_sdk import assess_data

            with self.app.sdk_factory(self.app.settings) as sdk:
                if readiness:
                    readiness.ensure(sdk, "prq")
                value = sdk.records.get_visit_cases(mrn)
            if not isinstance(value, list) or any(not isinstance(case, VisitCase) or case.patient_mrn != mrn for case in value):
                raise ValueError("就診索引病人不符或格式不正確。")
            return Acquisition(clean(value), "network", timestamp(), clean(assess_data(value)))
        def save(value, _saved, assessment):
            with self.app.store.library.batch():
                document = db.save("visits", {"id": mrn, "mrn": mrn, "cases": value, "assessment": assessment})
                store.save_step(mrn, "visits", "visits", value, self.app.username, assessment=assessment)
            return value, document["updated_at"]
        result = self.acquire(("visits",), mrn, "visits", load=load, fetch=fetch, save=save, force=force)
        # Validate old cache before use as well; never infer identity from its key.
        validate_visits(result.value, mrn)
        for value in result.value:
            model(VisitCase, value)
        return result

    def case_numeric(self, mrn, case, *, force=False, readiness=None):
        from .analysis_fetch import clean, validate_case_report
        from .review import case_key

        if not isinstance(case, VisitCase) or case.patient_mrn != mrn:
            raise ValueError("單次數值報告就診識別不符。")
        store, db = self.app.analysis.store, self.app.review.db
        key, record_id = "numeric-case:" + digest(case.identity), case_key(case)
        def load():
            value = self.cached_numeric(self.app, mrn, case)
            return (value.value, value.saved_at, value.assessment) if value else None
        def fetch():
            from vghks_sdk import assess_data

            with self.app.sdk_factory(self.app.settings) as sdk:
                if readiness:
                    readiness.ensure(sdk, "prq")
                report = sdk.records.get_numeric_report(case)
            if report.case.identity != case.identity or report.case.patient_mrn != mrn:
                raise ValueError("數值報告就診不符。")
            if self._case_selector(report.case) != self._case_selector(case):
                raise ValueError("數值報告完整就診參照不符。")
            value = clean(report)
            validate_case_report(value, mrn, case)
            value["query_selector_sha256"] = digest(self._case_selector(case))
            return Acquisition(value, "network", timestamp(), clean(assess_data(report)))
        def save(value, _saved, assessment):
            with self.app.store.library.batch():
                store.save_step(mrn, key, "numeric", value, self.app.username, assessment=assessment)
                document = db.save("numeric", {"id": record_id, "mrn": mrn, "record_id": record_id,
                    "status": "ready", "payload": value, "cached": False, "assessment": assessment})
            return value, document["updated_at"]
        return self.acquire(("case_numeric", digest(self._case_selector(case))), mrn, "numeric",
                            load=load, fetch=fetch, save=save, force=force)

    @classmethod
    def cached_numeric(cls, app, mrn, case, *, allow_stale=False, allow_partial=False):
        """One local-only compatibility reader, with exactly the live cache guards."""
        from .analysis_fetch import validate_case_report
        from .review import case_key

        if not isinstance(case, VisitCase) or case.patient_mrn != mrn or not case.visit_date:
            raise ValueError("單次數值報告就診識別不符。")
        record_id = case_key(case)
        step = app.analysis.store.step(mrn, "numeric-case:" + digest(case.identity))
        document = app.review.db.get("numeric", record_id, required=False)
        if document and (document.get("mrn") != mrn or document.get("record_id") != record_id):
            return None
        if not allow_partial and any(saved and saved.get("assessment", {}).get("complete") is False for saved in (document, step)):
            return None
        values = []
        for value, saved, assessment in (
                (step["payload"], step["saved_at"], step.get("assessment", {})) if step else (None, "", {}),
                (document.get("payload"), document.get("updated_at"), document.get("assessment", {})) if document else (None, "", {})):
            if value is None or not (allow_stale or current_cache(case.visit_date.isoformat(), saved)):
                continue
            try:
                validate_case_report(value, mrn, case)
                valid = cls._same_case(value.get("case", {}), case, value.get("query_selector_sha256"))
            except (TypeError, ValueError, KeyError):
                continue
            if valid:
                values.append(Acquisition(copy.deepcopy(value), "cache", saved, copy.deepcopy(assessment)))
        return max(values, key=lambda value: value.saved_at) if values else None

    def soap(self, mrn, case, member=None, *, force=False, readiness=None):
        from .analysis_fetch import clean
        from .review import case_key
        from .soap_data import snapshot

        if not isinstance(case, VisitCase) or case.patient_mrn != mrn or not case.visit_date:
            raise ValueError("SOAP 就診識別不符。")
        library, record_id = self.app.store.library, case_key(case)
        member = copy.deepcopy(member or {})
        task_id = getattr(self.gateway.local, "task", {}).get("task_id", "")
        def load():
            record = library.get_record(record_id, include_versions=False)
            if record and record.get("mrn") == mrn and current_cache(case.visit_date.isoformat(), record["updated_at"]):
                if (record.get("source_mrn") or mrn, record.get("case_no"), record.get("date"), record.get("section_code")) != (
                        case.mrn, case.case_no, case.visit_date.isoformat(), case.section_code):
                    raise ValueError("已存 SOAP 就診識別不符。")
                if any(expected not in (None, "") and record.get(field) != expected for field, expected in (
                        ("case_index", case.index), ("doctor_card", case.doctor_card), ("doctor", case.doctor_name),
                        ("section", case.section_name))):
                    return None
                if record.get("query_selector_sha256") != digest(self._case_selector(case)):
                    if record.get("query_selector_sha256") or self._case_selector(case).get("detail_params"):
                        return None
                return {"record": record, "empty": False}, record["updated_at"]
            return None
        def fetch():
            with self.app.sdk_factory(self.app.settings) as sdk:
                if readiness:
                    readiness.ensure(sdk, "prq")
                soap = sdk.records.get_soap(case)
            if not isinstance(soap, SoapRecord) or soap.case.identity != case.identity or soap.case.patient_mrn != mrn:
                raise ParseError("SOAP identity mismatch", code="BOT_SOAP_CASE_MISMATCH")
            if self._case_selector(soap.case) != self._case_selector(case):
                raise ParseError("SOAP complete case reference mismatch", code="BOT_SOAP_CASE_MISMATCH")
            if not soap.full_text.strip():
                return {"record": None, "empty": True}
            record = {key: member.get(key, "") for key in ("name", "sex", "age", "birthday", "birthday_raw")}
            record.update(id=record_id, mrn=mrn, date=case.visit_date.isoformat(), section=case.section_name,
                section_code=case.section_code, case_no=case.case_no, source_mrn=case.mrn, case_index=case.index,
                doctor=case.doctor_name, doctor_card=case.doctor_card, updated_at=timestamp(),
                query_selector_sha256=digest(self._case_selector(case)), **snapshot(soap))
            return {"record": clean(record), "empty": False}
        def save(value, saved, _assessment):
            if value["empty"]:
                return value, saved
            library.save_record(value["record"], self.app.username, task_id)
            record = library.get_record(record_id, include_versions=False)
            return {"record": record, "empty": False}, record["updated_at"]
        epoch = self._epoch(mrn, "soap")
        result = self.acquire(("soap", digest(self._case_selector(case))), mrn, "soap",
                              load=load, fetch=fetch, save=save, force=force)
        if not result.value["empty"] and task_id:
            with self.condition:
                if epoch == self._epoch(mrn, "soap"):
                    with library.connect() as db:
                        db.execute("INSERT OR IGNORE INTO sources SELECT id,?,? FROM records WHERE id=? AND mrn=?",
                                   (self.app.username, task_id, record_id, mrn))
        return result

    @staticmethod
    def _case_selector(case):
        value = to_jsonable(case)
        value["lookup_mrn"] = case.patient_mrn
        if value.get("detail_params"):
            value["detail_params"] = {key: item for key, item in value["detail_params"].items()
                if key.casefold() not in {"hid", "ssid", "keyone", "keytwo", "keythree", "password", "cookie"}}
        return value

    @classmethod
    def _same_case(cls, value, case, fingerprint=None):
        from .analysis_fetch import model

        try:
            actual, expected = cls._case_selector(model(VisitCase, value)), cls._case_selector(case)
            if fingerprint:
                if fingerprint != digest(expected):
                    return False
                # clean() intentionally omits transient detail parameters. The
                # fingerprint attests those only after fetch validated the full
                # returned case; every persisted observable field still agrees.
                actual_detail, expected_detail = actual.pop("detail_params", None), expected.pop("detail_params", None)
                return actual == expected and (not actual_detail or actual_detail == expected_detail)
            return actual == expected
        except (ValueError, TypeError, KeyError):
            return False

    def branch(self, mrn, cls, method, reference, *, verified_cases=(), force=False, fast_attachment=False, readiness=None):
        """Share only reviewed report units; keep pacing and alias proofs distinct."""
        from vghks_sdk import assess_data

        from .analysis_fetch import clean, model, validate_patient, validate_visits
        from .encoding import report_attachment_downloads
        from .report_cache import branch_complete

        allowed_methods = {"get_order_detail", "get_order_report", "get_pacs_study", "download_pdf", "download_pacs_image"}
        if method not in allowed_methods:
            raise ValueError("此查詢不支援共用取得。")
        cases = clean(list(verified_cases))
        validate_visits(cases, mrn)
        allowed = {mrn, *(case["mrn"] for case in cases)}
        source = reference.get("mrn", "")
        if source not in allowed:
            raise ValueError("報告參照病歷號未經就診清單核對。")
        validate_patient(reference, source)
        ref = model(cls, reference)  # SDK constructors retain endpoint/path validation.
        binary = method in {"download_pdf", "download_pacs_image"}
        if fast_attachment and not binary:
            raise ValueError("只有報告附件可使用指定下載節奏。")
        key = method + ":" + digest(reference)
        store = self.app.analysis.store
        def load():
            candidates = []
            for saved in (store.step(mrn, key), store.step(mrn, "history-asset:" + key)):
                if not saved:
                    continue
                if binary:
                    try:
                        store.asset(saved["payload"]["digest"])
                    except (KeyError, ValueError):
                        continue
                elif not branch_complete(saved["payload"], saved.get("assessment")):
                    continue
                else:
                    validate_patient(saved["payload"], source)
                    returned = saved["payload"].get("reference") if isinstance(saved["payload"], dict) else None
                    if not isinstance(returned, dict):
                        continue
                    try:
                        if to_jsonable(model(cls, returned)) != to_jsonable(ref):
                            continue
                    except (SDKError, TypeError, ValueError, KeyError):
                        continue
                candidates.append((saved["payload"], saved["saved_at"], saved.get("assessment", {})))
            return max(candidates, key=lambda value: value[1]) if candidates else None
        def fetch():
            with report_attachment_downloads() if fast_attachment else nullcontext(), self.app.sdk_factory(self.app.settings) as sdk:
                if readiness:
                    readiness.ensure(sdk, "prq")
                value = getattr(sdk.orders, method)(ref)
            returned = getattr(value, "reference", None)
            if returned is not None and to_jsonable(returned) != to_jsonable(ref):
                raise ValueError("報告回傳完整參照不符。")
            assessment = clean(assess_data(value))
            if not binary:
                value = clean(value)
                validate_patient(value, source)
            return Acquisition(value, "network", timestamp(), assessment)
        def save(value, saved, assessment):
            with self.app.store.library.batch():
                if binary:
                    value = store.save_asset(mrn, key, value, self.app.username)
                else:
                    store.save_step(mrn, key, "report", value, self.app.username, assessment=assessment)
                store.save_step(mrn, "history-asset:" + key, "asset" if binary else "report", value,
                                self.app.username, assessment=assessment)
            return value, saved
        return self.acquire(("branch", method, digest(reference), digest(cases)), mrn, "orders",
                            load=load, fetch=fetch, save=save, force=force,
                            pacing="report_attachment" if fast_attachment else "normal")
