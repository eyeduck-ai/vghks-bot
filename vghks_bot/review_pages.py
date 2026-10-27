"""Task-snapshot projections for bounded review lists and one-patient reads."""
from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict

from vghks_sdk.identifiers import normalize_mrn

from .bounded_search import search as regex_search
from .demographics import merge_demographics
from .migrations import migrate, statements
from .settings import today

META_FIELDS = ("id", "kind", "name", "account_id", "set_id", "created_at", "mode", "cutoff",
               "department_keyword", "department_keywords", "department_filter", "department",
               "force", "refresh", "refresh_mrn", "base_task_id", "new_count", "reused_count")
MEMBER_FIELDS = ("mrn", "name", "sex", "age", "birthday", "birthday_raw", "registrations")
SUMMARY_FIELDS = ("mrn", "name", "sex", "age", "birthday", "birthday_raw", "status", "message",
                  "processing", "checked_at", "index_checked_at", "reused", "reuse_label")
SEARCH_CACHE_BYTES = 16 * 1024 * 1024


def object_sql(fields, source):
    return "json_object(" + ",".join("'" + field + "',json_extract(" + source + ",'$." + field + "')" for field in fields) + ")"


def summary_sql(source):
    pairs = ",".join("'" + field + "',json_extract(" + source + ",'$." + field + "')" for field in SUMMARY_FIELDS)
    return ("json_object(" + pairs + ",'record_count',coalesce(json_array_length(" + source + ",'$.records'),0),"
            "'dates',json((SELECT json_group_array(day) FROM (SELECT DISTINCT json_extract(value,'$.date') AS day "
            "FROM json_each(" + source + ",'$.records') WHERE day IS NOT NULL))),"
            "'auto_tags',json((SELECT json_group_array(json(value)) FROM (SELECT DISTINCT "
            "json_object('category',json_extract(m.value,'$.category'),'category_name',json_extract(m.value,'$.category_name')) AS value "
            "FROM json_each(" + source + ",'$.records') r,json_each(r.value,'$.matches') m))),"
            "'scope_issue_count',(SELECT count(*) FROM json_each(" + source + ",'$.records') "
            "WHERE coalesce(json_array_length(value,'$.tag_scope_issues'),0)>0))")


def member_source_sql(source):
    # This comparison contains only bounded member metadata, never handoff SOAP.
    return "(SELECT json_group_array(json(" + object_sql(MEMBER_FIELDS, "value") + ")) FROM json_each(" + source + ",'$.members'))"


