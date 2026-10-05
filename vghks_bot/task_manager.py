"""Account-local task admission and bounded foreground execution."""
from __future__ import annotations

import hashlib
import json
import threading
from collections import deque
from contextlib import contextmanager


def request_key(values):
    normalized = {**values, "kind": values.get("kind", "review"), "force": values.get("force") is True,
                  "refresh": values.get("refresh") is True}
    if normalized["kind"] == "resolve":
        normalized.setdefault("identifier_kind", "mrn")
    if isinstance(normalized.get("identifiers"), str):
        normalized["identifiers"] = normalized["identifiers"].split()
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class TaskManager:
    def __init__(self, queue, *, foreground_limit=4, pending_limit=64):
        self.queue = queue
        self.foreground_limit, self.pending_limit = foreground_limit, pending_limit
        self.lock = threading.RLock()
        self.active = set()
        self.pending = deque()
        self.requests = {}

    @contextmanager
    def admission(self, count=1, *, foreground=False):
        from .jobs import BusyError

        with self.lock:
            waiting = count
            if foreground:
                waiting -= min(count, self.foreground_limit - len(self.active))
            if self.queue.qsize() + len(self.pending) + waiting > self.pending_limit:
                raise BusyError("此帳號的待執行任務已額滿，請等待完成或暫停後再試。")
            yield

    def existing(self, key):
        with self.lock:
            return self.requests.get(key)

    def submit(self, task_id, key, launch, cancel):
        with self.lock:
            self.requests[key] = task_id
            if len(self.active) < self.foreground_limit:
                self.active.add(task_id)
                launch()
            else:
                self.pending.append((task_id, launch, cancel))

    def complete(self, task_id):
        with self.lock:
            self.active.discard(task_id)
            self.requests = {key: value for key, value in self.requests.items() if value != task_id}
            while self.pending and len(self.active) < self.foreground_limit:
                key, launch, _ = self.pending.popleft()
                self.active.add(key)
                launch()

    def cancel(self, task_id):
        callback = None
        with self.lock:
            for item in self.pending:
                if item[0] == task_id:
                    self.pending.remove(item)
                    callback = item[2]
                    self.requests = {key: value for key, value in self.requests.items() if value != task_id}
                    break
        if callback:
            callback()
        return bool(callback)

    def cancel_pending(self):
        with self.lock:
            ids = [item[0] for item in self.pending]
        for key in ids:
            self.cancel(key)

    def remove_queued(self, task_id):
        # Queue owns its mutex; no running job can be removed by this path.
        with self.queue.mutex:
            job = next((job for job in self.queue.queue if job is not None and job[0].data["id"] == task_id), None)
            if job is None:
                return None
            self.queue.queue.remove(job)
            self.queue.unfinished_tasks -= 1
            if not self.queue.unfinished_tasks:
                self.queue.all_tasks_done.notify_all()
            self.queue.not_full.notify_all()
            return job
