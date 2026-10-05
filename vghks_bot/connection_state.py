"""Application decisions from SDK metadata; never infer failure from clinical text."""
from __future__ import annotations

from dataclasses import asdict, fields

from vghks_sdk import (
    ApplicationSessionExpiredError,
    AuthenticationError,
    AuthorizationError,
    LoginRejectedError,
    NotAuthenticatedError,
    ParseError,
    PasswordChangeRequiredError,
    RequestError,
)
from vghks_sdk.core.errors import ErrorInfo, SDKError, error_info

LOGIN_REJECTED = {"AUTH_LOGIN_REJECTED", "PORTAL_LOGIN_REJECTED", "PORTAL_LOGIN_HTTP_DENIED"}
PASSWORD_REQUIRED = "PORTAL_PASSWORD_CHANGE_REQUIRED"


def stored_error_info(value, depth=0):
    """Read safe metadata from old/new local diagnostics with a bounded cause chain."""
    value = value if isinstance(value, dict) else {}
    names = {field.name for field in fields(ErrorInfo)} - {"code", "category", "cause"}
    context = {key: value[key] for key in names if key in value}
    if depth < 7 and isinstance(value.get("cause"), dict):
        context["cause"] = stored_error_info(value["cause"], depth + 1)
    return ErrorInfo(value.get("code") or "SDK_ERROR", value.get("category") or "INTERNAL", **context)


def failure_state(error):
    info = error if isinstance(error, ErrorInfo) else error_info(error)
    root = info.root_cause
    chain, current = set(), info
    while current is not None and id(current) not in chain:
        chain.add(id(current))
        if current.code == PASSWORD_REQUIRED:
            action = "password_change"
            break
        if current.code in LOGIN_REJECTED:
            action = "credentials"
            break
        current = current.cause
    else:
        action = ("network" if root.category == "NETWORK" else
                  "login" if info.code == "AUTH_NOT_AUTHENTICATED" else
                  "reconnect" if info.category == "AUTHENTICATION" else
                  "permission" if info.category == "AUTHORIZATION" else
                  "service" if info.category == "HTTP" else
                  "lookup" if info.category == "NOT_FOUND" else
                  "inspect")
    pause = info.category in {"AUTHENTICATION", "NETWORK"} or (
        info.category == "HTTP" and info.http_status in {429, 500, 502, 503, 504}
    )
    if info.code == "EARNINGS_PASSWORD_REJECTED":
        pause = False
    return {"code": info.code, "category": info.category, "phase": info.phase,
            "root_cause": asdict(root), "retry_safe": info.retry_safe,
            "retry_recommended": info.retry_recommended, "action": action, "pause": pause,
            # A failed SDK recovery has already consumed its login attempt. An
            # outer read's retry flag is never permission to replay a login POST.
            "auto_reconnect": False}


def should_pause(exc):
    return failure_state(exc)["pause"]


