"""Private handoff evidence and bounded public worklist representations."""
from __future__ import annotations

import json

from .library import encoded
from .settings import timestamp


def public_document(value):
    """Keep source IDs/version metadata while omitting private SOAP snapshots."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "handoff_sources" and isinstance(item, list):
                result[key] = [public_document({field: data for field, data in source.items()
                                              if field != "record"}) for source in item]
            else:
                result[key] = public_document(item)
        return result
    if isinstance(value, list):
        return [public_document(item) for item in value]
    return value


def _purge(value, record_ids, mrns, patient_mrn=""):
    changed = False
    if isinstance(value, list):
        for item in value:
            changed = _purge(item, record_ids, mrns, patient_mrn) or changed
    elif isinstance(value, dict):
        patient_mrn = value.get("mrn", patient_mrn)
        sources = value.get("handoff_sources")
        if isinstance(sources, list):
            retained = [source for source in sources if source.get("record_id") not in record_ids
                        and patient_mrn not in mrns and source.get("record", {}).get("mrn") not in mrns]
            if retained != sources:
                value["handoff_sources"] = retained
                changed = True
        references = value.get("source_records")
        if isinstance(references, list):
            retained = [] if isinstance(sources, list) and patient_mrn in mrns else [
                key for key in references if key not in record_ids]
            if retained != references:
                value["source_records"] = retained
                changed = True
        for item in value.values():
            changed = _purge(item, record_ids, mrns, patient_mrn) or changed
    return changed


def purge_sources(db, *, record_ids=(), mrns=()):
    """Remove clinical evidence atomically, preserving worklists and cloud IDs."""
    record_ids, mrns = set(record_ids), set(mrns)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, keys in (("bot_documents", ("kind", "id")), ("bot_task_items", ("task_id", "key")),
                        ("analysis_cohorts", ("id",)), ("analysis_runs", ("id",))):
        if table not in tables:
            continue
        rows = db.execute(f"SELECT * FROM {table} WHERE instr(payload,'\"handoff_sources\"')>0").fetchall()
        for row in rows:
            value = json.loads(row["payload"])
            if _purge(value, record_ids, mrns):
                has_updated = "updated_at" in row.keys()
                updated = timestamp()
                if has_updated:
                    value["updated_at"] = updated
                assignments = "payload=?,updated_at=?" if has_updated else "payload=?"
                args = [encoded(value), updated] if has_updated else [encoded(value)]
                db.execute(f"UPDATE {table} SET {assignments} WHERE " + " AND ".join(f"{key}=?" for key in keys),
                           [*args, *(row[key] for key in keys)])
