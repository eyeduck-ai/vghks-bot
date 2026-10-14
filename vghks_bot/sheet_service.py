"""Account-scoped Sheets access, independent of cataract analysis or hospital SDK."""
from __future__ import annotations

import threading

from .google_sheets import GoogleSettings
from .sheet_journal import reclaim
from .sheet_mutex import sheet_mutex
from .sheet_schema import HOSPITALS


def account_hospital(app):
    root = getattr(app, "root", None)
    if root is not None and hasattr(root, "registry"):
        account = root.registry.account(app.account_id) or {}
        campus = str(account.get("campus", "")).strip()
        return campus if campus in HOSPITALS else ""
    return "高榮"  # Standalone workspaces predate account campus settings.


class SheetService:
    def __init__(self, app):
        self.app = app
        self.google = GoogleSettings(app.store.directory)
        self.client_factory = self.google.client
        self.lock = threading.Lock()
        self.busy = False
        self.receipt_sources = []

    def connection_sources(self, _=None):
        root = getattr(self.app, "root", None)
        sources = []
        if root is not None and hasattr(root, "registry"):
            for account in root.registry.accounts():
                if account["id"] == self.app.account_id:
                    continue
                config = GoogleSettings.saved(root.directory / "accounts" / account["id"])
                if config and config["configured"] and config["spreadsheet_id"]:
                    sources.append({**{key: account[key] for key in ("id", "username", "label", "campus")}, **config})
        return {"sources": sources}

    def copy_connection(self, values):
        root = getattr(self.app, "root", None)
        if root is None or not hasattr(root, "registry"):
            raise ValueError("此工作區沒有其他帳號可帶入。")
        if root.read_only:
            raise ValueError("唯讀檢閱不能變更刀表連線設定。")
        source = root.registry.account(values.get("source_account_id"))
        if not source or source["id"] == self.app.account_id:
            raise ValueError("請選擇目前資料庫中的其他來源帳號。")
        with self.app.lock:
            sync = getattr(self.app, "sheet_sync", None)
            if self.busy or sync and sync.status()["running"] or not self.lock.acquire(blocking=False):
                raise ValueError("請等待目前刀表作業結束，再帶入連線設定。")
            try:
                return self.google.copy_from(root.directory / "accounts" / source["id"])
            finally:
                self.lock.release()

    def reclaim(self, client, book=None):
        if getattr(getattr(self.app, "root", self.app), "read_only", False):
            return
        receipts = [receipt for source in self.receipt_sources for receipt in source(client.key)]
        if not receipts:
            return
        # Only metadata is needed for recovering cleanup after an older run.
        if book is None:
            reader = getattr(client, "read_metadata", None)
            book = reader() if reader else (getattr(client, "read_all", None) or client.read)()
        reclaim(client, book, receipts)

    def sheet_operation(self, callback):
        with self.app.lock:
            if self.busy:
                raise ValueError("刀表作業進行中，請稍候。")
            self.busy = True
        acquired, client = False, None
        try:
            acquired = self.lock.acquire(blocking=hasattr(self.app, "root"))
            if not acquired:
                raise ValueError("刀表作業進行中，請稍候。")
            client = self.client_factory()
            with sheet_mutex(client.key):
                self.reclaim(client)
                return callback(client)
        finally:
            try:
                if client:
                    client.close()
            finally:
                with self.app.lock:
                    self.busy = False
                if acquired:
                    self.lock.release()
