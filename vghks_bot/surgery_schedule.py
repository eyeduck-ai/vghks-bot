"""Account-scoped OPPL schedules, persisted independently of patient analyses."""
from __future__ import annotations

import calendar
import re
from datetime import date

from .analysis_fetch import clean
from .analysis_numeric import iso_day
from .analysis_store import digest
from .clinical_identity import doctor_identity
from .settings import parse_range, timestamp, today

KIND = "surgery_schedule"
EBOARD_URL = "http://wmc01p:29080/EBoardWeb/opEBoardLogon"


def default_range(day=None):
    start = day or today()
    year, month = divmod(start.year * 12 + start.month - 1 + 2, 12)
    end = date(year, month + 1, min(start.day, calendar.monthrange(year, month + 1)[1]))
    return {"start": start.isoformat(), "end": end.isoformat(), "department": "OPH"}


def schedule_day(value):
    value = str(value or "").strip()
    if re.fullmatch(r"\d{7}", value):
        value = f"{value[:3]}/{value[3:5]}/{value[5:]}"
    return iso_day(value)


def text(value):
    if isinstance(value, dict):
        value = value.get("dts", "")
    return str(value).strip() if value is not None else ""


def schedule_rows(records, card, day):
    """Date buckets describe the schedule date, never clinical completion."""
    rows = []
    for index, record in enumerate(records):
        extra = record.get("extra") or {}
        raw_date = text(record.get("surgery_date"))
        scheduled = schedule_day(raw_date)
        physician = text(record.get("doctor_card"))
        mismatch = bool(physician and doctor_identity(physician) != doctor_identity(card))
        group = "unconfirmed" if mismatch or not scheduled else "past" if scheduled < day else "upcoming"
        name = text(record.get("patient_name")) or next((text(extra.get(k)) for k in (
            "orpatnam", "orpatnm", "orname", "hnamec", "patient_name", "name", "姓名") if text(extra.get(k))), "")
        rows.append({**record, "row_id": str(index), "name": name, "raw_date": raw_date,
                     "date": scheduled, "group": group,
                     "note": "回傳醫師與查詢醫師不符" if mismatch else "日期待辨識" if not scheduled else ""})
    return rows


class SurgerySchedule:
    def __init__(self, app):
        self.app, self.db = app, app.review.db

    def require_idle(self):
        if self.db.task_summaries(kinds=(KIND,), statuses=("queued", "running", "cancelling")):
            raise ValueError("手術排程正在查詢，請等待完成或先暫停。")

    def prepare(self, task, values):
        self.require_idle()
        defaults = default_range()
        start, end = parse_range({k: values.get(k, defaults[k]) for k in ("start", "end")}, allow_future=True)
        department = values.get("department", "OPH")
        if not isinstance(department, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", department.strip()):
            raise ValueError("請輸入院內手術科別代碼，例如眼科 OPH。")
        doctor_card = values.get("doctor_card", self.app.username)
        if not isinstance(doctor_card, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", doctor_card.strip()):
            raise ValueError("請輸入有效的醫師卡號。")
        query = {"start": start.isoformat(), "end": end.isoformat(), "department": department.strip().upper(),
                 "doctor_card": doctor_card.strip()}
        self.db.save("preferences", {"id": KIND, "query": query})
        return {**task, **query, "name": f"手術排程 · {query['doctor_card']} · {query['start']} ～ {query['end']}"}

    def execute(self, state, task):
        state.check_cancel()
        # A paused task whose complete response was already saved can finish
        # locally; a new explicit query always checks the hospital again.
        old = self.db.item(task["id"], "schedule")
        if old and old.get("status") == "ready":
            return
        self.app.review.report(state, task, stage=f"正在查詢 {task['doctor_card']} 的手術排程")
        with self.app.sdk_factory(self.app.settings) as sdk:
            records = clean(sdk.surgery.get_schedule(
                task["doctor_card"], date.fromisoformat(task["start"]), date.fromisoformat(task["end"]),
                department=task["department"]))
        state.check_cancel()
        if not isinstance(records, list) or any(not isinstance(r, dict) or
                not isinstance(r.get("extra"), (dict, type(None))) for r in records):
            raise ValueError("手術排程格式無法辨識；先前保存的排程保留。")
        query = {k: task[k] for k in ("start", "end", "department", "doctor_card")}
        key = digest([query, records])
        # Unchanged responses share one payload. Task/result references retain
        # each fetch time and remain correct after subsequent range changes.
        fetched_at = self.app.gateway.response_at() or timestamp()
        result = {"snapshot_id": key, "task_id": task["id"], "fetched_at": fetched_at, **query}
        entries = [("surgery_schedule_result", {"id": task["id"], **result})]
        if not self.db.get("surgery_schedule_snapshot", key, required=False):
            entries.append(("surgery_schedule_snapshot", {"id": key, "records": records}))
        self.db.save_batch(entries)
        self.db.item(task["id"], "schedule", {"status": "ready", "count": len(records), **result})
        self.app.review.report(state, task, stage=f"已保存 {len(records)} 筆手術排程")

    def overview(self, values=None):
        values = values or {}
        tasks = self.db.task_list((KIND,), values)
        tasks.sort(key=lambda t: t["created_at"], reverse=True)
        results = self.db.documents("surgery_schedule_result", limit=1)
        result = results[0] if results else None
        if values.get("task_id"):
            task = self.db.get("task", values["task_id"])
            if task["kind"] != KIND:
                raise ValueError("此任務不是手術排程查詢。")
            result = self.db.get("surgery_schedule_result", task["id"], required=False)
        day = today().isoformat()
        if result:
            snapshot = self.db.get("surgery_schedule_snapshot", result["snapshot_id"])
            result = {**result, "rows": schedule_rows(snapshot["records"], result["doctor_card"], day)}
        preferences = self.db.get("preferences", KIND, required=False)
        return {"today": day, "defaults": default_range(), "doctor_card": self.app.username,
                "query": {"doctor_card": self.app.username, **(preferences["query"] if preferences else default_range())},
                "result": result, "tasks": tasks}
