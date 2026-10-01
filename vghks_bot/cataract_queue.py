"""Account-local, cooperative cataract prefetch; waits never occupy the task worker."""
from __future__ import annotations

import random
import threading
import time
from contextlib import nullcontext

from .analysis_fetch import AnalysisYield as CataractYield
from .analysis_fetch import PatientCollector, RunSessions
from .scanner import Cancelled, safe_failure
from .storage import ACTIVE, StorageError


class CataractQueue:
    def __init__(self, analysis, *, clock=time.monotonic, delay=random.uniform):
        self.analysis, self.app = analysis, analysis.app
        self.clock, self.delay = clock, delay
        self.condition = threading.Condition(threading.RLock())
        self.cohort_id, self.pending, self.jobs = "", [], {}
        self.active, self.priority, self.next_at = "", "", 0.0
        self.paused, self.closed, self.reason = True, False, ""
        self.thread = None
        self.rebase = set()
        # Restart only restores a paused queue; opening a cached page does not restart it.
        for cohort in analysis.store.documents("analysis_cohorts"):
            saved = cohort.get("cataract_queue")
            if saved and saved.get("state") != "completed":
                saved.update(state="paused", reason="程式重啟，請按繼續背景抓取。", active_mrn="")
                if not getattr(getattr(self.app, "root", None), "read_only", False):
                    analysis.store.save_document("analysis_cohorts", cohort)

    def online(self):
        return not hasattr(self.app, "gateway") or self.app.gateway.online

    def snapshot(self, cohort_id=None):
        with self.condition:
            if cohort_id and cohort_id != self.cohort_id:
                cohort = self.analysis.store.document("analysis_cohorts", cohort_id)
                saved = cohort.get("cataract_queue", {"state": "idle", "pending": [], "active_mrn": ""})
                if saved.get("state") not in {"idle", "completed"}:
                    saved.update(state="paused", active_mrn="", reason="請按繼續背景抓取。")
                return saved
            state = ("paused" if self.paused else "running" if self.active else
                     "waiting" if self.pending and self.next_at > self.clock() else
                     "queued" if self.pending else "completed")
            return {"state": state, "pending": list(self.pending), "active_mrn": self.active,
                    "priority_mrn": self.priority, "wait_seconds": max(0, round(self.next_at - self.clock(), 1)),
                    "reason": self.reason}

    def persist(self):
        with self.condition:
            key, value = self.cohort_id, self.snapshot()
        if key and not self.closed:
            try:
                with self.app.lock:
                    cohort = self.analysis.store.document("analysis_cohorts", key)
                    cohort["cataract_queue"] = value
                    self.analysis.store.save_document("analysis_cohorts", cohort)
            except StorageError:
                with self.condition:
                    self.paused, self.reason = True, "保存失敗，已停止背景抓取。"

    def finish(self, state):
        try:
            self.analysis.finish(state)
        except StorageError:
            self.app._storage_failure(state)
            with self.condition:
                self.paused, self.reason = True, "保存失敗，已停止背景抓取。"

    def handle(self, values):
        action = values.get("action", "start")
        if action not in {"start", "prioritize", "pause", "resume"}:
            raise ValueError("背景抓取操作不正確。")
        if getattr(getattr(self.app, "root", None), "read_only", False):
            raise ValueError("目前是唯讀檢閱，無法啟動背景抓取。")
        self.app._available()
        cohort = self.analysis.store.document("analysis_cohorts", values.get("cohort_id"))
        account = getattr(self.app, "account_id", None)
        members = [row for row in cohort["members"] if not account or row["account_id"] == account]
        mrn = values.get("mrn", "")
        if mrn and mrn not in {row["mrn"] for row in members}:
            raise ValueError("病人不在此帳號的分析清單。")
        if not members:
            raise ValueError("清單沒有此帳號可抓取的病人。")
        if action != "pause" and not self.online():
            raise ValueError("請先登入此帳號，再繼續背景抓取。")
        states = {row["mrn"]: row for row in self.analysis.cataract_status({"cohort_id": cohort["id"]})["members"]}
        saved = cohort.get("cataract_queue", {})
        previous = None
        retired = []
        with self.condition:
            if self.cohort_id != cohort["id"]:
                if self.cohort_id:
                    previous = self.cohort_id, self.snapshot()
                    previous[1].update(state="paused", active_mrn="", reason="已更換集合，請按繼續背景抓取。")
                self.cohort_id, self.pending = cohort["id"], []
                for key in list(self.jobs):
                    if key[0] != self.cohort_id and key[1] != self.active:
                        retired.append(self.jobs.pop(key))
                self.priority, self.next_at, self.reason = "", 0.0, ""
                # Existing segments belong to the previous collection, and stop at the next boundary.
                self.paused = False
                if saved.get("state") == "paused" and action != "resume":
                    self.paused = True
                    self.reason = saved.get("reason", "請按繼續背景抓取。")
                for member in members:
                    status = states[member["mrn"]]
                    resumed = member["mrn"] in saved.get("pending", []) and saved.get("state") == "paused"
                    if not status["ready"] and not status["cache_cleared"] and (not status["attempted"] or resumed):
                        self.pending.append(member["mrn"])
            if action == "pause":
                self.paused, self.reason = True, "背景抓取已暫停。"
            elif action == "resume":
                self.paused, self.reason, self.next_at = False, "", 0.0
            elif action == "prioritize" and mrn:
                status = states[mrn]
                if not status["ready"] and not status["cache_cleared"]:
                    if mrn not in self.pending and not status["attempted"]:
                        self.pending.append(mrn)
                    if mrn in self.pending:
                        self.priority, self.next_at = mrn, 0.0
            elif action == "start" and mrn in self.pending and mrn != self.active and not self.paused:
                self.priority = mrn
            if action == "start":
                for member in members:
                    status = states[member["mrn"]]
                    if not status["ready"] and not status["attempted"] and member["mrn"] not in self.pending:
                        self.pending.append(member["mrn"])
            self.condition.notify_all()
            if self.thread is None:
                self.thread = threading.Thread(target=self.work, daemon=True, name="cataract-prefetch")
                self.thread.start()
        if previous:
            with self.app.lock:
                old = self.analysis.store.document("analysis_cohorts", previous[0])
                old["cataract_queue"] = previous[1]
                self.analysis.store.save_document("analysis_cohorts", old)
        for state, _, collector, sessions in retired:
            state.update(status="interrupted", message="已更換集合；已保存資料保留。")
            self.finish(state)
            collector.stack.close()
            sessions.close()
        self.persist()
        return self.snapshot(cohort["id"])

    def checkpoint(self, cohort_id, mrn):
        with self.condition:
            if self.closed or self.app.closing or self.paused or cohort_id != self.cohort_id:
                raise CataractYield()
            if not self.online():
                self.paused, self.reason = True, "帳號離線，請重新連線後按繼續。"
                raise CataractYield()
            if self.priority and self.priority != mrn or self.app.queue.unfinished_tasks:
                raise CataractYield()

    def notify(self):
        with self.condition:
            self.condition.notify_all()

    def invalidate(self, mrn):
        with self.condition:
            self.rebase.add((self.cohort_id, mrn))

    def pause(self, reason):
        with self.condition:
            self.paused, self.reason = True, reason
            self.condition.notify_all()
        self.persist()

    def discard(self, cohort_id):
        with self.condition:
            if self.cohort_id != cohort_id:
                return
            if self.active:
                raise ValueError("請先暫停並等待正在進行的讀取完成。")
            jobs = list(self.jobs.values())
            self.jobs.clear()
            self.cohort_id, self.pending, self.priority = "", [], ""
            self.paused, self.next_at = True, 0.0
            self.condition.notify_all()
        for state, _, collector, sessions in jobs:
            state.update(status="interrupted", message="分析清單已移除；已保存資料保留。")
            self.finish(state)
            collector.stack.close()
            sessions.close()

    def work(self):
        while True:
            with self.condition:
                if self.closed:
                    break
                if not self.online():
                    self.paused, self.reason = True, "帳號離線，請重新連線後按繼續。"
                if self.paused or not self.pending or self.app.queue.unfinished_tasks:
                    self.condition.wait(.25)
                    continue
                if self.priority not in self.pending:
                    self.priority = ""
                wait = self.next_at - self.clock()
                if wait > 0 and not self.priority:
                    self.condition.wait(min(wait, 1))
                    continue
                mrn = self.priority if self.priority in self.pending else self.pending[0]
                cohort_id = self.cohort_id
                self.active, self.priority = mrn, ""
            self.run_patient(cohort_id, mrn)
        for state, _, collector, sessions in self.jobs.values():
            state.update(status="interrupted", message="背景抓取已中斷；已保存資料保留。")
            self.finish(state)
            collector.stack.close()
            sessions.close()
        self.jobs.clear()

    def run_patient(self, cohort_id, mrn):
        key = (cohort_id, mrn)
        state = None
        completed = False
        try:
            if key in self.jobs:
                state = self.jobs[key][0]
            status = next(row for row in self.analysis.cataract_status({"cohort_id": cohort_id})["members"]
                          if row["mrn"] == mrn)
            if status["ready"] or status["cache_cleared"]:
                if state:
                    state.update(status="completed" if status["ready"] else "cancelled", stage="done",
                                 message="已重用保存結果。" if status["ready"] else "本機資料已清除，等待主動更新。")
                completed = True
                return
            if key in self.jobs and key in self.rebase:
                old = self.jobs.pop(key)
                old[2].stack.close()
                old[3].close()
                old[0].update(status="interrupted", message="已保存讀取進度，改用更新後的本機資料。")
                self.finish(old[0])
                self.rebase.discard(key)
            if key not in self.jobs:
                state, _, source = self.analysis.start(
                    {"cohort_id": cohort_id, "modules": ["cataract"], "mrn": mrn}, enqueue=False)
                sessions = RunSessions(self.app.sdk_factory, state)
                member = source["analysis"]["members"][0]
                collector = PatientCollector(self.analysis.store, state, member,
                    source["settings"][member["account_id"]], self.app.sdk_factory,
                    source["analysis"]["options"], sessions, getattr(self.app, "review", None),
                    checkpoint=lambda: self.checkpoint(cohort_id, mrn))
                self.jobs[key] = state, source, collector, sessions
            state, source, collector, sessions = self.jobs[key]
            with self.app.lock:
                self.app.states[state.data["id"]] = state
                self.app.idle.clear()
            context = self.app.gateway.task_context(state.data["id"], "analysis") if hasattr(self.app, "gateway") else nullcontext()
            state.update(status="running", stage="analysis", message=f"{mrn} · 背景取得歷年資料…")
            with context:
                collector.run()
            state.count(patients_done=1)
            state.update(status="partial" if state.data["counts"]["errors"] else "completed", stage="done",
                         message="部分查詢未完成，請查看 DEBUG 後續跑。" if state.data["counts"]["errors"] else "分析完成；資料已保存。")
            completed = True
        except CataractYield:
            if state:
                state.update(status="paused", message="已保存讀取進度，等待背景續接。")
        except Cancelled:
            if state:
                state.update(status="cancelled", message="已停止，先前資料已保留。")
            completed = True
        except StorageError:
            if state:
                self.app._storage_failure(state)
            completed = True
            with self.condition:
                self.paused, self.reason = True, "保存失敗，已停止背景抓取。"
        except Exception as exc:
            category = getattr(getattr(exc, "info", None), "category", "")
            disconnected = not self.online() or category in {"NETWORK", "AUTHENTICATION"}
            if state:
                message, code = safe_failure(exc)
                state.issue("白內障背景抓取", message, code=code, mrn=mrn)
                state.update(status="paused" if disconnected else "failed", message=message)
            completed = not disconnected
            if disconnected:
                self.pause("連線失敗，請重新登入後按繼續。")
        finally:
            if state:
                self.finish(state)
            with self.condition:
                self.active = ""
                if completed and cohort_id == self.cohort_id:
                    if mrn in self.pending:
                        self.pending.remove(mrn)
                    if self.priority == mrn:
                        self.priority = ""
                    self.next_at = self.clock() + self.delay(10, 20)
                if completed or cohort_id != self.cohort_id:
                    job = self.jobs.pop(key, None)
                    if job:
                        if not completed:
                            job[0].update(status="interrupted", message="已更換病人集合；已保存資料保留。")
                            self.finish(job[0])
                        job[2].stack.close()
                        job[3].close()
            if state:
                with self.app.lock:
                    if state.data["status"] not in ACTIVE:
                        self.app.states.pop(state.data["id"], None)
                    if not any(row.data["status"] in ACTIVE for row in self.app.states.values()):
                        self.app.idle.set()
            self.persist()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        if self.thread:
            self.thread.join()
