"""Validate patient identity; SDK owns session checks and recovery."""
from dataclasses import asdict

from vghks_sdk import NotFoundError, ParseError
from vghks_sdk.core.errors import error_info

from .connection_state import require_ready
from .settings import timestamp

MESSAGES = {
    "PATIENT_NOT_FOUND": "登入已確認，此院區查無該病歷號；請核對病歷號與院區。",
    "PATIENT_LOOKUP_UNCONFIRMED": "院方回應無法確認病人基本資料；尚不能判定病歷號不存在，可至爬蟲紀錄查看 DEBUG。",
    "DEMOGRAPHICS_PATIENT_MISMATCH": "院方回傳的病歷號與查詢不符，這筆基本資料未保存；請核對 DEBUG。",
    "DEMOGRAPHICS_PERMISSION_DENIED": "院方拒絕讀取病人基本資料，請確認此帳號的院內權限。",
}


def check_session(connection):
    """Use the public SDK readiness check without an additional page probe."""
    return require_ready(connection.auth.check(only=("webmaas",)))


def validate_demographics(value, mrn):
    field = value.get if isinstance(value, dict) else lambda key, default="": getattr(value, key, default)
    if field("mrn") != mrn:
        raise ParseError("patient identity did not match", code="DEMOGRAPHICS_PATIENT_MISMATCH")
    if not isinstance(field("name"), str) or not field("name").strip():
        raise ParseError("patient demographics were incomplete", code="DEMOGRAPHICS_INCOMPLETE")


def get_demographics(gateway, mrn, *args, **kwargs):
    """Call the SDK directly; only a confirmed empty result needs a second read."""
    pending = []

    def remember(exc, phase="patients.get_demographics"):
        from .diagnostics import failure_details

        recorder = gateway.recorder
        pending.append({"mrn": mrn, "phase": phase,
            "query": {"phase": phase, "label": "病人基本資料" if phase.startswith("patients.") else "基本資料登入檢查"},
            "session_id": gateway.session_id, "sdk_run_id": recorder.run_id if recorder else "",
            "error": failure_details(exc), "transport": recorder.failure_transport(exc) if recorder else {}})

    def emit(recovered, exc=None):
        for row in pending:
            gateway.record_diagnostic({**row, "recovered": recovered,
                "outcome": asdict(error_info(exc)) if exc else {}})

    try:
        for attempt in range(2):
            try:
                value = gateway.connection.patients.get_demographics(mrn, *args, **kwargs)
                validate_demographics(value, mrn)
                break
            except NotFoundError as exc:
                remember(exc)
                # SDK NotFound also means that a nonempty response contained
                # no matching MRN. Never treat another patient's rows as empty.
                empty = gateway.recorder.demographics_empty() if gateway.recorder else True
                if error_info(exc).code != "WEBMAAS_PATIENT_NOT_FOUND" or not empty:
                    raise ParseError("patient response did not confirm an empty result",
                        code="PATIENT_LOOKUP_UNCONFIRMED", cause=error_info(exc)) from exc
                if attempt == 0:
                    try:
                        check_session(gateway.connection)
                    except Exception as check_error:
                        remember(check_error, "auth.check")
                        raise
                    continue
                raise NotFoundError("authenticated patient lookup was empty", code="PATIENT_NOT_FOUND",
                                    cause=error_info(exc)) from exc
            except ParseError as exc:
                remember(exc)
                if error_info(exc).code == "DEMOGRAPHICS_INCOMPLETE":
                    raise ParseError("patient lookup could not be confirmed",
                                     code="PATIENT_LOOKUP_UNCONFIRMED", cause=error_info(exc)) from exc
                raise
            except Exception as exc:
                remember(exc)
                raise
    except Exception as exc:
        gateway.context = None
        gateway.observe_auth()
        gateway.note_failure(exc, "patients")
        emit(False, exc)
        gateway.record_event("patients", "get_demographics", "error", exc)
        raise
    gateway.context = None
    gateway.local.response_at = timestamp()
    emit(True)
    gateway.record_result("patients", "get_demographics", value)
    return value
