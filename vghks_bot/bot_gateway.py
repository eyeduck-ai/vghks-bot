"""One SDK session per account, serialized patient context and bounded concurrency."""
from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from time import monotonic
from types import SimpleNamespace

from vghks_sdk import AuthenticationError, NotAuthenticatedError, assess_data
from vghks_sdk.core.errors import SDKError, error_info
from vghks_sdk.identifiers import normalize_national_id
from vghks_sdk.models import to_jsonable
from vghks_sdk.parsing.prq import parse_patient_identity, require_patient_context

from .clinical_identity import verified_visit_cases
from .connection_state import failure_state, readiness_error, require_ready, stored_error_info
from .diagnostics import failure_details, operation_query_context
from .jobs import BusyError
from .patient_lookup import get_demographics
from .scanner import Cancelled, safe_failure
from .settings import timestamp


class NetworkGate:
    def __init__(self, maximum=3):
        self.maximum, self.active = maximum, 0
        self.condition = threading.Condition()

    def set_limit(self, value):
        if type(value) is not int or not 1 <= value <= 4:
            raise ValueError("同時連線帳號數需為 1–4。")
        with self.condition:
            self.maximum = value
            self.condition.notify_all()

    @contextmanager
    def slot(self, *, deadline=None, cancel=None):
        with self.condition:
            deadline = deadline if deadline is not None else monotonic() + 30
            while self.active >= self.maximum:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise BusyError("等待院內連線名額逾時，請稍後重試；目前查詢仍在執行。")
                self.condition.wait(min(remaining, .2))
            self.active += 1
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()


