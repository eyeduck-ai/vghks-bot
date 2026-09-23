"""Account registry and per-account durable workbench documents."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime

from .database_format import schema
from .portable_credentials import seal, unseal
from .settings import timestamp


def fresh(saved, seconds):
    try:
        return 0 <= (datetime.fromisoformat(timestamp()) - datetime.fromisoformat(saved)).total_seconds() < seconds
    except (TypeError, ValueError):
        return False


class Registry:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL, campus TEXT NOT NULL, secret BLOB);
                CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=30)
            try:
                db.row_factory = sqlite3.Row
                schema(db)
                with db:
                    yield db
            finally:
                db.close()

    def accounts(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT id,username,label,campus,secret IS NOT NULL AS remembered FROM accounts ORDER BY rowid")]

    def account(self, key):
        return next((r for r in self.accounts() if r["id"] == key), None)

    def save(self, username, label, campus, password, remember, key=None):
        with self.connect() as db:
            existing = db.execute("SELECT id FROM accounts WHERE username=?", (username,)).fetchone()
            if existing and key and existing[0] != key:
                raise ValueError("此登入帳號已存在。")
            key = key or (existing[0] if existing else uuid.uuid4().hex)
            old = self.account(key)
            if old and old["username"] != username:
                raise ValueError("請新增帳號；既有工作區不可改成另一個登入帳號。")
            secret = seal(db, password.encode("utf-8")) if remember else None
            db.execute("INSERT INTO accounts VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label,campus=excluded.campus,secret=excluded.secret",
                       (key, username, label, campus, secret))
        self.preference("last_account", key)
        return self.account(key)

    def password(self, key):
        with self.connect() as db:
            row = db.execute("SELECT secret FROM accounts WHERE id=?", (key,)).fetchone()
            if not row or row[0] is None:
                return ""
            return unseal(db, row[0]).decode("utf-8")

    def forget(self, key):
        with self.connect() as db:
            db.execute("UPDATE accounts SET secret=NULL WHERE id=?", (key,))

    def preference(self, key, value=None):
        with self.connect() as db:
            if value is not None:
                db.execute("INSERT OR REPLACE INTO preferences VALUES(?,?)", (key, json.dumps(value)))
            row = db.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None


class WorkbenchStore:
    def __init__(self, library):
        self.library = library
        with library.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bot_documents (
                    kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS bot_task_items (
                    task_id TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(task_id,key));
                CREATE TABLE IF NOT EXISTS bot_deleted_records (id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL);
            """)
        for task in self.all("task"):
            if task["status"] in {"queued", "running", "cancelling"}:
                self.save("task", {**task, "status": "paused", "message": "上次中斷，待續跑"})

    def save(self, kind, value):
        value = {**value, "id": value.get("id") or uuid.uuid4().hex, "updated_at": timestamp()}
        with self.library.connect() as db:
            db.execute("INSERT OR REPLACE INTO bot_documents VALUES(?,?,?,?)",
                       (kind, value["id"], json.dumps(value, ensure_ascii=False), value["updated_at"]))
        return value

    def save_batch(self, entries):
        """Commit a current value and its observation/notification together."""
        packed = [(kind, {**value, "id": value.get("id") or uuid.uuid4().hex, "updated_at": timestamp()})
                  for kind, value in entries]
        with self.library.connect() as db:
            db.executemany("INSERT OR REPLACE INTO bot_documents VALUES(?,?,?,?)", [
                (kind, value["id"], json.dumps(value, ensure_ascii=False), value["updated_at"])
                for kind, value in packed])
        return [value for _, value in packed]

    def get(self, kind, key, *, required=True):
        with self.library.connect() as db:
            row = db.execute("SELECT payload FROM bot_documents WHERE kind=? AND id=?", (kind, key)).fetchone()
        if not row and required:
            raise ValueError("資料不存在於此帳號工作區。")
        return json.loads(row[0]) if row else None

    def all(self, kind):
        with self.library.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM bot_documents WHERE kind=? ORDER BY updated_at DESC,id", (kind,))]

    def delete(self, kind, key):
        with self.library.connect() as db:
            db.execute("DELETE FROM bot_documents WHERE kind=? AND id=?", (kind, key))

    def item(self, task, key, value=None):
        with self.library.connect() as db:
            if value is not None:
                db.execute("INSERT OR REPLACE INTO bot_task_items VALUES(?,?,?)", (task, key, json.dumps(value, ensure_ascii=False)))
            row = db.execute("SELECT payload FROM bot_task_items WHERE task_id=? AND key=?", (task, key)).fetchone()
            return json.loads(row[0]) if row else None

    def items(self, task):
        with self.library.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM bot_task_items WHERE task_id=? ORDER BY rowid", (task,))]

    def deleted_since(self, key, created):
        with self.library.connect() as db:
            row = db.execute("SELECT deleted_at FROM bot_deleted_records WHERE id=?", (key,)).fetchone()
        return bool(row and row[0] >= created)

    def purge_records(self, ids):
        if not isinstance(ids, list) or not 1 <= len(ids) <= 1000 or any(
                not isinstance(key, str) or len(key) != 24 or any(c not in "0123456789abcdef" for c in key) for key in ids):
            raise ValueError("請選擇要刪除的就診紀錄。")
        selected = set(ids)
        with self.library.connect() as db:
            # Journal deletion and task-copy removal commit together. If the
            # EXE closes next, Store completes the pending library deletion.
            db.executemany("INSERT OR IGNORE INTO pending_deletions VALUES(?)", [(key,) for key in selected])
            db.executemany("INSERT OR REPLACE INTO bot_deleted_records VALUES(?,?)", [(key, timestamp()) for key in selected])
            for row in db.execute("SELECT task_id,key,payload FROM bot_task_items").fetchall():
                value = json.loads(row[2])
                previous = value.get("records", [])
                value["records"] = [r for r in previous if r["id"] not in selected]
                if len(previous) != len(value["records"]) or value.get("record_id") in selected:
                    if value.get("record_id") in selected:
                        value = {"status": "deleted", "record_id": value["record_id"]}
                    elif not value["records"]:
                        value["status"] = "deleted"
                    db.execute("UPDATE bot_task_items SET payload=? WHERE task_id=? AND key=?", (json.dumps(value, ensure_ascii=False), row[0], row[1]))
            for key in selected:
                db.execute("DELETE FROM bot_documents WHERE kind='numeric' AND id=?", (key,))
            for row in db.execute("SELECT kind,id,payload FROM bot_documents WHERE kind IN ('set','task')").fetchall():
                value = json.loads(row[2])
                for member in value.get("members", []):
                    member["source_records"] = [key for key in member.get("source_records", []) if key not in selected]
                db.execute("UPDATE bot_documents SET payload=? WHERE kind=? AND id=?",
                           (json.dumps(value, ensure_ascii=False), row[0], row[1]))