def failure_message(error):
    info = error if isinstance(error, ErrorInfo) else error_info(error)
    state = failure_state(info)
    action, root = state["action"], info.root_cause
    if action == "password_change":
        return "院方要求變更密碼，查詢已暫停；請先至院方入口變更，再以新密碼重新連線。"
    if action == "credentials":
        return "院方未接受此次登入，查詢已暫停；請確認登入資料或院方限制後重新連線。"
    if action == "network":
        reasons = {
            "NETWORK_DNS_FAILED": "無法解析院內系統位址，請檢查院內網路、VPN 或 DNS。",
            "NETWORK_CONNECT_TIMEOUT": "院內連線建立逾時，請檢查院內網路或 VPN。",
            "NETWORK_READ_TIMEOUT": "院內系統回應逾時，請稍後再試。",
            "NETWORK_PROXY_FAILED": "代理伺服器連線失敗，請檢查代理與院內網路設定。",
            "TLS_VERIFY_FAILED": "院內連線憑證驗證失敗，請檢查憑證與連線設定。",
            "TLS_PROTOCOL_FAILED": "院內加密連線建立失敗，請檢查連線設定。",
        }
        text = reasons.get(root.code, "無法連線至院內系統，請檢查院內網路或 VPN。")
        context = ("恢復 WebMAAS 查詢連線時發生網路問題；" if info.code == "WEBMAAS_SSO_RECOVERY_FAILED" else
                   "重新連線時發生網路問題；" if info.category == "AUTHENTICATION" else "")
        return context + text
    if info.code == "AUTH_NOT_AUTHENTICATED":
        return "尚未建立院內登入，請先重新連線再續跑。"
    if info.code == "WEBMAAS_SESSION_TIMEOUT":
        reason = ("SDK 重建 WebMAAS 查詢連線後仍收到逾時頁面" if info.attempt == 2 else
                  "院方回報 WebMAAS 查詢連線逾時")
        return reason + "，查詢已暫停；請稍後續跑，必要時重新連線。"
    if info.code == "WEBMAAS_SSO_RECOVERY_FAILED":
        return "SDK 未能恢復 WebMAAS 查詢連線，查詢已暫停；請查看 DEBUG，再重新連線後續跑。"
    if (info.code == "AUTH_READINESS_INCOMPLETE" and root.code in {
            "WEBMAAS_QUERY_FORM_MISSING", "WEBMAAS_QUERY_TOKEN_MISSING"}):
        return "重新驗證後仍無法確認病人查詢頁面，查詢已暫停；尚不能判定病歷號不存在，請查看 DEBUG 或重新連線。"
    if info.code in {"AUTH_HTTP_DENIED", "AUTH_RELOGIN_FAILED", "AUTH_READINESS_INCOMPLETE", "AUTH_CHECK_FAILED"}:
        return "院內登入狀態未能恢復或確認，查詢已暫停；請重新連線後續跑。"
    if info.code == "AUTH_EXPIRED" or info.code.startswith("AUTH_SESSION_"):
        return "院內登入階段已失效，查詢已暫停；請重新連線後續跑。"
    if info.code == "PRQ_ACCESS_REVIEW_REQUIRED":
        return "院方要求病歷調閱審查，請確認可用的調閱原因與帳號權限。"
    if info.category == "HTTP":
        if info.http_status == 404:
            return "院內查詢端點回應 HTTP 404，尚不能判定病人或資料不存在；請查看 DEBUG。"
        if info.http_status == 429:
            return "院內系統限制查詢頻率，請稍後續跑。"
        return "院內系統暫時無法完成請求，請稍後再試。"
    return {
        "AUTHENTICATION": "院內登入狀態無法確認，請重新連線後續跑。",
        "AUTHORIZATION": "此帳號未取得院方讀取權限，請確認院內授權。",
        "NOT_FOUND": "此次查詢條件下查無資料，請核對查詢條件。",
        "PARSE": "院內回傳格式無法辨識，這筆資料尚未確認；已保存資料保留。",
        "CONFIGURATION": "查詢或連線設定有誤，請檢查設定。",
    }.get(info.category, "查詢未完成，請依錯誤代碼檢查。")


def readiness_error(report):
    """Recreate the typed error without dropping causes, phase or retry safety."""
    failed = next((t for t in getattr(report, "targets", ()) if t.status == "ERROR"), None)
    issue = getattr(failed, "issue", None)
    if not isinstance(issue, ErrorInfo):
        return AuthenticationError("readiness was not confirmed", code="AUTH_READINESS_INCOMPLETE")
    if issue.category in {"NETWORK", "HTTP"}:
        exc = RequestError("hospital readiness failed", status_code=issue.http_status)
        exc.info = issue
        return exc
    kind = (PasswordChangeRequiredError if issue.code == PASSWORD_REQUIRED else
            ApplicationSessionExpiredError if issue.code == "WEBMAAS_SESSION_TIMEOUT" else
            LoginRejectedError if issue.code in LOGIN_REJECTED else
            NotAuthenticatedError if issue.code == "AUTH_NOT_AUTHENTICATED" else
            {"AUTHENTICATION": AuthenticationError, "AUTHORIZATION": AuthorizationError,
             "PARSE": ParseError}.get(issue.category, SDKError))
    exc = kind("hospital readiness failed")
    exc.info = issue
    return exc


def require_ready(report):
    if not report.ok:
        raise readiness_error(report)
    return report
