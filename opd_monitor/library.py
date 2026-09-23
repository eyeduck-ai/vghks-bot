"""Persistent clinical cache and encounter corpus in the account database."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from .settings import timestamp, today
from .soap_preview import ap_preview
from .storage import StorageError
from .tags import classification


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def registration_id(row):
    fields = (row.get("visit_date"), row.get("mrn"), row.get("section_code"), row.get("room"), row.get("doctor_card"))
    return hashlib.sha256(encoded(fields).encode()).hexdigest()[:24]


def current_cache(day, saved_at):
    if day < today().isoformat():
        # A list/SOAP fetched during that clinic day may still be incomplete.
        # Refresh it once after the day ends, then reuse it indefinitely.
        return isinstance(saved_at, str) and saved_at[:10] > day
    try:
        return 0 <= (datetime.fromisoformat(timestamp()) - datetime.fromisoformat(saved_at)).total_seconds() < 60
    except (ValueError, TypeError):
        return False


class Library:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS list_cache (
                    account TEXT NOT NULL, day TEXT NOT NULL, fetched_at TEXT NOT NULL,
                    rows_json TEXT NOT NULL, PRIMARY KEY(account, day));
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY, mrn TEXT NOT NULL, day TEXT NOT NULL,
                    section TEXT NOT NULL, payload TEXT NOT NULL,
                    first_saved TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS records_day ON records(day, mrn);
                CREATE INDEX IF NOT EXISTS records_patient ON records(mrn, day);
                CREATE TABLE IF NOT EXISTS versions (
                    record_id TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL,
                    saved_at TEXT NOT NULL, PRIMARY KEY(record_id, digest));
                CREATE TABLE IF NOT EXISTS sources (
                    record_id TEXT NOT NULL, account TEXT NOT NULL, run_id TEXT NOT NULL,
                    PRIMARY KEY(record_id, account, run_id));
                CREATE TABLE IF NOT EXISTS checks (
                    account TEXT NOT NULL, registration_id TEXT NOT NULL, day TEXT NOT NULL,
                    record_ids TEXT NOT NULL, checked_at TEXT NOT NULL,
                    PRIMARY KEY(account, registration_id));
                CREATE TABLE IF NOT EXISTS imports (run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS pending_deletions (record_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS patient_tags (
                    mrn TEXT NOT NULL, tag_id TEXT NOT NULL, added_at TEXT NOT NULL,
                    PRIMARY KEY(mrn, tag_id));
                CREATE INDEX IF NOT EXISTS patient_tags_group ON patient_tags(tag_id, mrn);
            """)

    @contextmanager
    def connect(self):
        from .database_format import schema

        with self.lock:
            db = None
            try:
                db = sqlite3.connect(self.path, timeout=15)
                schema(db)
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA synchronous=FULL")
                with db:
                    yield db
            except sqlite3.Error as exc:
                raise StorageError("本機資料庫無法讀寫，先前資料已保留。") from exc
            finally:
                if db is not None:
                    db.close()

    def cached_list(self, account, day, *, allow_stale=False):
        with self.connect() as db:
            value = db.execute("SELECT * FROM list_cache WHERE account=? AND day=?", (account, day)).fetchone()
        if value and (allow_stale or current_cache(day, value["fetched_at"])):
            return {"rows": json.loads(value["rows_json"]), "fetched_at": value["fetched_at"]}
        return None

    def save_list(self, account, day, rows, observed_at=None):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO list_cache VALUES(?,?,?,?)", (account, day, observed_at or timestamp(), encoded(rows)))

    def registration_statuses(self, account, start, end):
        """One DB read for a whole list; do not load SOAP just to show status."""
        with self.connect() as db:
            checked = db.execute("SELECT registration_id,record_ids FROM checks WHERE account=? AND day BETWEEN ? AND ?", (account, start, end)).fetchall()
            existing = {r[0] for r in db.execute("SELECT id FROM records WHERE id NOT IN (SELECT record_id FROM pending_deletions)")}
        result = {}
        for row in checked:
            ids = json.loads(row["record_ids"])
            if all(key in existing for key in ids):
                result[row["registration_id"]] = ids
        return result

    def delete_list(self, account, start, end):
        with self.connect() as db:
            count = db.execute("DELETE FROM list_cache WHERE account=? AND day BETWEEN ? AND ?", (account, start, end)).rowcount
        return count

    def save_record(self, record, account, run_id, observed_at=None):
        payload = {k: v for k, v in record.items() if k not in {"matches", "manual_tags", "accounts", "version_count", "first_saved", "updated_at", "cached", "ap_preview", "tag_scope_issues"}}
        content = encoded(payload)
        digest = hashlib.sha256(content.encode()).hexdigest()
        now = observed_at or record.get("updated_at") or timestamp()
        with self.connect() as db:
            if db.execute("SELECT 1 FROM pending_deletions WHERE record_id=?", (record["id"],)).fetchone():
                return
            db.execute("""INSERT INTO records VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                payload=excluded.payload, updated_at=excluded.updated_at WHERE excluded.updated_at>=records.updated_at""",
                (record["id"], record["mrn"], record["date"], record.get("section_code", ""), content, now, now))
            db.execute("INSERT OR IGNORE INTO versions VALUES(?,?,?,?)", (record["id"], digest, content, now))
            db.execute("INSERT OR IGNORE INTO sources VALUES(?,?,?)", (record["id"], account, run_id))

    def get_record(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM records WHERE id=? AND id NOT IN (SELECT record_id FROM pending_deletions)", (key,)).fetchone()
            if not row:
                return None
            result = json.loads(row["payload"])
            result.update(first_saved=row["first_saved"], updated_at=row["updated_at"])
            result["accounts"] = [r[0] for r in db.execute("SELECT DISTINCT account FROM sources WHERE record_id=? ORDER BY account", (key,))]
            result["versions"] = [{**json.loads(r["payload"]), "saved_at": r["saved_at"]} for r in db.execute("SELECT * FROM versions WHERE record_id=? ORDER BY saved_at DESC", (key,))]
            result["version_count"] = len(result["versions"])
            return result

    def completed_registration(self, account, row, *, allow_stale=False):
        with self.connect() as db:
            checked = db.execute("SELECT * FROM checks WHERE account=? AND registration_id=?", (account, registration_id(row))).fetchone()
            if not checked or (not allow_stale and not current_cache(checked["day"], checked["checked_at"])):
                return None
            ids = json.loads(checked["record_ids"])
            records = []
            for key in ids:
                record = db.execute("SELECT payload,updated_at FROM records WHERE id=? AND id NOT IN (SELECT record_id FROM pending_deletions)", (key,)).fetchone()
                if record is None:
                    return None
                records.append({**json.loads(record[0]), "updated_at": record[1]})
            return records

    def save_check(self, account, row, record_ids, observed_at=None):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO checks VALUES(?,?,?,?,?)", (account, registration_id(row), row["visit_date"], encoded(record_ids), observed_at or timestamp()))

    def import_run(self, summary, records):
        with self.connect() as db:
            row = db.execute("SELECT revision FROM imports WHERE run_id=?", (summary["id"],)).fetchone()
        if row and row[0] == summary["revision"]:
            return
        for record in records:
            if all(k in record for k in ("id", "mrn", "date", "soap")):
                if not summary.get("source_id"):
                    self.save_record(record, summary.get("account", ""), summary["id"], summary.get("created_at"))
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO imports VALUES(?,?)", (summary["id"], summary["revision"]))

    def imported(self, summary):
        with self.connect() as db:
            row = db.execute("SELECT revision FROM imports WHERE run_id=?", (summary["id"],)).fetchone()
            return bool(row and row[0] == summary["revision"])

    def search(self, values, settings, *, all_results=False):
        query = values.get("q", "")
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("搜尋字串最多 500 字。")
        clauses = ["r.id NOT IN (SELECT record_id FROM pending_deletions)"]
        args = []
        for field, expression in (("start", "r.day>=?"), ("end", "r.day<=?"), ("mrn", "r.mrn=?")):
            if values.get(field):
                if not isinstance(values[field], str):
                    raise ValueError("篩選條件不正確。")
                clauses.append(expression)
                args.append(values[field])
        for field in ("account", "tag", "group"):
            if not isinstance(values.get(field, ""), str):
                raise ValueError("篩選條件不正確。")
        if values.get("start") and values.get("end") and values["start"] > values["end"]:
            raise ValueError("結束日期不可早於開始日期。")
        if values.get("account"):
            clauses.append("EXISTS(SELECT 1 FROM sources s WHERE s.record_id=r.id AND s.account=?)")
            args.append(values["account"])
        category = values.get("tag", "")
        if category not in {"", "__untagged", "__tagged", *(c.id for c in settings.categories)}:
            raise ValueError("tag 不存在，請重新選擇。")
        with self.connect() as db:
            raw = db.execute("SELECT r.* FROM records r WHERE " + " AND ".join(clauses) + " ORDER BY day DESC, mrn, id", args).fetchall()
            accounts = {}
            for row in db.execute("SELECT DISTINCT record_id, account FROM sources ORDER BY account"):
                accounts.setdefault(row[0], []).append(row[1])
            versions = dict(db.execute("SELECT record_id, COUNT(*) FROM versions GROUP BY record_id"))
        result = []
        counts = {c.id: 0 for c in settings.categories}
        manual = self.manual_tags(settings)
        untagged = 0
        scope_issue_count = 0
        for row in raw:
            record = json.loads(row["payload"])
            # Unicode casefold + literal substring also supports Chinese and
            # punctuation, unlike token-based full-text indexes.
            if query.strip().casefold() not in " ".join(str(record.get(k, "")) for k in ("mrn", "name", "sex", "age", "date", "section", "doctor", "case_no", "soap")).casefold():
                continue
            record.update(classification(record, settings))
            scope_issue_count += bool(record["tag_scope_issues"])
            record["manual_tags"] = manual.get(record["mrn"], [])
            tags = {m["category"] for m in record["matches"]} | {t["id"] for t in record["manual_tags"]}
            for tag in tags:
                counts[tag] += 1
            untagged += int(not tags)
            if category == "__untagged" and tags or category == "__tagged" and not tags or category not in {"", "__untagged", "__tagged"} and category not in tags:
                continue
            record.update(accounts=accounts.get(record["id"], []), version_count=versions.get(record["id"], 1), first_saved=row["first_saved"], updated_at=row["updated_at"])
            result.append(record)
        try:
            offset, limit = int(values.get("offset", 0)), int(values.get("limit", 50))
        except (ValueError, TypeError):
            raise ValueError("分頁不正確。") from None
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("分頁不正確。")
        patients = list(dict.fromkeys(r["mrn"] for r in result))
        by_patient = values.get("group") == "patient"
        selected = set(patients[offset:offset + limit])
        page = result if all_results else [r for r in result if r["mrn"] in selected] if by_patient else result[offset:offset + limit]
        for record in page:
            record["ap_preview"] = ap_preview(record)
        return {"records": page, "total": len(result), "patients": len(patients), "page_total": len(patients) if by_patient else len(result),
            "tag_counts": counts, "untagged": untagged, "offset": offset, "limit": limit,
            "scope_issue_count": scope_issue_count}

    def manual_tags(self, settings):
        definitions = {t.id: t for t in settings.categories}
        result = {}
        with self.connect() as db:
            for row in db.execute("SELECT * FROM patient_tags ORDER BY added_at, tag_id"):
                if row["tag_id"] in definitions:
                    result.setdefault(row["mrn"], []).append({"id": row["tag_id"],
                        "name": definitions[row["tag_id"]].name, "added_at": row["added_at"]})
        return result

    def prune_patient_tags(self, settings):
        ids = [t.id for t in settings.categories]
        with self.connect() as db:
            db.execute("DELETE FROM patient_tags WHERE tag_id NOT IN (" + ",".join("?" for _ in ids) + ")", ids)

    def set_patient_tags(self, mrns, tag_ids, *, remove=False):
        with self.connect() as db:
            before = db.total_changes
            if remove:
                db.executemany("DELETE FROM patient_tags WHERE mrn=? AND tag_id=?",
                               [(mrn, tag) for mrn in mrns for tag in tag_ids])
            else:
                added_at = timestamp()
                db.executemany("INSERT OR IGNORE INTO patient_tags VALUES(?,?,?)",
                               [(mrn, tag, added_at) for mrn in mrns for tag in tag_ids])
            return db.total_changes - before

    def tag_patients(self, values, settings, *, all_results=False):
        query, tag, source = (values.get(k, "") for k in ("q", "tag", "source"))
        if not all(isinstance(v, str) for v in (query, tag, source)) or len(query) > 500:
            raise ValueError("tag 群組篩選格式不正確。")
        if not isinstance(values.get("mrn", ""), str):
            raise ValueError("病歷號格式不正確。")
        if tag and tag not in {t.id for t in settings.categories} or source not in {"", "auto", "manual"}:
            raise ValueError("tag 或來源不存在，請重新選擇。")
        grouped = {}

        def patient(mrn):
            return grouped.setdefault(mrn, {"mrn": mrn, "name": "", "accounts": set(),
                "source_records": [], "auto_tags": {}, "manual_tags": []})

        records = self.search({}, settings, all_results=True)["records"]
        for record in records:
            item = patient(record["mrn"])
            item["name"] = item["name"] or record.get("name", "")
            item["accounts"].update(record.get("accounts", []))
            item["source_records"].append(record["id"])
            for match in record["matches"]:
                item["auto_tags"][match["category"]] = {"id": match["category"], "name": match["category_name"]}
        for mrn, tags in self.manual_tags(settings).items():
            patient(mrn)["manual_tags"] = tags
        result, counts = [], {t.id: 0 for t in settings.categories}
        for item in grouped.values():
            if values.get("mrn") and item["mrn"] != values["mrn"]:
                continue
            if query.strip().casefold() not in (item["mrn"] + " " + item["name"]).casefold():
                continue
            auto_ids, manual_ids = set(item["auto_tags"]), {t["id"] for t in item["manual_tags"]}
            tags = auto_ids if source == "auto" else manual_ids if source == "manual" else auto_ids | manual_ids
            for key in tags:
                counts[key] += 1
            if not tags or tag and tag not in tags:
                continue
            item["accounts"] = sorted(item["accounts"])
            item["auto_tags"] = list(item["auto_tags"].values())
            result.append(item)
        result.sort(key=lambda p: p["mrn"])
        try:
            offset, limit = int(values.get("offset", 0)), int(values.get("limit", 40))
        except (ValueError, TypeError):
            raise ValueError("分頁不正確。") from None
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("分頁不正確。")
        return {"patients": result if all_results else result[offset:offset + limit],
                "total": len(result), "tag_counts": counts, "offset": offset, "limit": limit,
                "categories": settings.public()["categories"]}

    def stats(self):
        with self.connect() as db:
            counts = db.execute("SELECT COUNT(*), COUNT(DISTINCT mrn) FROM records WHERE id NOT IN (SELECT record_id FROM pending_deletions)").fetchone()
            return {"records": counts[0], "patients": counts[1], "cached_days": db.execute("SELECT COUNT(*) FROM list_cache").fetchone()[0],
                "accounts": [r[0] for r in db.execute("SELECT DISTINCT account FROM sources ORDER BY account")]}

    def pending_deletions(self):
        with self.connect() as db:
            return [r[0] for r in db.execute("SELECT record_id FROM pending_deletions")]

    def prepare_delete(self, ids):
        if not isinstance(ids, list) or not 1 <= len(ids) <= 1000 or any(not isinstance(k, str) or len(k) != 24 or any(c not in "0123456789abcdef" for c in k) for k in ids):
            raise ValueError("請選擇要刪除的就診紀錄。")
        with self.connect() as db:
            db.executemany("INSERT OR IGNORE INTO pending_deletions VALUES(?)", [(key,) for key in ids])

    def finish_delete(self, ids):
        with self.connect() as db:
            for key in ids:
                db.execute("DELETE FROM checks WHERE instr(record_ids, ?) > 0", (key,))
                for table, column in (("records", "id"), ("versions", "record_id"), ("sources", "record_id"), ("pending_deletions", "record_id")):
                    db.execute(f"DELETE FROM {table} WHERE {column}=?", (key,))
