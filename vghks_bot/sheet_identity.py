"""Stable schedule identities and a non-destructive Sheets migration planner.

The ID belongs to an entire row, not its address, patient, date or Calendar ID.
All functions accept the typed ``SheetsClient.read_all`` book representation.
"""
from __future__ import annotations

import copy
import re
import uuid

from .analysis_numeric import norm
from .sheet_schema import EVENT_ALIASES, EVENT_HEADER, cell_text, monthly_row, sheet_headers

SYNC_ID_HEADER = "ScheduleSyncId"
EVENT_ID_HEADER = EVENT_HEADER
EVENT_ID_ALIASES = EVENT_ALIASES
SYNC_STATE_TITLE = "__VGHKS_SYNC_STATE"
UID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
MONTH_PATTERN = re.compile(r"\d{4}(?:0[1-9]|1[0-2])\Z")
ARCHIVE_PATTERN = re.compile(r"刀表封存_\d{4}\Z")


def text(cell):
    return cell_text(cell)


def schema_kind(sheet):
    title = sheet.get("properties", {}).get("title", "")
    if MONTH_PATTERN.fullmatch(title):
        return "month", 0
    if title in {"FU", "All"}:
        return "fu", 0
    if ARCHIVE_PATTERN.fullmatch(title):
        return "archive", 5
    return None, None


def header_columns(sheet, header_row=0):
    return sheet_headers(sheet, header_row)


def _value(row, cols, label):
    index = cols.get(label)
    return text(row[index]) if index is not None and index < len(row) else ""


def _kind(row, cols):
    mrn = norm(_value(row, cols, "病歷號"))
    if mrn == "◆ 刀日":
        return "day"
    if mrn or norm(_value(row, cols, "姓名")):
        return "patient"
    return "orphan"


def scan_identities(book):
    """Return every present ID, including malformed/orphan/archive IDs.

    A duplicated/invalid ID is never repaired by inventing a replacement. These
    rows must remain visible to the monitor's deletion protection and the UI.
    ``by_id`` values are lists so callers cannot accidentally pick a duplicate.
    """
    by_id, issues = {}, []
    for sheet in book.get("sheets", []):
        schema, header_row = schema_kind(sheet)
        if schema is None:
            continue
        try:
            cols = header_columns(sheet, header_row)
        except ValueError as exc:
            issues.append({"code": "duplicate_header", "sheet": sheet["properties"]["title"], "reason": str(exc)})
            continue
        uid_col = cols.get(SYNC_ID_HEADER)
        if uid_col is None:
            # Even before ID migration, invalid blocks must prevent any write.
            uid_col = -1
        in_block = schema != "month"
        for index, row in enumerate(sheet.get("rows", [])[header_row + 1:], header_row + 1):
            kind = _kind(row, cols)
            if schema == "month":
                parsed = monthly_row(row, cols)
                kind = parsed["kind"]
                if kind in {"day", "invalid_header"}:
                    in_block = kind == "day"
                    if not in_block:
                        issues.append({"code": "invalid_day_header", "sheet": sheet["properties"]["title"],
                                       "row": index, "reason": parsed["reason"]})
                elif kind == "patient" and not in_block:
                    issues.append({"code": "missing_day_context", "sheet": sheet["properties"]["title"], "row": index})
                if kind == "blank":
                    kind = "orphan"
            uid = text(row[uid_col]).strip() if 0 <= uid_col < len(row) else ""
            if not uid:
                continue
            location = {"sheet": sheet["properties"]["title"], "sheet_id": sheet["properties"]["sheetId"],
                        "row": index, "column": uid_col, "schema": schema,
                        "kind": kind if in_block else "orphan", "sync_id": uid}
            by_id.setdefault(uid, []).append(location)
            if not UID_PATTERN.fullmatch(uid):
                issues.append({"code": "invalid_sync_id", **location})
            if location["kind"] == "orphan":
                issues.append({"code": "orphan_sync_id", **location})
    for uid, locations in by_id.items():
        if len(locations) > 1:
            issues.append({"code": "duplicate_sync_id", "sync_id": uid, "locations": locations})
    return {"by_id": by_id, "issues": issues}


def _last_content_column(sheet, *, exclude=()):
    cols = header_columns(sheet, 5 if schema_kind(sheet)[0] == "archive" else 0)
    skipped = {cols[label] for label in exclude if label in cols}
    return max((index for row in sheet.get("rows", []) for index, cell in enumerate(row)
                if index not in skipped and (cell.get("userEnteredValue") or cell.get("note") or cell.get("textFormatRuns"))), default=-1)


