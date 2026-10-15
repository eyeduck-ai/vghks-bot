"""Pure, journal-friendly two-phase archive planning. Never delete before readback.

The caller durably saves the complete plan and native source snapshots before
sending ``requests``. Each phase needs its own atomic management metadata token.
An uncertain batch is checked, never blindly replayed. ``build_archive_delete``
is the only function which can produce a deleteSheet request.
"""
from __future__ import annotations

import copy
import re
import secrets
import uuid
from datetime import date, datetime

from .settings import today as local_today
from .sheet_identity import (
    ARCHIVE_PATTERN,
    MONTH_PATTERN,
    SYNC_ID_HEADER,
    UID_PATTERN,
    scan_identities,
)
from .sheet_plan import CELL_FIELDS, native_cell
from .sheet_schema import (
    DATE_HEADER,
    EVENT_HEADER,
    canonical_header,
    cell_text,
    monthly_row,
    sheet_headers,
)
from .sheet_statistics import ARCHIVE_CONTRACT_NOTE

SOURCE_MONTH = "ArchiveSourceMonth"
ROW_TYPE = "ArchiveRowType"
ARCHIVE_HEADER_ROW = 5
MONTH_HEADER, DATE_ROW, PATIENT_ROW = "MONTH_HEADER", "DATE_HEADER", "PATIENT"
REQUIRED = (DATE_HEADER, "醫院", "病歷號", "姓名", "TEL", "GA", "側別", "診斷", "Grade", "術式",
            "IOL", "IOL Target", "IOL Final", "Axis", "Plan", "心得", "SN", "CDE", "Energy Time",
            "Energy %", "屈光數據", EVENT_HEADER, SYNC_ID_HEADER)
_DYNAMIC = re.compile(r"\b(?:INDIRECT|OFFSET|IMPORTRANGE)\s*\(", re.I)


def _scalar_constant_formula(formula):
    # Deliberately small allow-list, not a general Sheets formula parser. After
    # removing quoted literal strings / booleans, only numeric scalar arithmetic
    # and comparison operators may remain. Every function, range, array, named
    # reference and positional dependency fails closed before any copy request.
    remaining = re.sub(r'"(?:[^"]|"")*"', "", formula)
    remaining = re.sub(r"\b(?:TRUE|FALSE)\b", "1", remaining, flags=re.I)
    return bool(re.fullmatch(r"=[0-9.\s+*/^%&<>=()\-]*", remaining))


def _literal(value):
    return {"userEnteredValue": {"stringValue": str(value)}}


def _cell(row, index):
    return row[index] if index < len(row) else {}


def _text(row, columns, label):
    return cell_text(_cell(row, columns[label])).strip()


def _clean(value):
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in sorted(value.items()) if item not in (None, "", {}, [])}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _rows(sheet):
    rows = [[_clean(native_cell(cell)) for cell in row] for row in sheet.get("rows", [])]
    for row in rows:
        for cell in row:
            # Sheets may return both the legacy RGB and ColorStyle even when
            # only ColorStyle was written. Style has API precedence; compare
            # that semantic value rather than a derived duplicate RGB field.
            fmt = cell.get("userEnteredFormat", {})
            if "backgroundColorStyle" in fmt:
                fmt.pop("backgroundColor", None)
            text = fmt.get("textFormat", {})
            if "foregroundColorStyle" in text:
                text.pop("foregroundColor", None)
        while row and not row[-1]:
            row.pop()
    while rows and not rows[-1]:
        rows.pop()
    return rows


def _dimensions(items, default):
    result = [{"pixelSize": item.get("pixelSize", default), "hiddenByUser": item.get("hiddenByUser", False)}
              for item in items]
    while result and result[-1] == {"pixelSize": default, "hiddenByUser": False}:
        result.pop()
    return result


def _display_effect(cell):
    effective = cell.get("effectiveFormat", {})
    result = {key: copy.deepcopy(effective[key]) for key in ("backgroundColor", "backgroundColorStyle") if key in effective}
    text = effective.get("textFormat", {})
    chosen = {key: copy.deepcopy(text[key]) for key in ("bold", "italic", "strikethrough", "foregroundColor", "foregroundColorStyle") if key in text}
    if "backgroundColorStyle" in result:
        result.pop("backgroundColor", None)
    if "foregroundColorStyle" in chosen:
        chosen.pop("foregroundColor", None)
    if chosen:
        result["textFormat"] = chosen
    return result


