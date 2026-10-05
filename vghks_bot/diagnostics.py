"""Account-scoped diagnostics and local clinical response evidence for SDK repair."""
import hashlib
import io
import json
import platform
import re
import sys
import zipfile
from dataclasses import asdict
from pathlib import Path
from traceback import extract_tb

from vghks_sdk import __version__ as sdk_version
from vghks_sdk.core.errors import error_info

from . import __version__
from .debug_trace import TraceRecorder
from .settings import timestamp

PHASE_NAMES = {
    "auth.check": "登入與連線檢查", "auth.login": "登入", "auth.reconnect": "重新連線",
    "patients.get_demographics": "病人基本資料", "patients.resolve_identity": "病人身分核對",
    "records.get_visit_cases": "就診索引", "records.get_soap": "SOAP 病歷",
    "records.get_numeric_history": "歷年數值報告", "records.get_numeric_report": "該次數值報告",
    "orders.get_order_history": "歷年醫囑索引", "orders.get_case_orders": "該次醫囑索引",
    "orders.get_order_report": "醫囑報告", "orders.get_order_detail": "醫囑明細",
    "orders.download_pdf": "PDF 附件", "orders.download_pacs_image": "影像附件",
}
STAGE_NAMES = {"order_index": "醫囑索引", "order": "醫囑報告", "asset": "報告附件",
               "numeric": "數值報告", "visits": "就診索引", "soap": "SOAP 病歷"}
JS_REASONS = {
    "JS_EXPRESSION_UNSUPPORTED": "院方 JavaScript 欄位指定式超出 SDK 的靜態解析支援範圍。",
    "JS_BRANCH_UNSUPPORTED": "院方 JavaScript 條件分支超出 SDK 的靜態解析支援範圍。",
    "JS_ASSIGNMENT_INVALID": "院方 JavaScript 欄位指定式不完整，SDK 無法確認欄位內容。",
    "PRQ_ORDER_EXPRESSION_UNSUPPORTED": "院方醫囑資料列含 SDK 尚不支援的 JavaScript 表達式。",
}
_PARSER_VARIABLES = {"orderStr", "qrcodeStr", "mydate", "rcpDt", "orspDept",
                     "rtNameStr", "freqnStr", "argfileStr"}
_JS_TOKENS = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`|"
                        r"[\w$]+|\s+|.", re.DOTALL)
def analysis_query_context(key, kind):
    """Only store known query types; hashed report keys remain separate identifiers."""
    if key in {"orders-history:*", "orders-history:OR"}:
        category = key.split(":", 1)[1]
        return {"phase": "orders.get_order_history", "category": category,
                "label": "歷年醫囑索引 · " + ("全部類別" if category == "*" else "手術類")}
    prefixes = {"orders-case:": "orders.get_case_orders", "numeric-case:": "records.get_numeric_report",
                "soap:": "records.get_soap"}
    phase = {"numeric-history": "records.get_numeric_history", "visits": "records.get_visit_cases"}.get(key, "")
    phase = phase or next((value for prefix, value in prefixes.items() if key.startswith(prefix)), "")
    return {"phase": phase, "label": PHASE_NAMES.get(phase, STAGE_NAMES.get(kind, kind))}


def operation_query_context(service, method, args, kwargs):
    phase = f"{service}.{method}"
    context = {"phase": phase, "label": PHASE_NAMES.get(phase, phase)}
    if phase == "orders.get_order_history":
        selected = args[1] if len(args) > 1 else kwargs.get("history_filter")
        category = getattr(selected, "category", None)
        # SDK filter codes are public constants, never arbitrary request values.
        if category in {"*", "OR"}:
            context.update(analysis_query_context("orders-history:" + category, "order_index"))
        days = getattr(selected, "lookback_days", None)
        if type(days) is int and 1 <= days <= 4000:
            context["lookback_days"] = days
    return context


def _parser_details(exc):
    """Retain the failing clinical expression, without dumping unrelated locals."""
    if error_info(exc).code not in JS_REASONS:
        return {}
    trace = exc.__traceback__
    while trace:
        frame = trace.tb_frame
        if frame.f_code.co_name == "evaluated_string_assignments" and Path(frame.f_code.co_filename).name == "jsliteral.py":
            match = frame.f_locals.get("match")
            if not isinstance(match, re.Match):
                trace = trace.tb_next
                continue
            variable = match.groupdict().get("name", "")
            result = {"parser": "evaluated_string_assignments"}
            if variable in _PARSER_VARIABLES:
                result["variable"] = variable
            operator = match.groupdict().get("op", "")
            if operator in {"=", "+="}:
                result["assignment_operator"] = operator
            expression = frame.f_locals.get("expression")
            if isinstance(expression, str):
                tokens = []
                for token in _JS_TOKENS.findall(expression[:8192]):
                    if token.isspace():
                        continue
                    tokens.append("[字串]" if token[0] in "'\"`" and len(token) > 1 else
                                  "[數字]" if token.isdecimal() else
                                  "[識別字]" if re.fullmatch(r"[\w$]+", token) else
                                  token if token in "+-*/%=?:.,()[]{}!&|<>;" else "?")
                    if len(tokens) == 60:
                        tokens.append("…")
                        break
                result.update(expression_shape=" ".join(tokens), expression_length=len(expression),
                              expression_sha256=hashlib.sha256(expression.encode("utf-8")).hexdigest(),
                              expression=expression[:65536], expression_truncated=len(expression) > 65536)
            return result
        trace = trace.tb_next
    return {}


