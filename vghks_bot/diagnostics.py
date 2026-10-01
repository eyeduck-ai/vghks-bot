"""Account-scoped diagnostics and local clinical response evidence for SDK repair."""
import hashlib
import io
import json
import re
import threading
import zipfile
from dataclasses import asdict
from pathlib import Path
from traceback import extract_tb
from urllib.parse import urlsplit

from vghks_sdk import __version__ as sdk_version
from vghks_sdk.core.diagnostics import DiagnosticRecorder
from vghks_sdk.core.errors import error_info

from . import __version__
from .settings import timestamp

PHASE_NAMES = {
    "auth.check": "登入與連線檢查", "auth.login": "登入", "auth.reconnect": "重新連線",
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
_ENDPOINTS = {"/PRQWeb/QueryOrderResult.do", "/PRQWeb/QueryOrderDetail.do",
              "/PRQWeb/QueryReportByOrder.do", "/PRQWeb/QueryBillingSOAP.do",
              "/PRQWeb/QueryCaseList.do", "/PRQWeb/QueryCaseDetail.do",
              "/PRQWeb/QueryPatientRecord.do", "/PRQWeb/QueryResNumCenter.do"}
_CAPTURE_OPERATIONS = {"get_order_history", "get_case_orders", "get_order_report", "get_order_detail",
                       "get_soap", "get_numeric_history", "get_numeric_report", "get_visit_cases",
                       "get_upload_history", "get_scanned_records"}


class TraceRecorder(DiagnosticRecorder):
    """Keep SDK traces redacted; store clinical pages separately on parse failure."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.http_local = threading.local()
        self.response_encoding = "auto"
        self.contains_medical_response = False

    def start_operation(self, *, name, app_key):
        operation_id = super().start_operation(name=name, app_key=app_key)
        self.http_local.current = {"operation": name, "operation_id": operation_id}
        self.http_local.failure = {}
        return operation_id

    def record_http_request(self, **values):
        request_id = super().record_http_request(**values)
        current = getattr(self.http_local, "current", {})
        if current:
            path = urlsplit(values["url"]).path
            self.http_local.current = {"operation": current["operation"], "operation_id": current["operation_id"],
                                       "request_id": request_id, "attempt": values["attempt"],
                                       "endpoint_path": path if path in _ENDPOINTS else ""}
        return request_id

    def record_http_response(self, **values):
        super().record_http_response(**values)
        current = getattr(self.http_local, "current", {})
        if current.get("request_id") == values["request_id"]:
            response = values["response"]
            current.update(http_status=int(response.status_code), response_bytes=len(response.content or b""),
                           elapsed_ms=round(max(0, values["elapsed_seconds"]) * 1000, 1))
            if current["operation"] in _CAPTURE_OPERATIONS:
                current["_response"] = response.content or b""
                current["_content_type"] = response.headers.get("Content-Type", "")

    def finish_operation(self, *, operation_id, name, status, exc=None):
        super().finish_operation(operation_id=operation_id, name=name, status=status, exc=exc)
        if exc is not None:
            current = getattr(self.http_local, "current", {})
            failure = {k: value for k, value in current.items() if not k.startswith("_")}
            if error_info(exc).category == "PARSE" and "_response" in current:
                from .encoding import decode_response

                raw = current["_response"]
                source = decode_response(raw, current.get("_content_type", ""), self.response_encoding).encode("utf-8")
                files = []
                try:
                    for suffix, content in (("bin", raw), ("html", source)):
                        name = f"response-{operation_id}-{current['request_id']}.{suffix}"
                        target = self.directory / name
                        temporary = self.directory / (name + ".tmp")
                        temporary.write_bytes(content)
                        temporary.replace(target)
                        files.append({"name": name, "sha256": hashlib.sha256(content).hexdigest(),
                                      "size_bytes": len(content)})
                    failure["evidence"] = {"files": files, "contains_medical_values": True,
                                           "source_encoding": "utf-8", "complete": True}
                    self.contains_medical_response = True
                except (OSError, ValueError):
                    failure["capture_error"] = "RAW_EVIDENCE_SAVE_FAILED"
            self.http_local.failure = {**failure, "error_code": error_info(exc).code}
        self.http_local.current = {}

    def failure_transport(self, exc):
        context = getattr(self.http_local, "failure", {})
        return {k: value for k, value in context.items() if k != "error_code"} if context.get("error_code") == error_info(exc).code else {}

    def finalize(self, **values):
        super().finalize(**values)
        if self.contains_medical_response:
            summary = json.loads(self.summary_path.read_text(encoding="utf-8"))
            summary.update(contains_raw_request_or_response=True, contains_clinical_response=True)
            from .storage import atomic_json

            atomic_json(self.summary_path, summary)


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
    return {**asdict(error_info(exc)), "exception_type": type(exc).__name__,
            "reason": JS_REASONS.get(error_info(exc).code, ""), "parser_context": _parser_details(exc),
            "stack": [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                      for frame in extract_tb(exc.__traceback__)[-10:]]}


def failure_summary(row):
    error = row.get("error") or {}
    code = error.get("code") or row.get("code", "")
    query = row.get("query") or analysis_query_context(row.get("key", ""), row.get("stage", ""))
    phase = row.get("phase") or query.get("phase", "")
    label = query.get("label") or PHASE_NAMES.get(phase, STAGE_NAMES.get(row.get("stage"), row.get("stage") or phase))
    if phase == "orders.get_order_history" and not query.get("category"):
        label = "歷年醫囑索引 · 查詢類別未記錄"
    category = error.get("category", "")
    reason = JS_REASONS.get(code) or error.get("reason") or row.get("message") or {
        "PARSE": "院方回應內容無法由 SDK 確認。", "NETWORK": "院內連線中斷或逾時。",
        "HTTP": "院方回應 HTTP 錯誤。", "AUTHENTICATION": "登入或授權檢查未通過。",
    }.get(category, "未保存更具體的原因，請核對錯誤碼與程式位置。")
    index_failure = phase == "orders.get_order_history"
    impact = ("這份歷年醫囑索引未確認，醫囑清單可能不完整；已保存的 SOAP、數值、報告與附件仍可檢閱。"
              if index_failure else "此項讀取未完成；其他已成功保存的資料保留。")
    action = ("需要 SDK 支援這種回應格式；續跑會重用成功資料並補查未完成項目。可匯出 DEBUG 供 SDK 維護者比對。"
              if code in JS_REASONS else
              "恢復院內連線後可續跑；已保存資料會優先重用。" if category in {"NETWORK", "HTTP", "AUTHENTICATION"} else
              "核對錯誤碼及程式位置後續跑；若仍失敗，可匯出 DEBUG 提供查核。")
    stack = error.get("stack") or []
    transport = row.get("transport") or {}
    return {"id": row.get("id", ""), "mrn": row.get("mrn", ""), "phase": phase, "label": label or "未記錄階段",
            "query": query, "code": code, "category": category, "reason": reason, "impact": impact,
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

    def query(self, values):
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
        failures = failure_summaries(rows[:100], attempts, events, include_events=not mrn)
        return {"items": rows[:100], "total": len(rows), "saved_attempts": attempts,
                "sdk_events": events, "failures": failures,
                "task": {k: task[k] for k in ("id", "kind", "name", "analysis_name", "modules", "status", "created_at", "finished_at", "message", "error_code")
                         if k in task} if task else None,
                "storage": f"accounts/{self.app.account_id}/clinical.sqlite3",
                "sdk_logs": f"accounts/{self.app.account_id}/diagnostics/",
                "contains_raw_response": any(row.get("transport", {}).get("evidence", {}).get("files") for row in rows[:100]),
                "contains_medical_values": any(row.get("error", {}).get("parser_context", {}).get("expression")
                                                or row.get("transport", {}).get("evidence", {}).get("files") for row in rows[:100])}

    def export(self, values):
        report = self.query(values)
        buffer = io.BytesIO()
        included, missing, runs = set(), [], set()
        base = (self.app.store.directory / "diagnostics").resolve()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for row in report["items"]:
                run_id = row.get("sdk_run_id", "")
                if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
                    continue
                runs.add(run_id)
                for file in row.get("transport", {}).get("evidence", {}).get("files", []):
                    name = file.get("name", "")
                    if not isinstance(name, str) or not re.fullmatch(r"response-\d+-\d+\.(?:html|bin)", name):
                        continue
                    member = f"evidence/{run_id}/{name}"
                    if member in included:
                        continue
                    path = (base / run_id / name).resolve()
                    if not path.is_relative_to(base) or not path.is_file():
                        missing.append(member)
                        continue
                    content = path.read_bytes()
                    if hashlib.sha256(content).hexdigest() != file.get("sha256"):
                        missing.append(member + "（內容摘要不符）")
                        continue
                    archive.writestr(member, content)
                    included.add(member)
            for run_id in sorted(runs):
                for name in ("diagnostics.jsonl", "summary.json"):
                    path = (base / run_id / name).resolve()
                    if path.is_relative_to(base) and path.is_file():
                        archive.writestr(f"evidence/{run_id}/{name}", path.read_bytes())
            report["export"] = {"evidence_files": sorted(included), "missing_files": missing}
            archive.writestr("debug.json", json.dumps(report, ensure_ascii=False, indent=2))
            archive.writestr("README.txt", "VGHKS-bot DEBUG\n\n"
                "debug.json：任務、查詢類別、失敗原因、程式位置與出錯表達式。\n"
                "evidence/：解析失敗時的完整回應 bytes（.bin）、UTF-8 HTML（.html）及 SDK 請求時序。\n"
                "醫療資料值保留，可將 .html 交給相同版本 SDK 的純解析函式重現問題。\n"
                "只包含目前帳號及所選任務／病人的失敗證據；舊版未保存的內容無法補回。\n")
        return buffer.getvalue()
