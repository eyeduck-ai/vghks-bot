"""One SDK session per account, serialized patient context and bounded concurrency."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from types import SimpleNamespace

from vghks_sdk import AuthenticationError
from vghks_sdk.core.errors import error_info
from vghks_sdk.identifiers import normalize_national_id
from vghks_sdk.parsing.prq import parse_patient_identity, require_patient_context

from .clinical_identity import verified_visit_cases
from .diagnostics import failure_details
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
    def slot(self):
        with self.condition:
            self.condition.wait_for(lambda: self.active < self.maximum)
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

    @contextmanager
    def task_context(self, task_id, kind):
        previous = getattr(self.local, "task", {})
        self.local.task = {"task_id": task_id, "task_kind": kind}
        try:
            yield
        finally:
            self.local.task = previous

    def record_diagnostic(self, value):
        self.local.diagnostic_emitted = True
        if self.diagnostic:
            return self.diagnostic({**getattr(self.local, "task", {}),
                "sdk_run_id": self.recorder.run_id if self.recorder else "", **value})

    def record_failure(self, exc, service, method, args=()):
        if getattr(self.local, "diagnostic_emitted", False):
            return
        first = args[0] if args else None
        mrn = getattr(first, "mrn", "") or getattr(getattr(first, "order", None), "mrn", "")
        if isinstance(first, str) and service in {"records", "orders", "patients"}:
            mrn = first
        visit = {k: str(getattr(first, k)) for k in ("visit_date", "case_no", "case_type", "section_code")
                 if getattr(first, k, None) is not None}
        return self.record_diagnostic({"mrn": mrn, "phase": f"{service}.{method}", "visit": visit,
            "error": failure_details(exc), "recovered": False})

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
        with self.condition:
            if front:
                self.foreground_waiters += 1
                self.condition.notify_all()
            else:
                self.condition.wait_for(lambda: not self.foreground_waiters)
        self.lock.acquire()
        if front:
            with self.condition:
                self.foreground_waiters -= 1
                self.condition.notify_all()
        try:
            with self.gate.slot():
                yield
        finally:
            self.lock.release()

    def login(self, settings):
        with self.serial():
            self._close()
            self.settings = settings
            self.local.diagnostic_emitted = False
            try:
                self.manager = self.factory(settings)
                runtime = getattr(self.manager, "_runtime", None)
                if runtime is not None and self.recorder_factory:
                    self.recorder = self.recorder_factory()
                    runtime.diagnostics = runtime.transport.diagnostics = self.recorder
                self.connection = self.manager.__enter__()
                if not self.connection.auth.check(only=("prq",)).ok:
                    raise ValueError("院內登入失敗，請確認帳號及密碼。")
                self.online = True
            except Exception as exc:
                self.record_failure(exc, "auth", "login")
                self._close(error=exc)
                raise

    def _close(self, error=None):
        self.online = False
        try:
            if self.manager:
                self.manager.__exit__(None, None, None)
        finally:
            self.manager = self.connection = self.settings = self.context = None
            if self.recorder:
                recorder, self.recorder = self.recorder, None
                recorder.finalize(command="bot_session", status="ERROR" if error else "CLOSED",
                                  exit_code=1 if error else 0, error=error)

    def close(self):
        with self.lock:
            self._close()

    @contextmanager
    def lease(self, _settings):
        if not self.online:
            raise ValueError("帳號目前離線，請先登入再執行網路任務。")
        yield SimpleNamespace(**{service: ServiceProxy(self, service) for service in (
            "auth", "opd", "records", "patients", "orders", "surgery", "reviews", "earnings")})

    def invoke(self, service, method, *args, **kwargs):
        with self.serial():
            if not self.online:
                raise ValueError("帳號已登出；請登入後續跑。")
            self.local.diagnostic_emitted = False
            try:
                # Foreground calls can change PRQ's patient context between two
                # background steps. Restore it before reading any case/report.
                mrn = getattr(args[0], "mrn", None) if args else None
                if mrn and service in {"records", "orders"} and self.context != mrn:
                    verified_visit_cases(self.connection, mrn, self.record_diagnostic)
                    self.context = mrn
                    self.local.diagnostic_emitted = False
                if service == "records" and method == "get_visit_cases" and args and isinstance(args[0], str):
                    value = verified_visit_cases(self.connection, args[0], self.record_diagnostic)
                else:
                    value = getattr(getattr(self.connection, service), method)(*args, **kwargs)
                # Stamp inside the serialized SDK call, before another worker
                # can fetch a newer response. Each caller keeps its own stamp.
                self.local.response_at = timestamp()
                if service == "records" and method in {"get_visit_cases", "find_visit_cases"}:
                    self.context = args[0] if args else None
                elif service in {"records", "orders"} and args and isinstance(args[0], str):
                    self.context = args[0]
                elif service in {"patients", "auth", "opd", "surgery", "reviews", "earnings"}:
                    self.context = None
                return value
            except AuthenticationError as exc:
                self.record_failure(exc, service, method, args)
                if service != "earnings" or not error_info(exc).code.startswith("EARNINGS_"):
                    self.online = False
                self.context = None
                raise
            except Exception as exc:
                self.record_failure(exc, service, method, args)
                self.context = None
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
        with self.serial():
            if not self.online:
                raise ValueError("請先登入。")
            self.local.diagnostic_emitted = False
            try:
                mrn = self._resolve_identity(identifier)
                self.context = mrn
                return mrn
            except Exception as exc:
                self.context = None
                if isinstance(exc, AuthenticationError):
                    self.online = False
                self.record_failure(exc, "patients", "resolve_identity")
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
