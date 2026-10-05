"""Keep cheap success counters; persist bounded detail only for anomalous reads."""
from __future__ import annotations

import json
import threading
from collections import Counter, OrderedDict, deque
from dataclasses import asdict
from datetime import UTC, datetime
from time import monotonic

from vghks_sdk import __version__ as sdk_version
from vghks_sdk.core.diagnostics import DiagnosticRecorder, _safe_code, _safe_field_name
from vghks_sdk.core.errors import error_info
from vghks_sdk.parsing.portal import has_portal_login_form

from . import __version__
from .debug_evidence import (
    BUFFER_LIMIT,
    EVENT_LIMIT,
    EVIDENCE_LIMIT,
    RESPONSE_LIMIT,
    TRACE_LIMIT,
    Redactor,
    file_reference,
    json_line,
    navigation,
    response_shape,
    response_snapshot,
    save_response,
)

POLICY = {"mode": "failure_detail", "success_summary_every": 50, "context_operations": 8,
          "response_limit_bytes": RESPONSE_LIMIT, "buffer_limit_bytes": BUFFER_LIMIT,
          "operation_evidence_limit_bytes": EVIDENCE_LIMIT, "operation_trace_limit_bytes": TRACE_LIMIT,
          "buffer_event_limit": EVENT_LIMIT, "successful_response_bodies": False,
          "supporting_readiness_limit_bytes": 16384, "supporting_readiness_seconds": 60}


