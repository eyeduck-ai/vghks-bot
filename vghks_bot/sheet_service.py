"""Account-scoped Sheets access, independent of cataract analysis or hospital SDK."""
from __future__ import annotations

import threading

from .google_sheets import GoogleSettings
from .sheet_journal import reclaim
from .sheet_mutex import sheet_mutex


class SheetService:
    def __init__(self, app):
        self.app = app
        self.google = GoogleSettings(app.store.directory)
        self.client_factory = self.google.client
        self.lock = threading.Lock()
        self.busy = False
        self.receipt_sources = []

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
