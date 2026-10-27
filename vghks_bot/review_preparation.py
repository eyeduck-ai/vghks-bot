"""Finite, explicit account-local preparation of an existing review worklist."""
from __future__ import annotations

import copy
import json
import threading
import uuid
from contextlib import contextmanager

from .analysis_store import digest
from .connection_state import should_pause
from .scanner import Cancelled, ScanState, safe_failure
from .settings import timestamp, today
from .storage import StorageError

DEFAULTS = {"soap": True, "numeric": False, "orders": False, "profile": False,
            "reports": False, "attachments": False, "max_reports": 3, "max_attachments": 10}
FINISHED = {"ready", "limited"}
ACTIVE = {"queued", "running", "waiting"}


class PreparationYield(Cancelled):
    """Leave at a saved boundary without cancelling an already sent HTTP request."""


class PreparationState(ScanState):
    def __init__(self, owner, token):
        super().__init__(today(), today(), id=token, kind="review_preparation", account_id=owner.app.account_id,
                         account=owner.app.username, name="背景準備病歷")
        self.owner, self.token = owner, token

    def check_cancel(self):
        super().check_cancel()
        self.owner.boundary(self.token)

    def commit_guard(self):
        return self.owner.commit_guard(self.token)

    def preparation_report(self, stage):
        self.owner.stage(self.token, stage)

    def preparation_attachment(self):
        return self.owner.admit_attachment(self.token)


