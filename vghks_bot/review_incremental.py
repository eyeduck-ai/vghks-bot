"""Explicit review ancestry, immutable copied results and targeted SOAP refresh."""
from __future__ import annotations

import copy
import json
from datetime import datetime

from vghks_sdk.identifiers import normalize_mrn

from .library import registration_id
from .settings import TAIPEI, today
from .tags import classification


def source_task(review, values):
    key = values.get("base_task_id")
    if key is None:
        if "refresh_mrn" in values:
            raise ValueError("單人重新抓取需要指定來源檢閱。")
        return None
    if not isinstance(key, str) or not key or len(key) > 128:
        raise ValueError("來源檢閱代碼不正確。")
    source = review._note_task({"task_id": key})
    if source.get("account_id", review.app.account_id) != review.app.account_id:
        raise ValueError("來源檢閱不屬於此帳號。")
    if "refresh_mrn" in values:
        mrn = normalize_mrn(values["refresh_mrn"])
        if mrn not in {member["mrn"] for member in source["members"]}:
            raise ValueError("病人不在來源檢閱中。")
        if source["status"] not in {"completed", "partial"}:
            raise ValueError("請先完成或續跑目前檢閱，再重新抓取單一病人。")
        if values.get("force") or values.get("refresh"):
            raise ValueError("不能同時指定單人與全部重新抓取。")
    return source


def scope(task):
    keywords = task.get("department_keywords")
    if keywords is None and task.get("department_keyword"):
        keywords = [task["department_keyword"]]
    department = (sorted({word.strip().casefold() for word in keywords}) if keywords else
                  [task.get("department_filter", "same"), task.get("department", {})])
    return [task["mode"], task["cutoff"], department]


def same_day(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.tzinfo is not None and parsed.astimezone(TAIPEI).date() == today()
    except (ValueError, TypeError):
        return False


def registrations(member):
    return sorted({registration_id(row) for row in member.get("registrations", [])})


def inherit(review, task, source):
    """Called in the short task-creation transaction, with no network operations."""
    if source is None:
        return
    task["base_task_id"] = source["id"]
    originals = {member["mrn"]: member for member in source["members"]}
    saved = {row["mrn"]: row for row in review.db.items(source["id"])}
    members = {member["mrn"]: member for member in task["members"]}
    task["new_mrns"] = [mrn for mrn in members if mrn not in originals]
    task["new_count"] = len(task["new_mrns"])
    task["reused_count"] = 0
    task["recheck_mrns"] = []
    settings = review.app.settings.update({"categories": task["categories"]})
    target = task.get("refresh_mrn")
    matching = scope(task) == scope(source)
    for mrn, member in members.items():
        old = saved.get(mrn)
        checked = ((old or {}).get("checked_at") or (old or {}).get("index_checked_at", ""))
        if mrn in originals and not same_day(checked):
            task["recheck_mrns"].append(mrn)
        if not old or mrn == target:
            continue
        complete = (not old.get("processing") and
                    (old["status"] == "no_visit" or old["status"] == "deleted" or
                     old["status"] == "ready" and bool(old.get("records"))))
        available = all(review.app.store.library.get_record(record["id"]) is not None
                        for record in old.get("records", []))
        reusable = (matching and same_day(checked) and complete and available and
                    not task["force"] and not task["refresh"] and
                    (task["mode"] != "registration" or
                     registrations(member) == registrations(originals[mrn])))
        if not (target or reusable):
            continue
        value = copy.deepcopy(old)
        value.update(member, checked_at=checked, reused=True, reused_from_task_id=source["id"],
                     reuse_label="沿用今日結果" if same_day(checked) else "沿用原檢閱結果")
        for record in value.get("records", []):
            record.update(cached=True, **classification(record, settings))
        review.db.item(task["id"], mrn, value)
        task["reused_count"] += 1
    task["pending_count"] = len(members) - task["reused_count"]
    # Copy only retained members' notes, without scanning other review histories.
    with review.app.store.library.connect() as db:
        notes = db.execute("SELECT payload FROM bot_documents WHERE kind='review_note' "
                           "AND id IN (SELECT ? || ':' || value FROM json_each(?))",
                           (source["id"], json.dumps(list(members)))).fetchall()
    for row in notes:
        note = json.loads(row[0])
        mrn = note["id"].split(":", 1)[1]
        review.db.save("review_note", {"id": task["id"] + ":" + mrn, "text": note["text"]})


def ordered_members(task):
    members = task["members"]
    if task.get("refresh_mrn"):
        return [member for member in members if member["mrn"] == task["refresh_mrn"]]
    new = set(task.get("new_mrns", []))
    return sorted(members, key=lambda member: member["mrn"] not in new)