def _archive_cell(sheet, cell):
    result = native_cell(cell)
    if not sheet.get("conditionalFormats"):
        return result
    effect = _display_effect(cell)
    if not effect:
        return result
    fmt = result.setdefault("userEnteredFormat", {})
    text = effect.pop("textFormat", {})
    if "backgroundColorStyle" in effect:
        fmt.pop("backgroundColor", None)
    elif "backgroundColor" in effect:
        fmt.pop("backgroundColorStyle", None)
    fmt.update(effect)
    if text:
        if "foregroundColorStyle" in text:
            fmt.setdefault("textFormat", {}).pop("foregroundColor", None)
        elif "foregroundColor" in text:
            fmt.setdefault("textFormat", {}).pop("foregroundColorStyle", None)
        fmt.setdefault("textFormat", {}).update(text)
    return result


def _projection(sheet):
    grid = sheet["properties"].get("gridProperties", {})
    dimensions = _dimensions(sheet.get("column_widths", []), 100)
    effects = []
    if sheet.get("conditionalFormats"):
        effects = [[_display_effect(cell) for cell in row] for row in sheet.get("rows", [])]
        for row in effects:
            while row and not row[-1]:
                row.pop()
        while effects and not effects[-1]:
            effects.pop()
    return {"properties": {"sheetId": sheet["properties"].get("sheetId"),
                           "title": sheet["properties"].get("title"),
                           "gridProperties": {key: grid.get(key, 0) for key in
                                              ("rowCount", "columnCount", "frozenRowCount", "frozenColumnCount")}},
            "rows": _rows(sheet), "merges": _clean(sheet.get("merges", [])),
            "column_widths": dimensions,
            "row_metadata": _dimensions(sheet.get("row_metadata", []), 21),
            "conditionalFormats": _clean(sheet.get("conditionalFormats", [])), "display_effects": effects,
            "charts": _clean(sheet.get("charts", [])),
            "protectedRanges": _clean(sheet.get("protectedRanges", []))}


def _same(a, b):
    return _projection(a) == _projection(b)


def _sheets(book):
    result = {}
    ids = set()
    for sheet in book.get("sheets", []):
        properties = sheet.get("properties", {})
        title, sid = properties.get("title"), properties.get("sheetId")
        if not title or title in result or not isinstance(sid, int) or sid in ids:
            raise ValueError("刀表分頁名稱／識別碼重複或不完整，不能封存。")
        result[title] = sheet
        ids.add(sid)
    return result


def _width(sheet, header_row=0):
    rows = sheet.get("rows", [])
    last = max((index + 1 for row in rows for index, cell in enumerate(row)
                if cell.get("userEnteredValue") or cell.get("note") or cell.get("textFormatRuns")), default=0)
    return last


def _labels(sheet, header_row=0):
    sheet_headers(sheet, header_row)  # Canonical duplicates fail closed.
    row = sheet.get("rows", [])[header_row]
    width = _width(sheet, header_row)
    labels = [canonical_header(_cell(row, index)) for index in range(width)]
    missing = set(REQUIRED) - set(labels)
    if missing:
        raise ValueError(f"{sheet['properties']['title']} 缺少封存必要欄位：{'、'.join(sorted(missing))}；請先準備刀表。")
    # A standard empty spacer is allowed. Unknown unnamed columns fail closed.
    if sum(not label for label in labels) > 1:
        raise ValueError("未命名人工欄無法可靠重映射，請先命名欄位。")
    for index, label in enumerate(labels):
        if not label and any(_cell(r, index).get("userEnteredValue") or _cell(r, index).get("note")
                             or _cell(r, index).get("textFormatRuns")
                             for r in sheet.get("rows", [])[header_row + 1:]):
            raise ValueError("未命名欄含人工資料，請先命名後再封存。")
    return labels


def _assert_editable(sheet):
    if any(p.get("warningOnly") is not True and p.get("requestingUserCanEdit") is not True
           for p in sheet.get("protectedRanges", [])):
        raise ValueError(f"{sheet['properties']['title']} 有目前帳號無權編輯的嚴格保護；未修改保護設定。")