def initialize(db):
    def apply(db):
        statements(db,
            "CREATE TABLE review_page_task (task_id TEXT PRIMARY KEY,payload TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1)",
            "CREATE TABLE review_page_members (task_id TEXT NOT NULL,mrn TEXT NOT NULL,ordinal INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(task_id,mrn))",
            "CREATE INDEX review_page_order ON review_page_members(task_id,ordinal,mrn)",
            "CREATE TABLE review_page_member_source (task_id TEXT PRIMARY KEY,source_json TEXT NOT NULL)",
            "CREATE TABLE review_page_patients (task_id TEXT NOT NULL,mrn TEXT NOT NULL,payload TEXT NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(task_id,mrn))",
            "CREATE TABLE review_page_search (task_id TEXT NOT NULL,mrn TEXT NOT NULL,ordinal INTEGER NOT NULL,soap TEXT NOT NULL,PRIMARY KEY(task_id,mrn,ordinal))",
            "CREATE TABLE review_page_ready (task_id TEXT PRIMARY KEY,last_key TEXT NOT NULL DEFAULT '',complete INTEGER NOT NULL DEFAULT 0)")
        for operation in ("INSERT", "UPDATE"):
            db.execute(f"CREATE TRIGGER review_page_task_{operation.lower()} AFTER {operation} ON bot_documents "
                "WHEN new.kind='task' AND json_extract(new.payload,'$.kind')='review' BEGIN "
                "INSERT INTO review_page_task VALUES(new.id," + object_sql(META_FIELDS, "new.payload") + ",1) "
                "ON CONFLICT(task_id) DO UPDATE SET payload=excluded.payload,revision=review_page_task.revision+1;"
                "INSERT OR IGNORE INTO review_page_ready(task_id,complete) VALUES(new.id,NOT EXISTS(SELECT 1 FROM bot_task_items WHERE task_id=new.id));END")
            membership_changed = " AND coalesce((SELECT source_json FROM review_page_member_source WHERE task_id=new.id),'') IS NOT " + member_source_sql("new.payload")
            db.execute(f"CREATE TRIGGER review_page_members_{operation.lower()} AFTER {operation} ON bot_documents "
                "WHEN new.kind='task' AND json_extract(new.payload,'$.kind')='review'" + membership_changed + " BEGIN "
                "DELETE FROM review_page_members WHERE task_id=new.id;"
                "INSERT OR IGNORE INTO review_page_members SELECT new.id,json_extract(value,'$.mrn'),cast(key AS INTEGER)," +
                object_sql(MEMBER_FIELDS, "value") + " FROM json_each(new.payload,'$.members') WHERE json_type(value,'$.mrn')='text';"
                "INSERT INTO review_page_member_source VALUES(new.id," + member_source_sql("new.payload") + ") "
                "ON CONFLICT(task_id) DO UPDATE SET source_json=excluded.source_json;END")
            db.execute(f"CREATE TRIGGER review_page_item_{operation.lower()} AFTER {operation} ON bot_task_items "
                "WHEN EXISTS(SELECT 1 FROM bot_task_state WHERE task_id=new.task_id AND json_extract(payload,'$.kind')='review') BEGIN "
                "INSERT INTO review_page_patients VALUES(new.task_id,new.key," + summary_sql("new.payload") + ",1) "
                "ON CONFLICT(task_id,mrn) DO UPDATE SET payload=excluded.payload,revision=review_page_patients.revision+1;"
                "DELETE FROM review_page_search WHERE task_id=new.task_id AND mrn=new.key;"
                "INSERT INTO review_page_search SELECT new.task_id,new.key,cast(key AS INTEGER),coalesce(json_extract(value,'$.soap'),'') FROM json_each(new.payload,'$.records');"
                "UPDATE review_page_task SET revision=revision+1 WHERE task_id=new.task_id;"
                "UPDATE bot_task_state SET revision=revision+1 WHERE task_id=new.task_id;END")
        db.execute("CREATE TRIGGER review_page_item_delete AFTER DELETE ON bot_task_items BEGIN "
                   "DELETE FROM review_page_patients WHERE task_id=old.task_id AND mrn=old.key;"
                   "DELETE FROM review_page_search WHERE task_id=old.task_id AND mrn=old.key;"
                   "UPDATE review_page_task SET revision=revision+1 WHERE task_id=old.task_id;"
                   "UPDATE bot_task_state SET revision=revision+1 WHERE task_id=old.task_id;END")
        db.execute("CREATE TRIGGER review_page_task_delete AFTER DELETE ON bot_documents WHEN old.kind='task' BEGIN "
                   "DELETE FROM review_page_task WHERE task_id=old.id;DELETE FROM review_page_members WHERE task_id=old.id;"
                   "DELETE FROM review_page_patients WHERE task_id=old.id;DELETE FROM review_page_search WHERE task_id=old.id;"
                   "DELETE FROM review_page_ready WHERE task_id=old.id;END")
        db.execute("CREATE TRIGGER review_page_member_source_delete AFTER DELETE ON bot_documents WHEN old.kind='task' BEGIN "
                   "DELETE FROM review_page_member_source WHERE task_id=old.id;END")
        for operation in ("INSERT", "UPDATE", "DELETE"):
            db.execute(f"CREATE TRIGGER review_page_tags_{operation.lower()} AFTER {operation} ON patient_tags BEGIN "
                       "INSERT INTO bot_revisions VALUES('patient_tags',1) ON CONFLICT(kind) DO UPDATE SET revision=revision+1;END")
    migrate(db, "review_pages_v1", apply)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def literal_span(text, folded_query):
    folded = text.casefold()
    start = folded.find(folded_query)
    if start < 0:
        return None
    end = start + len(folded_query)
    if len(folded) == len(text):
        return [start, end]
    # Case folding can expand one original character (ß -> ss, ﬃ -> ffi).
    # Map only successful matches; binary-search prefixes in C instead of
    # building a per-character array for every long clinical text.
    left, right = 0, min(start, len(text))
    while left < right:
        middle = (left + right + 1) // 2
        if len(text[:middle].casefold()) <= start:
            left = middle
        else:
            right = middle - 1
    original_start = left
    left, right = original_start, min(end, len(text))
    while left < right:
        middle = (left + right) // 2
        if len(text[:middle].casefold()) >= end:
            right = middle
        else:
            left = middle + 1
    return [original_start, left]


