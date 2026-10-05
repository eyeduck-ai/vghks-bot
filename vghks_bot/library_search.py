"""Literal Unicode search and tag facets over rebuildable local projections."""
from __future__ import annotations

import hashlib
import json

from .library import encoded
from .migrations import migrate, statements
from .soap_preview import ap_preview
from .tags import classification

TAG_RULES_VERSION = 1

def initialize(db):
    def apply(db):
        statements(db,
            "CREATE TABLE record_search (id TEXT PRIMARY KEY,digest TEXT NOT NULL,text TEXT NOT NULL,name TEXT NOT NULL)",
            "CREATE TABLE record_classification (record_id TEXT NOT NULL,rules TEXT NOT NULL,issues INTEGER NOT NULL,PRIMARY KEY(record_id,rules))",
            "CREATE INDEX record_classification_rules ON record_classification(rules,record_id,issues)",
            "CREATE INDEX records_search_scope ON records(day DESC,mrn,id)",
            "CREATE TABLE record_auto_tags (record_id TEXT NOT NULL,rules TEXT NOT NULL,tag_id TEXT NOT NULL,PRIMARY KEY(record_id,rules,tag_id))",
            "CREATE INDEX record_auto_tags_group ON record_auto_tags(rules,tag_id,record_id)",
            "CREATE TABLE record_search_dirty (id TEXT PRIMARY KEY)",
            "CREATE TABLE IF NOT EXISTS bot_task_record_refs (task_id TEXT NOT NULL,key TEXT NOT NULL,record_id TEXT NOT NULL,PRIMARY KEY(task_id,key,record_id))",
            "CREATE INDEX IF NOT EXISTS bot_task_record_scope ON bot_task_record_refs(task_id,record_id)",
            "CREATE TRIGGER record_search_insert AFTER INSERT ON records BEGIN INSERT INTO record_search_dirty VALUES(new.id) ON CONFLICT(id) DO NOTHING; END",
            "CREATE TRIGGER record_search_update AFTER UPDATE ON records BEGIN INSERT INTO record_search_dirty VALUES(new.id) ON CONFLICT(id) DO NOTHING; DELETE FROM record_classification WHERE record_id=new.id; DELETE FROM record_auto_tags WHERE record_id=new.id; END",
            "CREATE TRIGGER record_search_delete AFTER DELETE ON records BEGIN DELETE FROM record_search WHERE id=old.id; DELETE FROM record_search_dirty WHERE id=old.id; DELETE FROM record_classification WHERE record_id=old.id; DELETE FROM record_auto_tags WHERE record_id=old.id; END",
            "INSERT OR IGNORE INTO record_search_dirty SELECT id FROM records")
    migrate(db, "record_search_v1", apply)


def rules_key(settings):
    return hashlib.sha256(encoded([TAG_RULES_VERSION, settings.public()["categories"]]).encode()).hexdigest()


def project(db, record, content, settings):
    key = record["id"]
    fingerprint = hashlib.sha256(content.encode()).hexdigest()
    text = " ".join(str(record.get(k, "")) for k in
                    ("mrn", "name", "sex", "age", "date", "section", "doctor", "case_no", "soap")).casefold()
    db.execute("INSERT OR REPLACE INTO record_search VALUES(?,?,?,?)",
               (key, fingerprint, text, record.get("name", "")))
    result = classification(record, settings)
    rules = rules_key(settings)
    db.execute("INSERT OR REPLACE INTO record_classification VALUES(?,?,?)",
               (key, rules, int(bool(result["tag_scope_issues"]))))
    db.execute("DELETE FROM record_auto_tags WHERE record_id=? AND rules=?", (key, rules))
    db.executemany("INSERT INTO record_auto_tags VALUES(?,?,?)",
                   [(key, rules, tag) for tag in {m["category"] for m in result["matches"]}])
    db.execute("DELETE FROM record_search_dirty WHERE id=?", (key,))


def ensure(library, settings):
    rules = rules_key(settings)
    # Bounded batches keep an interrupted rebuild resumable and avoid loading
    # a whole corpus. Readers of a different rules version retain their index.
    while True:
        with library.connect() as db:
            rows = db.execute("SELECT r.id,r.payload FROM records r WHERE r.id IN (SELECT i.id FROM records i "
                              "WHERE i.id IN (SELECT id FROM record_search_dirty) OR NOT EXISTS"
                              "(SELECT 1 FROM record_classification c WHERE c.record_id=i.id AND c.rules=?) LIMIT 200)",
                              (rules,)).fetchall()
            for row in rows:
                project(db, json.loads(row["payload"]), row["payload"], settings)
            library._search_settings = settings
        if len(rows) < 200:
            return rules