def _formula_checks(book, months, *, complete=False):
    loaded = {s["properties"]["sheetId"] for s in book.get("sheets", [])}
    source_ids = {s["properties"]["sheetId"] for s in book.get("sheets", []) if s["properties"]["title"] in months}
    if complete and any(p.get("sheetId") not in loaded for p in book.get("all_sheet_properties", [])):
        raise ValueError("刪除來源前必須完整讀取所有分頁公式；尚有分頁未核對。")
    if any(item.get("range", {}).get("sheetId") in source_ids for item in book.get("namedRanges", [])):
        raise ValueError("有命名範圍綁定待封存月份，請先人工處理；來源未刪除。")

    def references_source(value):
        if isinstance(value, dict):
            return value.get("sheetId") in source_ids or any(references_source(v) for v in value.values())
        return isinstance(value, list) and any(references_source(v) for v in value)

    for sheet in book.get("sheets", []):
        title = sheet["properties"]["title"]
        if sheet.get("charts") and (title in months or references_source(sheet["charts"])):
            raise ValueError(f"{title} 含待封存月份的圖表或圖表引用，無法完整保留；請先人工處理。")
        for index, row in enumerate(sheet.get("rows", [])):
            for cell in row:
                formula = cell.get("userEnteredValue", {}).get("formulaValue", "")
                if not formula:
                    continue
                if any(re.search(r"(?:'" + re.escape(month) + r"'|(?<![0-9])" + re.escape(month) + r")\s*!", formula)
                       for month in months):
                    raise ValueError(f"{title} 含引用待刪月份的公式，請先人工處理，來源未刪除。")
                if _DYNAMIC.search(formula):
                    raise ValueError(f"{title} 含動態分頁／位址公式，無法安全驗證刪頁影響。")
                if title in months and index > 0 and not _scalar_constant_formula(formula):
                    raise ValueError(f"{title} 病人資料公式不是純常數運算，可能含函式、位址或陣列；請先人工處理。")


def _identities(book, *, allowed=None):
    allowed = allowed or {}
    scan = scan_identities(book)
    for issue in scan["issues"]:
        if issue["code"] == "duplicate_sync_id":
            locations = sorted((item["sheet"], item["row"]) for item in issue["locations"])
            if locations == allowed.get(issue["sync_id"]):
                continue
        raise ValueError("刀表識別／結構有衝突，請先整理後封存。")
    events = {}
    for sheet in book.get("sheets", []):
        title = sheet["properties"]["title"]
        if not (MONTH_PATTERN.fullmatch(title) or ARCHIVE_PATTERN.fullmatch(title) or title in {"FU", "All"}):
            continue
        columns = sheet_headers(sheet, 5 if ARCHIVE_PATTERN.fullmatch(title) else 0)
        if EVENT_HEADER not in columns:
            continue
        for index, row in enumerate(sheet.get("rows", [])):
            if index <= (5 if ARCHIVE_PATTERN.fullmatch(title) else 0):
                continue
            event = _text(row, columns, EVENT_HEADER)
            if event:
                events.setdefault(event, []).append((title, index))
    for event, locations in events.items():
        if len(locations) > 1 and sorted(locations) != allowed.get("event:" + event):
            raise ValueError("CalendarEventId 重複，請先核對綁定後封存。")


def _source(sheet, month):
    labels = _labels(sheet)
    if {SOURCE_MONTH, ROW_TYPE}.intersection(labels):
        raise ValueError(f"{month} 月表使用封存專用欄名 ArchiveSourceMonth／ArchiveRowType；請先重新命名人工欄。")
    columns = sheet_headers(sheet)
    _assert_editable(sheet)
    frozen = {"rows": [[_archive_cell(sheet, cell) for cell in row] for row in sheet.get("rows", [])]}
    last = max(len(_rows(frozen)), len(_dimensions(sheet.get("row_metadata", []), 21)),
               max((m.get("endRowIndex", 0) for m in sheet.get("merges", [])), default=0))
    source_rows = copy.deepcopy(sheet.get("rows", []))
    while len(source_rows) < last:
        source_rows.append([])
    for index, row in enumerate(source_rows[:last]):
        for cell in row:
            condition = cell.get("dataValidation", {}).get("condition", {})
            validation_type = condition.get("type", "")
            if validation_type in {"CUSTOM_FORMULA", "ONE_OF_RANGE"} or validation_type != "ONE_OF_LIST" and any(
                    str(value.get("userEnteredValue", "")).lstrip().startswith("=") for value in condition.get("values", [])):
                raise ValueError(f"{month}!{index + 1} 資料驗證含公式／儲存格範圍，搬列後可能改變語意；請先人工處理。")
    kinds, active, patients = [], False, 0
    for index, row in enumerate(source_rows[1:last], 1):
        parsed = monthly_row(row, columns)
        kind = parsed["kind"]
        if kind == "invalid_header":
            raise ValueError(f"{month}!{index + 1} 刀日標題不完整，不能封存。")
        if kind == "day":
            if parsed["date"].replace("-", "")[:6] != month:
                raise ValueError(f"{month}!{index + 1} 刀日日期不屬於該月份，不能封存。")
            active = True
        if kind == "patient" and not active:
            raise ValueError(f"{month}!{index + 1} 病人列沒有上方刀日。")
        uid = _text(row, columns, SYNC_ID_HEADER)
        event = _text(row, columns, EVENT_HEADER)
        if kind in {"patient", "day"} and not UID_PATTERN.fullmatch(uid):
            raise ValueError(f"{month}!{index + 1} 缺少有效永久識別碼；請先準備刀表。")
        if kind == "blank":
            if uid or event or any(cell.get("userEnteredValue") for cell in row):
                raise ValueError(f"{month}!{index + 1} 有無法分類的人工內容／識別碼，不能封存。")
        for label in (SYNC_ID_HEADER, EVENT_HEADER):
            if _cell(row, columns[label]).get("userEnteredValue", {}).get("formulaValue"):
                raise ValueError("永久識別欄不能是公式。")
        kinds.append(DATE_ROW if kind == "day" else PATIENT_ROW if kind == "patient" else "")
        patients += kind == "patient"
    if not any(kind == DATE_ROW for kind in kinds):
        raise ValueError(f"{month} 沒有完整刀日區塊，不能封存。")
    snapshot = copy.deepcopy(sheet)
    snapshot["rows"] = source_rows
    return {"sheet": snapshot, "labels": labels, "kinds": kinds, "patients": patients}