class ReviewPages:
    def __init__(self, review):
        self.review, self.library = review, review.app.store.library
        self.lock = threading.RLock()
        self.search_cache = OrderedDict()

    def ensure(self, key):
        if not isinstance(key, str) or not key or len(key) > 128:
            raise ValueError("檢閱任務代碼不正確。")
        with self.library.read_snapshot() as db:
            task = db.execute("SELECT payload FROM bot_task_state WHERE task_id=?", (key,)).fetchone()
            if not task or json.loads(task[0]).get("kind") != "review":
                raise ValueError("此帳號沒有指定的病歷檢閱任務。")
            ready = db.execute("SELECT complete FROM review_page_ready WHERE task_id=?", (key,)).fetchone()
            if ready and ready[0]:
                return
        # Legacy backfill uses small transactions and only the requested task.
        # Source SOAP is never decoded into Python just to build a list.
        with self.lock:
            for cached in list(self.search_cache):
                if cached[0] == key:
                    del self.search_cache[cached]
            with self.library.connect() as db:
                db.execute("INSERT OR IGNORE INTO review_page_task SELECT id," + object_sql(META_FIELDS, "payload") +
                           ",1 FROM bot_documents WHERE kind='task' AND id=?", (key,))
                db.execute("INSERT OR IGNORE INTO review_page_members SELECT d.id,json_extract(j.value,'$.mrn'),cast(j.key AS INTEGER)," +
                           object_sql(MEMBER_FIELDS, "j.value") + " FROM bot_documents d,json_each(d.payload,'$.members') j "
                           "WHERE d.kind='task' AND d.id=? AND json_type(j.value,'$.mrn')='text'", (key,))
                db.execute("INSERT OR IGNORE INTO review_page_member_source SELECT id," + member_source_sql("payload") +
                           " FROM bot_documents WHERE kind='task' AND id=?", (key,))
                db.execute("INSERT OR IGNORE INTO review_page_ready(task_id) VALUES(?)", (key,))
            while True:
                with self.library.connect() as db:
                    state = db.execute("SELECT last_key,complete FROM review_page_ready WHERE task_id=?", (key,)).fetchone()
                    if state is None:
                        raise ValueError("檢閱任務已移除。")
                    last, complete = state
                    if complete:
                        return
                    keys = [row[0] for row in db.execute("SELECT key FROM bot_task_items WHERE task_id=? AND key>? ORDER BY key LIMIT 200", (key, last))]
                    if not keys:
                        db.execute("UPDATE review_page_ready SET complete=1 WHERE task_id=?", (key,))
                        return
                    args = (key, last, keys[-1])
                    db.execute("INSERT OR IGNORE INTO review_page_patients SELECT task_id,key," + summary_sql("payload") +
                               ",1 FROM bot_task_items WHERE task_id=? AND key>? AND key<=?", args)
                    db.execute("INSERT OR IGNORE INTO review_page_search SELECT i.task_id,i.key,cast(j.key AS INTEGER),"
                               "coalesce(json_extract(j.value,'$.soap'),'') FROM bot_task_items i,json_each(i.payload,'$.records') j "
                               "WHERE i.task_id=? AND i.key>? AND i.key<=?", args)
                    db.execute("UPDATE review_page_ready SET last_key=? WHERE task_id=?", (keys[-1], key))

    @staticmethod
    def filters(values):
        query, mode, tag = values.get("q", ""), values.get("search_mode", "text"), values.get("tag", "")
        if not isinstance(query, str) or len(query) > 500 or mode not in {"text", "regex"} or not isinstance(tag, str):
            raise ValueError("搜尋格式不正確。")
        return query, mode, tag

    def task(self, db, key):
        row = db.execute("SELECT t.payload,s.payload AS state,s.updated_at,s.revision FROM review_page_task t "
                         "JOIN bot_task_state s ON s.task_id=t.task_id WHERE t.task_id=?", (key,)).fetchone()
        if not row:
            raise ValueError("此帳號沒有指定的病歷檢閱任務。")
        value = {**json.loads(row["payload"]), **{k: v for k, v in json.loads(row["state"]).items() if v is not None},
                 "updated_at": row["updated_at"], "revision": row["revision"]}
        if value.get("account_id") not in {None, self.review.app.account_id}:
            raise ValueError("檢閱任務不屬於此帳號。")
        return value

    def matching(self, db, key, query, mode, tag, *, matches=None):
        clauses, args = ["m.task_id=?"], [key]
        if query:
            hit = sorted(self.search_matches(db, key, query, mode) if matches is None else matches)
            clauses.append("m.mrn IN (SELECT value FROM json_each(?))")
            args.append(json.dumps(hit))
        if tag:
            definitions = [category.id for category in self.review.app.settings.categories]
            clauses.append("(EXISTS(SELECT 1 FROM review_page_patients p,json_each(p.payload,'$.auto_tags') a "
                           "WHERE p.task_id=m.task_id AND p.mrn=m.mrn AND json_extract(a.value,'$.category')=?) OR "
                           "EXISTS(SELECT 1 FROM patient_tags pt WHERE pt.mrn=m.mrn AND pt.tag_id=? AND pt.tag_id IN (SELECT value FROM json_each(?))))")
            args += [tag, tag, json.dumps(definitions)]
        return " AND ".join(clauses), args

    def search_matches(self, db, key, query, mode):
        generation = db.execute("SELECT revision FROM review_page_task WHERE task_id=?", (key,)).fetchone()[0]
        cache_key = (key, generation, query, mode)
        with self.lock:
            cached = self.search_cache.get(cache_key)
            hit = cached[0] if cached is not None else None
            if cached is not None:
                self.search_cache.move_to_end(cache_key)
        if hit is None:
            hit = {}
            cursor = db.execute("SELECT mrn,ordinal,soap FROM review_page_search WHERE task_id=? ORDER BY mrn,ordinal", (key,))
            if mode == "regex":
                rows = cursor.fetchall()
                # One bounded search covers the entire task, never one deadline per page/patient.
                found = regex_search(query, [row["soap"] for row in rows])
                for index, spans in found.items():
                    row = rows[index]
                    hit.setdefault(row["mrn"], {})[row["ordinal"]] = spans
            else:
                folded = query.casefold()
                # Stream each SOAP once; count/page/navigation reuse only IDs and spans.
                for row in cursor:
                    span = literal_span(row["soap"], folded)
                    if span is not None:
                        hit.setdefault(row["mrn"], {})[row["ordinal"]] = [span]
            with self.lock:
                size = sum(128 + len(mrn) * 2 + sum(64 + len(spans) * 96 for spans in records.values())
                           for mrn, records in hit.items())
                if size <= SEARCH_CACHE_BYTES:
                    self.search_cache[cache_key] = (hit, size)
                while len(self.search_cache) > 8 or sum(item[1] for item in self.search_cache.values()) > SEARCH_CACHE_BYTES:
                    self.search_cache.popitem(last=False)
        return hit

    def overlays(self, db, mrns):
        packed = json.dumps(list(dict.fromkeys(mrns)))
        profiles = {}
        for row in db.execute("SELECT id,payload FROM bot_documents WHERE kind='profile' AND id IN (SELECT value FROM json_each(?))", (packed,)):
            profile = json.loads(row["payload"])
            if profile.get("status") == "resolved" and profile.get("mrn") == row["id"]:
                profiles[row["id"]] = profile
        manual = self.library.manual_tags(self.review.app.settings, mrns=mrns)
        return profiles, manual

    def revisions(self, rows, profiles, manual, query, mode):
        return {row["mrn"]: fingerprint([row["member"], row["payload"], row["item_revision"], profiles.get(row["mrn"]),
                  manual.get(row["mrn"], []), query, mode, today().isoformat()]) for row in rows}

    @staticmethod
    def row_sql():
        return ("SELECT m.mrn,m.ordinal,m.payload AS member,p.payload,coalesce(p.revision,0) AS item_revision "
                "FROM review_page_members m LEFT JOIN review_page_patients p ON p.task_id=m.task_id AND p.mrn=m.mrn ")

    def navigation(self, db, key, mrn, where, args):
        position = db.execute("SELECT count(*) FROM review_page_members m WHERE " + where +
            " AND m.ordinal<(SELECT ordinal FROM review_page_members WHERE task_id=? AND mrn=?)", [*args, key, mrn]).fetchone()[0]
        ordinal = db.execute("SELECT m.ordinal FROM review_page_members m WHERE " + where + " AND m.mrn=?", [*args, mrn]).fetchone()
        total = db.execute("SELECT count(*) FROM review_page_members m WHERE " + where, args).fetchone()[0]
        if not ordinal:
            return {"previous_mrn": None, "next_mrn": None, "position": None, "total": total}
        previous = db.execute("SELECT m.mrn FROM review_page_members m WHERE " + where + " AND m.ordinal<? ORDER BY m.ordinal DESC LIMIT 1", [*args, ordinal[0]]).fetchone()
        following = db.execute("SELECT m.mrn FROM review_page_members m WHERE " + where + " AND m.ordinal>? ORDER BY m.ordinal LIMIT 1", [*args, ordinal[0]]).fetchone()
        return {"previous_mrn": previous[0] if previous else None, "next_mrn": following[0] if following else None,
                "position": position, "total": total}

    def page(self, values):
        key = values.get("id")
        self.ensure(key)
        query, mode, tag = self.filters(values)
        try:
            limit = int(values.get("limit", 40))
            offset = int(values.get("offset", int(values.get("page", 0)) * limit))
        except (TypeError, ValueError):
            raise ValueError("分頁不正確。") from None
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("分頁不正確。")
        focus = normalize_mrn(values["focus_mrn"]) if values.get("focus_mrn") else ""
        selected = normalize_mrn(values["selected_mrn"]) if values.get("selected_mrn") else ""
        with self.library.read_snapshot() as db:
            task = self.task(db, key)
            where, args = self.matching(db, key, query, mode, tag)
            total = db.execute("SELECT count(*) FROM review_page_members m WHERE " + where, args).fetchone()[0]
            focus_found = False
            if focus:
                nav = self.navigation(db, key, focus, where, args)
                if nav["position"] is not None:
                    offset, focus_found = nav["position"] // limit * limit, True
            offset = min(offset, max(0, (total - 1) // limit * limit))
            rows = db.execute(self.row_sql() + "WHERE " + where + " ORDER BY m.ordinal,m.mrn LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
            revision_rows = list(rows)
            if selected and all(row["mrn"] != selected for row in rows):
                extra = db.execute(self.row_sql() + "WHERE m.task_id=? AND m.mrn=?", (key, selected)).fetchone()
                if not extra:
                    raise ValueError("病人不在此檢閱任務中。")
                revision_rows.append(extra)
            profiles, manual = self.overlays(db, [row["mrn"] for row in revision_rows])
            revisions = self.revisions(revision_rows, profiles, manual, query, mode)
            patients = []
            for row in rows:
                patient = {**{k: v for k, v in json.loads(row["member"]).items() if v is not None}, **({k: v for k, v in json.loads(row["payload"]).items() if v is not None} if row["payload"] else
                           {"status": "pending", "record_count": 0, "dates": [], "auto_tags": [], "message": "待處理"})}
                patient = merge_demographics(patient, profiles.get(row["mrn"], {}))
                patients.append({**{k: v for k, v in patient.items() if k not in {"registrations", "national_id", "mobile_phone", "home_phone", "phone", "contact", "basic_info_at"}},
                                 "manual_tags": manual.get(row["mrn"], []), "data_revision": revisions[row["mrn"]]})
            definitions = [category.id for category in self.review.app.settings.categories]
            facet_rows = db.execute("WITH tags AS (SELECT p.mrn,json_extract(a.value,'$.category') AS tag_id FROM review_page_patients p,json_each(p.payload,'$.auto_tags') a WHERE p.task_id=? "
                "UNION SELECT m.mrn,t.tag_id FROM review_page_members m JOIN patient_tags t ON t.mrn=m.mrn WHERE m.task_id=? AND t.tag_id IN (SELECT value FROM json_each(?))) "
                "SELECT tag_id,count(*) FROM tags GROUP BY tag_id", (key, key, json.dumps(definitions))).fetchall()
            counts = {row[0]: row[1] for row in facet_rows}
            stats = db.execute("SELECT count(*),coalesce(sum(json_extract(payload,'$.record_count')>0),0),coalesce(sum(json_extract(payload,'$.scope_issue_count')),0) FROM review_page_patients WHERE task_id=?", (key,)).fetchone()
            all_total = db.execute("SELECT count(*) FROM review_page_members WHERE task_id=?", (key,)).fetchone()[0]
            pending = db.execute("SELECT count(*) FROM review_page_members m LEFT JOIN review_page_patients p ON p.task_id=m.task_id AND p.mrn=m.mrn WHERE m.task_id=? AND (p.mrn IS NULL OR json_extract(p.payload,'$.processing'))", (key,)).fetchone()[0]
            revision = fingerprint([task["revision"], revisions, total, counts, offset, query, mode, tag])
            result = {"task": task, "patients": patients, "total": total, "all_total": all_total, "offset": offset, "limit": limit,
                      "page": offset // limit, "tag_counts": counts, "scope_issue_count": stats[2], "has_results": bool(stats[1]),
                      "incremental": {"new": task.get("new_count", 0), "reused": task.get("reused_count", 0), "pending": pending} if task.get("base_task_id") else None,
                      "revision": revision, "patient_revisions": revisions, "focus_found": focus_found}
            if selected:
                result.update(selected_revision=revisions[selected], selected_navigation=self.navigation(db, key, selected, where, args))
            return result

    def patient(self, values):
        key, mrn = values.get("id"), normalize_mrn(values.get("mrn", ""))
        self.ensure(key)
        query, mode, tag = self.filters(values)
        with self.library.read_snapshot() as db:
            self.task(db, key)
            row = db.execute(self.row_sql() + "WHERE m.task_id=? AND m.mrn=?", (key, mrn)).fetchone()
            if not row:
                raise ValueError("病人不在此檢閱任務中。")
            patient = self.review.db.item(key, mrn)
            if patient is None:
                source = db.execute("SELECT j.value FROM bot_documents d,json_each(d.payload,'$.members') j WHERE d.kind='task' AND d.id=? AND json_extract(j.value,'$.mrn')=?", (key, mrn)).fetchone()
                patient = {**json.loads(source[0]), "status": "pending", "records": [], "message": "待處理"}
            if patient.get("mrn") != mrn:
                raise ValueError("檢閱病人身分不符。")
            profiles, manual = self.overlays(db, [mrn])
            revision = self.revisions([row], profiles, manual, query, mode)[mrn]
            patient = merge_demographics(patient, profiles.get(mrn, {}))
            patient["manual_tags"] = manual.get(mrn, [])
            records = patient.get("records", [])
            task_matches = self.search_matches(db, key, query, mode) if query else {}
            matches = task_matches.get(mrn, {})
            for index, record in enumerate(records):
                record.update(highlights=[list(span) for span in matches.get(index, [])], search_hit=not query or index in matches)
            where, args = self.matching(db, key, query, mode, tag, matches=task_matches)
            return {"task_id": key, "patient": patient, "revision": revision, **self.navigation(db, key, mrn, where, args)}

    def notes(self, values, *, summary=False):
        key = values.get("task_id")
        self.ensure(key)
        with self.library.read_snapshot() as db:
            self.task(db, key)
            revision = self.review.db.revisions().get("review_note", 0)
            if summary:
                count = db.execute("SELECT count(*) FROM bot_documents WHERE kind='review_note' AND id IN (SELECT ? || ':' || mrn FROM review_page_members WHERE task_id=?)", (key, key)).fetchone()[0]
                return {"task_id": key, "note_count": count, "revision": revision}
            supplied = values.get("mrns")
            if not isinstance(supplied, list) or len(supplied) > 100 or any(not isinstance(mrn, str) for mrn in supplied):
                raise ValueError("備註分頁需指定最多 100 位病人。")
            mrns = list(dict.fromkeys(normalize_mrn(mrn) for mrn in supplied))
            selection = json.dumps(mrns)
            rows = db.execute("SELECT mrn,payload FROM review_page_members WHERE task_id=? AND mrn IN (SELECT value FROM json_each(?))", (key, selection)).fetchall()
            if len(rows) != len(mrns):
                raise ValueError("病人不在此檢閱任務中。")
            members = {row["mrn"]: {k: v for k, v in json.loads(row["payload"]).items() if v is not None} for row in rows}
            notes = {row["id"].split(":", 1)[1]: json.loads(row["payload"]) for row in db.execute("SELECT id,payload FROM bot_documents WHERE kind='review_note' AND id IN (SELECT ? || ':' || value FROM json_each(?))", (key, selection))}
            registrations = {row["id"]: json.loads(row["payload"]) for row in db.execute("SELECT id,payload FROM bot_documents WHERE kind='registrations' AND id IN (SELECT value FROM json_each(?))", (selection,))}
            patients = []
            for mrn in mrns:
                member, note = members[mrn], notes.get(mrn, {})
                patients.append({"mrn": mrn, "name": member.get("name") or "", "sequence_no": self.review._registration_sequence(member, registrations=registrations),
                                 "text": note.get("text", ""), "updated_at": note.get("updated_at", "")})
            return {"task_id": key, "patients": patients, "revision": revision}

    def selected(self, key, mrns):
        """Read only requested immutable snapshots for a scoped tool handoff."""
        self.ensure(key)
        selection = json.dumps(mrns)
        with self.library.read_snapshot() as db:
            task = self.task(db, key)
            members = {row["mrn"]: {k: v for k, v in json.loads(row["payload"]).items() if v is not None} for row in db.execute(
                "SELECT mrn,payload FROM review_page_members WHERE task_id=? AND mrn IN (SELECT value FROM json_each(?))", (key, selection))}
            if len(members) != len(mrns):
                raise ValueError("病人不在此檢閱任務中。")
            saved = {row["key"]: json.loads(row["payload"]) for row in db.execute(
                "SELECT key,payload FROM bot_task_items WHERE task_id=? AND key IN (SELECT value FROM json_each(?))", (key, selection))}
            profiles, manual = self.overlays(db, mrns)
            patients = []
            for mrn in mrns:
                patient = saved.get(mrn) or {**members[mrn], "status": "pending", "records": [], "message": "待處理"}
                if patient.get("mrn") != mrn:
                    raise ValueError("檢閱病人身分不符。")
                patient = merge_demographics(patient, profiles.get(mrn, {}))
                patient["manual_tags"] = manual.get(mrn, [])
                patients.append(patient)
            return {"task": task, "patients": patients}
