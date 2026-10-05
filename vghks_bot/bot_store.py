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
                CREATE TABLE IF NOT EXISTS bot_sdk_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL, service TEXT NOT NULL, method TEXT NOT NULL,
                    status TEXT NOT NULL, error_code TEXT NOT NULL, message TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS bot_sdk_events_task ON bot_sdk_events(task_id,id);
                CREATE TABLE IF NOT EXISTS bot_deleted_records (id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS bot_documents_recent ON bot_documents(kind,updated_at DESC,id);
                CREATE INDEX IF NOT EXISTS bot_task_status ON bot_documents(json_extract(payload,'$.status'),updated_at,id)
                    WHERE kind='task';
                CREATE TABLE IF NOT EXISTS bot_revisions (kind TEXT PRIMARY KEY, revision INTEGER NOT NULL);
                CREATE TRIGGER IF NOT EXISTS bot_insert_revision AFTER INSERT ON bot_documents BEGIN
                    INSERT INTO bot_revisions VALUES(new.kind,1) ON CONFLICT(kind) DO UPDATE SET revision=revision+1;
                END;
                CREATE TRIGGER IF NOT EXISTS bot_update_revision AFTER UPDATE ON bot_documents BEGIN
                    INSERT INTO bot_revisions VALUES(new.kind,1) ON CONFLICT(kind) DO UPDATE SET revision=revision+1;
                END;
                CREATE TRIGGER IF NOT EXISTS bot_delete_revision AFTER DELETE ON bot_documents BEGIN
                    INSERT INTO bot_revisions VALUES(old.kind,1) ON CONFLICT(kind) DO UPDATE SET revision=revision+1;
                END;
                CREATE TRIGGER IF NOT EXISTS bot_event_revision AFTER INSERT ON bot_sdk_events BEGIN
                    INSERT INTO bot_revisions VALUES('sdk_event',1) ON CONFLICT(kind) DO UPDATE SET revision=revision+1;
                END;
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(bot_sdk_events)")}
            if "assessment" not in columns:
                db.execute("ALTER TABLE bot_sdk_events ADD COLUMN assessment TEXT NOT NULL DEFAULT '{}'")
            from .task_data import initialize

            initialize(db)
        for task in self.task_summaries(statuses=("queued", "running", "cancelling")):
            self.save("task", {**self.get("task", task["id"]), "status": "paused", "message": "上次中斷，待續跑"})

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
            state = db.execute("SELECT payload,updated_at,revision FROM bot_task_state WHERE task_id=?", (key,)).fetchone() if kind == "task" else None
        if not row and required:
            raise ValueError("資料不存在於此帳號工作區。")
        value = json.loads(row[0]) if row else None
        if value is not None and state:
            value.update({k: v for k, v in json.loads(state["payload"]).items() if v is not None or k in value})
            value.update(updated_at=state["updated_at"], revision=state["revision"])
        return value

    def all(self, kind):
        if kind == "task":
            return self.documents(kind)
        with self.library.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM bot_documents WHERE kind=? ORDER BY updated_at DESC,id", (kind,))]

    def documents(self, kind, *, filters=None, keys=None, limit=None, offset=0):
        clauses, args = ["d.kind=?"], [kind]
        if keys is not None:
            clauses.append("d.id IN (SELECT value FROM json_each(?))")
            args.append(json.dumps(list(keys)))
        for field, value in (filters or {}).items():
            if field not in {"kind", "report_id", "apply_seq", "cohort_id", "mrn"}:
                raise ValueError("資料篩選欄位不正確。")
            clauses.append("json_extract(d.payload,'$." + field + "')=?")
            args.append(value)
        suffix = ""
        if limit is not None:
            suffix = " LIMIT ? OFFSET ?"
            args += [limit, offset]
        with self.library.connect() as db:
            rows = db.execute("SELECT d.payload,s.payload AS state,s.updated_at,s.revision FROM bot_documents d "
                              "LEFT JOIN bot_task_state s ON d.kind='task' AND s.task_id=d.id WHERE " +
                              " AND ".join(clauses) + " ORDER BY coalesce(s.updated_at,d.updated_at) DESC,d.id" + suffix, args).fetchall()
        result = []
        for row in rows:
            value = json.loads(row["payload"])
            if row["state"]:
                value.update({k: v for k, v in json.loads(row["state"]).items() if v is not None or k in value})
                value.update(updated_at=row["updated_at"], revision=row["revision"])
            result.append(value)
        return result

    def headers(self, kind, fields, *, filters=None):
        """Extract metadata inside SQLite without decoding document bodies."""
        if any(not field.replace('_', '').isalnum() for field in fields):
            raise ValueError("資料欄位不正確。")
        columns = ",".join("json_extract(payload,'$." + field + "') AS " + field for field in fields)
        clauses, args = ["kind=?"], [kind]
        for field, value in (filters or {}).items():
            if field not in {"report_id", "kind", "month", "apply_seq"}:
                raise ValueError("資料篩選欄位不正確。")
            clauses.append("json_extract(payload,'$." + field + "')=?")
            args.append(value)
        with self.library.connect() as db:
            return [dict(row) for row in db.execute("SELECT " + columns + " FROM bot_documents WHERE " +
                " AND ".join(clauses) + " ORDER BY updated_at DESC,id", args)]

    def update_task(self, key, changes):
        """Progress updates never rewrite the task's immutable patient list."""
        with self.library.connect() as db:
            row = db.execute("SELECT payload FROM bot_task_state WHERE task_id=?", (key,)).fetchone()
            if not row:
                raise ValueError("任務不存在。")
            value = {**json.loads(row[0]), **changes}
            now = timestamp()
            db.execute("UPDATE bot_task_state SET payload=?,updated_at=?,revision=revision+1 WHERE task_id=?",
                       (json.dumps(value, ensure_ascii=False), now, key))
        return {**value, "updated_at": now}

    def item_counts(self, key, **options):
        from .task_data import item_counts

        with self.library.connect() as db:
            return item_counts(db, key, **options)

    def revisions(self):
        with self.library.connect() as db:
            return dict(db.execute("SELECT kind,revision FROM bot_revisions"))

    def task_summaries(self, *, watched=None, kinds=None, statuses=None, key=None):
        clauses, args = ["1"], []
        for field, values in (("kind", kinds), ("status", statuses)):
            if values is not None:
                clauses.append("json_extract(payload,'$." + field + "') IN (SELECT value FROM json_each(?))")
                args.append(json.dumps(list(values)))
        if key is not None:
            clauses.append("task_id=?")
            args.append(key)
        if watched is not None:
            watched = list(dict.fromkeys(watched))[:20]
            clauses.append("task_id IN (SELECT task_id FROM bot_task_state WHERE json_extract(payload,'$.status') IN ('queued','running','cancelling') "
                           "UNION SELECT task_id FROM (SELECT task_id FROM bot_task_state ORDER BY updated_at DESC,task_id LIMIT 20) "
                           "UNION SELECT value FROM json_each(?))")
            args.append(json.dumps(watched))
        with self.library.connect() as db:
            rows = db.execute("SELECT task_id,payload,updated_at,revision FROM bot_task_state WHERE " +
                              " AND ".join(clauses) + " ORDER BY updated_at DESC,task_id", args).fetchall()
        return [{**{k: v for k, v in json.loads(row["payload"]).items() if v is not None},
                 "id": row["task_id"], "updated_at": row["updated_at"], "revision": row["revision"]} for row in rows]

    def task_list(self, kinds, values):
        tasks = self.task_summaries(kinds=kinds)
        if str((values or {}).get("summary", "")) == "1":
            return tasks
        return self.documents("task", keys=[task["id"] for task in tasks])

    def delete(self, kind, key):
        with self.library.connect() as db:
            db.execute("DELETE FROM bot_documents WHERE kind=? AND id=?", (kind, key))

    def delete_many(self, kind, keys):
        """Delete exactly these document keys in a single transaction."""
        with self.library.connect() as db:
            cursor = db.executemany("DELETE FROM bot_documents WHERE kind=? AND id=?",
                                   [(kind, key) for key in keys])
            return cursor.rowcount

    def review_note_counts(self):
        """Return task counts without reading or exposing clinical note text."""
        with self.library.connect() as db:
            return dict(db.execute("SELECT substr(id,1,instr(id,':')-1),count(*) "
                "FROM bot_documents WHERE kind='review_note' AND instr(id,':')>0 GROUP BY 1"))

    def item(self, task, key, value=None):
        with self.library.connect() as db:
            if value is not None:
                db.execute("INSERT OR REPLACE INTO bot_task_items VALUES(?,?,?)", (task, key, json.dumps(value, ensure_ascii=False)))
            row = db.execute("SELECT payload FROM bot_task_items WHERE task_id=? AND key=?", (task, key)).fetchone()
            return json.loads(row[0]) if row else None

    def items(self, task):
        with self.library.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM bot_task_items WHERE task_id=? ORDER BY rowid", (task,))]

    def sdk_event(self, *, session_id, task_id, service, method, status, error_code="", message="", assessment=None):
        # Persist only operation names and safe error summaries; SDK arguments and
        # response bodies can contain credentials or clinical data.
        with self.library.connect() as db:
            db.execute("INSERT INTO bot_sdk_events(session_id,task_id,occurred_at,service,method,status,error_code,message,assessment) "
                       "VALUES(?,?,?,?,?,?,?,?,?)", (session_id, task_id, timestamp(), service, method,
                       status, error_code, message, json.dumps(assessment or {})))

    def sdk_events(self, *, task_id="", session_id="", limit=200):
        if not task_id and not session_id:
            return []
        column, key = ("task_id", task_id) if task_id else ("session_id", session_id)
        with self.library.connect() as db:
            rows = db.execute(f"SELECT occurred_at,service,method,status,error_code,message,assessment "
                              f"FROM bot_sdk_events WHERE {column}=? ORDER BY id DESC LIMIT ?", (key, limit))
            return [{**dict(row), "assessment": json.loads(row["assessment"])} for row in rows]

    def sdk_sessions(self):
        with self.library.connect() as db:
            rows = db.execute("SELECT session_id, MIN(occurred_at) AS created_at, MAX(occurred_at) AS updated_at, "
                              "MAX(CASE WHEN status='error' THEN 1 ELSE 0 END) AS has_error "
                              "FROM bot_sdk_events WHERE task_id='' AND service='auth' AND method='login' "
                              "GROUP BY session_id ORDER BY created_at DESC")
            return [{"id": row["session_id"], "kind": "sdk_session", "name": "SDK 工作階段", "created_at": row["created_at"],
                     "updated_at": row["updated_at"], "status": "failed" if row["has_error"] else "completed"}
                    for row in rows]

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