def _archive_structure(sheet):
    title = sheet["properties"]["title"]
    if len(sheet.get("rows", [])) <= ARCHIVE_HEADER_ROW:
        raise ValueError(f"{title} 缺少第六列表頭，既有封存不可信。")
    labels = _labels(sheet, ARCHIVE_HEADER_ROW)
    columns = sheet_headers(sheet, ARCHIVE_HEADER_ROW)
    if not {SOURCE_MONTH, ROW_TYPE} <= columns.keys():
        raise ValueError(f"{title} 缺少封存來源／種類欄。")
    months, current, in_day = [], "", False
    for index, row in enumerate(sheet.get("rows", [])[6:], 6):
        month, kind = _text(row, columns, SOURCE_MONTH), _text(row, columns, ROW_TYPE)
        uid, event = _text(row, columns, SYNC_ID_HEADER), _text(row, columns, EVENT_HEADER)
        entered = any(cell.get("userEnteredValue") for cell in row)
        if not entered and not (month or kind or uid or event):
            continue
        if not MONTH_PATTERN.fullmatch(month) or month[:4] != title[-4:]:
            raise ValueError(f"{title}!{index + 1} 封存來源月份無效。")
        if kind == MONTH_HEADER:
            if month in months or months and month < months[-1] or uid or event:
                raise ValueError(f"{title} 月份區段重複、順序錯誤或帶有識別碼。")
            current, in_day = month, False
            months.append(month)
        elif month != current or kind not in {DATE_ROW, PATIENT_ROW}:
            raise ValueError(f"{title}!{index + 1} 不屬於有效封存區段。")
        elif kind == DATE_ROW:
            parsed = monthly_row(row, columns)
            if parsed["kind"] != "day" or parsed["date"].replace("-", "")[:6] != month:
                raise ValueError(f"{title}!{index + 1} 封存刀日無效。")
            in_day = True
        elif not in_day or monthly_row(row, columns)["kind"] != "patient":
            raise ValueError(f"{title}!{index + 1} 封存病人列缺少刀日／身分。")
        if kind in {DATE_ROW, PATIENT_ROW} and not UID_PATTERN.fullmatch(uid):
            raise ValueError(f"{title}!{index + 1} 封存永久識別碼不完整。")
        if event and kind != PATIENT_ROW:
            raise ValueError(f"{title}!{index + 1} 非病人列含日曆識別碼。")
    return labels, columns, months


def _range(sheet, start, end, width):
    return {"sheetId": sheet["properties"]["sheetId"], "startRowIndex": start, "endRowIndex": end,
            "startColumnIndex": 0, "endColumnIndex": width}


def _write_rows(requests, sheet, start, rows, width):
    grid = sheet["properties"]["gridProperties"]
    if start + len(rows) > grid["rowCount"]:
        requests.append({"appendDimension": {"sheetId": sheet["properties"]["sheetId"], "dimension": "ROWS",
                                              "length": start + len(rows) - grid["rowCount"]}})
        grid["rowCount"] = start + len(rows)
    target = sheet.setdefault("rows", [])
    while len(target) < start + len(rows):
        target.append([])
    target[start:start + len(rows)] = copy.deepcopy(rows)
    for offset in range(0, len(rows), 200):
        part = rows[offset:offset + 200]
        requests.append({"updateCells": {"range": _range(sheet, start + offset, start + offset + len(part), width),
                                          "rows": [{"values": [native_cell(cell) for cell in row]} for row in part],
                                          "fields": CELL_FIELDS}})


