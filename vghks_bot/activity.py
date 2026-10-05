"""Search and page task families without returning the complete workbench."""
from __future__ import annotations

from datetime import date

CHILD_KINDS = {"history", "numeric", "registrations"}
STATUSES = {"queued", "running", "cancelling", "completed", "partial", "paused", "cancelled", "interrupted", "failed"}
SYSTEMS = {"門診系統", "審查查詢", "薪資業績", "手術系統", "進階工具", "SDK"}
LABELS = {"queued": "排隊中", "running": "執行中", "completed": "完成", "partial": "部分完成",
          "paused": "待續跑", "cancelled": "已暫停", "interrupted": "待續跑", "failed": "失敗", "cancelling": "暫停中"}


def system(task):
    kind = task.get("kind", "")
    if kind.startswith("approval_"):
        return "審查查詢"
    if kind.startswith("earnings_"):
        return "薪資業績"
    if kind == "surgery_schedule":
        return "手術系統"
    if task.get("session"):
        return "SDK"
    return "進階工具" if kind == "analysis" or task.get("cohort_id") else "門診系統"


def search(work, values):
    query = values.get("q", "")
    if not isinstance(query, str) or len(query) > 500:
        raise ValueError("搜尋字串最多 500 字。")
    origin, status = values.get("system", ""), values.get("status", "")
    if origin not in {"", *SYSTEMS} or status not in {"", "active", *STATUSES}:
        raise ValueError("紀錄篩選不正確。")
    start, end = values.get("start", ""), values.get("end", "")
    for value in (start, end):
        if value:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except (TypeError, ValueError):
                raise ValueError("日期篩選不正確。") from None
    if start and end and start > end:
        raise ValueError("結束日期不可早於開始日期。")
    try:
        offset, limit = int(values.get("offset", 0)), int(values.get("limit", 40))
    except (TypeError, ValueError):
        raise ValueError("分頁不正確。") from None
    if offset < 0 or not 1 <= limit <= 200:
        raise ValueError("分頁不正確。")
    if values.get("notes", "") not in {"", "1"}:
        raise ValueError("備註篩選不正確。")
    # File journals and live state locks must be read before the SQLite batch.
    runs = [{**r, "bot": False} for r in work.history()["runs"] if r.get("kind") != "bot"]
    with work.store.library.read_snapshot() as db:
        tasks = [{**t, "bot": True} for t in work.review.db.task_summaries()]
        notes = work.review.db.review_note_counts()
        sessions = [{**s, "session": True} for s in work.review.db.sdk_sessions()]
        # Verify ancestry and membership even for records created by older releases.
        links = dict(db.execute("""SELECT c.id,p.id FROM bot_documents c JOIN bot_documents p
            ON p.kind='task' AND p.id=json_extract(c.payload,'$.review_task_id')
            AND json_extract(p.payload,'$.kind')='review'
            WHERE c.kind='task' AND json_extract(c.payload,'$.kind') IN ('history','numeric','registrations')
            AND (json_extract(c.payload,'$.account_id') IS NULL OR json_extract(p.payload,'$.account_id') IS NULL
                 OR json_extract(c.payload,'$.account_id')=json_extract(p.payload,'$.account_id'))
            AND EXISTS(SELECT 1 FROM json_each(p.payload,'$.members') m
                       WHERE json_extract(m.value,'$.mrn')=json_extract(c.payload,'$.mrn'))"""))
        member_matches = set()
        needle = query.strip().casefold()
        if needle:
            db.create_function("casefold", 1, lambda text: str(text or "").casefold())
            member_matches = {r[0] for r in db.execute("""SELECT DISTINCT t.id FROM bot_documents t,
                json_each(t.payload,'$.members') m WHERE t.kind='task' AND
                instr(casefold(coalesce(json_extract(m.value,'$.name'),'')||' '||json_extract(m.value,'$.mrn')),?)>0""", (needle,))}
        children = {}
        for task in tasks:
            if task["id"] in links:
                children.setdefault(links[task["id"]], []).append(task)
        roots = [t for t in tasks if t["id"] not in links] + runs + sessions
        groups = []
        for task in roots:
            family = [task, *children.get(task["id"], [])] if task.get("bot") else [task]
            if origin and system(task) != origin:
                continue
            wanted = {"queued", "running", "cancelling"} if status == "active" else {status}
            if status and not any(t.get("status") in wanted for t in family):
                continue
            day = task.get("created_at", "")[:10]
            if start and day < start or end and day > end:
                continue
            if values.get("notes") and not (task.get("bot") and task.get("kind") == "review" and notes.get(task["id"], 0)):
                continue
            if needle and task["id"] not in member_matches and not any(needle in " ".join(str(t.get(k, "")) for k in
                    ("name", "analysis_name", "id", "mrn", "patient_name", "resource", "reference", "created_at")) .casefold()
                    or needle in system(t).casefold() or needle in LABELS.get(t.get("status"), "").casefold() for t in family):
                continue
            latest = max(max(t.get("created_at", ""), t.get("updated_at", "")) for t in family)
            groups.append((latest, task, family))
        groups.sort(key=lambda g: (g[0], g[1]["id"]), reverse=True)
        selected = []
        for _, task, family in groups[offset:offset + limit]:
            if task.get("bot") and task.get("kind") == "review":
                parent = work.review.db.get("task", task["id"])
                task["members"] = [{"mrn": m["mrn"], "name": m.get("name", "")} for m in parent.get("members", [])]
                task["note_count"] = notes.get(task["id"], 0)
            selected.extend(family)
    return {"tasks": selected, "total": len(groups), "total_groups": len(roots), "offset": offset, "limit": limit}
