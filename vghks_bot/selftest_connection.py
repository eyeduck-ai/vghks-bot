"""Frozen-runtime checks for SDK acquisition metadata and session decisions."""
from types import SimpleNamespace

from vghks_sdk import AuthenticationError, RequestError, assess_data
from vghks_sdk.core.errors import error_info
from vghks_sdk.models import PasswordStatus

from .connection_state import failure_state, readiness_error
from .diagnostics import failure_details, failure_summary


def check_connection_contract(workspace):
    inner = RequestError("synthetic", code="NETWORK_DNS_FAILED", retry_safe=True, retry_recommended=True)
    outer = AuthenticationError("synthetic", code="AUTH_RELOGIN_FAILED",
                                phase="REAUTHENTICATION", cause=error_info(inner))
    report = SimpleNamespace(targets=(SimpleNamespace(status="ERROR", issue=error_info(outer)),))
    info = error_info(readiness_error(report))
    assert info.root_cause.code == "NETWORK_DNS_FAILED" and info.retry_safe is False
    decision = failure_state(info)
    assert decision["action"] == "network" and not decision["auto_reconnect"]
    summary = failure_summary({"error": failure_details(outer), "phase": "patients.get_demographics"})
    assert summary["root_cause"]["code"] == "NETWORK_DNS_FAILED" and "DNS" in summary["reason"]
    assert assess_data([]).availability == "EMPTY" and assess_data({}).availability == "UNKNOWN"
    gateway = workspace.gateway
    old = getattr(gateway.connection.auth, "password_status", None)
    gateway.connection.auth.password_status = PasswordStatus("EXPIRING", 3, "VISIBLE_TEXT")
    try:
        gateway.record_result("orders", "synthetic_empty", [])
        assert gateway.online and gateway.password_status["remaining_days"] == 3
        assert gateway.local.assessment["availability"] == "EMPTY"
    finally:
        if old is None:
            del gateway.connection.auth.password_status
        else:
            gateway.connection.auth.password_status = old
        gateway.password_status = {}