def failure_details(exc):
    from .connection_state import failure_state

    return {**asdict(error_info(exc)), **failure_state(exc), "exception_type": type(exc).__name__,
            "reason": JS_REASONS.get(error_info(exc).code, ""), "parser_context": _parser_details(exc),
            "stack": [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                      for frame in extract_tb(exc.__traceback__)[-10:]]}


def failure_summary(row):
    error = row.get("error") or {}
    outcome = row.get("outcome") or error
    code = outcome.get("code") or row.get("code", "")
    query = row.get("query") or analysis_query_context(row.get("key", ""), row.get("stage", ""))
    phase = row.get("phase") or query.get("phase", "")
    label = query.get("label") or PHASE_NAMES.get(phase, STAGE_NAMES.get(row.get("stage"), row.get("stage") or phase))
    if phase == "orders.get_order_history" and not query.get("category"):
        label = "歷年醫囑索引 · 查詢類別未記錄"
    from .patient_lookup import MESSAGES

    category = outcome.get("category", "")
    from .connection_state import failure_message, failure_state, stored_error_info

    info = stored_error_info({**outcome, "code": code, "category": category})
    decision = failure_state(info)
    reason = MESSAGES.get(code) or JS_REASONS.get(code) or error.get("reason") or row.get("message") or {
        "PARSE": "院方回應內容無法由 SDK 確認。", "NETWORK": "院內連線中斷或逾時。",
        "HTTP": "院方回應 HTTP 錯誤。", "AUTHENTICATION": "登入或授權檢查未通過。",
        "AUTHORIZATION": "院方未授權此帳號讀取資料。", "NOT_FOUND": "回應未包含可核對的資料，需確認查詢條件。",
    }.get(category, "未保存更具體的原因，請核對錯誤碼與程式位置。")
    if category in {"AUTHENTICATION", "NETWORK", "HTTP", "AUTHORIZATION"}:
        reason = failure_message(info)
    index_failure = phase == "orders.get_order_history"
    impact = ("這份歷年醫囑索引未確認，醫囑清單可能不完整；已保存的 SOAP、數值、報告與附件仍可檢閱。"
              if index_failure else "此項讀取未完成；其他已成功保存的資料保留。")
    action = ("需要 SDK 支援這種回應格式；續跑會重用成功資料並補查未完成項目。可匯出 DEBUG 供 SDK 維護者比對。"
              if code in JS_REASONS else
              "恢復院內連線後可續跑；已保存資料會優先重用。" if category in {"NETWORK", "HTTP", "AUTHENTICATION"} else
              "核對錯誤碼及程式位置後續跑；若仍失敗，可匯出 DEBUG 提供查核。")
    if code == "PATIENT_NOT_FOUND":
        action = "請核對病歷號及院區；院內登入與基本資料查詢已重新確認。"
    elif decision["action"] == "password_change":
        action = "先至院方入口變更密碼，再以新密碼重新連線及續跑；已保存資料保留。"
    elif decision["action"] == "credentials":
        action = "確認此帳號的登入資料及院方限制後重新連線，再續跑未完成查詢。"
    elif decision["action"] == "network":
        action = "先恢復院內網路、VPN 或連線設定，再重新連線及續跑；網路錯誤不代表密碼錯誤。"
    elif category == "AUTHORIZATION":
        action = "請確認此帳號的院內讀取權限，再重試查詢。"
    if row.get("recovered") and "reconnected" in row:
        reason = "查詢曾失敗；已確認登入並重新取得正確病人基本資料。"
        action = "基本資料已保存，可繼續使用；此紀錄保留供查核。"
    elif row.get("recovered"):
        reason = "查詢曾發生異常，後續重試或連線恢復後已取得結果。"
        action = "可繼續使用；詳細失敗與恢復過程保留於 DEBUG。"
    stack = error.get("stack") or []
    transport = row.get("transport") or {}
    return {"id": row.get("id", ""), "mrn": row.get("mrn", ""), "phase": phase, "label": label or "未記錄階段",
            "query": query, "code": code, "category": category, "reason": reason, "impact": impact,
            "root_cause": decision["root_cause"], "error_phase": info.phase,
            "retry_safe": info.retry_safe, "retry_recommended": info.retry_recommended,
            "next_step": action, "recovered": bool(row.get("recovered")), "recorded_at": row.get("recorded_at", ""),
            "parser_context": error.get("parser_context") or {}, "location": stack[-1] if stack else {},
            "detail_note": "未保存確切的 JavaScript 指定欄位或語法結構，無法從這份紀錄還原。"
                           if code in JS_REASONS and not error.get("parser_context") else "",
            "app_version": row.get("app_version", ""), "sdk_version": row.get("sdk_version", ""),
            "endpoint_path": error.get("endpoint_path") or transport.get("endpoint_path", ""),
            "http_status": error.get("http_status") if error.get("http_status") is not None else transport.get("http_status"),
            "attempt": error.get("attempt") or transport.get("attempt"), "transport": transport}


