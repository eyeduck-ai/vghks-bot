"""Small task headers and item counters, maintained alongside original data."""
from __future__ import annotations

import json

from .migrations import migrate, statements

FIELDS = ("id", "kind", "name", "status", "created_at", "finished_at", "message", "error_code",
          "mrn", "patient_name", "resource", "review_task_id", "account_id", "set_id", "done", "total", "progress",
          "sync_checked_at", "case_total", "order_total", "all_available", "backfill", "record_id",
          "automatic", "force", "refresh", "start", "end", "doctor_card", "department", "apply_seq")
ARRAYS = ("members", "identifiers", "references", "parts", "report_kinds", "reports", "discovered")


def header(value):
    result = {key: value[key] for key in FIELDS if key in value}
    for key in ARRAYS:
        result[key + "_count"] = len(value.get(key, []))
    result["member_count"] = result["members_count"]
    return result


def save_header(db, value):
    db.execute("INSERT INTO bot_task_state VALUES(?,?,?,1) ON CONFLICT(task_id) DO UPDATE SET "
               "payload=excluded.payload,updated_at=excluded.updated_at,revision=bot_task_state.revision+1",
               (value["id"], json.dumps(header(value), ensure_ascii=False), value["updated_at"]))


def initialize(db):
    def apply(db):
        statements(db,
            "CREATE TABLE bot_task_state (task_id TEXT PRIMARY KEY,payload TEXT NOT NULL,updated_at TEXT NOT NULL,revision INTEGER NOT NULL)",
            "CREATE INDEX bot_task_state_recent ON bot_task_state(updated_at DESC,task_id)",
            "CREATE INDEX bot_task_state_status ON bot_task_state(json_extract(payload,'$.status'),updated_at DESC)",
            "CREATE INDEX bot_task_state_kind ON bot_task_state(json_extract(payload,'$.kind'),updated_at DESC)",
            "CREATE TABLE bot_task_item_summary (task_id TEXT NOT NULL,key TEXT NOT NULL,status TEXT NOT NULL,processing INTEGER NOT NULL,checked_at TEXT,has_period INTEGER NOT NULL,has_reference INTEGER NOT NULL,mrn TEXT,PRIMARY KEY(task_id,key))",
            "CREATE INDEX bot_task_item_counter ON bot_task_item_summary(task_id,status,processing,checked_at)",
            "CREATE INDEX bot_report_reference ON bot_documents(kind,json_extract(payload,'$.report_id'),updated_at DESC,id)",
            "CREATE TRIGGER bot_task_state_update AFTER UPDATE ON bot_task_state BEGIN INSERT INTO bot_revisions VALUES('task',1) ON CONFLICT(kind) DO UPDATE SET revision=revision+1; END",
            "CREATE TRIGGER bot_task_document_delete AFTER DELETE ON bot_documents WHEN old.kind='task' BEGIN DELETE FROM bot_task_state WHERE task_id=old.id; END")
        for operation in ("INSERT", "UPDATE"):
            fields = ["'id'", "new.id"]
            for field in FIELDS[1:]:
                fields.extend(("'" + field + "'", "json_extract(new.payload,'$." + field + "')"))
            for field in ARRAYS:
                fields.extend(("'" + field + "_count'", "coalesce(json_array_length(new.payload,'$." + field + "'),0)"))
            fields.extend(("'member_count'", "coalesce(json_array_length(new.payload,'$.members'),0)"))
            db.execute(f"CREATE TRIGGER bot_header_{operation.lower()} AFTER {operation} ON bot_documents WHEN new.kind='task' BEGIN "
                       "INSERT INTO bot_task_state VALUES(new.id,json_object(" + ",".join(fields) + "),new.updated_at,1) "
                       "ON CONFLICT(task_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at,revision=bot_task_state.revision+1; END")
            db.execute(f"""CREATE TRIGGER bot_item_{operation.lower()} AFTER {operation} ON bot_task_items BEGIN
                INSERT OR REPLACE INTO bot_task_item_summary VALUES(new.task_id,new.key,
                    coalesce(json_extract(new.payload,'$.status'),''),coalesce(json_extract(new.payload,'$.processing'),0),
                    json_extract(new.payload,'$.checked_at'),json_type(new.payload,'$.period') IS NOT NULL,
                    coalesce(json_extract(new.payload,'$.reference'),'') NOT IN ('','{{}}','[]',0),json_extract(new.payload,'$.mrn'));
                DELETE FROM bot_task_record_refs WHERE task_id=new.task_id AND key=new.key;
                INSERT OR IGNORE INTO bot_task_record_refs SELECT new.task_id,new.key,json_extract(value,'$.id')
                    FROM json_each(new.payload,'$.records') WHERE json_type(value,'$.id')='text';
            END""")
        db.execute("""CREATE TRIGGER bot_item_delete AFTER DELETE ON bot_task_items BEGIN
            DELETE FROM bot_task_item_summary WHERE task_id=old.task_id AND key=old.key;
            DELETE FROM bot_task_record_refs WHERE task_id=old.task_id AND key=old.key;
        END""")
        # SQL backfill avoids parsing clinical bodies in Python. The marker and
        # both projections roll back together if opening the database stops.
        db.execute("""INSERT INTO bot_task_item_summary SELECT task_id,key,
            coalesce(json_extract(payload,'$.status'),''),coalesce(json_extract(payload,'$.processing'),0),
            json_extract(payload,'$.checked_at'),json_type(payload,'$.period') IS NOT NULL,
            coalesce(json_extract(payload,'$.reference'),'') NOT IN ('','{}','[]',0),json_extract(payload,'$.mrn') FROM bot_task_items""")
        db.execute("INSERT OR IGNORE INTO bot_task_record_refs SELECT i.task_id,i.key,json_extract(j.value,'$.id') "
                   "FROM bot_task_items i,json_each(i.payload,'$.records') j WHERE json_type(j.value,'$.id')='text'")
        for row in db.execute("SELECT id,payload,updated_at FROM bot_documents WHERE kind='task'").fetchall():
            value = json.loads(row["payload"])
            value.update(id=row["id"], updated_at=row["updated_at"])
            save_header(db, value)
    migrate(db, "task_projections_v1", apply)


def item_counts(db, key, *, counter="default", checked_at=None, check_current=False, failed_counted=False):
    predicates = {"default": "NOT processing", "ready": "status='ready'",
                  "period": "has_period", "reference": "has_reference"}
    predicate = predicates[counter]
    args = []
    if check_current:
        predicate += " AND checked_at IS ?"
        args.append(checked_at)
    failure = "NOT processing AND status IN ('error','partial','forbidden')"
    if failed_counted:
        failure += " AND (" + predicate + ")"
        args += args[:]
    row = db.execute("SELECT coalesce(sum(" + predicate + "),0),coalesce(sum(" + failure +
                     "),0) FROM bot_task_item_summary WHERE task_id=?", [*args, key]).fetchone()
    return row[0], row[1]
