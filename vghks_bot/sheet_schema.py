"""Shared typed Sheets parsing contract for planning and Calendar reconciliation."""
from __future__ import annotations

import math
import re
from datetime import date, timedelta

from .analysis_numeric import norm

DATE_HEADER = "日期／報到時間"
DAY_MARKER = "◆ 刀日"
HOSPITALS = frozenset({"高榮", "聯醫"})
DIAGNOSIS_FORMULA_MARKER = "SURGERY_MONTHLY_DIAGNOSIS_SUMMARY_V2"
EVENT_HEADER = "CalendarEventId"
EVENT_ALIASES = (EVENT_HEADER, "日曆eventID", "日曆apiEventID")
_ALIASES = {norm(label).casefold(): EVENT_HEADER for label in EVENT_ALIASES}
_ALIASES.update({norm(label): DATE_HEADER for label in (DATE_HEADER, "時間")})
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_FULL_DATE = re.compile(r"([0-9]{4})[-/.]([0-9]{1,2})[-/.]([0-9]{1,2})(?:\s+(Sun|Mon|Tue|Wed|Thu|Fri|Sat))?", re.I)


def cell_text(cell):
    value = cell.get("userEnteredValue", {})
    if "stringValue" in value:
        return str(value["stringValue"])
    if "numberValue" in value:
        number = value["numberValue"]
        return str(int(number)) if math.isfinite(number) and float(number).is_integer() else str(number)
    if "boolValue" in value:
        return str(value["boolValue"])
    return str(cell.get("formattedValue", ""))


def cancelled_cell(cell):
    """Use displayed strike-through, including rich-text overrides on the MRN."""
    entered = cell.get("userEnteredFormat", {}).get("textFormat", {}).get("strikethrough", False)
    base = bool(cell.get("effectiveFormat", {}).get("textFormat", {}).get("strikethrough", entered))
    runs = cell.get("textFormatRuns", [])
    if not runs:
        return base
    length = len(cell_text(cell).encode("utf-16-le")) // 2
    position, struck = 0, base
    for run in sorted(runs, key=lambda value: value.get("startIndex", 0)):
        start = min(length, max(0, run.get("startIndex", 0)))
        if start > position and struck:
            return True
        position, struck = start, bool(run.get("format", {}).get("strikethrough", base))
    return struck and position < length


def canonical_header(cell_or_text):
    """Resolve only documented aliases and the known GAS diagnosis summary."""
    cell = cell_or_text if isinstance(cell_or_text, dict) else {}
    label = norm(cell_text(cell) if isinstance(cell_or_text, dict) else cell_or_text)
    formula = cell.get("userEnteredValue", {}).get("formulaValue", "")
    if DIAGNOSIS_FORMULA_MARKER in formula or label == "診斷" or label.startswith("診斷|"):
        return "診斷"
    return _ALIASES.get(label.casefold(), _ALIASES.get(label, label))


def sheet_headers(sheet, header_row=0):
    rows = sheet.get("rows", [])
    row = rows[header_row] if len(rows) > header_row else []
    result = {}
    for index, cell in enumerate(row):
        label = canonical_header(cell)
        if label:
            if label in result:
                raise ValueError(f"{sheet['properties']['title']} 有重複欄名 {label}，請先整理欄位。")
            result[label] = index
    return result


def full_date(cell_or_text):
    """Pure 2000–2099 date, with an optional correct English weekday."""
    cell = cell_or_text if isinstance(cell_or_text, dict) else {}
    entered = (cell.get("userEnteredValue", {}) if isinstance(cell_or_text, dict) else
               {"numberValue": cell_or_text} if isinstance(cell_or_text, (int, float)) else {})
    if "numberValue" in cell.get("effectiveValue", {}):
        entered = cell["effectiveValue"]
    if "numberValue" in entered:
        number = entered["numberValue"]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number != int(number):
            return ""
        try:
            day = date(1899, 12, 30) + timedelta(days=int(number))
        except (OverflowError, ValueError):
            return ""
        return day.isoformat() if 2000 <= day.year <= 2099 else ""
    raw = str(cell_text(cell) if isinstance(cell_or_text, dict) else cell_or_text).strip()
    match = _FULL_DATE.fullmatch(raw)
    weekday = ""
    if match:
        year, month, day = map(int, match.groups()[:3])
        weekday = (match[4] or "").casefold()
    elif re.fullmatch(r"[0-9]{8}", raw):
        year, month, day = int(raw[:4]), int(raw[4:6]), int(raw[6:])
    else:
        return ""
    try:
        day = date(year, month, day)
    except ValueError:
        return ""
    if not 2000 <= day.year <= 2099 or weekday and weekday != _WEEKDAYS[day.weekday()]:
        return ""
    return day.isoformat()


def _cell(row, cols, label):
    index = cols.get(label)
    return row[index] if index is not None and index < len(row) else {}


def _date_hint(cell):
    entered = cell.get("userEnteredValue", {})
    if "numberValue" in cell.get("effectiveValue", {}):
        entered = cell["effectiveValue"]
    number = entered.get("numberValue")
    if isinstance(number, (int, float)) and not isinstance(number, bool):
        date_format = cell.get("userEnteredFormat", {}).get("numberFormat", {}).get("type") in {"DATE", "DATE_TIME"}
        # Plain three/four digit numbers may be existing HHmm arrival times.
        # Actual schedule serials or explicitly date-formatted numbers cannot
        # be mistaken for an arrival time when their date is invalid.
        return not math.isfinite(number) or number >= 36526 or date_format and number >= 1
    raw = cell_text(cell).strip()
    return bool(re.search(r"(?:^|[^0-9])[0-9]{3,4}[-/.][0-9]{1,2}[-/.][0-9]{1,2}(?:$|[^0-9])", raw)
                or re.fullmatch(r"[0-9]{8}", raw))


def monthly_row(row, cols):
    """Classify a row without ever inheriting a preceding block past bad headers."""
    mrn = norm(cell_text(_cell(row, cols, "病歷號")))
    person = norm(cell_text(_cell(row, cols, "姓名")))
    cell = _cell(row, cols, DATE_HEADER)
    day = full_date(cell)
    hospital = norm(cell_text(_cell(row, cols, "醫院")))
    event_id = cell_text(_cell(row, cols, EVENT_HEADER)).strip()
    if mrn.startswith("◆") or day or _date_hint(cell):
        if not day or mrn != DAY_MARKER or hospital not in HOSPITALS or event_id:
            return {"kind": "invalid_header", "date": day, "hospital": hospital,
                    "reason": "刀日標題須有純日期、◆ 刀日、高榮或聯醫，且日曆事件 ID 必須留空；請核對日期與報到時間。"}
        return {"kind": "day", "date": day, "hospital": hospital, "reason": ""}
    return {"kind": "patient" if mrn or person else "blank", "date": "", "hospital": hospital, "reason": ""}
