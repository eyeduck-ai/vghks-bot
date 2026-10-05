"""Aggregate clinical cache metadata without loading clinical documents."""
from __future__ import annotations

import json

from .pagination import page


def read(owner, values):
    from .library_data import CATEGORIES, step_category

    limit, offset = page(values)
    with owner.library.read_snapshot() as db:
        db.create_function("cache_category", 2, step_category, deterministic=True)
        db.create_function("casefold", 1, lambda value: str(value or "").casefold(), deterministic=True)
        # Counts and names are projections. SOAP, numeric bodies and report
        # versions never cross the SQLite boundary for inventory queries.
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        source = "SELECT * FROM bot_documents" if "bot_documents" in tables else "SELECT '' AS kind,'' AS id,'{}' AS payload,'' AS updated_at WHERE 0"
        prefix = "WITH documents AS (" + source + """), categories AS (
            SELECT mrn,cache_category(key,kind) AS category,count(*) AS count,0 AS versions,
                   max(saved_at) AS updated_at FROM analysis_steps
                WHERE kind!='numeric_structured' GROUP BY mrn,category
            UNION ALL SELECT v.mrn,cache_category(v.key,coalesce(s.kind,'')),0,count(*),max(v.saved_at)
                FROM analysis_versions v LEFT JOIN analysis_steps s ON s.mrn=v.mrn AND s.key=v.key GROUP BY 1,2
            UNION ALL SELECT coalesce(nullif(json_extract(payload,'$.mrn'),''),id),kind,1,0,updated_at
                FROM documents WHERE kind IN ('profile','registrations','visits','numeric')
            UNION ALL SELECT r.mrn,cache_category(r.key,coalesce(s.kind,'')),0,0,''
                FROM analysis_asset_refs r JOIN analysis_assets a ON a.digest=r.digest
                LEFT JOIN analysis_steps s ON s.mrn=r.mrn AND s.key=r.key GROUP BY 1,2
        ), names AS (
            SELECT r.mrn,coalesce(i.name,json_extract(r.payload,'$.name'), '') AS name,0 AS priority,r.updated_at
                FROM records r LEFT JOIN record_search i ON i.id=r.id AND NOT EXISTS(SELECT 1 FROM record_search_dirty d WHERE d.id=r.id)
            UNION ALL SELECT coalesce(nullif(json_extract(payload,'$.mrn'),''),id),
                coalesce(json_extract(payload,'$.name'),''),1,updated_at
                FROM documents WHERE kind IN ('profile','registrations','visits','numeric')
            UNION ALL SELECT json_extract(m.value,'$.mrn'),coalesce(json_extract(m.value,'$.name'),''),2,d.updated_at
                FROM documents d,json_each(d.payload,'$.members') m WHERE d.kind='set'
            UNION ALL SELECT json_extract(m.value,'$.mrn'),coalesce(json_extract(m.value,'$.name'),''),3,c.updated_at
                FROM analysis_cohorts c,json_each(c.payload,'$.members') m
        ), patients AS (
            SELECT c.mrn,coalesce((SELECT name FROM names n WHERE n.mrn=c.mrn AND name!=''
                ORDER BY priority DESC,updated_at DESC LIMIT 1),'') AS name,max(c.updated_at) AS updated_at
                FROM categories c GROUP BY c.mrn
        ), filtered AS (SELECT * FROM patients WHERE instr(casefold(mrn||' '||name),?)>0) """
        query = str(values.get("q", "")).strip().casefold()
        total = db.execute(prefix + "SELECT count(*) FROM filtered", (query,)).fetchone()[0]
        suffix, args = ("", [query]) if limit is None else (" LIMIT ? OFFSET ?", [query, limit, offset])
        rows = [dict(row) for row in db.execute(prefix + "SELECT * FROM filtered ORDER BY name,mrn" + suffix, args)]
        packed = json.dumps([row["mrn"] for row in rows])
        scope = "mrn IN (SELECT value FROM json_each(?))"
        by_mrn = {row["mrn"]: {**row, "categories": {}, "attachment_bytes": 0} for row in rows}
        for row in db.execute(prefix + "SELECT mrn,category,sum(count) AS count,sum(versions) AS versions "
                              "FROM categories WHERE " + scope + " GROUP BY mrn,category", (query, packed)):
            by_mrn[row["mrn"]]["categories"][row["category"]] = {
                "count": row["count"], "versions": row["versions"], "attachments": 0, "attachment_bytes": 0}
        seen = set()
        for row in db.execute("SELECT DISTINCT r.mrn,cache_category(r.key,coalesce(s.kind,'')) AS category,a.digest,a.size "
                              "FROM analysis_asset_refs r JOIN analysis_assets a ON a.digest=r.digest "
                              "LEFT JOIN analysis_steps s ON s.mrn=r.mrn AND s.key=r.key WHERE r." + scope, (packed,)):
            patient = by_mrn[row["mrn"]]
            item = patient["categories"][row["category"]]
            item["attachments"] += 1
            item["attachment_bytes"] += row["size"]
            key = row["mrn"], row["digest"]
            if key not in seen:
                seen.add(key)
                patient["attachment_bytes"] += row["size"]
        list_where, list_args = ["1"], []
        for field, op in (("start", ">="), ("end", "<=")):
            if values.get(field):
                list_where.append("day" + op + "?")
                list_args.append(values[field])
        condition = " AND ".join(list_where)
        list_total = db.execute("SELECT count(*) FROM list_cache WHERE " + condition, list_args).fetchone()[0]
        if limit is not None:
            list_args += [limit, offset]
        lists = [dict(row) for row in db.execute("SELECT account,day,fetched_at AS updated_at,json_array_length(rows_json) AS count "
                 "FROM list_cache WHERE " + condition + " ORDER BY day DESC,account" + suffix, list_args)]
    result = {"patients": list(by_mrn.values()), "lists": lists,
              "categories": [{"id": key, "name": name} for key, name in CATEGORIES.items()]}
    if limit is not None:
        result.update(total=total, list_total=list_total, limit=limit, offset=offset)
    return result