def _identity_target_columns(sheet, cols, target_columns=None):
    """Predict ensure_identity_columns positions without changing the snapshot."""
    result, last = {}, _last_content_column(sheet)
    for label in (EVENT_ID_HEADER, SYNC_ID_HEADER):
        col = cols.get(label)
        if col is None:
            col = (target_columns or {}).get(label, last + 1)
            last = max(last, col)
        result[label] = col
    return result


def _protected_identity_issues(sheet, identity_cols):
    """Fail closed before hiding or writing a column the requester cannot edit.

    A warning protection does not override another strict protection. GridRange
    omitted bounds are unbounded, and an omitted sheetId means sheet zero.
    """
    issues = []
    sid = sheet["properties"]["sheetId"]
    row_count = sheet["properties"].get("gridProperties", {}).get("rowCount", len(sheet.get("rows", [])))
    for protection in sheet.get("protectedRanges", []):
        if protection.get("warningOnly") is True or protection.get("requestingUserCanEdit") is True:
            continue
        protected = protection.get("range", {})
        if protected.get("sheetId", 0) != sid:
            continue
        row_start = protected.get("startRowIndex", 0)
        row_end = min(row_count, protected.get("endRowIndex", row_count))
        if row_start >= row_end:
            continue
        for label, col in identity_cols.items():
            if protected.get("startColumnIndex", 0) <= col < protected.get("endColumnIndex", float("inf")):
                issues.append({"code": "protected_identity_range", "sheet": sheet["properties"]["title"],
                               "column": col, "header": label, "protected_range_id": protection.get("protectedRangeId"),
                               "range": copy.deepcopy(protected),
                               "reason": f"{sheet['properties']['title']} 的 {label} 欄受到嚴格保護，"
                                         "目前存取帳號無編輯權限；請由試算表擁有者調整該保護的編輯權限後重試。"})
    return issues