def _copy_row_dimensions(requests, target, start, source, count):
    dimensions = target.setdefault("row_metadata", [])
    available = source.get("row_metadata", [])
    groups = []
    for index in range(min(count, len(available))):
        properties = {key: value for key, value in available[index].items() if key in {"pixelSize", "hiddenByUser"}}
        if not properties:
            continue
        row = start + index
        while len(dimensions) <= row:
            dimensions.append({})
        dimensions[row] = copy.deepcopy(properties)
        if groups and groups[-1][1] == row and groups[-1][2] == properties:
            groups[-1][1] = row + 1
        else:
            groups.append([row, row + 1, properties])
    for first, end, properties in groups:
        requests.append({"updateDimensionProperties": {"range": {"sheetId": target["properties"]["sheetId"],
            "dimension": "ROWS", "startIndex": first, "endIndex": end}, "properties": properties,
            "fields": ",".join(properties)}})


def _freeze_existing_archive(requests, target):
    """Explicit, backed-up presentation migration, included in the copy preview."""
    if not target.get("conditionalFormats"):
        return False
    rows = [[_archive_cell(target, cell) for cell in row] for row in target.get("rows", [])]
    _write_rows(requests, target, 0, rows, len(_labels(target, ARCHIVE_HEADER_ROW)))
    for index in reversed(range(len(target["conditionalFormats"]))):
        requests.append({"deleteConditionalFormatRule": {"sheetId": target["properties"]["sheetId"], "index": index}})
    target["conditionalFormats"] = []
    return True


def _insert_archive_rows(requests, target, start, count):
    """Model native insertion, preserving sheet identity and existing row content."""
    if target.get("charts") or any(cell.get("userEnteredValue", {}).get("formulaValue") and
            not _scalar_constant_formula(cell["userEnteredValue"]["formulaValue"])
            for row in target.get("rows", []) for cell in row):
        raise ValueError("既有封存含圖表或相依公式，不能安全補入較早月份；請先人工核對。")
    for merge in target.get("merges", []):
        if merge.get("startRowIndex", 0) < start < merge["endRowIndex"]:
            raise ValueError("補入月份的位置跨越既有合併儲存格，請先人工核對。")
    requests.append({"insertDimension": {"range": {"sheetId": target["properties"]["sheetId"],
        "dimension": "ROWS", "startIndex": start, "endIndex": start + count}, "inheritFromBefore": False}})
    requests.append({"updateDimensionProperties": {"range": {"sheetId": target["properties"]["sheetId"],
        "dimension": "ROWS", "startIndex": start, "endIndex": start + count},
        "properties": {"pixelSize": 21, "hiddenByUser": False}, "fields": "pixelSize,hiddenByUser"}})
    target["properties"]["gridProperties"]["rowCount"] += count
    target["rows"][start:start] = [[] for _ in range(count)]
    dimensions = target.setdefault("row_metadata", [])
    while len(dimensions) < start:
        dimensions.append({})
    dimensions[start:start] = [{} for _ in range(count)]
    ranges = list(target.get("merges", []))
    for protection in target.get("protectedRanges", []):
        ranges.extend([protection.get("range", {}), *protection.get("unprotectedRanges", [])])
    for value in ranges:
        if value.get("startRowIndex", 0) >= start:
            value["startRowIndex"] = value.get("startRowIndex", 0) + count
        if value.get("endRowIndex", 0) > start:
            value["endRowIndex"] += count


