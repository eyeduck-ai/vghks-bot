"""Portable, account-scoped failure evidence without credentials or response bodies."""
from dataclasses import asdict
from pathlib import Path
from traceback import extract_tb

from vghks_sdk import __version__ as sdk_version
from vghks_sdk.core.diagnostics import DiagnosticRecorder
from vghks_sdk.core.errors import error_info

from . import __version__
from .settings import timestamp


def failure_details(exc):
    return {**asdict(error_info(exc)), "exception_type": type(exc).__name__,
            "stack": [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                      for frame in extract_tb(exc.__traceback__)[-10:]]}


class Diagnostics:
    def __init__(self, app):
        self.app, self.db = app, app.review.db

    def recorder(self):
        import uuid

        run_id = uuid.uuid4().hex
        return DiagnosticRecorder(self.app.store.directory / "diagnostics" / run_id, run_id=run_id)

    def save(self, value):
        return self.db.save("patient_diagnostic", {"schema_version": 1, "recorded_at": timestamp(),
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
                attempts = [{k: issue[k] for k in ("mrn", "date", "stage", "message", "code") if k in issue}
                            for issue in run.get("issues", []) if not mrn or issue.get("mrn") == mrn]
        events = self.db.sdk_events(task_id=task_id, session_id=session_id) if task_id or session_id else []
        return {"items": rows[:100], "total": len(rows), "saved_attempts": attempts,
                "sdk_events": events,
                "task": {k: task[k] for k in ("id", "kind", "status", "created_at", "finished_at", "message", "error_code")
                         if k in task} if task else None,
                "storage": f"accounts/{self.app.account_id}/clinical.sqlite3",
                "sdk_logs": f"accounts/{self.app.account_id}/diagnostics/",
                "contains_raw_response": False}
