"""Durable per-case follow-up; approval snapshots remain immutable."""
import threading
from datetime import datetime, timedelta

from vghks_sdk.models.review import ReviewCaseRef

from .analysis_fetch import clean
from .analysis_store import digest
from .monitoring import interval
from .scanner import Cancelled, safe_failure
from .settings import timestamp
from .storage import StorageError

FINAL_CODES = {"1", "2"}
ATTENTION_CODES = {"3", "4", "5", "7"}


def case_content(case, *, fields=False):
    value = {k: v for k, v in case.items() if k != "fields"}
    if fields:
        value["fields"] = {k: v for k, v in case.get("fields", {}).items() if k != "UniqueID"}
    return value


def later(at, hours):
    return (datetime.fromisoformat(at) + timedelta(hours=hours)).isoformat(timespec="microseconds")


class ApprovalTracker:
    def __init__(self, approvals):
        self.approvals, self.db, self.app = approvals, approvals.db, approvals.app
        self.lock = threading.RLock()
        # Each account waits for an explicit visit during this process. Saving
        # this flag would restart monitoring before the next visit after launch.
        self.monitor_started = False

    def enter(self, _values=None):
        with self.app.root.lock:
            with self.lock:
                self.monitor_started = True
            self.schedule()
        return {"monitor_started": True}

    def preferences(self, values=None):
        current = self.db.get("preferences", "approval_tracking", required=False) or {
            "id": "approval_tracking", "enabled": True, "automatic": True, "hours": 24, "next_check": ""}
        if values is not None:
            if type(values.get("automatic", True)) is not bool or type(values.get("enabled", True)) is not bool:
                raise ValueError("監控開關設定不正確。")
            frequency = interval(values, current)
            with self.lock:
                next_check = later(current["last_attempt"], frequency["hours"]) if current.get("last_attempt") else ""
                if values.get("enabled", True) and not current.get("enabled", True):
                    next_check = ""
                current = self.db.save("preferences", {**current, **frequency, "enabled": values.get("enabled", True),
                    "automatic": values.get("automatic", True), "next_check": next_check})
                rows = self.db.all("approval_tracking")
                self.db.save_batch([("approval_tracking", {**r, "next_check": later(r["checked_at"], current["hours"])})
                                    for r in rows if not r["closed"] and not r.get("error")])
        return current

    def observe(self, case, *, source="", checked_at=None, source_kind="list"):
        ref = ReviewCaseRef(case.get("reference", {}).get("apply_seq")).apply_seq
        checked = checked_at or timestamp()
        with self.lock:
            previous = self.db.get("approval_case", ref, required=False)
            if previous and previous["case"].get("mrn") and previous["case"]["mrn"] != case.get("mrn"):
                raise ValueError("審查案件的病人身分與已保存資料不符。")
            old = self.db.get("approval_tracking", ref, required=False)
            # A stale cached query must never roll the current case backwards.
            if old and old["checked_at"] > checked:
                return old
            changed = bool(old and old["verify_code"] != case["verify_code"])
            source_key = ref + ":" + source_kind
            prior_source = self.db.get("approval_source", source_key, required=False)
            content = case_content(case, fields=True)
            fingerprint = digest(content)
            meaningful = bool(previous and (case_content(previous["case"]) != case_content(case)
                              or prior_source and prior_source["fingerprint"] != fingerprint))
            changes = []
            if meaningful:
                comparisons = [("", case_content(previous["case"]), case_content(case))]
                if prior_source:
                    prior_version = self.db.get("approval_version", prior_source["version"])
                    comparisons.append(("fields.", case_content(prior_version["case"], fields=True)["fields"], content["fields"]))
                for prefix, before, after in comparisons:
                    for key in sorted(before.keys() | after.keys()):
                        if before.get(key) != after.get(key):
                            changes.append({"field": prefix + key, "before": before.get(key), "after": after.get(key)})
            version_id = digest([ref, source_kind, fingerprint])
            item = {**(old or {}), "id": ref, "verify_code": case["verify_code"],
                    "checked_at": checked, "next_check": later(checked, self.preferences()["hours"]),
                    "closed": case["verify_code"] in FINAL_CODES,
                    "paused": bool(old and old.get("paused")), "error": "", "failures": 0,
                    "needs_attention": case["verify_code"] in ATTENTION_CODES or case["review_label"] == "未辨識"}
            if meaningful:
                item.update(changed_at=checked, previous_code=(old or {}).get("verify_code", ""), unread=True)
            merged = {**case, "fields": {**(previous or {}).get("case", {}).get("fields", {}), **case.get("fields", {})}}
            values = [("approval_case", {"id": ref, "case": merged, "fetched_at": checked}),
                      ("approval_tracking", item),
                      ("approval_source", {"id": source_key, "fingerprint": fingerprint, "version": version_id, "checked_at": checked})]
            if not self.db.get("approval_version", version_id, required=False):
                values.append(("approval_version", {"id": version_id, "apply_seq": ref, "source_kind": source_kind,
                    "case": case, "checked_at": checked, "source": source}))
            if not old or not previous or meaningful:
                values.append(("approval_observation", {"apply_seq": ref, "case": case,
                    "checked_at": checked, "source": source, "status_changed": changed,
                    "version": version_id, "source_kind": source_kind, "changes": changes,
                    "closed_correction": bool(meaningful and old and old["closed"])}))
            self.db.save_batch(values)
            return {**item, "change": "new" if not previous else "changed" if meaningful else "unchanged", "version": version_id}

    def seed(self):
        tracked = {r["id"] for r in self.db.all("approval_tracking")}
        for row in self.db.all("approval_case"):
            if row["id"] not in tracked:
                self.observe(row["case"], source="saved", checked_at=row["fetched_at"])

    def overview(self, values=None):
        self.seed()
        now = timestamp()
        cases = {r["id"]: r for r in self.db.all("approval_case")}
        rows = [{**t, "case": cases[t["id"]]["case"],
                 "due": not t["closed"] and not t["paused"] and t["next_check"] <= now}
                for t in self.db.all("approval_tracking") if t["id"] in cases]
        rows.sort(key=lambda r: (not r.get("unread", False), not r["due"], r["closed"], r["next_check"], r["id"]))
        counts = {"pending": sum(not r["closed"] and not r["paused"] for r in rows),
                  "due": sum(r["due"] for r in rows), "unread": sum(bool(r.get("unread")) for r in rows),
                  "closed": sum(r["closed"] for r in rows)}
        mode = (values or {}).get("mode", "pending")
        if mode == "pending":
            rows = [r for r in rows if not r["closed"]]
        elif mode == "changed":
            rows = [r for r in rows if r.get("unread")]
        elif mode not in {"all", "closed"}:
            raise ValueError("追蹤篩選不正確。")
        if mode == "closed":
            rows = [r for r in rows if r["closed"]]
        tasks = self.db.task_list(("approval_refresh", "approval_sync", "approval_options", "approval_case"), values)
        # A paused task blocks automatic monitoring. Its resume control must
        # remain reachable even after many newer manual jobs have completed.
        visible = [t for i, t in enumerate(tasks) if i < 20 or t["status"] in {
            "queued", "running", "cancelling", "paused", "partial", "failed"}]
        from .pagination import slice_rows

        return {"rows": slice_rows(rows, values or {}), "total": len(rows), "counts": counts, "preferences": self.preferences(), "tasks": visible,
                "monitor_started": self.monitor_started}

    def counts(self):
        """Badges need tracking flags, never complete clinical case documents."""
        with self.db.library.connect() as db:
            row = db.execute("""SELECT
                coalesce(sum(NOT json_extract(t.payload,'$.closed') AND NOT json_extract(t.payload,'$.paused')),0),
                coalesce(sum(NOT json_extract(t.payload,'$.closed') AND NOT json_extract(t.payload,'$.paused')
                    AND json_extract(t.payload,'$.next_check')<=?),0),
                coalesce(sum(coalesce(json_extract(t.payload,'$.unread'),0)),0),
                coalesce(sum(json_extract(t.payload,'$.closed')),0)
                FROM bot_documents t JOIN bot_documents c ON c.kind='approval_case' AND c.id=t.id
                WHERE t.kind='approval_tracking'""", (timestamp(),)).fetchone()
        return dict(zip(("pending", "due", "unread", "closed"), row, strict=True))

    def change(self, values):
        refs = values.get("ids")
        if not isinstance(refs, list) or not refs or len(refs) > 2000:
            raise ValueError("請選取追蹤案件。")
        action = values.get("action")
        if action not in {"pause", "resume", "seen"}:
            raise ValueError("追蹤操作不正確。")
        with self.lock:
            rows = [self.db.get("approval_tracking", ReviewCaseRef(r).apply_seq) for r in refs]
            for row in rows:
                if action == "seen":
                    row.update(unread=False, seen_at=timestamp())
                else:
                    row["paused"] = action == "pause"
                    if action == "resume" and not row["closed"]:
                        row["next_check"] = timestamp()
            self.db.save_batch([("approval_tracking", row) for row in rows])
        return {"ok": True}

    def prepare(self, task, values):
        available = self.overview({"mode": "all"})["rows"]
        selected = values.get("ids")
        if selected is not None and (not isinstance(selected, list) or not selected or len(selected) > 2000):
            raise ValueError("請選取待追蹤案件。")
        known = {r["id"] for r in available}
        if selected and any(r not in known for r in selected):
            raise ValueError("追蹤案件不存在於此帳號。")
        rows = [r for r in available if not r["closed"] and not r["paused"]
                and (r["id"] in selected if selected is not None else r["due"] or not values.get("due_only"))]
        if not rows:
            raise ValueError("目前沒有需要更新的未結案件。")
        self.require_idle()
        return {**task, "name": "審查追蹤更新", "references": [r["id"] for r in rows],
                "automatic": bool(values.get("automatic"))}

    def require_idle(self):
        if any(t["kind"] in {"approval_refresh", "approval_sync"} and t["status"] in {"queued", "running", "cancelling"}
               for t in self.db.task_summaries(kinds=('approval_refresh','approval_sync','approval_options','approval_case'))):
            raise ValueError("追蹤更新已在執行或排隊，請等待完成。")

    def execute(self, sdk, state, task):
        for ref in task["references"]:
            state.check_cancel()
            old = self.db.item(task["id"], ref)
            if old and old["status"] == "ready":
                continue
            track = self.db.get("approval_tracking", ref)
            if track["closed"] or track["paused"]:
                self.db.item(task["id"], ref, {"status": "skipped", "apply_seq": ref})
                self.app.review.report(state, task, stage="已略過結案／暫停案件")
                continue
            self.app.review.report(state, task, stage="正在更新案件 " + ref)
            try:
                response = sdk.reviews.get_case(ReviewCaseRef(ref))
                checked_at = self.app.gateway.response_at()
                case = clean(response)
                if case.get("reference", {}).get("apply_seq") != ref:
                    raise ValueError("審查明細與案件編號不符。")
                self.observe(case, source=task["id"], checked_at=checked_at, source_kind="detail")
                self.db.item(task["id"], ref, {"status": "ready", "apply_seq": ref, "case": case})
            except (Cancelled, StorageError):
                raise
            except Exception as exc:
                message, code = safe_failure(exc)
                with self.lock:
                    track = self.db.get("approval_tracking", ref)
                    failures = track.get("failures", 0) + 1
                    self.db.save("approval_tracking", {**track, "error": message, "failures": failures,
                        "last_attempt": timestamp(), "next_check": later(timestamp(), max(self.preferences()["hours"], min(24, 2 ** min(failures-1, 5))))})
                self.db.item(task["id"], ref, {"status": "error", "apply_seq": ref, "message": message, "code": code})
                from .connection_state import should_pause

                if not self.app.gateway.online or should_pause(exc):
                    raise
            self.app.review.report(state, task, stage="更新結果已保存")

    def schedule(self):
        with self.lock:
            self._schedule()

    def _schedule(self):
        if not self.monitor_started:
            return
        if self.app.root.read_only or self.app.closing or not self.app.gateway.online or not self.app.entered:
            return
        if not self.preferences().get("enabled", True) or not self.preferences()["automatic"]:
            return
        if any(t["kind"] in {"approval_refresh", "approval_sync"} and t["status"] in {"queued", "running", "cancelling", "paused"}
               for t in self.db.task_summaries(kinds=('approval_refresh','approval_sync','approval_options','approval_case'))):
            return
        if self.preferences().get("next_check", "") <= timestamp():
            self.app.review.start({"kind": "approval_sync", "automatic": True})

    def started(self, task):
        if task["kind"] == "approval_sync":
            now, value = timestamp(), self.preferences()
            self.db.save("preferences", {**value, "last_task": task["id"], "last_attempt": now,
                "next_check": later(now, value["hours"])})