def ensure_identity_columns(sheet, requests, *, hide=True, header_row=0, target_columns=None):
    """Plan narrow technical-column writes and hiding, without adding protections."""
    cols = header_columns(sheet, header_row)
    grid = sheet["properties"]["gridProperties"]
    rows = sheet.setdefault("rows", [])
    while len(rows) <= header_row:
        rows.append([])
    if EVENT_ID_HEADER in cols and text(rows[header_row][cols[EVENT_ID_HEADER]]) != EVENT_ID_HEADER:
        col = cols[EVENT_ID_HEADER]
        rows[header_row][col]["userEnteredValue"] = {"stringValue": EVENT_ID_HEADER}
        cols[EVENT_ID_HEADER] = col
        requests.append({"updateCells": {"range": {"sheetId": sheet["properties"]["sheetId"],
                         "startRowIndex": header_row, "endRowIndex": header_row + 1,
                         "startColumnIndex": col, "endColumnIndex": col + 1},
                         "rows": [{"values": [{"userEnteredValue": {"stringValue": EVENT_ID_HEADER}}]}], "fields": "userEnteredValue"}})
    for label in (EVENT_ID_HEADER, SYNC_ID_HEADER):
        if label not in cols:
            # Do not reuse an unnamed column: it may contain manual row data.
            col = (target_columns or {}).get(label, _last_content_column(sheet) + 1)
            if any(col < len(row) and (row[col].get("userEnteredValue") or row[col].get("note") or row[col].get("textFormatRuns")) for row in rows):
                raise ValueError("技術欄目的位置已有人工內容，請先對齊月表欄位。")
            extra = max(0, col + 1 - grid["columnCount"])
            if extra:
                requests.append({"appendDimension": {"sheetId": sheet["properties"]["sheetId"],
                                  "dimension": "COLUMNS", "length": extra}})
                grid["columnCount"] += extra
            while len(rows[header_row]) <= col:
                rows[header_row].append({})
            rows[header_row][col] = {"userEnteredValue": {"stringValue": label}}
            requests.append({"updateCells": {"range": {"sheetId": sheet["properties"]["sheetId"],
                             "startRowIndex": header_row, "endRowIndex": header_row + 1,
                             "startColumnIndex": col, "endColumnIndex": col + 1},
                             "rows": [{"values": [copy.deepcopy(rows[header_row][col])]}], "fields": "userEnteredValue"}})
            cols[label] = col
        col = cols[label]
        metadata = sheet.setdefault("column_widths", [])
        while len(metadata) <= col:
            metadata.append({})
        if hide and not metadata[col].get("hiddenByUser"):
            requests.append({"updateDimensionProperties": {
                "range": {"sheetId": sheet["properties"]["sheetId"], "dimension": "COLUMNS",
                          "startIndex": col, "endIndex": col + 1},
                "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})
            metadata[col]["hiddenByUser"] = True
    return cols


def set_row_identity(sheet, row_index, requests, *, sync_id=None, header_row=0, hide=True):
    cols = ensure_identity_columns(sheet, requests, hide=hide, header_row=header_row)
    row, col = sheet["rows"][row_index], cols[SYNC_ID_HEADER]
    while len(row) <= col:
        row.append({})
    existing = text(row[col]).strip()
    if existing:
        if not UID_PATTERN.fullmatch(existing):
            raise ValueError("ScheduleSyncId 格式錯誤，請先整理刀表。")
        return existing
    uid = sync_id or uuid.uuid4().hex
    if not UID_PATTERN.fullmatch(uid):
        raise ValueError("ScheduleSyncId 必須是 32 位小寫 UUID hex。")
    if row[col].get("userEnteredValue", {}).get("formulaValue"):
        raise ValueError("ScheduleSyncId 不可覆寫公式。")
    row[col]["userEnteredValue"] = {"stringValue": uid}
    requests.append({"updateCells": {"range": {"sheetId": sheet["properties"]["sheetId"],
                     "startRowIndex": row_index, "endRowIndex": row_index + 1,
                     "startColumnIndex": col, "endColumnIndex": col + 1},
                     "rows": [{"values": [{"userEnteredValue": {"stringValue": uid}}]}], "fields": "userEnteredValue"}})
    return uid


def plan_identity_setup(book, *, include_archives=True):
    result, requests, issues = copy.deepcopy(book), [], []
    month_sheets = [s for s in result.get("sheets", []) if schema_kind(s)[0] == "month"]
    technical = (*EVENT_ID_ALIASES, SYNC_ID_HEADER)
    last_manual = -1
    event_positions, uid_positions = [], []
    for sheet in month_sheets:
        try:
            cols = header_columns(sheet)
            last_manual = max(last_manual, _last_content_column(sheet, exclude=technical))
            event_positions.extend(cols[label] for label in EVENT_ID_ALIASES if label in cols)
            if SYNC_ID_HEADER in cols:
                uid_positions.append(cols[SYNC_ID_HEADER])
        except ValueError:
            pass
    event_target = max([last_manual + 1, *event_positions])
    uid_target = max([last_manual + 1, event_target + 1, *uid_positions])
    month_targets = {EVENT_ID_HEADER: event_target, SYNC_ID_HEADER: uid_target}
    # Check the entire setup before even planning a hide/header/ID write. A
    # caller must never accidentally apply the unblocked part of a migration.
    for sheet in result.get("sheets", []):
        schema, header_row = schema_kind(sheet)
        if schema is None or (schema == "archive" and not include_archives):
            continue
        try:
            cols = header_columns(sheet, header_row)
            targets = _identity_target_columns(sheet, cols, month_targets if schema == "month" else None)
            issues.extend(_protected_identity_issues(sheet, targets))
        except ValueError:
            continue  # The normal schema diagnostics below report bad headers.
    if issues:
        issues.extend(scan_identities(result)["issues"])
        return {"book": result, "requests": [], "issues": issues, "changed": False, "can_apply": False}
    for sheet in result.get("sheets", []):
        schema, header_row = schema_kind(sheet)
        if schema is None or (schema == "archive" and not include_archives):
            continue
        try:
            cols = header_columns(sheet, header_row)
            if "病歷號" not in cols or "姓名" not in cols:
                issues.append({"code": "missing_identity_header", "sheet": sheet["properties"]["title"]})
                continue
            if schema == "archive" and "ArchiveRowType" not in cols:
                issues.append({"code": "missing_archive_type", "sheet": sheet["properties"]["title"],
                               "reason": "封存分頁缺少 ArchiveRowType，請先核對原始封存格式。"})
                continue
            ensure_identity_columns(sheet, requests, header_row=header_row,
                                    target_columns=month_targets if schema == "month" else None)
            valid_block = schema != "month"
            for index, row in enumerate(sheet["rows"][header_row + 1:], header_row + 1):
                kind = _kind(row, cols)
                if schema == "month":
                    parsed = monthly_row(row, cols)
                    kind = parsed["kind"]
                    if kind in {"day", "invalid_header"}:
                        valid_block = kind == "day"
                if kind in {"patient", "day"} and valid_block:
                    col = header_columns(sheet, header_row)[SYNC_ID_HEADER]
                    if col < len(row) and text(row[col]).strip():
                        continue
                    try:
                        set_row_identity(sheet, index, requests, header_row=header_row)
                    except ValueError as exc:
                        issues.append({"code": "identity_write_blocked", "sheet": sheet["properties"]["title"],
                                       "row": index, "reason": str(exc)})
        except ValueError as exc:
            issues.append({"code": "duplicate_header", "sheet": sheet["properties"]["title"], "reason": str(exc)})
    issues.extend(scan_identities(result)["issues"])
    return {"book": result, "requests": requests, "issues": issues, "changed": bool(requests), "can_apply": not issues}
