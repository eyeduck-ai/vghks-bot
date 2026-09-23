"""Shared, durable analysis data. SDK sessions and credentials never enter SQLite."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid

from .library import encoded
from .settings import timestamp
from .storage import StorageError


def digest(value):
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("分析清單或作業代碼不正確。")
    return value


class AnalysisStore:
    def __init__(self, library):
        self.library = library
        self.assets = library.path.parent / "assets"
        self.assets.mkdir(exist_ok=True)
        with library.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS analysis_cohorts (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS analysis_runs (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS analysis_steps (
                    mrn TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, saved_at TEXT NOT NULL, account TEXT NOT NULL,
                    PRIMARY KEY(mrn,key));
                CREATE TABLE IF NOT EXISTS analysis_versions (
                    mrn TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL,
                    payload TEXT NOT NULL, saved_at TEXT NOT NULL,
                    PRIMARY KEY(mrn,key,digest));
                CREATE TABLE IF NOT EXISTS analysis_assets (
                    digest TEXT PRIMARY KEY, mime TEXT NOT NULL, size INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS analysis_asset_refs (
                    mrn TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL,
                    PRIMARY KEY(mrn,key,digest));
                CREATE TABLE IF NOT EXISTS sheet_previews (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
            """)
            for row in db.execute("SELECT id,payload FROM analysis_runs").fetchall():
                value = json.loads(row["payload"])
                if value.get("status") in {"queued", "running", "cancelling"}:
                    value.update(status="interrupted", message="上次中斷，可接續已保存資料。")
                    db.execute("UPDATE analysis_runs SET payload=? WHERE id=?", (encoded(value), row["id"]))
        self.prune_assets()

    def prune_assets(self):
        # Recover interrupted deletions/writes. Only application-named blobs
        # inside this data directory are eligible; referenced content is retained.
        with self.library.connect() as db:
            live = {r[0] for r in db.execute("SELECT DISTINCT digest FROM analysis_asset_refs")}
            db.execute("DELETE FROM analysis_assets WHERE digest NOT IN (SELECT digest FROM analysis_asset_refs)")
        removed = 0
        for path in self.assets.iterdir():
            if path.is_file() and (re.fullmatch(r"[a-f0-9]{64}", path.name) and path.name not in live
                                   or re.fullmatch(r"[a-f0-9]{64}\.[a-f0-9]{32}\.tmp", path.name)):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def save_document(self, table, value):
        if table not in {"analysis_cohorts", "analysis_runs", "sheet_previews"}:
            raise ValueError("資料種類不正確。")
        identifier(value["id"])
        with self.library.connect() as db:
            db.execute(f"INSERT OR REPLACE INTO {table} VALUES(?,?,?)",
                       (value["id"], encoded(value), timestamp()))
        return value

    def document(self, table, key):
        if table not in {"analysis_cohorts", "analysis_runs", "sheet_previews"}:
            raise ValueError("資料種類不正確。")
        identifier(key)
        with self.library.connect() as db:
            row = db.execute(f"SELECT payload FROM {table} WHERE id=?", (key,)).fetchone()
        if row is None:
            raise ValueError("資料已刪除或不存在。")
        return json.loads(row[0])

    def documents(self, table):
        if table not in {"analysis_cohorts", "analysis_runs", "sheet_previews"}:
            raise ValueError("資料種類不正確。")
        with self.library.connect() as db:
            return [json.loads(r[0]) for r in db.execute(f"SELECT payload FROM {table} ORDER BY updated_at DESC")]

    def step(self, mrn, key):
        with self.library.connect() as db:
            row = db.execute("SELECT * FROM analysis_steps WHERE mrn=? AND key=?", (mrn, key)).fetchone()
        if row:
            return {**dict(row), "payload": json.loads(row["payload"])}
        return None

    def steps(self, mrn, kind=None):
        with self.library.connect() as db:
            rows = db.execute("SELECT * FROM analysis_steps WHERE mrn=?" + (" AND kind=?" if kind else ""),
                              (mrn, kind) if kind else (mrn,)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def raw_data(self, mrn):
        with self.library.connect() as db:
            versions = [dict(r) for r in db.execute(
                "SELECT key,digest,payload,saved_at FROM analysis_versions WHERE mrn=? ORDER BY key,saved_at", (mrn,))]
        for value in versions:
            value["payload"] = json.loads(value["payload"])
        return {"steps": self.steps(mrn), "versions": versions}

    def save_step(self, mrn, key, kind, payload, account):
        content, saved = encoded(payload), timestamp()
        with self.library.connect() as db:
            db.execute("INSERT OR IGNORE INTO analysis_versions VALUES(?,?,?,?,?)",
                       (mrn, key, digest(payload), content, saved))
            db.execute("INSERT OR REPLACE INTO analysis_steps VALUES(?,?,?,?,?,?)",
                       (mrn, key, kind, content, saved, account))

    def save_asset(self, mrn, key, asset, account):
        content = asset.content
        if not content:
            raise ValueError("附件內容為空。")
        # Only serve validated raster/PDF bytes, never active HTML from an endpoint.
        if content.startswith(b"%PDF-"):
            mime = "application/pdf"
        elif content.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif content.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif content.startswith((b"GIF87a", b"GIF89a")):
            mime = "image/gif"
        else:
            raise ValueError("附件不是可辨識的 PDF 或影像。")
        sha = hashlib.sha256(content).hexdigest()
        path = self.assets / sha
        if not path.exists():
            temporary = self.assets / (sha + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as file:
                    file.write(content)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, path)
            except OSError as exc:
                raise StorageError("無法保存附件，已停止抓取。") from exc
            finally:
                temporary.unlink(missing_ok=True)
        value = {"digest": sha, "mime": mime, "size": len(content)}
        with self.library.connect() as db:
            db.execute("INSERT OR IGNORE INTO analysis_assets VALUES(?,?,?)", (sha, mime, len(content)))
            db.execute("INSERT OR IGNORE INTO analysis_asset_refs VALUES(?,?,?)", (mrn, key, sha))
        self.save_step(mrn, key, "asset", value, account)
        return value

    def asset(self, sha):
        if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ValueError("附件代碼不正確。")
        with self.library.connect() as db:
            row = db.execute("""SELECT * FROM analysis_assets WHERE digest=?
                AND EXISTS(SELECT 1 FROM analysis_asset_refs WHERE digest=?)""", (sha, sha)).fetchone()
        path = self.assets / sha
        if row is None or not path.is_file():
            raise ValueError("附件已刪除或尚未取得。")
        return path, row["mime"]

    def records(self, mrn):
        with self.library.connect() as db:
            return [json.loads(r[0]) for r in db.execute("""SELECT payload FROM records
                WHERE mrn=? AND id NOT IN (SELECT record_id FROM pending_deletions)
                ORDER BY day DESC,updated_at DESC""", (mrn,))]

    def invalidate_previews(self, mrns):
        selected = set(mrns)
        with self.library.connect() as db:
            for row in db.execute("SELECT id,payload FROM sheet_previews").fetchall():
                payload = json.loads(row["payload"])
                if selected.intersection(payload.get("mrns", [])):
                    db.execute("DELETE FROM sheet_previews WHERE id=?", (row["id"],))

    def delete_patients(self, mrns):
        self.invalidate_previews(mrns)
        with self.library.connect() as db:
            for mrn in mrns:
                for table in ("analysis_steps", "analysis_versions", "analysis_asset_refs"):
                    db.execute(f"DELETE FROM {table} WHERE mrn=?", (mrn,))
            # Remove derived per-patient progress/errors, including historical runs.
            for row in db.execute("SELECT id,payload FROM analysis_runs").fetchall():
                value = json.loads(row["payload"])
                value["issues"] = [v for v in value.get("issues", []) if v.get("mrn") not in mrns]
                value["members"] = [v for v in value.get("members", []) if v["mrn"] not in mrns]
                value["message"] = "資料已手動刪除；其餘已保存資料保留。"
                db.execute("UPDATE analysis_runs SET payload=? WHERE id=?", (encoded(value), row["id"]))
            unused = [r[0] for r in db.execute("""SELECT digest FROM analysis_assets
                WHERE digest NOT IN (SELECT digest FROM analysis_asset_refs)""")]
            db.execute("DELETE FROM analysis_assets WHERE digest NOT IN (SELECT digest FROM analysis_asset_refs)")
        for sha in unused:
            (self.assets / sha).unlink(missing_ok=True)
        return {"ok": True, "patients": len(mrns), "attachments": len(unused)}
