"""VGHKS-bot root and fully isolated account workspaces."""
from __future__ import annotations

import secrets
import sys
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

from vghks_sdk import LoginRejectedError

from . import __version__
from .approvals import Approvals
from .bot_gateway import AccountGateway, NetworkGate
from .bot_store import Registry
from .database_format import manifest
from .diagnostics import Diagnostics
from .earnings import Earnings
from .jobs import Application, BusyError
from .review import Review
from .scanned_records import ScanArchive
from .scanner import create_sdk, safe_failure
from .settings import FOLLOWUP_TAG, Account, credentials, today
from .storage import StorageError
from .surgery_schedule import SurgerySchedule


class Workspace(Application):
    def __init__(self, root, info):
        self.root = root
        self.account_id, self.username = info["id"], info["username"]
        self.gateway = AccountGateway(root.sdk_factory, root.gate)
        self.session_lock = threading.RLock()
        super().__init__(root.settings, root.directory / "accounts" / self.account_id, self.gateway.lease)
        self.accounts = {self.account_id: Account(self.account_id, username=self.username, label=info["label"])}
        self.launch_token, self.session_token, self.csrf_token = root.launch_token, root.session_token, root.csrf_token
        self.analysis.sheet_lock = root.sheet_lock
        self.review = Review(self)
        self._migrate_default_tags()
        self.scans = ScanArchive(self)
        self.diagnostics = Diagnostics(self)
        self.gateway.diagnostic = self.diagnostics.save
        self.gateway.event = self.review.db.sdk_event
        self.gateway.recorder_factory = self.diagnostics.recorder
        self.approvals = Approvals(self)
        self.earnings = Earnings(self)
        self.surgery_schedule = SurgerySchedule(self)
        self.entered = False
        self.offline_mode = False

    def _migrate_default_tags(self):
        if self.root.read_only:
            return
        key = "default-tags-v1"
        if self.review.db.get("preferences", key, required=False):
            return
        tags = self.settings.categories
        # A fresh workspace already receives the new defaults. Explicit custom
        # constructor settings are preserved; only saved older accounts migrate.
        if (self.store.directory / "settings.json").exists() and not any(
            tag.id == FOLLOWUP_TAG.id or tag.name == FOLLOWUP_TAG.name for tag in tags
        ):
            if len(tags) >= 50:
                self.store.warnings.append("自動 TAG 已達 50 個，尚未加入追蹤；請先調整 TAG 設定。")
                return
            self.save_settings({"categories": [asdict(tag) for tag in (*tags, FOLLOWUP_TAG)]})
        self.review.db.save("preferences", {"id": key, "followup": True})

    def bootstrap(self):
        return {**super().bootstrap(), "context": self.root.context_token, "read_only": self.root.read_only}

    def _active_account(self, _key):
        # Account queues serialize jobs; multiple queued jobs are allowed.
        return False

    def login(self, password, info):
        with self.session():
            self._login(password, info)

    @contextmanager
    def session(self):
        if not self.session_lock.acquire(timeout=30):
            raise BusyError("此帳號正在登入或登出，請稍後重試。")
        try:
            yield
        finally:
            self.session_lock.release()

    def _login(self, password, info):
        settings = replace(self.settings, username=self.username, password=password)
        self.gateway.login(settings)
        account = self.accounts[self.account_id]
        self.accounts[self.account_id] = replace(account, password=password, label=info["label"])
        self.entered = True
        self.offline_mode = False

    def logout(self):
        with self.session():
            self._logout()

    def _logout(self):
        self.analysis.cataract_queue.pause("帳號已登出，重新登入後可繼續背景抓取。")
        self.gateway.online = False
        with self.lock:
            for state in self.states.values():
                if not state.data.get("cataract_background"):
                    state.cancel.set()
        with self.review.lock:
            for state in self.review.foreground.values():
                state.cancel.set()
        self.task_manager.cancel_pending()
        self.gateway.close()
        self.accounts[self.account_id] = replace(self.accounts[self.account_id], password="")
        self.entered = False
        self.offline_mode = False

    def delete_records(self, values):
        with self.lock, self.review.lock:
            if self.review.foreground:
                raise BusyError("請先等待延伸查詢完成或暫停，再刪除。")
            self._available()
            if not self.idle.is_set() or self.analysis.sheet_busy:
                raise BusyError("請先暫停此帳號的任務，再刪除。")
            ids = values.get("ids")
            # Validate and durably mark before removing derived task copies.
            self.review.db.purge_records(ids)
            return super().delete_records(values)

    def library_search(self, values):
        values = dict(values)
        task_id = values.pop("task_id", "")
        if not task_id:
            return super().library_search(values)
        tasks = self.review.db.task_summaries(key=task_id)
        if not tasks:
            raise ValueError("資料不存在於此帳號工作區。")
        task = tasks[0]
        if task["kind"] != "review":
            raise ValueError("請選擇病歷檢閱任務。")
        values.update(task_id=task_id)
        values.setdefault("limit", 40)
        result = self.store.library.search(values, self.settings)
        return {**result, "categories": self.settings.public()["categories"], "stats": self.store.library.stats()}

    def request_close(self):
        if hasattr(self, "review"):
            with self.review.lock:
                for state in self.review.foreground.values():
                    state.cancel.set()
        super().request_close()

    def close(self):
        self.request_close()
        self.gateway.close(wait=True)
        with self.review.lock:
            threads = list(self.review.threads.values())
        for thread in threads:
            thread.join()
        self.worker.join()