def search(library, values, settings, *, all_results=False):
    query = values.get("q", "")
    if not isinstance(query, str) or len(query) > 500:
        raise ValueError("搜尋字串最多 500 字。")
    for field in ("start", "end", "mrn", "account", "tag", "group", "task_id"):
        if not isinstance(values.get(field, ""), str):
            raise ValueError("篩選條件不正確。")
    if values.get("start") and values.get("end") and values["start"] > values["end"]:
        raise ValueError("結束日期不可早於開始日期。")
    definitions = {c.id: c for c in settings.categories}
    category = values.get("tag", "")
    if category not in {"", "__untagged", "__tagged", *definitions}:
        raise ValueError("tag 不存在，請重新選擇。")
    try:
        offset, limit = int(values.get("offset", 0)), int(values.get("limit", 50))
    except (ValueError, TypeError):
        raise ValueError("分頁不正確。") from None
    if offset < 0 or not 1 <= limit <= 200:
        raise ValueError("分頁不正確。")
    rules = ensure(library, settings)
    clauses = ["r.id NOT IN (SELECT record_id FROM pending_deletions)"]
    args = [rules]
    for field, expression in (("start", "r.day>=?"), ("end", "r.day<=?"), ("mrn", "r.mrn=?")):
        if values.get(field):
            clauses.append(expression)
            args.append(values[field])
    if query.strip():
        clauses.append("instr(s.text,?)>0")
        args.append(query.strip().casefold())
    if values.get("account"):
        clauses.append("EXISTS(SELECT 1 FROM sources a WHERE a.record_id=r.id AND a.account=?)")
        args.append(values["account"])
    if values.get("task_id"):
        clauses.append("r.id IN (SELECT record_id FROM bot_task_record_refs WHERE task_id=?)")
        args.append(values["task_id"])
    text_join = "JOIN record_search s ON s.id=r.id " if query.strip() else ""
    cte = ("WITH base AS (SELECT r.id,r.mrn,r.day,c.issues FROM records r " + text_join +
           "JOIN record_classification c ON c.record_id=r.id AND c.rules=? WHERE " + " AND ".join(clauses) + "), "
           "tags AS MATERIALIZED (SELECT b.id,t.tag_id FROM base b JOIN record_auto_tags t ON t.record_id=b.id AND t.rules=? "
           "UNION SELECT b.id,t.tag_id FROM base b JOIN patient_tags t ON t.mrn=b.mrn WHERE t.tag_id IN (" +
           (",".join("?" for _ in definitions) or "NULL") + ")), ")
    args += [rules, *definitions]
    predicate = "1"
    if category in {"__tagged", "__untagged"}:
        predicate = "b.id " + ("NOT " if category == "__untagged" else "") + "IN (SELECT id FROM tags)"
    elif category:
        predicate = "b.id IN (SELECT id FROM tags WHERE tag_id=?)"
    filtered = "filtered AS (SELECT b.* FROM base b WHERE " + predicate + ") "
    filtered_args = [*args, *([category] if category not in {"", "__tagged", "__untagged"} else [])]
    by_patient = values.get("group") == "patient"
    with library.read_snapshot() as db:
        facets = db.execute(cte + "facet AS (SELECT 1) SELECT tag_id,count(*) FROM tags GROUP BY tag_id", args).fetchall()
        extra = db.execute(cte + "facet AS (SELECT 1) SELECT coalesce(sum(issues),0),"
                           "coalesce(sum(id NOT IN (SELECT id FROM tags)),0) FROM base", args).fetchone()
        totals = db.execute(cte + filtered + "SELECT count(*),count(DISTINCT mrn) FROM filtered", filtered_args).fetchone()
        page_clause, page_args = "", []
        if not all_results:
            selected = ("SELECT mrn FROM filtered GROUP BY mrn ORDER BY max(day) DESC,mrn" if by_patient else
                        "SELECT id FROM filtered ORDER BY day DESC,mrn,id") + " LIMIT ? OFFSET ?"
            page_clause = "WHERE r." + ("mrn" if by_patient else "id") + " IN (" + selected + ")"
            page_args = [limit, offset]
        raw = db.execute(cte + filtered + "SELECT r.* FROM records r JOIN filtered f ON f.id=r.id " +
                         page_clause + " ORDER BY r.day DESC,r.mrn,r.id", [*filtered_args, *page_args]).fetchall()
        ids = [r["id"] for r in raw]
        accounts, versions = {}, {}
        if ids:
            # JSON array avoids SQLite parameter limits for deliberate select-all.
            selection = json.dumps(ids)
            for row in db.execute("SELECT DISTINCT record_id,account FROM sources WHERE record_id IN (SELECT value FROM json_each(?)) ORDER BY account", (selection,)):
                accounts.setdefault(row[0], []).append(row[1])
            versions = dict(db.execute("SELECT record_id,count(*) FROM versions WHERE record_id IN (SELECT value FROM json_each(?)) GROUP BY record_id", (selection,)))
        manual = library.manual_tags(settings, mrns=list({r["mrn"] for r in raw}))
        result = []
        for row in raw:
            record = json.loads(row["payload"])
            record.update(classification(record, settings), manual_tags=manual.get(record["mrn"], []),
                          accounts=accounts.get(record["id"], []), version_count=versions.get(record["id"], 1),
                          first_saved=row["first_saved"], updated_at=row["updated_at"])
            record["ap_preview"] = ap_preview(record)
            result.append(record)
    counts = dict.fromkeys(definitions, 0)
    counts.update(dict(facets))
    return {"records": result, "total": totals[0], "patients": totals[1],
            "page_total": totals[1] if by_patient else totals[0], "tag_counts": counts,
            "untagged": extra[1], "scope_issue_count": extra[0], "offset": offset, "limit": limit}


def index_rows(library, settings):
    """Patient-tag selection needs identities and tag IDs, never SOAP bodies."""
    rules = ensure(library, settings)
    names = {c.id: c.name for c in settings.categories}
    with library.read_snapshot() as db:
        rows = db.execute("SELECT r.id,r.mrn,s.name FROM records r JOIN record_search s ON s.id=r.id "
                          "WHERE r.id NOT IN (SELECT record_id FROM pending_deletions) ORDER BY r.day DESC,r.mrn,r.id").fetchall()
        tags, accounts = {}, {}
        for row in db.execute("SELECT record_id,tag_id FROM record_auto_tags WHERE rules=?", (rules,)):
            tags.setdefault(row[0], []).append({"category": row[1], "category_name": names[row[1]]})
        for row in db.execute("SELECT DISTINCT record_id,account FROM sources ORDER BY account"):
            accounts.setdefault(row[0], []).append(row[1])
        return [{**dict(row), "matches": tags.get(row["id"], []), "accounts": accounts.get(row["id"], [])} for row in rows]