class ReviewPreparation:
    def __init__(self, review):
        self.review, self.app, self.db = review, review.app, review.db
        self.condition = threading.Condition(threading.RLock())
        self.data, self.state, self.thread = None, None, None
        self.closed = False
        self.epoch = 0
        self.dirty, self.member_counts, self.error_cache = set(), {}, {}
        self.member_lookup = {}
        self.latest = ""
        self.source_task, self.source_stamp = None, ""
        with self.app.store.library.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS review_preparation_members (preparation_id TEXT,mrn TEXT,ordinal INTEGER,"
                       "payload TEXT,PRIMARY KEY(preparation_id,mrn))")
        # Reading a saved worklist never starts network work, including after restart.
        saved_rows = self.db.all("review_preparation")
        self.latest = saved_rows[0]["id"] if saved_rows else ""
        if not self.read_only():
            for saved in saved_rows:
                if saved.get("state") in ACTIVE:
                    saved.update(state="paused", active_mrn="", active_resource="",
                                 reason="程式重啟，請按繼續準備。")
                    self.db.save("review_preparation", saved)

    def read_only(self):
        return bool(getattr(getattr(self.app, "root", None), "read_only", False))

    @staticmethod
    def definition(task):
        # Progress revisions change while the original task runs; its query definition does not.
        value = {key: task.get(key) for key in ("id", "account_id", "created_at", "mode", "cutoff",
            "department", "department_filter", "department_keyword", "department_keywords", "categories")}
        # Deleting saved records removes provenance links, not the query scope.
        value["members"] = [{key: item for key, item in member.items() if key != "source_records"}
                            for member in task.get("members", [])]
        return digest(value)

    def source(self, key):
        task = self.db.get("task", key)
        if task.get("kind") != "review" or task.get("account_id") != self.app.account_id:
            raise ValueError("請選擇目前帳號的病歷檢閱任務。")
        return task

    @staticmethod
    def options(values):
        if not isinstance(values, dict) or set(values) - set(DEFAULTS):
            raise ValueError("背景準備選項不正確。")
        result = {**DEFAULTS, **values}
        if any(type(result[key]) is not bool for key in DEFAULTS if key not in {"max_reports", "max_attachments"}):
            raise ValueError("背景準備選項需為勾選值。")
        for key, maximum in (("max_reports", 5), ("max_attachments", 20)):
            if type(result[key]) is not int or not 1 <= result[key] <= maximum:
                raise ValueError("每人報告上限為 5 份、附件上限為 20 份。")
        if result["attachments"] and not result["reports"]:
            raise ValueError("請先選擇報告內容，才能準備附件。")
        if result["reports"]:
            result["orders"] = True
        if not any(result[key] for key in ("soap", "numeric", "orders", "profile", "reports")):
            raise ValueError("請至少選擇一項準備內容。")
        return result

    def _saved(self, key):
        return self.db.get("review_preparation", key, required=False)

    def _load(self, key, *, full=False):
        saved = self._saved(key)
        if saved:
            with self.app.store.library.connect() as db:
                rows = db.execute("SELECT payload FROM review_preparation_members WHERE preparation_id=? ORDER BY ordinal" +
                                  ("" if full else " LIMIT 40"), (saved["preparation_id"],)).fetchall()
            saved["members"] = [json.loads(row[0]) for row in rows]
        return saved

    def snapshot(self, key=None, *, compact=False):
        explicit = bool(key)
        with self.condition:
            saved = None
            if self.data and (not key or self.data["review_task_id"] == key):
                saved = {name: value for name, value in self.data.items() if name not in {"members", "errors"}}
                if not compact:
                    saved.update(members=copy.deepcopy(self.data["members"][:40]), errors=self.data.get("errors", [])[:20])
        if saved is None and key:
            if explicit:
                self.source(key)
            saved = self._saved(key) if compact else self._load(key)
        if saved is None and not key and self.latest:
            saved = self._saved(self.latest) if compact else self._load(self.latest)
        if not saved:
            return {"review_task_id": key or "", "state": "idle", "reason": "",
                    **({} if compact else {"members": [], "errors": []}),
                    "progress": {"done": 0, "total": 0, "unit": "項準備", "failed": 0, "stage": "尚未啟用"}}
        result = {name: saved.get(name, "") for name in ("review_task_id", "preparation_id", "name", "created_at",
                  "updated_at", "state", "reason", "active_mrn", "active_resource", "options", "revision")}
        rows = []
        for member in saved.get("members", []) if not compact else []:
            steps = list(member["steps"].values())
            count = sum(step["status"] in FINISHED for step in steps)
            failures = sum(step["status"] == "failed" for step in steps)
            blocks = sum(step["status"] == "blocked" for step in steps)
            pending = any(step["status"] == "pending" for step in steps)
            reason = next((step.get("message", "") for step in steps if step["status"] in {"blocked", "failed"}), "")
            status = ("running" if saved.get("active_mrn") == member["mrn"] else "pending" if pending else
                      "partial" if failures else "blocked" if blocks else "ready")
            rows.append({"mrn": member["mrn"], "status": status, "done": count, "total": len(steps),
                         "failed": failures, "blocked": blocks, "reason": reason})
        done, failed, blocked, total = (saved.get(name, 0) for name in ("done_count", "failed_count", "blocked_count", "total_count"))
        result.update(scope_count=saved.get("scope_count", len(rows)), member_total=saved.get("scope_count", len(rows)),
                      error_total=saved.get("error_total", 0), done_count=done, failed_count=failed, blocked_count=blocked,
                      remaining_count=total - done - failed - blocked,
                      progress={"done": done, "total": total, "unit": "項準備", "failed": failed + blocked,
                                "stage": saved.get("reason") or saved.get("active_resource") or "背景準備"})
        if not compact:
            result.update(members=rows, errors=saved.get("errors", [])[:20])
        return result

    def read(self, values):
        return self.snapshot(values.get("review_task_id"))

    def persist(self):
        # All callers hold only this short metadata lock; never any gateway lock.
        if self.data and not self.read_only():
            if self.data.get("active_mrn"):
                self.dirty.add(self.data["active_mrn"])
            totals = list(self.data.get("totals", [0, 0, 0, 0]))
            dirty = [self.member_lookup[mrn] for mrn in self.dirty if mrn in self.member_lookup]
            for _, member in dirty:
                previous = self.member_counts.get(member["mrn"], [0, 0, 0, 0])
                steps = member["steps"]
                current = [sum(step["status"] in FINISHED for step in steps.values()),
                           sum(step["status"] == "failed" for step in steps.values()),
                           sum(step["status"] == "blocked" for step in steps.values()), len(steps)]
                totals = [total + new - old for total, new, old in zip(totals, current, previous, strict=True)]
                self.member_counts[member["mrn"]] = current
                errors = []
                for resource, step in steps.items():
                    if step["status"] in {"blocked", "failed"}:
                        errors.append({"mrn": member["mrn"], "resource": resource,
                            "message": step.get("message", ""), "code": step.get("code", "")})
                if errors:
                    self.error_cache[member["mrn"]] = errors
                else:
                    self.error_cache.pop(member["mrn"], None)
            errors = []
            for group in self.error_cache.values():
                errors.extend(group[:20 - len(errors)])
                if len(errors) >= 20:
                    break
            self.data.update(totals=totals, done_count=totals[0], failed_count=totals[1], blocked_count=totals[2],
                total_count=totals[3], scope_count=len(self.data["members"]), error_total=totals[1] + totals[2], errors=errors)
            self.data["revision"] = self.data.get("revision", 0) + 1
            self.data["updated_at"] = timestamp()
            with self.app.store.library.batch() as db:
                for index, member in dirty:
                    db.execute("INSERT OR REPLACE INTO review_preparation_members VALUES(?,?,?,?)",
                        (self.data["preparation_id"], member["mrn"], index, json.dumps(member, ensure_ascii=False)))
                self.db.save("review_preparation", {key: value for key, value in self.data.items() if key != "members"})
            self.dirty.clear()

    def handle(self, values):
        from .jobs import BusyError

        action = values.get("action", "start")
        if action not in {"start", "pause", "resume", "retry"}:
            raise ValueError("背景準備操作不正確。")
        task = self.source(values.get("review_task_id"))
        if self.read_only():
            raise ValueError("目前是唯讀檢閱，無法啟動背景準備。")
        self.app._available()
        if action != "pause" and not self.app.gateway.online:
            raise ValueError("請先登入目前帳號，再繼續背景準備。")
        with self.condition:
            saved = self.data if self.data and self.data["review_task_id"] == task["id"] else self._load(task["id"], full=True)
            if action == "start":
                options = self.options(values.get("options", {}))
                selected = values.get("mrns")
                known = [member["mrn"] for member in task["members"]]
                if selected is not None and (not isinstance(selected, list) or not selected or
                    any(not isinstance(mrn, str) or mrn not in known for mrn in selected)):
                    raise ValueError("請選擇此任務中的病人。")
                selected = [mrn for mrn in known if selected is None or mrn in selected]
                scope = digest([self.definition(task), selected, options])
                if saved and saved.get("scope") == scope:
                    return self.snapshot(task["id"])
                if self.data and self.data.get("active_mrn"):
                    raise BusyError("請先暫停背景準備，等目前讀取完成後再更換範圍。")
                if self.data:
                    self.data.update(state="paused", reason="已更換準備範圍。")
                    self.persist()
                self.epoch += 1
                saved = {"id": task["id"], "review_task_id": task["id"], "preparation_id": uuid.uuid4().hex,
                    "name": "背景準備 · " + task.get("name", "病歷檢閱"), "created_at": timestamp(),
                    "source_definition": self.definition(task), "scope": scope, "options": options,
                    "members": [{"mrn": mrn, "steps": {name: {"status": "pending"} for name in
                        ("soap", "numeric", "orders", "profile", "reports") if options[name]}} for mrn in selected],
                    "state": "queued", "reason": "", "active_mrn": "", "active_resource": ""}
                self.data = saved
                self.member_lookup = {member["mrn"]: (index, member) for index, member in enumerate(saved["members"])}
                self.member_counts, self.error_cache = {}, {}
                self.dirty = {member["mrn"] for member in saved["members"]}
                self._cleared()
            else:
                if not saved:
                    raise ValueError("請先啟用此任務的背景準備。")
                if self.data and self.data.get("active_mrn") and self.data is not saved:
                    raise BusyError("請先暫停目前的背景準備。")
                if saved["source_definition"] != self.definition(task):
                    raise ValueError("檢閱範圍已變更，請重新設定背景準備。")
                if self.data is not saved:
                    self.member_counts, self.error_cache = {}, {}
                    self.member_lookup = {member["mrn"]: (index, member) for index, member in enumerate(saved["members"])}
                    saved["totals"] = [0, 0, 0, 0]
                    self.dirty = {member["mrn"] for member in saved["members"]}
                self.data = saved
                self._cleared()
                if action == "pause":
                    saved.update(state="paused", reason="已暫停；目前讀取完成後不再開始下一項。")
                    if self.state:
                        self.state.cancel.set()
                else:
                    if action == "retry":
                        saved["authorized_at"] = timestamp()
                        for member in saved["members"]:
                            for step in member["steps"].values():
                                if step["status"] in {"failed", "blocked"}:
                                    step.clear()
                                    step["status"] = "pending"
                            self.dirty.add(member["mrn"])
                        saved["cursor"] = 0
                    saved.update(state="queued", reason="")
            self.persist()
            self.latest = task["id"]
            self.source_task = copy.deepcopy(task)
            self.source_stamp = self._source_stamp(task["id"])
            if action != "pause" and self.thread is None:
                self.thread = threading.Thread(target=self.work, daemon=True, name="review-preparation")
                self.thread.start()
            self.condition.notify_all()
        return self.snapshot(task["id"])

    def _cleared(self):
        with self.app.store.library.connect() as db:
            cleared = {(row["mrn"], row["category"]) for row in db.execute("SELECT mrn,category,deleted_at FROM clinical_cache_deletions")
                       if not self.data.get("authorized_at") or row["deleted_at"] > self.data["authorized_at"]}
        for member in self.data["members"]:
            for name, step in member["steps"].items():
                category = "orders" if name in {"reports", "orders"} or name.startswith("report:") else name
                if (member["mrn"], category) in cleared:
                    step.update(status="blocked", code="CACHE_CLEARED", message="資料已清除；需明確允許重新準備。")
                    self.dirty.add(member["mrn"])

    def invalidate(self, mrns, categories):
        with self.condition:
            if not self.data:
                return
            if self.data.get("active_mrn") in mrns:
                self.epoch += 1
                if self.state:
                    self.state.cancel.set()
                self.data.update(state="paused", reason="本機資料已清除；需明確重新準備。")
            for member in self.data["members"]:
                if member["mrn"] not in mrns:
                    continue
                for name, step in member["steps"].items():
                    category = "orders" if name in {"reports", "orders"} or name.startswith("report:") else name
                    if category in categories:
                        step.update(status="blocked", code="CACHE_CLEARED", message="資料已清除；需明確允許重新準備。")
                        self.dirty.add(member["mrn"])
            self.data["cursor"] = 0
            if self.data["state"] in {"completed", "partial"}:
                self.data.update(state="partial", reason="已清除項目需明確重新準備。")
            self.persist()
            self.condition.notify_all()

    def busy(self):
        manager = self.app.task_manager
        with manager.lock:
            foreground = bool(manager.active or manager.pending)
        return bool(self.app.queue.unfinished_tasks or foreground or self.app.gateway.foreground_waiters)

    def valid(self, token):
        return bool(not self.closed and not self.app.closing and not self.read_only() and self.data and
                    self.data["preparation_id"] == token and self.state and self.state.token == token and
                    self.state.epoch == self.epoch and self.state.session == self.app.resources.login_epoch)

    def boundary(self, token):
        with self.condition:
            if not self.valid(token):
                raise Cancelled()
            if self.data["state"] == "paused" or not self.app.gateway.online or self.busy():
                raise PreparationYield()
        stamp = self._source_stamp(self.data["review_task_id"])
        if stamp != self.source_stamp:
            task = self.source(self.data["review_task_id"])
            if self.definition(task) != self.data["source_definition"]:
                raise Cancelled()
            self.source_task, self.source_stamp = task, stamp

    def _source_stamp(self, key):
        with self.app.store.library.connect() as db:
            row = db.execute("SELECT rowid,updated_at FROM bot_documents WHERE kind='task' AND id=?", (key,)).fetchone()
        return tuple(row) if row else None

    @contextmanager
    def commit_guard(self, token):
        with self.condition:
            if not self.valid(token):
                raise Cancelled()
            if self._source_stamp(self.data["review_task_id"]) != self.source_stamp:
                task = self.source(self.data["review_task_id"])
                if self.definition(task) != self.data["source_definition"]:
                    raise Cancelled()
            yield

    def stage(self, token, message):
        with self.condition:
            if self.valid(token):
                self.data["reason"] = message

    def admit_attachment(self, token):
        """Durably charge every admitted binary acquisition, including failures."""
        with self.commit_guard(token):
            member = self.member_lookup[self.data["active_mrn"]][1]
            used = sum(step.get("attachment_count", 0) for step in member["steps"].values())
            options = self.data["options"]
            if not options["attachments"] or used >= options["max_attachments"]:
                return False
            step = member["steps"][self.data["active_resource"]]
            step["attachment_count"] = step.get("attachment_count", 0) + 1
            self.dirty.add(member["mrn"])
            self.persist()
            return True

    def pause(self, reason, *, invalidate=False):
        with self.condition:
            if invalidate:
                self.epoch += 1
                if self.state:
                    self.state.cancel.set()
            if self.data and (self.data["state"] in ACTIVE or self.data["state"] == "paused" or self.data.get("active_mrn")):
                self.data.update(state="paused", reason=reason)
                self.persist()
            self.condition.notify_all()

    def _job(self):
        for index in range(self.data.get("cursor", 0), len(self.data["members"])):
            member = self.data["members"][index]
            for resource, step in member["steps"].items():
                if step["status"] == "pending":
                    self.data["cursor"] = index
                    return member, resource, step
        return None

    def work(self):
        try:
            self._work()
        except StorageError:
            with self.condition:
                if self.data:
                    self.data.update(state="paused", active_mrn="", active_resource="", reason="保存失敗，已停止背景準備。")
                self.epoch += 1
                if self.state:
                    self.state.cancel.set()
        finally:
            with self.condition:
                self.thread = None
                state = self.state
            if state:
                with self.app.lock:
                    self.app.states.pop(state.token, None)
                    if not any(row.data["status"] in {"queued", "running", "cancelling"} for row in self.app.states.values()):
                        self.app.idle.set()

    def _work(self):
        from .bot_gateway import AcquisitionDeferred

        while True:
            with self.condition:
                if self.closed:
                    return
                if not self.data or self.data["state"] not in ACTIVE:
                    self.condition.wait(.2)
                    continue
                if not self.app.gateway.online or self.read_only():
                    self.pause("帳號離線或唯讀，請重新登入後按繼續準備。")
                    continue
                if self.busy():
                    self.data.update(state="waiting", reason="等待目前的前景或檢閱任務完成。")
                    self.condition.wait(.2)
                    continue
                job = self._job()
                if not job:
                    failed = any(step["status"] in {"failed", "blocked"} for member in self.data["members"] for step in member["steps"].values())
                    self.data.update(state="partial" if failed else "completed", reason="部分項目未完成，可查看原因後明確重試。" if failed else "此範圍已準備完成。")
                    self.persist()
                    continue
                member, resource, step = job
                token = self.data["preparation_id"]
                state = PreparationState(self, token)
                state.epoch, state.session = self.epoch, self.app.resources.login_epoch
                self.state = state
                self.data.update(state="running", active_mrn=member["mrn"], active_resource=resource, reason="")
                self.persist()
            with self.app.lock:
                self.app.states[token] = state
                self.app.idle.clear()
            yielded = False
            try:
                state.check_cancel()
                with self.app.gateway.task_context(token, "review_preparation", state.cancel), \
                        self.app.gateway.query_priority(lambda: False, self.busy):
                    result = self.execute(member, resource, state)
                with state.commit_guard():
                    step.update(status=result.get("status", "ready"), **{key: value for key, value in result.items() if key != "status"})
                    self.dirty.add(member["mrn"])
            except (PreparationYield, AcquisitionDeferred):
                yielded = True
                with self.condition:
                    if self.data["state"] != "paused":
                        self.data.update(state="waiting", reason="等待目前的前景或原檢閱任務完成。")
            except Cancelled:
                with self.condition:
                    if self.data and self.data["preparation_id"] == token and self.data["state"] != "paused":
                        self.data.update(state="paused", reason="準備範圍或登入狀態已變更；已保存內容保留。")
            except StorageError:
                self.pause("保存失敗，已停止背景準備。", invalidate=True)
            except Exception as exc:
                message, code = safe_failure(exc)
                if not getattr(self.app.gateway.local, "diagnostic_emitted", False):
                    self.app.diagnostics.failure(exc, task_id=token, mrn=member["mrn"], phase="review_preparation")
                with self.condition:
                    step.update(status="failed", message=message, code=code)
                    self.dirty.add(member["mrn"])
                if not self.app.gateway.online or should_pause(exc):
                    self.pause(message)
            finally:
                with self.condition:
                    if self.data and self.data["preparation_id"] == token:
                        self.data.update(active_mrn="", active_resource="")
                        self.persist()
                with self.app.lock:
                    self.app.states.pop(token, None)
                    if not any(row.data["status"] in {"queued", "running", "cancelling"} for row in self.app.states.values()):
                        self.app.idle.set()
                if yielded:
                    with self.condition:
                        self.condition.wait(.2)

    def execute(self, member, resource, state):
        task = self.source_task
        task = {**task, "status": self.db.task_summaries(key=task["id"])[0]["status"]}
        mrn = member["mrn"]
        if resource == "soap":
            if task["status"] in {"queued", "running"}:
                raise PreparationYield()
            if task["status"] in {"paused", "cancelled", "cancelling", "failed", "interrupted"}:
                return {"status": "blocked", "code": "SOURCE_PAUSED", "message": "原檢閱任務已暫停；請先明確續跑原任務。"}
            old = self.db.item(task["id"], mrn)
            records = {record["id"] for record in (old or {}).get("records", [])}
            missing = {attempt["record_id"] for attempt in (old or {}).get("attempts", [])
                       if attempt.get("status") == "ready"} - records
            if old and (old.get("status") == "deleted" or missing):
                with self.app.store.library.connect() as db:
                    cleared = db.execute("SELECT deleted_at FROM clinical_cache_deletions WHERE mrn=? AND category='soap'", (mrn,)).fetchone()
                authorized = self.data.get("authorized_at", "")
                if not authorized or cleared and cleared[0] > authorized:
                    return {"status": "blocked", "code": "CACHE_CLEARED", "message": "病歷已清除；需明確重試才能重新準備。"}
            if old and not missing and (old.get("status") == "ready" and records or
                    old.get("status") in {"no_source", "future", "no_visit", "unknown", "missing"}):
                return {"status": "ready"}
            source = next(row for row in task["members"] if row["mrn"] == mrn)
            query = {**task, "force": False, "refresh": False, "preparation": True,
                     "created_at": self.data.get("authorized_at", self.data["created_at"]), "recheck_mrns": []}
            result = self.review.patient(query, source, state)
            with state.commit_guard():
                self.db.item(task["id"], mrn, {**result, "checked_at": result.get("index_checked_at") or timestamp(),
                    "source_task_id": task["id"], "reused": False})
            return {"status": "failed" if result["status"] in {"error", "partial", "forbidden"} else "ready",
                    "message": result.get("message", "")}
        if resource == "profile":
            self.review.basic_info({"mrn": mrn, "force": False,
                "patient_name": next(row.get("name", "") for row in task["members"] if row["mrn"] == mrn)}, state=state)
            return {"status": "ready"}
        if resource == "reports":
            rows, _ = self.review.history._stored(mrn, "orders", "")
            selected = [row["id"] for row in rows or [] if row["ophthalmic"]][:self.data["options"]["max_reports"]]
            with state.commit_guard():
                for reference in selected:
                    member["steps"].setdefault("report:" + reference, {"status": "pending"})
            return {"status": "ready", "report_count": len(selected)}
        query = {"id": self.data["preparation_id"], "review_task_id": task["id"], "mrn": mrn,
                 "resource": "order_report" if resource.startswith("report:") else resource,
                 "reference": resource.removeprefix("report:") if resource.startswith("report:") else "", "force": False}
        if resource.startswith("report:"):
            options = self.data["options"]
            used = sum(step.get("attachment_count", 0) for name, step in member["steps"].items() if name != resource)
            query["preparation_limits"] = {"attachments": options["attachments"],
                "max_attachments": max(0, options["max_attachments"] - used)}
        result = self.review.history.execute(query, state)
        attempts = member["steps"][resource].get("attachment_count", 0)
        if result.get("preparation_complete"):
            return {"status": "limited" if result.get("limited") else "ready", "attachment_count": attempts}
        return {"status": "failed" if result["status"] == "partial" else "ready", "attachment_count": attempts,
                "message": "部分資料尚未完整，請明確重試。" if result["status"] == "partial" else ""}

    def close(self, *, wait=False):
        with self.condition:
            try:
                self.pause("程式已關閉，請重新啟動後明確繼續準備。", invalidate=True)
            except StorageError:
                pass
            self.closed = True
            self.condition.notify_all()
            thread = self.thread
        if wait and thread and thread is not threading.current_thread():
            thread.join()