class AccountGateway:
    def __init__(self, factory, gate):
        self.factory, self.gate = factory, gate
        self.lock = threading.RLock()
        self.condition = threading.Condition()
        self.foreground_waiters = 0
        self.local = threading.local()
        self.connection = self.manager = self.settings = None
        self.online = False
        self.context = None
        self.diagnostic = None
        self.recorder_factory = self.recorder = None
        self.event = None
        self.session_id = ""
        self.recovery_count = 0
        self.last_error_code = ""
        self.connection_issue = {}
        self.connection_error = None
        self.password_status = {}
        self.auth_generation = 0
        self.wait_timeout = 30.0

    @contextmanager
    def task_context(self, task_id, kind, cancel=None):
        previous = getattr(self.local, "task", {})
        previous_cancel = getattr(self.local, "cancel", None)
        self.local.task = {"task_id": task_id, "task_kind": kind}
        self.local.cancel = cancel
        try:
            yield
        finally:
            self.local.task = previous
            self.local.cancel = previous_cancel

    def record_diagnostic(self, value):
        self.local.diagnostic_emitted = True
        if self.diagnostic:
            return self.diagnostic({**getattr(self.local, "task", {}),
                "session_id": self.session_id, "sdk_run_id": self.recorder.run_id if self.recorder else "", **value})

    def record_failure(self, exc, service, method, args=(), kwargs=None):
        if getattr(self.local, "diagnostic_emitted", False):
            return
        first = args[0] if args else None
        mrn = getattr(first, "mrn", "") or getattr(getattr(first, "order", None), "mrn", "")
        if isinstance(first, str) and service in {"records", "orders", "patients"}:
            mrn = first
        visit = {k: str(getattr(first, k)) for k in ("visit_date", "case_no", "case_type", "section_code")
                 if getattr(first, k, None) is not None}
        transport = self.recorder.failure_transport(exc) if self.recorder else {}
        if self.recorder:
            self.recorder.consume_incidents()
        return self.record_diagnostic({"mrn": mrn, "phase": f"{service}.{method}", "visit": visit,
            "query": operation_query_context(service, method, args, kwargs or {}), "transport": transport,
            "error": failure_details(exc), "recovered": False})

    def record_event(self, service, method, status, exc=None, assessment=None):
        if exc:
            self.note_failure(exc, service)
            if self.recorder:
                self.recorder.consume_incidents()
        if not self.event:
            return
        message, code = safe_failure(exc) if exc else ("", "")
        if exc and error_info(exc).category == "NOT_FOUND":
            assessment = {"availability": "NOT_FOUND", "item_count": 0, "complete": True,
                          "issues": [], "warnings": []}
        try:
            self.event(session_id=self.session_id, task_id=getattr(self.local, "task", {}).get("task_id", ""),
                       service=service, method=method, status=status, error_code=code, message=message,
                       assessment=assessment or {})
        except Exception:
            # An audit write must not turn a successful hospital response into a
            # failed clinical task. The task and SDK recorder remain available.
            pass

    def note_failure(self, exc, service=""):
        info = error_info(exc)
        if service == "earnings" and info.code.startswith("EARNINGS_"):
            return
        state = failure_state(info)
        if state["pause"]:
            self.connection_error = info
            self.last_error_code = info.code
            self.connection_issue = state
            if info.category in {"AUTHENTICATION", "NETWORK"}:
                self.online = False

    def require_online(self):
        if not self.online:
            if self.connection_error is not None:
                exc = SDKError("account queries are paused")
                exc.info = self.connection_error
                raise exc
            raise NotAuthenticatedError("account login is unavailable")

    def observe_auth(self, report=None):
        auth = getattr(self.connection, "auth", None)
        notice = getattr(report, "password_status", None) or getattr(auth, "password_status", None)
        if notice is not None and hasattr(notice, "status"):
            self.password_status = to_jsonable(notice)
        runtime = getattr(self.connection, "_runtime", None)
        generation = getattr(getattr(runtime, "auth", None), "generation", 0)
        if type(generation) is int:
            if self.auth_generation and generation > self.auth_generation:
                self.recovery_count += generation - self.auth_generation
            self.auth_generation = generation

    def record_result(self, service, method, value):
        self.observe_auth(value if service == "auth" and method == "check" else None)
        data = assess_data(value)
        status = "partial" if data.complete is False else "empty" if data.availability == "EMPTY" else "ok"
        self.local.assessment = to_jsonable(data)
        self.record_event(service, method, status, assessment=self.local.assessment)
        if status == "partial" and self.diagnostic:
            previous = getattr(self.local, "diagnostic_emitted", False)
            try:
                first = data.issues[0] if data.issues else None
                exc = SDKError("partial parsed result retained", category="PARSE",
                               code=first.code if first else "ACQUISITION_PARTIAL", phase="PARSE")
                self.record_diagnostic({"phase": f"{service}.{method}", "error": failure_details(exc),
                                        "assessment": self.local.assessment, "recovered": False,
                                        "transport": self.recorder.failure_transport(exc) if self.recorder else {}})
            finally:
                self.local.diagnostic_emitted = previous
        self._trace_incidents(recovered=status != "partial")
        if self.recorder:
            self.recorder.release_success()

    def _trace_incidents(self, *, recovered):
        if not self.recorder:
            return
        context = getattr(self.local, "trace_context", {})
        for incident in self.recorder.consume_incidents():
            exc = SDKError("SDK read retried after an observed failure", code=incident["code"],
                           category=incident["category"], operation=incident["operation"], retry_safe=True,
                           http_status=incident.get("http_status"),
                           cause=stored_error_info(incident["cause"]) if incident.get("cause") else None)
            if recovered and incident.get("application_recovery"):
                self.recovery_count += 1
            previous = getattr(self.local, "diagnostic_emitted", False)
            try:
                service, method = context.get("service", "auth"), context.get("method", "login")
                self.record_diagnostic({"mrn": getattr(self.local, "trace_mrn", ""),
                    "phase": f"{service}.{method}", "error": failure_details(exc),
                    "transport": incident["transport"], "recovered": recovered,
                    "interaction_id": context.get("interaction_id", ""), "sdk_recovery": True})
            finally:
                self.local.diagnostic_emitted = previous

    @contextmanager
    def trace_query(self, service, method, args):
        previous = getattr(self.local, "trace_context", {})
        previous_mrn = getattr(self.local, "trace_mrn", "")
        context = {**getattr(self.local, "task", {}), "interaction_id": uuid.uuid4().hex,
                   "service": service, "method": method}
        first = args[0] if args else None
        mrn = getattr(first, "mrn", "") or getattr(getattr(first, "order", None), "mrn", "")
        if isinstance(first, str) and (service in {"records", "orders"} or
                                      service == "patients" and method == "get_demographics"):
            mrn = first
        self.local.trace_context, self.local.trace_mrn = context, mrn
        if self.recorder:
            self.recorder.set_context(context)
        try:
            yield
        finally:
            self.local.trace_context, self.local.trace_mrn = previous, previous_mrn
            if self.recorder:
                self.recorder.set_context(previous)
                self.recorder.release_success()

    @contextmanager
    def foreground(self):
        self.local.foreground = True
        try:
            yield
        finally:
            self.local.foreground = False

    @contextmanager
    def serial(self):
        front = getattr(self.local, "foreground", False)
        cancel = getattr(self.local, "cancel", None)
        deadline = monotonic() + self.wait_timeout
        acquired, registered = False, False
        def check_wait():
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if monotonic() >= deadline:
                raise BusyError("此帳號仍有查詢占用連線，等待逾時；請稍後重試或先暫停該任務。")
        try:
            with self.condition:
                if front:
                    self.foreground_waiters += 1
                    registered = True
                    self.condition.notify_all()
                else:
                    while self.foreground_waiters:
                        check_wait()
                        self.condition.wait(min(.2, max(0, deadline - monotonic())))
            while not acquired:
                check_wait()
                acquired = self.lock.acquire(timeout=min(.2, max(0, deadline - monotonic())))
            if registered:
                with self.condition:
                    self.foreground_waiters -= 1
                    registered = False
                    self.condition.notify_all()
            with self.gate.slot(deadline=deadline, cancel=cancel):
                yield
        finally:
            if registered:
                with self.condition:
                    self.foreground_waiters -= 1
                    self.condition.notify_all()
            if acquired:
                self.lock.release()

    def login(self, settings):
        with self.serial():
            self._close()
            self.session_id = uuid.uuid4().hex
            self.local.diagnostic_emitted = False
            try:
                self._connect_locked(settings)
                self.record_event("auth", "login", "ok")
            except Exception as exc:
                self.record_failure(exc, "auth", "login")
                self.record_event("auth", "login", "error", exc)
                self._close(error=exc)
                raise

    def _connect_locked(self, settings):
        self.settings = settings
        self.manager = self.factory(settings)
        runtime = getattr(self.manager, "_runtime", None)
        if runtime is not None and self.recorder_factory:
            self.recorder = self.recorder_factory()
            self.recorder.response_encoding = settings.response_encoding
            self.recorder.set_context(getattr(self.local, "trace_context", {}))
            runtime.diagnostics = runtime.transport.diagnostics = self.recorder
        self.connection = self.manager.__enter__()
        report = self.connection.auth.check(only=("prq",))
        self.observe_auth(report)
        require_ready(report)
        self.online = True
        self._trace_incidents(recovered=True)
        self.last_error_code = ""
        self.connection_issue = {}
        self.connection_error = None

    def _reconnect_locked(self):
        settings = self.settings
        if not settings or not settings.password:
            raise AuthenticationError("active login is unavailable", code="AUTH_RELOGIN_FAILED")
        self._close()
        self.session_id = uuid.uuid4().hex
        context = getattr(self.local, "trace_context", {})
        if context:
            context["recovery"] = True
        try:
            self._connect_locked(settings)
            self.recovery_count += 1
            self.record_event("auth", "reconnect", "ok")
        except Exception as exc:
            self.record_failure(exc, "auth", "reconnect")
            self.record_event("auth", "reconnect", "error", exc)
            self._close(error=exc)
            raise

    @staticmethod
    def _can_recover(service, method, exc, *, sdk_managed_prq=False):
        # A real SDK runtime retries safe PRQ reads itself. PRQ reads can
        # conditionally submit an access decision, so an outer gateway retry
        # could replay a write whose outcome is uncertain. Synthetic SDKs have
        # no runtime and still need the gateway's one-time reconnect.
        if sdk_managed_prq and service in {"records", "orders"}:
            return False
        if not (method.startswith(("get_", "find_")) or
                service == "orders" and method in {"download_pdf", "download_pacs_image"} or
                service == "patients" and method == "resolve_identity" or
                service == "auth" and method == "check"):
            return False
        info = error_info(exc)
        return info.category == "AUTHENTICATION" and info.retry_safe is True and info.code in {
            "AUTH_EXPIRED", "AUTH_SESSION_REDIRECT",
            "AUTH_SESSION_LOGIN_PAGE", "AUTH_SESSION_LOGIN_FORM"}

    @staticmethod
    def _failed_readiness(service, method, value, args, kwargs):
        if service != "auth" or method != "check" or getattr(value, "ok", True):
            return False
        targets = kwargs.get("only", args[0] if args else ())
        return not targets or tuple(targets) != ("earnings",)

    def _close(self, error=None):
        self.online = False
        self.auth_generation = 0
        if error is None:
            self.connection_error = None
            self.connection_issue = {}
            self.last_error_code = ""
            self.password_status = {}
        try:
            if self.manager:
                self.manager.__exit__(None, None, None)
        finally:
            self.manager = self.connection = self.settings = self.context = None
            if self.recorder:
                recorder, self.recorder = self.recorder, None
                recorder.finalize(command="bot_session", status="ERROR" if error else "CLOSED",
                                  exit_code=1 if error else 0, error=error)

    def close(self, *, wait=False):
        acquired = self.lock.acquire() if wait else self.lock.acquire(timeout=self.wait_timeout)
        if not acquired:
            raise BusyError("此帳號的目前查詢尚未返回；已要求停止，請稍後再完成登出。")
        try:
            self._close()
        finally:
            self.lock.release()

    @contextmanager
    def lease(self, _settings):
        self.require_online()
        yield SimpleNamespace(**{service: ServiceProxy(self, service) for service in (
            "auth", "opd", "records", "patients", "orders", "surgery", "reviews", "earnings")})

    def invoke(self, service, method, *args, **kwargs):
        with self.serial(), self.trace_query(service, method, args):
            self.require_online()
            if service == "patients" and method == "get_demographics":
                self.local.diagnostic_emitted = False
                return get_demographics(self, *args, **kwargs)
            for attempt in range(2):
                self.local.diagnostic_emitted = False
                try:
                    # Restore PRQ's patient context after another task used it.
                    subject = args[0] if args else None
                    mrn = getattr(subject, "patient_mrn", None) or getattr(subject, "mrn", None)
                    if mrn and service in {"records", "orders"} and self.context != mrn:
                        verified_visit_cases(self.connection, mrn, self.record_diagnostic)
                        self.context = mrn
                    if service == "records" and method == "get_visit_cases" and args and isinstance(args[0], str):
                        value = verified_visit_cases(self.connection, args[0], self.record_diagnostic)
                    else:
                        value = getattr(getattr(self.connection, service), method)(*args, **kwargs)
                    if self._failed_readiness(service, method, value, args, kwargs):
                        self.observe_auth(value)
                        raise readiness_error(value)
                    self.local.response_at = timestamp()
                    if service == "records" and method in {"get_visit_cases", "find_visit_cases"}:
                        self.context = args[0] if args else None
                    elif service in {"records", "orders"} and args and isinstance(args[0], str):
                        self.context = args[0]
                    elif service in {"patients", "auth", "opd", "surgery", "reviews", "earnings"}:
                        self.context = None
                    self.record_result(service, method, value)
                    return value
                except SDKError as exc:
                    self.context = None
                    if (attempt == 0 and self.online and self._can_recover(
                            service, method, exc,
                            sdk_managed_prq=getattr(self.connection, "_runtime", None) is not None)
                            and getattr(self.connection, "_runtime", None) is None):
                        self.record_event(service, method, "error", exc)
                        try:
                            self._reconnect_locked()
                        except Exception as recovery:
                            self.record_diagnostic({"phase": f"{service}.{method}", "error": failure_details(exc),
                                                    "outcome": failure_details(recovery), "recovered": False})
                            raise
                        self.record_diagnostic({"phase": f"{service}.{method}", "error": failure_details(exc),
                                                "recovered": True})
                        continue
                    self.record_failure(exc, service, method, args, kwargs)
                    self.record_event(service, method, "empty" if error_info(exc).category == "NOT_FOUND" else "error", exc)
                    raise
                except Exception as exc:
                    self.context = None
                    self.record_failure(exc, service, method, args, kwargs)
                    self.record_event(service, method, "error", exc)
                    raise

    def response_at(self):
        return self.local.response_at

    def resolve_identity(self, national_id):
        """Expose the verified PRQ header even when its encounter list is empty.

        The pinned SDK resolves this header internally but drops the MRN when
        returning an empty list. Keep this compatibility bridge in one place;
        use the same SDK operations, auth, retries and identity parser.
        """
        identifier = normalize_national_id(national_id)
        with self.serial(), self.trace_query("patients", "resolve_identity", (identifier,)):
            self.require_online()
            for attempt in range(2):
                self.local.diagnostic_emitted = False
                try:
                    mrn = self._resolve_identity(identifier)
                    self.context = mrn
                    self.record_result("patients", "resolve_identity", mrn)
                    return mrn
                except Exception as exc:
                    self.context = None
                    if (attempt == 0 and self.online and isinstance(exc, SDKError)
                            and getattr(self.connection, "_runtime", None) is None
                            and self._can_recover("patients", "resolve_identity", exc)):
                        self.record_event("patients", "resolve_identity", "error", exc)
                        try:
                            self._reconnect_locked()
                        except Exception as recovery:
                            self.record_diagnostic({"phase": "patients.resolve_identity", "error": failure_details(exc),
                                                    "outcome": failure_details(recovery), "recovered": False})
                            raise
                        self.record_diagnostic({"phase": "patients.resolve_identity", "error": failure_details(exc),
                                                "recovered": True})
                        continue
                    self.record_failure(exc, "patients", "resolve_identity")
                    self.record_event("patients", "resolve_identity", "error", exc)
                    raise

    def _resolve_identity(self, identifier):
        sdk = self.connection
        if callable(getattr(sdk.patients, "resolve_identity", None)):
            return sdk.patients.resolve_identity(identifier)
        runtime = sdk._runtime
        from vghks_sdk.adapters.prq import _PATIENT_CONTEXT, _PATIENT_IDENTITY

        def operation():
            hid = runtime.auth.hid_for("prq")
            base = runtime.settings.prq_base_url.rstrip("/")
            html = runtime.request_text(_PATIENT_CONTEXT, f"{base}/QueryPatientRecord.do",
                params={"Use": "Case", "hid": hid},
                data={"id": identifier, "queryID": identifier, "queryPtID": "", "type": "2"})
            require_patient_context(html)
            header = runtime.request_text(_PATIENT_IDENTITY, f"{base}/Page/JSP/KS_Patient.jsp")
            return parse_patient_identity(header, expected_national_id=identifier)

        return runtime.execute(_PATIENT_CONTEXT, operation, operation_name="resolve_patient_identity")


class ServiceProxy:
    def __init__(self, gateway, service):
        self.gateway, self.service = gateway, service

    def __getattr__(self, method):
        return lambda *args, **kwargs: self.gateway.invoke(self.service, method, *args, **kwargs)