class BotApplication:
    def __init__(self, settings, data_dir=None, sdk_factory=create_sdk, *, read_only=False, source_directory=None):
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
        self.directory = Path(data_dir or base / "VGHKS-bot-data").resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = manifest(self.directory, create=True)
        self.read_only = read_only
        self.source_directory = Path(source_directory or self.directory)
        self.context_token = secrets.token_urlsafe(24)
        self._lock_file = (self.directory / ".lock").open("a+b")
        if sys.platform == "win32":
            import msvcrt
            try:
                self._lock_file.write(b"0")
                self._lock_file.flush()
                self._lock_file.seek(0)
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                self._lock_file.close()
                raise StorageError("此 VGHKS-bot 資料目錄已有程式使用。") from exc
        try:
            self.registry = Registry(self.directory / "accounts.sqlite3")
            self.gate = NetworkGate(self.registry.preference("concurrency") or 3)
        except Exception:
            self._lock_file.close()
            raise
        self.settings, self.sdk_factory = settings, sdk_factory
        self.workspaces = {}
        self.lock = threading.RLock()
        self.sheet_lock = threading.Lock()
        self.launch_token, self.session_token, self.csrf_token = (secrets.token_urlsafe(32) for _ in range(3))
        self.closing = False
        self.scheduler_stop = threading.Event()
        self.scheduler = threading.Thread(target=self._schedule, name="account-monitors", daemon=True)
        self.scheduler.start()

    def _schedule(self):
        while not self.scheduler_stop.wait(30):
            self.follow_up_tick()

    def follow_up_tick(self):
        # Switching or copying a database holds this lock. Never queue work in
        # the middle of a snapshot, and never block shutdown waiting for it.
        if not self.lock.acquire(blocking=False):
            return
        try:
            if self.closing or self.read_only:
                return
            for workspace in list(self.workspaces.values()):
                for schedule in (workspace.approvals.tracker.schedule, workspace.earnings.monitor.schedule):
                    try:
                        schedule()
                    except Exception:
                        # Local due reminders remain visible; a later tick can retry.
                        continue
        finally:
            self.lock.release()

    def workspace(self, key, *, require_entered=True):
        with self.lock:
            info = self.registry.account(key)
            if not info:
                raise ValueError("帳號不存在。")
            if key not in self.workspaces:
                self.workspaces[key] = Workspace(self, info)
            workspace = self.workspaces[key]
        if require_entered and not workspace.entered:
            raise ValueError("請先登入或選擇離線檢閱此帳號。")
        return workspace

    def public_accounts(self):
        return [{**a, "online": bool(self.workspaces.get(a["id"]) and self.workspaces[a["id"]].gateway.online)}
                for a in self.registry.accounts()]

    def bootstrap(self):
        return {"version": __version__, "csrf": self.csrf_token, "today": today().isoformat(),
                "accounts": self.public_accounts(), "last_account": self.registry.preference("last_account"),
                "concurrency": self.gate.maximum, "data_dir": str(self.source_directory),
                "database": self.database, "read_only": self.read_only, "context": self.context_token,
                "database_notice": getattr(self, "database_notice", "")}

    def login(self, values):
        if self.read_only:
            raise ValueError("唯讀資料庫不能登入院內，請建立可編輯副本。")
        if self.closing:
            raise ValueError("程式正在結束。")
        key = values.get("id")
        old = self.registry.account(key) if key else None
        if key and not old:
            raise ValueError("帳號不存在。")
        username, password = credentials(values.get("username", old["username"] if old else ""), values.get("password", ""))
        if not username:
            raise ValueError("請輸入登入帳號。")
        existing = next((a for a in self.registry.accounts() if a["username"] == username), None)
        if not key and existing:
            old, key = existing, existing["id"]
        if old and old["username"] != username:
            raise ValueError("請以新增帳號方式建立其他登入帳號。")
        if not password and key:
            workspace = self.workspaces.get(key)
            password = (workspace.accounts[key].password if workspace and workspace.entered else "") or self.registry.password(key)
        if not password:
            raise ValueError("請輸入此帳號的院內密碼。")
        label, campus = values.get("label", old["label"] if old else ""), values.get("campus", old["campus"] if old else "高榮")
        if any(not isinstance(v, str) or len(v) > 60 for v in (label, campus)):
            raise ValueError("帳號名稱與院區最多 60 字。")
        remember = values.get("remember", bool(old["remembered"]) if old else True)
        if type(remember) is not bool:
            raise ValueError("保存登入設定不正確。")
        key = key or uuid.uuid4().hex
        info = {"id": key, "username": username, "label": label, "campus": campus}
        with self.lock:
            if self.closing:
                raise ValueError("程式正在結束。")
            workspace = self.workspaces.get(key)
            if workspace is None:
                workspace = self.workspaces[key] = Workspace(self, info)
        with workspace.session():
            try:
                workspace.login(password, info)
            except BusyError:
                raise
            except Exception as exc:
                message, _ = safe_failure(exc)
                raise ValueError("登入未完成。" + message) from exc
            try:
                self.registry.save(username, label, campus, password, remember, key)
            except Exception:
                workspace.logout()
                raise
        return {"account": {**self.registry.account(key), "online": True}}

    def activate(self, key):
        """Select an account and restore only its own SDK session when needed."""
        workspace = self.workspace(key, require_entered=False)
        with workspace.session():
            return self._activate(key, workspace)

    def _activate(self, key, workspace):
        if self.read_only:
            return {**self.offline(key), "status": "offline"}
        if workspace.gateway.online:
            workspace.entered = True
            workspace.offline_mode = False
            self.registry.preference("last_account", key)
            return {"account": self.registry.account(key), "online": True, "status": "ready"}
        password = workspace.accounts[key].password or self.registry.password(key)
        if not password:
            return {"account": self.registry.account(key), "online": False, "status": "needs_password"}
        try:
            workspace.login(password, self.registry.account(key))
        except Exception as exc:
            message, code = safe_failure(exc)
            from .connection_state import failure_state

            issue = failure_state(exc)
            status = ("password_change_required" if issue["action"] == "password_change" else
                      "needs_password" if isinstance(exc, LoginRejectedError) or issue["action"] == "credentials" else
                      "unavailable")
            return {"account": self.registry.account(key), "online": False, "status": status,
                    "message": message, "error_code": code, "connection_issue": issue}
        self.registry.preference("last_account", key)
        return {"account": self.registry.account(key), "online": True, "status": "ready"}

    def offline(self, key):
        workspace = self.workspace(key, require_entered=False)
        with workspace.session():
            if workspace.gateway.online:
                workspace.logout()
            workspace.entered = True
            workspace.offline_mode = True
            self.registry.preference("last_account", key)
            return {"account": self.registry.account(key), "online": workspace.gateway.online}

    def logout(self, key):
        self.workspace(key, require_entered=False).logout()
        return {"ok": True}

    def forget(self, key):
        if not self.registry.account(key):
            raise ValueError("帳號不存在。")
        self.registry.forget(key)
        return {"ok": True}

    def set_concurrency(self, value):
        self.gate.set_limit(value)
        self.registry.preference("concurrency", value)
        return {"concurrency": value}

    def request_close(self):
        with self.lock:
            self.closing = True
            self.scheduler_stop.set()
            for workspace in self.workspaces.values():
                workspace.request_close()

    def close(self):
        if self._lock_file.closed:
            return
        self.request_close()
        self.scheduler.join()
        for workspace in self.workspaces.values():
            workspace.close()
        self._lock_file.close()