class TraceRecorder(DiagnosticRecorder):
    """SDK-compatible recorder with memory-only success HTTP and failure evidence."""

    def __init__(self, *args, **kwargs):
        self.http_local = threading.local()
        self.response_encoding = "auto"
        self.redactor = Redactor()
        self.contains_medical_response = False
        self._recent = deque(maxlen=8)
        self._counts = Counter()
        self._successes = 0
        self._checkpoint = monotonic()
        self._pending_incidents = deque(maxlen=64)
        self._storage_error = ""
        super().__init__(*args, **kwargs)

    def set_context(self, context):
        self.http_local.context = {key: value for key, value in context.items()
                                   if key in {"interaction_id", "task_id", "task_kind", "service", "method", "recovery"}}

    def _frame(self, *, create=False):
        stack = getattr(self.http_local, "stack", [])
        if stack:
            return stack[-1]
        if create:
            frame = getattr(self.http_local, "unscoped", None)
            if frame is None:
                frame = self._new_frame(0, "unscoped", "", 0)
                self.http_local.unscoped = frame
            return frame
        return getattr(self.http_local, "unscoped", None)

    def _new_frame(self, identifier, name, app, parent):
        return {"id": identifier, "name": name, "app": app, "parent": parent,
                "context": dict(getattr(self.http_local, "context", {})),
                "started": monotonic(), "started_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "events": deque(), "event_bytes": 0, "dropped_events": 0, "snapshots": OrderedDict(),
                "buffer_bytes": 0, "omitted_responses": 0, "request_count": 0, "response_bytes": 0,
                "files": {}, "responses": {}, "evidence_bytes": 0, "trace_bytes": 0,
                "promoted": False, "trace_started": False, "status": "RUNNING", "children": [],
                "trace_path": self.directory / f"operation-{identifier}.jsonl"}

    def _row(self, event, payload):
        self._sequence += 1
        return {"schema_version": self.schema_version, "run_id": self.run_id, "sequence": self._sequence,
                "timestamp_utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "event": _safe_code(event), **payload}

    def _base_write(self, row):
        try:
            self._handle.write(json_line(row))
            self._handle.flush()
        except OSError:
            self._storage_error = "TRACE_SAVE_FAILED"

    def _write(self, event, payload):
        with self._lock:
            row = self._row(event, payload)
            if event == "run_started":
                row.update(app_version=__version__, sdk_version=sdk_version, recording_policy=POLICY)
                self._run_header = row
                self._base_write(row)
                return
            frame = self._frame()
            if event == "operation_started":
                frame = self._new_frame(payload["operation_id"], payload["operation"], payload["application"],
                                        frame["id"] if frame else 0)
                stack = getattr(self.http_local, "stack", [])
                stack.append(frame)
                self.http_local.stack = stack
                row.update(parent_operation_id=frame["parent"], **frame["context"])
            if frame is None or event == "run_finished":
                self._base_write(row)
                return
            frame["events"].append(row)
            frame["event_bytes"] += len(json_line(row).encode("utf-8"))
            while len(frame["events"]) > EVENT_LIMIT or frame["event_bytes"] > TRACE_LIMIT:
                discarded = frame["events"].popleft()
                frame["event_bytes"] -= len(json_line(discarded).encode("utf-8"))
                frame["dropped_events"] += 1
            anomaly = event in {"http_network_error", "http_retry", "reauthentication_started",
                                "application_session_recovery_started"}
            if anomaly:
                frame["anomaly"] = event
                if event == "application_session_recovery_started":
                    frame["recovery_issue"] = payload.get("issue", {})
                late_auth_check = event == "reauthentication_started" and frame["name"] == "auth_check"
                self._promote(frame, capture=event != "http_network_error" and not late_auth_check)
                if late_auth_check:
                    candidates = []
                    for snapshot in frame["snapshots"].values():
                        path = snapshot["navigation"]["path"].lower()
                        if snapshot.get("requested_endpoint", "").lower().endswith(("/index.do", "/login.do")):
                            continue
                        if path.endswith(("/index.do", "/login.do", "/syserrorexception.jsp")) or has_portal_login_form(
                                snapshot["_raw"].decode("utf-8", errors="replace")):
                            candidates.append(snapshot)
                    for snapshot in candidates[:3]:
                        self._capture(frame, snapshot)
                    if not candidates:
                        frame["capture_error"] = "RECOVERY_RESPONSE_NOT_IDENTIFIED"
            elif frame["promoted"]:
                self._flush_detail(frame)

    def start_operation(self, *, name, app_key):
        unscoped = getattr(self.http_local, "unscoped", None)
        if unscoped:
            self._counts["unscoped_requests"] += unscoped["request_count"]
            self.http_local.unscoped = None
        self.http_local.failure = {}
        if name == "get_patient_demographics":
            self.clear_demographics()
        return super().start_operation(name=name, app_key=app_key)

    def record_http_request(self, **values):
        with self._lock:
            self.redactor.request(values["url"], values.get("kwargs", {}))
            self._request_sequence += 1
            identifier = self._request_sequence
            frame = self._frame(create=True)
            frame["request_count"] += 1
            kwargs = values.get("kwargs", {})
            route = navigation(values["url"])
            keys = {}
            for name, source in (("query_keys", "params"), ("form_keys", "data"), ("json_keys", "json")):
                mapping = kwargs.get(source) or {}
                candidates = mapping.keys() if isinstance(mapping, dict) else (
                    pair[0] for pair in mapping if isinstance(pair, (tuple, list)) and len(pair) == 2)
                keys[name] = sorted({_safe_field_name(str(key)) for key in candidates}) if not isinstance(mapping, (str, bytes)) else []
            keys["query_keys"] = sorted(set(keys["query_keys"]) | set(route["query_keys"]))
            frame["request"] = {"request_id": identifier, "attempt": values["attempt"],
                                "endpoint_path": route["path"], "method": values["method"].upper()}
            self._write("http_request", {"operation_id": frame["id"], "request_id": identifier,
                "method": values["method"].upper() if values["method"].upper() in {"GET", "POST"} else "OTHER",
                **route, **keys, "attempt": values["attempt"], "max_attempts": values["max_attempts"],
                "throttle_delay_ms": round(max(0, values["throttle_delay_seconds"]) * 1000, 1),
                "tls_verification_enabled": bool(values["tls_verification_enabled"]),
                "retry_safe": kwargs.get("retry_safe") if type(kwargs.get("retry_safe")) is bool else None,
                "allow_redirects": kwargs.get("allow_redirects", True)})
            return identifier

    def record_http_response(self, **values):
        response = values["response"]
        with self._lock:
            self.redactor.response(response.headers)
            for prior in (getattr(response, "history", ()) or ()):
                self.redactor.response(prior.headers)
            frame = self._frame(create=True)
            snapshot = response_snapshot(values["request_id"], response, values["elapsed_seconds"])
            snapshot["source_operation_id"] = frame["id"]
            snapshot["requested_endpoint"] = frame.get("request", {}).get("endpoint_path", "")
            frame["snapshots"][values["request_id"]] = snapshot
            frame["buffer_bytes"] += len(snapshot["_raw"])
            frame["response_bytes"] += snapshot["response_bytes"]
            while frame["buffer_bytes"] > BUFFER_LIMIT or len(frame["snapshots"]) > 8:
                _, old = frame["snapshots"].popitem(last=False)
                frame["buffer_bytes"] -= len(old["_raw"])
                frame["omitted_responses"] += 1
            if frame["name"] == "get_patient_demographics":
                self.http_local.demographics = (frame, snapshot)
            self._write("http_response", {"operation_id": frame["id"], "request_id": snapshot["request_id"],
                "status_code": snapshot["http_status"], "final_path": snapshot["navigation"]["path"],
                "navigation": snapshot["navigation"], "redirects": snapshot["redirects"],
                "elapsed_ms": snapshot["elapsed_ms"], "response_bytes": snapshot["response_bytes"],
                "headers": snapshot["headers"], "set_cookie_names": snapshot["set_cookie_names"]})
            if snapshot["http_status"] >= 400:
                frame["anomaly"] = "http_status"
                frame["anomaly_status"] = snapshot["http_status"]
                self._promote(frame)

    def _flush_detail(self, frame):
        if not frame["trace_started"]:
            context = self._row("incident_context", {"operation_id": frame["id"], "context": frame["context"],
                "parent_operation_id": frame["parent"], "recent_operations": list(self._recent)})
            frame["events"].append(context)
            frame["events"].appendleft(self._run_header)
            frame["trace_started"] = True
        try:
            with frame["trace_path"].open("a", encoding="utf-8", newline="\n") as handle:
                while frame["events"]:
                    row = frame["events"].popleft()
                    packed = json_line(row)
                    size = len(packed.encode("utf-8"))
                    if frame["trace_bytes"] + size <= TRACE_LIMIT:
                        handle.write(packed)
                        frame["trace_bytes"] += size
                    else:
                        frame["dropped_events"] += 1
                frame["event_bytes"] = 0
        except OSError:
            frame["capture_error"] = "TRACE_SAVE_FAILED"
            self._storage_error = "TRACE_SAVE_FAILED"

    def _capture(self, frame, snapshot, *, supporting=False):
        if not snapshot or snapshot["request_id"] in frame["responses"]:
            return
        if not snapshot.get("_raw"):
            return
        if frame["evidence_bytes"] >= EVIDENCE_LIMIT or len(frame["responses"]) >= 8:
            frame["capture_error"] = "EVIDENCE_LIMIT_REACHED"
            return
        try:
            result, written = save_response(self.directory, frame["id"], snapshot, self.redactor,
                                           self.response_encoding, EVIDENCE_LIMIT - frame["evidence_bytes"])
            result.update(supporting_readiness_response=supporting, navigation=snapshot["navigation"],
                          source_operation_id=snapshot.get("source_operation_id", frame["id"]))
            frame["responses"][snapshot["request_id"]] = result
            frame["evidence_bytes"] += written
            for file in result["files"]:
                frame["files"][file["name"]] = file
            self.contains_medical_response = self.contains_medical_response or bool(result["files"])
            self._write_for(frame, "response_evidence", {"operation_id": frame["id"], **result,
                "response": response_shape(snapshot, self.response_encoding, self.redactor)})
        except (OSError, ValueError, UnicodeError):
            frame["capture_error"] = "RAW_EVIDENCE_SAVE_FAILED"

    def _write_for(self, frame, event, payload):
        frame["events"].append(self._row(event, payload))
        if frame["promoted"]:
            self._flush_detail(frame)

    def _promote(self, frame, *, capture=True):
        if not frame["promoted"]:
            frame["promoted"] = True
            self._flush_detail(frame)
            readiness = getattr(self.http_local, "readiness", None)
            if (readiness and readiness is not frame and monotonic() - readiness["started"] < 60
                    and readiness["context"].get("interaction_id") == frame["context"].get("interaction_id")):
                for snapshot in list(readiness["snapshots"].values())[-1:]:
                    self._capture(frame, snapshot, supporting=True)
        else:
            self._flush_detail(frame)
        if capture:
            self._capture(frame, frame["snapshots"].get(frame.get("request", {}).get("request_id")))

    def _summary(self, frame):
        return {"operation_id": frame["id"], "operation": frame["name"], "application": frame["app"],
                "parent_operation_id": frame["parent"], "status": frame["status"], "started_at": frame["started_at"],
                "elapsed_ms": frame.get("elapsed_ms", round((monotonic() - frame["started"]) * 1000, 1)),
                "request_count": frame["request_count"], "response_bytes": frame["response_bytes"],
                "interaction_id": frame["context"].get("interaction_id", ""),
                "last_request": frame.get("request", {}), "has_detail": frame["promoted"]}

    def finish_operation(self, *, operation_id, name, status, exc=None, capture_operation_id=""):
        with self._lock:
            frame = self._frame()
            if frame is None or frame["id"] != operation_id:
                return super().finish_operation(operation_id=operation_id, name=name, status=status, exc=exc,
                                                capture_operation_id=capture_operation_id)
            if exc is not None:
                self._promote(frame)
            super().finish_operation(operation_id=operation_id, name=name, status=status, exc=exc,
                                     capture_operation_id=capture_operation_id)
            frame["status"] = status
            frame["finished"] = True
            frame["elapsed_ms"] = round((monotonic() - frame["started"]) * 1000, 1)
            stack = self.http_local.stack
            stack.pop()
            self._local.operation_id = stack[-1]["id"] if stack else 0
            if stack:
                parent = stack[-1]
                parent["children"].append(self._summary(frame))
                parent["children"] = parent["children"][-32:]
                parent["request_count"] += frame["request_count"]
                parent["response_bytes"] += frame["response_bytes"]
                if frame.get("request"):
                    parent["request"] = frame["request"]
                if frame["snapshots"]:
                    snapshot = next(reversed(frame["snapshots"].values()))
                    parent["snapshots"][snapshot["request_id"]] = snapshot
                    parent["buffer_bytes"] += len(snapshot["_raw"])
                    while parent["buffer_bytes"] > BUFFER_LIMIT or len(parent["snapshots"]) > 8:
                        _, old = parent["snapshots"].popitem(last=False)
                        parent["buffer_bytes"] -= len(old["_raw"])
                        parent["omitted_responses"] += 1
                for row in frame["events"]:
                    parent["events"].append(row)
                    parent["event_bytes"] += len(json_line(row).encode("utf-8"))
                while len(parent["events"]) > EVENT_LIMIT or parent["event_bytes"] > TRACE_LIMIT:
                    discarded = parent["events"].popleft()
                    parent["event_bytes"] -= len(json_line(discarded).encode("utf-8"))
                    parent["dropped_events"] += 1
                if frame["promoted"]:
                    child_evidence = self._transport(frame)["evidence"]
                    if not child_evidence["complete"]:
                        parent["capture_error"] = parent.get("capture_error") or "CHILD_EVIDENCE_INCOMPLETE"
                    for file in child_evidence["files"]:
                        if file["name"] in parent["files"]:
                            continue
                        if len(parent["files"]) >= 32 or parent["evidence_bytes"] + file["size_bytes"] > EVIDENCE_LIMIT:
                            parent["capture_error"] = parent.get("capture_error") or "EVIDENCE_LIMIT_REACHED"
                            continue
                        parent["files"][file["name"]] = file
                        parent["evidence_bytes"] += file["size_bytes"]
                if parent["promoted"]:
                    self._flush_detail(parent)
            self.http_local.last = frame
            if name == "auth_check" and status == "OK":
                tiny = [snapshot for snapshot in frame["snapshots"].values() if snapshot["response_bytes"] <= 16384]
                if tiny:
                    self.http_local.readiness = {**frame, "snapshots": OrderedDict([(tiny[-1]["request_id"], tiny[-1])])}
            summary = self._summary(frame)
            self._recent.append(summary)
            if frame["promoted"]:
                self._write_for(frame, "incident_finished", {**summary, "dropped_events": frame["dropped_events"],
                    "omitted_responses": frame["omitted_responses"], "capture_error": frame.get("capture_error", ""),
                    "children": frame["children"]})
                self._base_write(self._row("operation_summary", summary))
                if exc is None and frame.get("anomaly"):
                    # Failed bodies are already on disk. Queued summaries must
                    # not retain successful retry bodies until the gateway drains them.
                    self._pending_incidents.append({**frame, "snapshots": OrderedDict(
                        (key, {**snapshot, "_raw": b""}) for key, snapshot in frame["snapshots"].items())})
            else:
                self._successes += 1
                self._counts[name] += 1
                if frame["context"].get("recovery"):
                    self._base_write(self._row("operation_summary", summary))
                if self._successes % POLICY["success_summary_every"] == 0 or monotonic() - self._checkpoint >= 60:
                    self._flush_successes()
            if exc is not None:
                self.http_local.failure = {"error_code": error_info(exc).code, "frame": frame}

    def _flush_successes(self):
        if self._counts:
            self._base_write(self._row("activity_summary", {"successful_operations": dict(self._counts),
                                                          "total_successes": self._successes}))
            self._counts.clear()
            self._checkpoint = monotonic()

    def _transport(self, frame):
        snapshot = frame["snapshots"].get(frame.get("request", {}).get("request_id"), {})
        files = dict(frame["files"])
        if frame["trace_path"].is_file():
            try:
                files[frame["trace_path"].name] = file_reference(frame["trace_path"])
            except OSError:
                frame["capture_error"] = "TRACE_SAVE_FAILED"
        responses = list(frame["responses"].values())
        return {"operation": frame["name"], "operation_id": frame["id"],
                "parent_operation_id": frame["parent"], "interaction_id": frame["context"].get("interaction_id", ""),
                **frame.get("request", {}), **{key: snapshot[key] for key in
                    ("http_status", "response_bytes", "elapsed_ms", "navigation", "redirects") if key in snapshot},
                "children": frame["children"],
                "evidence": {"files": list(files.values()), "responses": responses,
                    "contains_medical_values": any(file["name"].endswith((".bin", ".html")) for file in files.values()),
                    "source_encoding": "utf-8", "complete": not (frame["dropped_events"] or frame.get("capture_error")
                        or any(response["truncated"] for response in responses)),
                    "response_encoding": self.response_encoding,
                    "dropped_events": frame["dropped_events"], "omitted_buffer_responses": frame["omitted_responses"],
                    "capture_error": frame.get("capture_error", "")}}

    def failure_transport(self, exc):
        failure = getattr(self.http_local, "failure", {})
        frame = failure.get("frame") if failure.get("error_code") == error_info(exc).code else getattr(self.http_local, "last", None)
        if frame is None or not frame["request_count"]:
            return {}
        if not frame["promoted"]:
            self._promote(frame)
            frame["validation_failed"] = True
            self._write_for(frame, "validation_failed", {"operation_id": frame["id"], "issue": asdict(error_info(exc))})
        elif frame["status"] == "OK" and not frame.get("validation_failed"):
            frame["validation_failed"] = True
            self._capture(frame, next(reversed(frame["snapshots"].values()), {}))
            self._write_for(frame, "validation_failed", {"operation_id": frame["id"], "issue": asdict(error_info(exc))})
        return self._transport(frame)

    def clear_demographics(self):
        self.http_local.demographics = None

    def demographics_empty(self):
        """Only distinguish a genuinely empty JSON result for MRN confirmation."""
        value = getattr(self.http_local, "demographics", None)
        try:
            return json.loads(value[1]["_raw"]) == [] if value else False
        except (ValueError, UnicodeError):
            return False

    def recovery_reference(self):
        frame = getattr(self.http_local, "last", None)
        interaction = frame["context"].get("interaction_id", "") if frame else ""
        return {"sdk_run_id": self.run_id, "summary": self._summary(frame) if frame else {},
                "operations": [entry for entry in self._recent if entry["interaction_id"] == interaction]}

    def consume_incidents(self):
        result = []
        while self._pending_incidents:
            frame = self._pending_incidents.popleft()
            if frame.get("recovery_issue"):
                code, category = "SDK_APPLICATION_SESSION_RECOVERED", "AUTHENTICATION"
            elif frame.get("anomaly") == "reauthentication_started":
                code, category = "SDK_SESSION_RECOVERED", "AUTHENTICATION"
            elif frame.get("anomaly_status"):
                code, category = "HTTP_" + str(frame["anomaly_status"]), "HTTP"
            else:
                code, category = "SDK_TRANSPORT_RECOVERED", "NETWORK"
            result.append({"code": code, "category": category, "operation": frame["name"],
                           "http_status": frame.get("anomaly_status"),
                           "application_recovery": bool(frame.get("recovery_issue")),
                           "cause": frame.get("recovery_issue"),
                           "transport": self._transport(frame)})
        return result

    def release_success(self):
        frame = getattr(self.http_local, "last", None)
        if frame and frame["finished"]:
            frame["snapshots"].clear()
            frame["events"].clear()
            frame["buffer_bytes"] = 0
        self.clear_demographics()

    def finalize(self, **values):
        self._flush_successes()
        super().finalize(**values)
        try:
            summary = json.loads(self.summary_path.read_text(encoding="utf-8"))
            summary.update(app_version=__version__, sdk_version=sdk_version, recording_policy=POLICY,
                contains_raw_request_or_response=self.contains_medical_response,
                contains_clinical_response=self.contains_medical_response, successful_operations=self._successes,
                capture_error=self._storage_error)
            from .storage import atomic_json

            atomic_json(self.summary_path, summary)
        except (OSError, ValueError):
            self._storage_error = "SUMMARY_SAVE_FAILED"
        self.release_success()
        self.http_local.readiness = None
        self.http_local.unscoped = None
        self.redactor.secrets.clear()
