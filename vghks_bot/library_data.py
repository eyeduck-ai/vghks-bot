"""Account-local clinical cache inventory and deliberate, previewed deletion."""
from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import date

from .library import encoded
from .settings import timestamp

CATEGORIES = {
    "numeric": "數值", "orders": "醫囑", "scans": "掃描", "visits": "就診索引",
    "profile": "基本資料", "registrations": "掛號紀錄", "analysis": "進階工具資料",
}
DEPENDENTS = {"numeric", "orders", "scans", "analysis"}


def step_category(key, kind=""):
    if key.startswith(("scan-", "scans-")) or kind == "scan_index":
        return "scans"
    if key.startswith("numeric-") or kind == "numeric":
        return "numeric"
    if key == "visits" or kind == "visits":
        return "visits"
    if key.startswith(("orders-", "history-order:", "collected-order:", "history-asset:",
                       "get_order_", "get_pacs_", "download_")) or kind in {
        "order", "order_index", "order_report", "report", "asset"
    }:
        return "orders"
    return "analysis"


class LibraryData:
    def __init__(self, app):
        self.app = app
        with self.library.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS clinical_cache_deletions "
                       "(mrn TEXT, category TEXT, deleted_at TEXT, PRIMARY KEY(mrn,category))")

    @property
    def library(self):
        return self.app.store.library

    def cleared(self, mrn):
        with self.library.connect() as db:
            return bool(db.execute("SELECT 1 FROM clinical_cache_deletions WHERE mrn=?", (mrn,)).fetchone())

    def soap_cleared(self, ids, mrns):
        with self.library.connect() as db:
            keys = [(row["mrn"], row["key"]) for row in db.execute("SELECT mrn,key FROM analysis_steps WHERE kind='cataract_soap' "
                    "AND json_extract(payload,'$.record_id') IN (SELECT value FROM json_each(?))", (json.dumps(ids),))]
            for table in ("analysis_steps", "analysis_versions"):
                db.executemany(f"DELETE FROM {table} WHERE mrn=? AND key=?", keys)
            db.executemany("INSERT OR REPLACE INTO clinical_cache_deletions VALUES(?,?,?)",
                           [(mrn, "soap", timestamp()) for mrn in mrns])

    def _snapshot(self, mrns, list_keys):
        packed = json.dumps(mrns)
        where = "mrn IN (SELECT value FROM json_each(?))"
        with self.library.read_snapshot() as db:
            steps = [dict(row) for row in db.execute("SELECT * FROM analysis_steps WHERE " + where, (packed,))]
            versions = [dict(row) for row in db.execute("SELECT * FROM analysis_versions WHERE " + where, (packed,))]
            # Other patients' references matter only when they share a selected
            # attachment. Include those references in the deletion fingerprint.
            refs = [dict(row) for row in db.execute("SELECT * FROM analysis_asset_refs WHERE digest IN "
                "(SELECT digest FROM analysis_asset_refs WHERE " + where + ") ORDER BY mrn,key,digest", (packed,))]
            digests = json.dumps(list({row["digest"] for row in refs}))
            assets = {row["digest"]: dict(row) for row in db.execute("SELECT * FROM analysis_assets "
                "WHERE digest IN (SELECT value FROM json_each(?))", (digests,))}
            lists = [dict(row) for row in db.execute("SELECT * FROM list_cache WHERE (account,day) IN "
                "(SELECT json_extract(value,'$.account'),json_extract(value,'$.day') FROM json_each(?))", (json.dumps(list_keys),))]
            records = [dict(row) for row in db.execute("SELECT DISTINCT mrn FROM records WHERE " + where, (packed,))]
            has_documents = db.execute("SELECT 1 FROM sqlite_master WHERE name='bot_documents'").fetchone()
            documents = [dict(row) for row in db.execute("SELECT * FROM bot_documents WHERE kind IN "
                "('profile','registrations','visits','numeric') AND coalesce(nullif(json_extract(payload,'$.mrn'),''),id) "
                "IN (SELECT value FROM json_each(?)) ORDER BY kind,id", (packed,))] if has_documents else []
        return steps, versions, refs, assets, lists, records, documents

    def read(self, values):
        from .library_inventory import read

        return read(self, values)

    def preview(self, values):
        mrns, selected = values.get("mrns", []), values.get("categories", [])
        list_keys = values.get("lists", [])
        if not isinstance(mrns, list) or len(mrns) > 1000 or any(not isinstance(m, str) for m in mrns):
            raise ValueError("請選擇此帳號的病人。")
        if not isinstance(selected, list) or len(selected) > len(CATEGORIES) or any(
                not isinstance(c, str) or c not in CATEGORIES for c in selected):
            raise ValueError("資料類型不正確。")
        if not isinstance(list_keys, list) or len(list_keys) > 1000:
            raise ValueError("請選擇門診掛號清單。")
        if bool(mrns) == bool(list_keys) or mrns and not selected:
            raise ValueError("請選擇病人與資料類型，或門診掛號清單。")
        if any(not isinstance(row, dict) or not isinstance(row.get("account"), str)
               or not isinstance(row.get("day"), str) for row in list_keys):
            raise ValueError("請選擇門診掛號清單。")
        steps, versions, refs, assets, lists, records, documents = self._snapshot(mrns, list_keys)
        known = {row["mrn"] for row in steps + versions + refs + records}
        known.update(json.loads(row["payload"]).get("mrn") or row["id"] for row in documents)
        if not set(mrns).issubset(known):
            raise ValueError("病人不存在於此帳號的本機資料。")
        categories = set(selected)
        if "visits" in categories:
            categories.update(DEPENDENTS)
        if categories & {"numeric", "orders", "scans"}:
            categories.add("analysis")
        kinds = {(row["mrn"], row["key"]): row["kind"] for row in steps}
        keys = {(row["mrn"], row["key"]) for row in steps + versions + refs
                if row["mrn"] in mrns and step_category(row["key"], kinds.get((row["mrn"], row["key"]), "")) in categories}
        removed_steps = [row for row in steps if (row["mrn"], row["key"]) in keys]
        removed_versions = [row for row in versions if (row["mrn"], row["key"]) in keys]
        removed_documents = []
        for row in documents:
            raw = json.loads(row["payload"])
            mrn = raw.get("mrn") or row["id"]
            if mrn in mrns and row["kind"] in categories & {"numeric", "visits", "profile", "registrations"}:
                removed_documents.append(row)
        keep_refs = [row for row in refs if (row["mrn"], row["key"]) not in keys]
        live = {row["digest"] for row in keep_refs}
        freed = {row["digest"] for row in refs if (row["mrn"], row["key"]) in keys} - live
        allowed_lists = {(row["account"], row["day"]): row for row in lists}
        picked_lists = []
        for row in list_keys:
            if not isinstance(row, dict) or not isinstance(row.get("account"), str) or not isinstance(row.get("day"), str) or (row["account"], row["day"]) not in allowed_lists:
                raise ValueError("門診清單不存在於此帳號。")
            date.fromisoformat(row["day"])
            picked_lists.append(allowed_lists[(row["account"], row["day"])])
        fingerprint = hashlib.sha256(encoded([str(self.library.path), sorted(set(mrns)), sorted(set(selected)),
                                             sorted(categories), sorted(keys), removed_steps, removed_versions,
                                             removed_documents, refs, picked_lists]).encode()).hexdigest()
        return {"mrns": sorted(set(mrns)), "categories": sorted(categories),
                "requested_categories": sorted(set(selected)), "cascaded": sorted(categories - set(selected)),
                "records": sum(row["kind"] != "numeric_structured" for row in removed_steps) + len(removed_documents),
                "versions": len(removed_versions),
                "attachments": len(freed), "attachment_bytes": sum(assets.get(sha, {}).get("size", 0) for sha in freed),
                "lists": [{"account": row["account"], "day": row["day"]} for row in picked_lists],
                "fingerprint": fingerprint}

    def delete(self, values, *, compatibility=False):
        from .jobs import BusyError

        review = getattr(self.app, "review", None)
        with self.app.lock, review.lock if review else nullcontext():
            self.app._available()
            if getattr(getattr(self.app, "root", None), "read_only", False):
                raise ValueError("目前是唯讀檢閱，無法清除資料。")
            if not self.app.idle.is_set() or self.app.analysis.sheet_busy or review and review.foreground:
                raise BusyError("請先等待或暫停此帳號的任務，再清除資料。")
            preview = self.preview(values)
            if not compatibility and values.get("fingerprint") != preview["fingerprint"]:
                raise ValueError("資料已變更，請重新預覽清除範圍。")
            selected = set(preview["mrns"])
            categories = set(preview["categories"])
            self.app.store.purge_index_copies(selected if "visits" in categories else [], preview["lists"])
            self.app.analysis.store.invalidate_previews(selected)
            with self.library.connect() as db:
                packed = json.dumps(sorted(selected))
                scope = "mrn IN (SELECT value FROM json_each(?))"
                keys = [(row["mrn"], row["key"]) for row in db.execute("SELECT * FROM analysis_steps WHERE " + scope, (packed,))
                        if row["mrn"] in selected and step_category(row["key"], row["kind"]) in categories]
                known_kinds = {(row["mrn"], row["key"]): row["kind"] for row in db.execute("SELECT * FROM analysis_steps WHERE " + scope, (packed,))}
                for table in ("analysis_versions", "analysis_asset_refs"):
                    keys.extend((row["mrn"], row["key"]) for row in db.execute(f"SELECT * FROM {table} WHERE " + scope, (packed,))
                                if row["mrn"] in selected and step_category(row["key"], known_kinds.get((row["mrn"], row["key"]), "")) in categories)
                for table in ("analysis_steps", "analysis_versions", "analysis_asset_refs"):
                    db.executemany(f"DELETE FROM {table} WHERE mrn=? AND key=?", sorted(set(keys)))
                db.executemany("INSERT OR REPLACE INTO clinical_cache_deletions VALUES(?,?,?)",
                               [(mrn, category, timestamp()) for mrn in selected for category in categories])
                if review:
                    for row in db.execute("SELECT * FROM bot_documents WHERE kind IN ('profile','registrations','visits','numeric','task') "
                        "AND coalesce(nullif(json_extract(payload,'$.mrn'),''),id) IN (SELECT value FROM json_each(?))", (packed,)).fetchall():
                        raw = json.loads(row["payload"])
                        mrn = raw.get("mrn") or row["id"]
                        if mrn in selected and row["kind"] in categories & {"numeric", "visits", "profile", "registrations"}:
                            db.execute("DELETE FROM bot_documents WHERE kind=? AND id=?", (row["kind"], row["id"]))
                        elif row["kind"] == "task" and raw.get("mrn") in selected:
                            resource = raw.get("resource", "")
                            kind = raw.get("kind", "")
                            category = {"profile": "profile", "resolve": "profile", "numeric": "numeric",
                                        "registrations": "registrations"}.get(kind) or (
                                "scans" if "scan" in resource else "numeric" if "numeric" in resource else
                                "orders" if "order" in resource else "visits" if resource in {"visits", "visit_soap"} else "")
                            if category in categories:
                                db.execute("UPDATE bot_task_items SET payload=? WHERE task_id=?",
                                           (encoded({"status": "deleted", "mrn": mrn, "message": "本機資料已清除"}), row["id"]))
                    # Resolve tasks carry multiple patients rather than a single mrn.
                    if "profile" in categories:
                        for row in db.execute("SELECT task_id,key,payload FROM bot_task_items WHERE json_extract(payload,'$.mrn') "
                            "IN (SELECT value FROM json_each(?)) AND json_extract(payload,'$.status')='resolved'", (packed,)).fetchall():
                            raw = json.loads(row["payload"])
                            if raw.get("mrn") in selected and raw.get("status") == "resolved":
                                db.execute("UPDATE bot_task_items SET payload=? WHERE task_id=? AND key=?",
                                           (encoded({"status": "deleted", "mrn": raw["mrn"]}), row["task_id"], row["key"]))
                db.executemany("DELETE FROM list_cache WHERE account=? AND day=?",
                               [(row["account"], row["day"]) for row in preview["lists"]])
            self.app.analysis.store.prune_assets()
            return {"ok": True, **preview, "patients": len(selected), "deleted": len(preview["lists"])}