def build_archive(book, months, today=None, transaction_id=None):
    """Build only the copy phase. The caller must save native backup before POST.

    Earlier months use native row insertion, never replace the annual sheet.
    """
    day = today or local_today()
    if isinstance(day, datetime):
        day = day.date()
    if not isinstance(day, date):
        raise ValueError("封存日期必須是 date。")
    current = day.strftime("%Y%m")
    if not isinstance(months, (list, tuple)) or not months or any(not isinstance(m, str) for m in months):
        raise ValueError("請選擇不重複的過去月份。")
    if len(set(months)) != len(months):
        raise ValueError("請選擇不重複的過去月份。")
    months = sorted(months)
    if any(not isinstance(month, str) or not MONTH_PATTERN.fullmatch(month) or month >= current
           or not 2000 <= int(month[:4]) <= 2099 for month in months):
        raise ValueError("只能封存已結束的 2000–2099 YYYYMM 月份。")
    original = _sheets(book)
    if any(month not in original for month in months):
        raise ValueError("來源月份已不存在；請核對未完成封存，不可另建重複封存。")
    _identities(book)
    _formula_checks(book, months)
    for title, sheet in original.items():
        if ARCHIVE_PATTERN.fullmatch(title):
            _archive_structure(sheet)
    result, requests, sources, targets = copy.deepcopy(book), [], {}, {}
    expected = _sheets(result)
    summaries = []
    for month in months:
        source = _source(original[month], month)
        sources[month] = copy.deepcopy(original[month])
        title = "刀表封存_" + month[:4]
        target = expected.get(title)
        labels = source["labels"]
        migrated = _freeze_existing_archive(requests, target) if target is not None else False
        if target is None:
            used = {s["properties"]["sheetId"] for s in result["sheets"]}
            used.update(p["sheetId"] for p in result.get("all_sheet_properties", []))
            sid = secrets.randbelow(2**30) + 1
            while sid in used:
                sid = secrets.randbelow(2**30) + 1
            header = [_archive_cell(source["sheet"], _cell(source["sheet"]["rows"][0], index)) for index in range(len(labels))]
            for index, label in enumerate(labels):
                header[index]["userEnteredValue"] = {"stringValue": label}
                if label != cell_text(_cell(source["sheet"]["rows"][0], index)):
                    header[index].pop("textFormatRuns", None)
            header.extend([_literal(SOURCE_MONTH), _literal(ROW_TYPE)])
            target = {"properties": {"sheetId": sid, "title": title, "gridProperties": {
                "rowCount": 1000, "columnCount": len(header), "frozenRowCount": 6}},
                "rows": [[_literal(title)], [], [], [], [], header], "merges": [], "column_widths": []}
            expected[title] = target
            result["sheets"].append(target)
            requests.append({"addSheet": {"properties": copy.deepcopy(target["properties"])}})
            _write_rows(requests, target, 0, target["rows"], len(header))
            _copy_row_dimensions(requests, target, 5, source["sheet"], 1)
        target_labels, cols, archived = _archive_structure(target)
        _assert_editable(target)
        if month in archived:
            raise ValueError(f"{month} 已封存；不覆寫既有封存。")
        # Header-name remap is safe for named fields. An unnamed spacer must have
        # the same clinical position; never infer where an unnamed manual column belongs.
        base = [label for label in target_labels if label not in {SOURCE_MONTH, ROW_TYPE}]
        if [label for label in base if not label] != [label for label in labels if not label]:
            raise ValueError("來源與封存的未命名欄結構不同，不能安全重映射。")
        unknown = [label for label in labels if label and label not in cols]
        for label in unknown:
            col = len(target_labels)
            target_labels.append(label)
            cols[label] = col
            while len(target["rows"][5]) <= col:
                target["rows"][5].append({})
            target["rows"][5][col] = _archive_cell(source["sheet"], source["sheet"]["rows"][0][labels.index(label)])
            target["rows"][5][col]["userEnteredValue"] = {"stringValue": label}
            target["rows"][5][col].pop("textFormatRuns", None)
            extra = max(0, col + 1 - target["properties"]["gridProperties"]["columnCount"])
            if extra:
                requests.append({"appendDimension": {"sheetId": target["properties"]["sheetId"], "dimension": "COLUMNS", "length": extra}})
                target["properties"]["gridProperties"]["columnCount"] += extra
            requests.append({"updateCells": {"range": {**_range(target, 5, 6, col + 1), "startColumnIndex": col},
                "rows": [{"values": [native_cell(target["rows"][5][col])]}], "fields": CELL_FIELDS}})
        width = len(target_labels)
        source_blanks = [index for index, label in enumerate(labels) if not label]
        target_blanks = [index for index, label in enumerate(target_labels) if not label]
        mapping = {index: cols[label] if label else target_blanks[source_blanks.index(index)] for index, label in enumerate(labels)}
        if source_blanks and [label for label in labels[:source_blanks[0]] if label not in {EVENT_HEADER, SYNC_ID_HEADER}] != [
                label for label in target_labels[:target_blanks[0]] if label not in {EVENT_HEADER, SYNC_ID_HEADER, SOURCE_MONTH, ROW_TYPE}]:
            raise ValueError("來源與封存的空白分隔欄位置不一致。")
        start = next((index for index, row in enumerate(target["rows"][6:], 6)
                      if _text(row, cols, ROW_TYPE) == MONTH_HEADER and _text(row, cols, SOURCE_MONTH) > month),
                     max(6, len(_rows(target))))
        rows = []
        separator = [{} for _ in range(width)]
        header_formulas = 0
        for index, cell in enumerate(source["sheet"]["rows"][0][:len(labels)]):
            copied = _archive_cell(source["sheet"], cell)
            formula = copied.get("userEnteredValue", {}).get("formulaValue")
            copied.pop("textFormatRuns", None)
            if not formula:
                copied.pop("userEnteredValue", None)
            elif labels[index] in {DATE_HEADER, "病歷號", "姓名", EVENT_HEADER, SYNC_ID_HEADER}:
                raise ValueError("來源識別／日期表頭含公式，無法安全保存封存標題。")
            else:
                header_formulas += 1
                value = cell.get("effectiveValue", {})
                if not value or "errorValue" in value:
                    value = {"stringValue": cell.get("formattedValue", "")}
                copied["userEnteredValue"] = copy.deepcopy(value)
                copied["note"] = (copied.get("note", "") + "\n封存時固定摘要；原始表頭公式：\n" + formula).lstrip("\n")
            separator[mapping[index]] = copied
        separator[cols[DATE_HEADER]]["userEnteredValue"] = {"stringValue": month}
        separator[cols[DATE_HEADER]]["note"] = (separator[cols[DATE_HEADER]].get("note", "") + "\n" + ARCHIVE_CONTRACT_NOTE).strip()
        separator[cols[SOURCE_MONTH]], separator[cols[ROW_TYPE]] = _literal(month), _literal(MONTH_HEADER)
        rows.append(separator)
        for index, kind in enumerate(source["kinds"], 1):
            row = [{} for _ in range(width)]
            for col, cell in enumerate(source["sheet"]["rows"][index]):
                if col < len(labels):
                    row[mapping[col]] = _archive_cell(source["sheet"], cell)
            if kind:
                row[cols[SOURCE_MONTH]], row[cols[ROW_TYPE]] = _literal(month), _literal(kind)
            rows.append(row)
        backfill = start < len(_rows(target))
        if backfill:
            _insert_archive_rows(requests, target, start, len(rows))
        _write_rows(requests, target, start, rows, width)
        _copy_row_dimensions(requests, target, start, source["sheet"], len(rows))
        for merge in source["sheet"].get("merges", []):
            first, end = merge.get("startColumnIndex", 0), merge["endColumnIndex"]
            if first < 0 or end > len(labels) or first >= end:
                raise ValueError("來源合併儲存格超出可辨識欄位，不能安全封存。")
            columns = [mapping[index] for index in range(first, end)]
            if merge.get("startRowIndex", 0) == 0 or columns != list(range(columns[0], columns[0] + len(columns))):
                raise ValueError("來源合併儲存格跨越表頭／重映射欄，無法安全封存。")
            for row_index in range(merge.get("startRowIndex", 0), merge["endRowIndex"]):
                for col_index in range(first, end):
                    if (row_index, col_index) != (merge.get("startRowIndex", 0), first) and _cell(
                            source["sheet"]["rows"][row_index], col_index).get("userEnteredValue"):
                        raise ValueError("來源合併範圍內有多個內容，拒絕可能遺失內容的合併。")
            mapped = {**copy.deepcopy(merge), "sheetId": target["properties"]["sheetId"],
                      "startRowIndex": start + merge.get("startRowIndex", 0), "endRowIndex": start + merge["endRowIndex"],
                      "startColumnIndex": columns[0], "endColumnIndex": columns[-1] + 1}
            target.setdefault("merges", []).append(mapped)
            requests.append({"mergeCells": {"range": mapped, "mergeType": "MERGE_ALL"}})
        metadata = target.setdefault("column_widths", [])
        while len(metadata) < width:
            metadata.append({})
        for index, properties in enumerate(source["sheet"].get("column_widths", [])[:len(labels)]):
            col = mapping[index]
            wanted = {k: v for k, v in properties.items() if k in {"pixelSize", "hiddenByUser"}}
            compared = {k: v for k, v in metadata[col].items() if k in {"pixelSize", "hiddenByUser"}}
            if labels[index] in {EVENT_HEADER, SYNC_ID_HEADER}:
                compared.pop("hiddenByUser", None)
                wanted.pop("hiddenByUser", None)
            if compared and wanted and any(compared.get(k, 100 if k == "pixelSize" else False) != v for k, v in wanted.items()):
                continue  # One annual column has one width; preserve the existing owner's choice.
            if wanted and not metadata[col]:
                metadata[col] = copy.deepcopy(wanted)
                requests.append({"updateDimensionProperties": {"range": {"sheetId": target["properties"]["sheetId"],
                    "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1}, "properties": wanted,
                    "fields": ",".join(wanted)}})
        for label in (EVENT_HEADER, SYNC_ID_HEADER, SOURCE_MONTH, ROW_TYPE):
            col = cols[label]
            metadata[col]["hiddenByUser"] = True
            requests.append({"updateDimensionProperties": {"range": {"sheetId": target["properties"]["sheetId"],
                "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
                "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})
        targets[title] = copy.deepcopy(target)
        summaries.append({"month": month, "target": title, "patients": source["patients"],
                          "blocks": source["kinds"].count(DATE_ROW), "copied_rows": len(rows),
                          "header_formula_snapshots": header_formulas,
                          "backfill": backfill, "migrated_existing_format": migrated,
                          "conditional_format_snapshot": bool(source["sheet"].get("conditionalFormats"))})
    return {"transaction_id": transaction_id or uuid.uuid4().hex, "phase": "copy", "requests": requests,
            "book": result, "expected": result, "sources": sources, "targets": targets,
            "original_targets": {title: copy.deepcopy(original.get(title)) for title in targets},
            "summary": {"months": summaries, "patients": sum(item["patients"] for item in summaries),
                        "phase": "copy", "requires_local_backup": True,
                        "format_policy": "封存固定目前顯示樣式，保留列高與人工隱藏列；原條件格式規則完整保存在本機備份，不搬動活的規則公式。",
                        "formula_policy": "月份表頭摘要固定為封存時結果；原公式保存在本機備份與封存註記。病人列只保留無函式／位址／陣列的純常數運算公式。"}}


def _allowed_duplicates(plan):
    allowed = {}
    for book in ({"sheets": list(plan["sources"].values())}, {"sheets": list(plan["targets"].values())}):
        for sheet in book["sheets"]:
            header = 5 if ARCHIVE_PATTERN.fullmatch(sheet["properties"]["title"]) else 0
            cols = sheet_headers(sheet, header)
            for index, row in enumerate(sheet.get("rows", [])[header + 1:], header + 1):
                for label, prefix in ((SYNC_ID_HEADER, ""), (EVENT_HEADER, "event:")):
                    value = _text(row, cols, label)
                    if value:
                        allowed.setdefault(prefix + value, []).append((sheet["properties"]["title"], index))
    return {key: sorted(locations) for key, locations in allowed.items() if len(locations) == 2}


def verify_archive(plan, fresh_book):
    """Return verified/not_applied/conflict. No exception or writes on uncertain check."""
    try:
        sheets = _sheets(fresh_book)
        if any(month not in sheets or not _same(source, sheets[month]) for month, source in plan["sources"].items()):
            return "conflict"
        unchanged = all((title not in sheets if old is None else title in sheets and _same(old, sheets[title]))
                        for title, old in plan["original_targets"].items())
        if unchanged:
            return "not_applied"
        for title, target in plan["targets"].items():
            if title not in sheets or not _same(target, sheets[title]):
                return "conflict"
            _archive_structure(sheets[title])
        _identities(fresh_book, allowed=_allowed_duplicates(plan))
        return "verified"
    except (ValueError, KeyError, IndexError, TypeError):
        return "conflict"


def build_archive_delete(plan, fresh_book):
    """Only verified copy readback can authorize a separate source deletion phase."""
    if verify_archive(plan, fresh_book) != "verified":
        raise ValueError("封存內容或來源尚未完整回讀核對；禁止刪除來源月份。")
    _formula_checks(fresh_book, list(plan["sources"]), complete=True)
    expected = copy.deepcopy(fresh_book)
    source_ids = {source["properties"]["sheetId"] for source in plan["sources"].values()}
    expected["sheets"] = [sheet for sheet in expected["sheets"] if sheet["properties"]["sheetId"] not in source_ids]
    if "all_sheet_properties" in expected:
        expected["all_sheet_properties"] = [p for p in expected["all_sheet_properties"] if p["sheetId"] not in source_ids]
    return {**copy.deepcopy(plan), "phase": "delete", "expected": expected, "book": expected,
            "requests": [{"deleteSheet": {"sheetId": sid}} for sid in sorted(source_ids)],
            "summary": {**copy.deepcopy(plan["summary"]), "phase": "delete"}}


def verify_archive_complete(plan, fresh_book):
    try:
        sheets = _sheets(fresh_book)
        if any(month in sheets for month in plan["sources"]):
            return "not_applied" if verify_archive(plan, fresh_book) == "verified" else "conflict"
        source_ids = {s["properties"]["sheetId"] for s in plan["sources"].values()}
        if any(s["properties"]["sheetId"] in source_ids for s in sheets.values()) or any(
                p.get("sheetId") in source_ids for p in fresh_book.get("all_sheet_properties", [])):
            return "conflict"
        for title, target in plan["targets"].items():
            if title not in sheets or not _same(target, sheets[title]):
                return "conflict"
            _archive_structure(sheets[title])
        _identities(fresh_book)
        return "verified"
    except (ValueError, KeyError, IndexError, TypeError):
        return "conflict"