def failure_summaries(rows, attempts, events, *, include_events=True):
    # A task issue, gateway diagnostic and SDK event describe the same failure.
    # Pair by identity/type rather than counting all three as unfinished items.
    summaries = [failure_summary(row) for row in rows]
    available = list(summaries)
    for entry in attempts:
        summary = failure_summary(entry)
        match = next((item for item in available if item["code"] == summary["code"]
                      and item["mrn"] == summary["mrn"]
                      and (not summary["phase"] or item["phase"] == summary["phase"])
                      and (not summary["query"].get("category") or
                           item["query"].get("category") == summary["query"]["category"])), None)
        if match is not None:
            entry.update(label=match["label"], phase=match["phase"])
            available.remove(match)
        else:
            entry.update(label=summary["label"], phase=summary["phase"])
            summaries.append(summary)
    if not summaries and include_events:
        summaries = [failure_summary({"phase": event["service"] + "." + event["method"],
                                      "recorded_at": event["occurred_at"], "code": event["error_code"],
                                      "message": event["message"]}) for event in events if event["status"] == "error"]
    return summaries


class Diagnostics:
    def __init__(self, app):
        self.app, self.db = app, app.review.db

    def recorder(self):
        import uuid

        run_id = uuid.uuid4().hex
        return TraceRecorder(self.app.store.directory / "diagnostics" / run_id, run_id=run_id)

    def save(self, value):
        return self.db.save("patient_diagnostic", {"schema_version": 2, "recorded_at": timestamp(),
            "app_version": __version__, "sdk_version": sdk_version, "account_id": self.app.account_id,
            **value})

    def failure(self, exc, *, task_id, mrn="", phase="", **values):
        return self.save({"task_id": task_id, "mrn": mrn, "phase": phase, "recovered": False,
                          "error": failure_details(exc), **values})

    def query(self, values, *, export=False):
        mrn, task_id, session_id = (values.get(key, "") for key in ("mrn", "task_id", "session_id"))
        if any(not isinstance(value, str) or len(value) > 100 for value in (mrn, task_id, session_id)) or not (mrn or task_id or session_id):
            raise ValueError("請指定病歷號、任務或 SDK 工作階段。")
        rows = [r for r in self.db.all("patient_diagnostic")
                if (not mrn or r.get("mrn") == mrn)
                and (not task_id or r.get("task_id") == task_id or mrn and not r.get("task_id"))
                and (not session_id or r.get("session_id") == session_id)]
        task = self.db.get("task", task_id, required=False) if task_id else None
        attempts = []
        if task:
            for item in self.db.items(task_id):
                if mrn and item.get("mrn") != mrn:
                    continue
                for entry in [item, *item.get("attempts", [])]:
                    if entry.get("code") or entry.get("status") in {"error", "forbidden", "empty", "missing"}:
                        attempts.append({"mrn": item.get("mrn", ""), **{k: entry[k] for k in
                            ("date", "case_no", "status", "message", "code") if k in entry}})
        elif task_id:
            try:
                run = self.app.store.load(task_id)
            except ValueError:
                run = None
            if run:
                task = run
                attempts = [{k: issue[k] for k in ("mrn", "date", "stage", "message", "code", "key", "query") if k in issue}
                            for issue in run.get("issues", []) if not mrn or issue.get("mrn") == mrn]
                self.app.analysis.label_history([task])
        events = self.db.sdk_events(task_id=task_id, session_id=session_id) if task_id or session_id else []
        limit = 1000 if export else 100
        selected = rows[:limit]
        failures = failure_summaries(selected, attempts, events, include_events=not mrn)
        return {"items": selected, "total": len(rows), "saved_attempts": attempts,
                "sdk_events": events, "failures": failures,
                "task": {k: task[k] for k in ("id", "kind", "name", "analysis_name", "modules", "status", "created_at", "finished_at", "message", "error_code")
                         if k in task} if task else None,
                "storage": f"accounts/{self.app.account_id}/clinical.sqlite3",
                "sdk_logs": f"accounts/{self.app.account_id}/diagnostics/",
                "contains_raw_response": any(file.get("name", "").endswith((".bin", ".html"))
                    for row in selected for file in row.get("transport", {}).get("evidence", {}).get("files", [])),
                "contains_medical_values": any(row.get("error", {}).get("parser_context", {}).get("expression")
                    or row.get("transport", {}).get("evidence", {}).get("contains_medical_values") for row in selected)}

    def export(self, values):
        report = self.query(values, export=True)
        buffer = io.BytesIO()
        included, missing, runs = set(), [], {}
        remaining = 128 * 1024 * 1024
        base = (self.app.store.directory / "diagnostics").resolve()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for row in report["items"]:
                run_id = row.get("sdk_run_id", "")
                if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
                    continue
                operations = runs.setdefault(run_id, set())
                if type(row.get("transport", {}).get("operation_id")) is int:
                    operations.add(row["transport"]["operation_id"])
                recovery = row.get("recovery", {})
                recovery_run = recovery.get("sdk_run_id", "")
                if isinstance(recovery_run, str) and re.fullmatch(r"[a-f0-9]{32}", recovery_run):
                    runs.setdefault(recovery_run, set()).update(entry["operation_id"] for entry in
                        recovery.get("operations", []) if type(entry.get("operation_id")) is int)
                for file in row.get("transport", {}).get("evidence", {}).get("files", []):
                    name = file.get("name", "")
                    if not isinstance(name, str) or not re.fullmatch(r"(?:response-\d+-\d+\.(?:html|bin)|operation-\d+\.jsonl)", name):
                        continue
                    member = f"evidence/{run_id}/{name}"
                    if member in included:
                        continue
                    path = (base / run_id / name).resolve()
                    if not path.is_relative_to(base) or not path.is_file():
                        missing.append(member)
                        continue
                    if path.stat().st_size > remaining:
                        missing.append(member + "（超過匯出容量上限）")
                        continue
                    content = path.read_bytes()
                    if hashlib.sha256(content).hexdigest() != file.get("sha256"):
                        missing.append(member + "（內容摘要不符）")
                        continue
                    archive.writestr(member, content)
                    included.add(member)
                    remaining -= len(content)
            for run_id, operations in sorted(runs.items()):
                trace = (base / run_id / "diagnostics.jsonl").resolve()
                if trace.is_relative_to(base) and trace.is_file():
                    from .debug_bundle import selected_trace

                    content, truncated = selected_trace(trace, operations, min(remaining, 4 * 1024 * 1024))
                    archive.writestr(f"evidence/{run_id}/diagnostics.jsonl", content)
                    remaining -= len(content)
                    if truncated:
                        missing.append(f"evidence/{run_id}/diagnostics.jsonl（時序達容量上限）")
                summary = (base / run_id / "summary.json").resolve()
                if summary.is_relative_to(base) and summary.is_file() and summary.stat().st_size <= remaining:
                    content = summary.read_bytes()
                    archive.writestr(f"evidence/{run_id}/summary.json", content)
                    remaining -= len(content)
            from .debug_trace import POLICY

            report["export"] = {"evidence_files": sorted(included), "missing_files": missing,
                "omitted_diagnostics": report["total"] - len(report["items"]), "recording_policy": POLICY,
                "incomplete_evidence": [row["id"] for row in report["items"]
                    if row.get("transport", {}).get("evidence", {}).get("complete") is False]}
            archive.writestr("sdk-context.json", json.dumps({"recording_policy": POLICY,
                "export_environment": {"app_version": __version__, "sdk_version": sdk_version,
                    "python_version": platform.python_version(), "platform": sys.platform,
                    "architecture": platform.machine(), "frozen": bool(getattr(sys, "frozen", False))},
                "incidents": [{"diagnostic_id": row["id"], "phase": row.get("phase", ""),
                    "app_version": row.get("app_version", ""), "sdk_version": row.get("sdk_version", ""),
                    "sdk_run_id": row.get("sdk_run_id", ""), "transport": row.get("transport", {}),
                    "recovered": row.get("recovered", False), "recovery": row.get("recovery", {})}
                    for row in report["items"]]}, ensure_ascii=False, indent=2))
            archive.writestr("debug.json", json.dumps(report, ensure_ascii=False, indent=2))
            archive.writestr("README.txt", "VGHKS-bot DEBUG\n\n"
                "debug.json：任務、查詢類別、失敗原因、程式位置與出錯表達式。\n"
                "sdk-context.json：程式／SDK／Python 版本、平台、證據限制、操作與請求編號、重連前後的關聯與恢復摘要。\n"
                "evidence/operation-*.jsonl：失敗、重試或 session 恢復的詳細時序，含 HTTP、轉址、錯誤與頁面結構。\n"
                "evidence/response-*.bin／.html：失敗回應與必要的前置登入檢查；帳密、token 等會遮罩，可能含醫療資料。\n"
                "成功查詢平時只保留彙總，成功的臨床回應不另存；恢復後保留精簡操作摘要。\n"
                "可將 .html 交給相同版本 SDK 的純解析函式重現問題；原始內容不會執行。\n"
                "遮罩、截斷、容量限制與保存失敗請核對 transport.evidence 及 export，不應將缺少內容視為院方空回應。\n"
                "只包含目前帳號及所選任務／病人的失敗證據；舊版未保存的內容無法補回。\n")
        return buffer.getvalue()
