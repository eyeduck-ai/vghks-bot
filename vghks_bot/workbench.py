"""Bounded local status reads; clinical history is loaded separately on demand."""
from __future__ import annotations

import hashlib
import json


def connection(work):
    return {"online": work.gateway.online, "offline_mode": work.offline_mode,
            "recovery_count": work.gateway.recovery_count,
            "connection_error_code": work.gateway.last_error_code,
            "connection_issue": work.gateway.connection_issue,
            "password_status": work.gateway.password_status}


def status(work, watched=(), run_id=""):
    with work.store.library.read_snapshot():
        revisions = work.review.db.revisions()
        categories = work.settings.public()["categories"]
        catalog = [revisions.get("set", 0), revisions.get("preferences", 0), categories]
        catalog_revision = hashlib.sha256(json.dumps(catalog, sort_keys=True).encode()).hexdigest()
        value = {"tasks": work.review.db.task_summaries(watched=watched),
                 "revisions": revisions, "catalog_revision": catalog_revision,
                 "approval_counts": work.approvals.tracker.counts(),
                 "approval_monitor_enabled": work.approvals.tracker.preferences().get("enabled", True),
                 **connection(work)}
    # Never hold a SQLite transaction while waiting on another task's lock.
    with work.lock:
        states = list(work.states.values())
    fields = {"id", "kind", "status", "created_at", "finished_at", "revision", "counts", "message", "progress",
              "account_id", "account", "account_label", "start", "end", "cohort_id", "modules", "name"}
    value["runs"] = [{k: v for k, v in state.snapshot(detail=False).items() if k in fields}
                     for state in states if state.data.get("kind") != "bot"]
    for key in list(dict.fromkeys(run_id.split(",")))[:20]:
        if key and not any(row["id"] == key for row in value["runs"]):
            value["runs"].append({k: v for k, v in work.store.metadata(key).items() if k in fields})
    value["active_count"] = sum(t.get("status") in {"queued", "running", "cancelling"}
                                for t in [*value["tasks"], *value["runs"]])
    return value


def snapshot(work, *, compact=False):
    with work.store.library.read_snapshot():
        counts = work.review.db.review_note_counts()
        tasks = work.review.db.task_summaries() if compact else work.review.db.all("task")
        for task in tasks:
            if task["kind"] == "review":
                task["note_count"] = counts.get(task["id"], 0)
        return {"sets": work.review.db.all("set"), "tasks": tasks,
                "sdk_sessions": [] if compact else work.review.db.sdk_sessions(),
                "preferences": work.review.preferences(), "categories": work.settings.public()["categories"],
                "approval_counts": work.approvals.tracker.counts(),
                "approval_monitor_enabled": work.approvals.tracker.preferences().get("enabled", True),
                "draft": work.review.db.get("draft", "current", required=False), **connection(work)}
