"""Pure full-workbook classification and Calendar presentation contract."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta

from .analysis_numeric import norm
from .settings import TAIPEI
from .sheet_plan import cell_at, cell_text, patient_key
from .sheet_schema import DATE_HEADER, EVENT_ALIASES, full_date, monthly_row, sheet_headers

SYNC_HEADER = "ScheduleSyncId"
EVENT_HEADER = "CalendarEventId"
OWNER_KEY = "vghks.calendar.owner"
ALIASES = {**{label: EVENT_HEADER for label in EVENT_ALIASES}, "時間": DATE_HEADER, "日期/報到時間": DATE_HEADER}
UID = re.compile(r"^[a-f0-9]{32}$")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def event_id(spreadsheet_id, sync_id):
    return "vghks" + digest([spreadsheet_id, sync_id])[:48]


def sheet_kind(sheet):
    name = sheet["properties"]["title"]
    if re.fullmatch(r"\d{4}(?:0[1-9]|1[0-2])", name):
        return "MONTHLY"
    if name in {"FU", "All"}:
        return "FU"
    if re.fullmatch(r"刀表封存_\d{4}", name):
        return "ARCHIVE"
    return ""


def headers(sheet):
    row_index = 5 if sheet_kind(sheet) == "ARCHIVE" else 0
    return row_index, sheet_headers(sheet, row_index)


def value(row, cols, label):
    return cell_text(cell_at(row, cols[label])).strip() if label in cols else ""


def identity(mrn, name):
    return "mrn:" + patient_key(mrn) if mrn.strip() else "name:" + norm(name)


def full_day(cell):
    return full_date(cell)


def scan(book, include_fu=True):
    """Return all identities, eligible patients, archives and deletion safety diagnostics."""
    patients, archive_ids, seen, events, issues, sheet_ids = [], set(), {}, {}, [], set()
    has_fu = any(s["properties"]["title"] == "FU" for s in book.get("sheets", []))
    for sheet in book.get("sheets", []):
        kind = sheet_kind(sheet)
        if not kind:
            continue
        sid, name = sheet["properties"]["sheetId"], sheet["properties"]["title"]
        sheet_ids.add(sid)
        try:
            header, cols = headers(sheet)
        except ValueError as exc:
            issues.append(str(exc))
            continue
        required = {"病歷號", "姓名", SYNC_HEADER, EVENT_HEADER}
        required.add("日期／報到時間" if kind == "MONTHLY" else "日期" if kind == "FU" else "ArchiveRowType")
        if kind == "MONTHLY":
            required.add("醫院")
        if required - cols.keys():
            issues.append(f"{name} 缺少欄位：{'、'.join(sorted(required - cols.keys()))}")
            continue
        block = None
        for index, row in enumerate(sheet.get("rows", [])[header + 1:], header + 1):
            mrn, person = value(row, cols, "病歷號"), value(row, cols, "姓名")
            uid, eid = value(row, cols, SYNC_HEADER), value(row, cols, EVENT_HEADER)
            ref = f"{name}!{index + 1}"
            is_header = False
            if kind == "MONTHLY":
                parsed = monthly_row(row, cols)
                if parsed["kind"] in {"day", "invalid_header"}:
                    if parsed["kind"] == "invalid_header":
                        issues.append(ref + " " + parsed["reason"])
                        block = None
                    else:
                        block = {"date": parsed["date"], "hospital": parsed["hospital"], "header_uid": uid}
                    is_header = True
            content = bool(mrn or person or is_header)
            if uid:
                if not UID.fullmatch(uid):
                    issues.append(ref + " 排程 ID 格式錯誤")
                if uid in seen:
                    issues.append(ref + " 排程 ID 重複於 " + seen[uid])
                seen[uid] = ref
                if not content and kind != "ARCHIVE":
                    issues.append(ref + " 只剩排程 ID，疑似僅搬移可見欄位")
            elif content:
                issues.append(ref + " 缺少排程 ID")
            if eid:
                if not uid or not content:
                    issues.append(ref + " 日曆 ID 沒有對應完整排程，請核對搬移結果")
                if eid in events:
                    issues.append(ref + " 日曆 ID 重複於 " + events[eid])
                events[eid] = ref
            if kind == "ARCHIVE":
                if uid:
                    archive_ids.add(uid)
                continue
            if is_header or not (mrn or person):
                continue
            if kind == "MONTHLY" and not block:
                issues.append(ref + " 病人列上方沒有有效刀日")
                continue
            if kind == "FU" and (not include_fu or name == "All" and has_fu):
                # Still include these IDs in deletion protection.
                archive_ids.add(uid)
                continue
            day = block["date"] if kind == "MONTHLY" else full_day(cell_at(row, cols["日期"]))
            if kind == "FU" and value(row, cols, "日期") and not day:
                issues.append(ref + " 追蹤日期無法辨識")
            hospital = block["hospital"] if kind == "MONTHLY" else value(row, cols, "醫院")
            cancelled = bool(cell_at(row, cols["病歷號"]).get("userEnteredFormat", {})
                             .get("textFormat", {}).get("strikethrough"))
            patients.append({"uid": uid, "event_id": eid, "identity": identity(mrn, person),
                "kind": kind, "sheet_id": sid, "sheet": name, "row": index, "cols": cols,
                "date": day, "hospital": hospital, "mrn": mrn, "name": person,
                "header_uid": block["header_uid"] if block and kind == "MONTHLY" else "",
                "time": value(row, cols, "日期／報到時間") if kind == "MONTHLY" else "",
                "time_cell": cell_at(row, cols.get("日期／報到時間", -1)) if kind == "MONTHLY" else {},
                "cancelled": cancelled, "fields": {k: value(row, cols, k) for k in cols}})
    return {"patients": patients, "archive_ids": archive_ids, "identities": set(seen),
            "issues": issues, "sheet_ids": sheet_ids}


def arrival(item):
    value_ = item["time_cell"].get("userEnteredValue", {})
    if "numberValue" in item["time_cell"].get("effectiveValue", {}):
        value_ = item["time_cell"]["effectiveValue"]
    if "numberValue" in value_ and 0 <= value_["numberValue"] < 1:
        minutes = round(value_["numberValue"] * 1440) % 1440
        return f"{minutes // 60:02}:{minutes % 60:02}", ""
    raw = item["time"].strip().replace("：", ":")
    if not raw:
        return "", ""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw) or re.fullmatch(r"(\d{1,2})(\d{2})", raw)
    if match:
        hour, minute = map(int, match.groups())
        if hour > 23 or minute > 59:
            raise ValueError(item["sheet"] + " 有無效報到時間")
        return f"{hour:02}:{minute:02}", ""
    return "", raw


def condition(item):
    fields = item["fields"]
    if item["kind"] == "FU":
        return fields.get("Condition", "")
    side = fields.get("側別", "")
    diagnosis = fields.get("診斷", "")
    grade = fields.get("Grade", "")
    procedure = fields.get("術式", "")
    iol = [fields.get(k, "") for k in ("IOL", "IOL Target", "IOL Final")]
    axis = fields.get("Axis", "").lstrip("@")
    if axis:
        iol.append("@" + axis)
    parts = []
    if diagnosis:
        parts.append(diagnosis + (f"({grade})" if grade else "") +
                     (" " + side if side and not re.search(r"\b(?:OD|OS|OU)\b", diagnosis) else ""))
    if procedure:
        if parts:
            parts.append("s/p")
        lens = " ".join(v for v in iol if v)
        parts.append(procedure + (f"({lens})" if lens else "") +
                     (" " + side if side and not re.search(r"\b(?:OD|OS|OU)\b", procedure) else ""))
    if item["date"]:
        parts.append(item["date"].replace("-", ""))
    return " ".join(parts)


def resource(item, spreadsheet_id):
    if not item["date"]:
        return None
    time_, note = arrival(item) if item["kind"] == "MONTHLY" else ("", "")
    ga = item["fields"].get("GA", "")
    all_day = item["kind"] == "FU" or bool(ga) or not time_
    colour = "8" if item["cancelled"] or item["kind"] == "FU" else "4" if ga else "10" if time_ else "2"
    descriptions = [("醫院", item["hospital"]), ("TEL", item["fields"].get("TEL", "")),
                    ("Plan", item["fields"].get("Plan", "")), ("GA", ga)]
    if all_day and time_:
        descriptions.append(("原報到時間", time_))
    if note:
        descriptions.append(("Time note", note))
    result = {"summary": " | ".join([item["mrn"], item["name"], condition(item)]),
              "description": "\n".join(f"{k}: {v}" for k, v in descriptions if v), "colorId": colour,
              "extendedProperties": {"private": {"vghksSource": spreadsheet_id, "vghksSyncId": item["uid"],
                  "surgerySyncKind": item["kind"], "surgerySyncState": "CANCELLED" if item["cancelled"] else "ACTIVE"}}}
    day = date.fromisoformat(item["date"])
    if all_day:
        result.update(start={"date": day.isoformat()}, end={"date": (day + timedelta(days=1)).isoformat()})
    else:
        hour, minute = map(int, time_.split(":"))
        start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TAIPEI)
        result.update(start={"dateTime": start.isoformat(), "timeZone": "Asia/Taipei"},
                      end={"dateTime": (start + timedelta(minutes=30)).isoformat(), "timeZone": "Asia/Taipei"})
    return result


def matches(actual, expected):
    if not actual:
        return False
    for key in ("summary", "description", "colorId"):
        if actual.get(key, "") != expected.get(key, ""):
            return False
    for key in ("start", "end"):
        a, e = actual.get(key, {}), expected[key]
        if "date" in e:
            if a.get("date") != e["date"]:
                return False
        else:
            try:
                if datetime.fromisoformat(a.get("dateTime", "").replace("Z", "+00:00")) != datetime.fromisoformat(e["dateTime"]):
                    return False
            except (ValueError, TypeError):
                return False
    private = actual.get("extendedProperties", {}).get("private", {})
    return all(private.get(k) == v for k, v in expected["extendedProperties"]["private"].items())
