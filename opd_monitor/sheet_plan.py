"""Pure Google Sheets change planner for month tabs containing 刀日 blocks."""
from __future__ import annotations

import copy
import re
import secrets
import uuid
from datetime import date, timedelta

from .analysis_numeric import iso_day, norm
from .analysis_store import digest
from .google_sheets import column_name
from .settings import timestamp
from .surgery_candidates import FIELD_NAMES

HEADERS = {"date": "日期／報到時間", "hospital": "醫院", "mrn": "病歷號", "name": "姓名",
           "tel": "TEL", "ga": "GA", "side": "側別", "diagnosis": "診斷", "grade": "Grade",
           "procedure": "術式", "iol": "IOL", "target": "IOL Target", "plan": "Plan"}
CELL_FIELDS = "userEnteredValue,userEnteredFormat,dataValidation,note,textFormatRuns"


def cell_text(cell):
    value = cell.get("userEnteredValue", {})
    if "stringValue" in value:
        return value["stringValue"]
    if "numberValue" in value:
        n = value["numberValue"]
        return str(int(n)) if float(n).is_integer() else str(n)
    if "boolValue" in value:
        return str(value["boolValue"])
    return str(cell.get("formattedValue", ""))


def patient_key(value):
    text = norm(value)
    return str(int(text)) if text.isdigit() else text


def typed_day(cell):
    value = cell.get("userEnteredValue", {})
    if "numberValue" in value:
        try:
            return (date(1899, 12, 30) + timedelta(days=int(value["numberValue"]))).isoformat()
        except (OverflowError, ValueError):
            return ""
    return iso_day(cell_text(cell))


def native_cell(cell):
    return {k: copy.deepcopy(v) for k, v in cell.items() if k in CELL_FIELDS.split(",")}


def fingerprint(book):
    value = copy.deepcopy(book)
    for sheet in value["sheets"]:
        sheet["rows"] = [[native_cell(c) for c in r] for r in sheet.get("rows", [])]
    return digest(value)


def columns(sheet):
    result = {}
    for index, cell in enumerate((sheet.get("rows") or [[]])[0]):
        text = norm(cell_text(cell))
        for field, label in HEADERS.items():
            if text == norm(label) or (field == "diagnosis" and text.startswith("診斷")):
                result[field] = index
        # The diagnosis summary header is a formula; its live formatted value
        # may be absent in a test/offline snapshot.
        if "診斷" in cell.get("userEnteredValue", {}).get("formulaValue", ""):
            result.setdefault("diagnosis", index)
    missing = set(HEADERS) - set(result)
    if missing:
        raise ValueError(f"{sheet['properties']['title']} 缺少欄位：{'、'.join(HEADERS[k] for k in sorted(missing))}")
    return result


def cell_at(row, index):
    return row[index] if index < len(row) else {}


def entries(sheet):
    cols, context = columns(sheet), None
    result = []
    for index, row in enumerate(sheet["rows"][1:], 1):
        mrn = cell_text(cell_at(row, cols["mrn"]))
        if norm(mrn) == "◆ 刀日":
            context = {"date": typed_day(cell_at(row, cols["date"])),
                       "hospital": cell_text(cell_at(row, cols["hospital"])), "index": index}
            result.append({"kind": "day", "row": row, "index": index, **context})
        elif context and mrn.strip():
            item = {k: cell_text(cell_at(row, col)) for k, col in cols.items()}
            item.update(date=context["date"], hospital=item["hospital"] or context["hospital"])
            result.append({"kind": "patient", "row": row, "index": index, "fields": item,
                           "day_index": context["index"]})
    return result


def row_location(sheet, row):
    return next(i for i, value in enumerate(sheet["rows"]) if value is row)


def row_label(sheet, index):
    return f"{sheet['properties']['title']}!{index + 1}"


def occupied(row):
    return any(c.get("userEnteredValue") or c.get("note") or c.get("textFormatRuns") for c in row)


def range_(sheet, row, start=0, end=None):
    return {"sheetId": sheet["properties"]["sheetId"], "startRowIndex": row, "endRowIndex": row + 1,
            "startColumnIndex": start, "endColumnIndex": end or sheet["properties"]["gridProperties"]["columnCount"]}


def matches(book, fields):
    exact, related = [], []
    for sheet in book["sheets"]:
        for entry in entries(sheet):
            if entry["kind"] != "patient":
                continue
            target = entry["fields"]
            if target["hospital"] != "高榮":
                continue
            same_mrn = patient_key(target["mrn"]) == patient_key(fields["mrn"])
            # A decorated/cancelled MRN is a possible match, never a new-row shortcut.
            decorated = bool(re.search(r"(?<!\d)0*" + re.escape(patient_key(fields["mrn"])) + r"(?!\d)", target["mrn"])) if fields["mrn"].isdigit() else False
            if not (same_mrn or decorated):
                continue
            item = {"sheet": sheet, "entry": entry, "ref": row_label(sheet, entry["index"])}
            if (same_mrn and target["date"] == fields["date"] and target["side"] == fields["side"]
                    and norm(target["procedure"]).casefold() == norm(fields["procedure"]).casefold()):
                exact.append(item)
            else:
                related.append(item)
    return exact, related


class Planner:
    def __init__(self, book, spreadsheet_id, proposals):
        self.original = book
        self.book = copy.deepcopy(book)
        self.sheet_id = spreadsheet_id
        self.proposals = proposals
        self.requests = []
        self.items = []
        self.watched = []
        self.structures = []
        self.id = uuid.uuid4().hex
        self.initial_refs = {}
        for sheet in self.book["sheets"]:
            columns(sheet)
            for entry in entries(sheet):
                if entry["kind"] == "patient":
                    self.initial_refs[row_label(sheet, entry["index"])] = (sheet, entry["row"])
        if not self.book["sheets"]:
            raise ValueError("刀表沒有可辨識的 YYYYMM 月分頁範本。")

    def capacity(self, sheet, index):
        count = sheet["properties"]["gridProperties"]["rowCount"]
        if index >= count:
            extra = index - count + 20
            self.requests.append({"appendDimension": {"sheetId": sheet["properties"]["sheetId"],
                                                       "dimension": "ROWS", "length": extra}})
            sheet["properties"]["gridProperties"]["rowCount"] += extra
        while len(sheet["rows"]) <= index:
            sheet["rows"].append([])

    def insert(self, sheet, index, cells):
        if index >= sheet["properties"]["gridProperties"]["rowCount"]:
            self.capacity(sheet, index)
        self.requests.append({"insertDimension": {"range": {"sheetId": sheet["properties"]["sheetId"],
            "dimension": "ROWS", "startIndex": index, "endIndex": index + 1}, "inheritFromBefore": index > 0}})
        sheet["properties"]["gridProperties"]["rowCount"] += 1
        while len(sheet["rows"]) < index:
            sheet["rows"].append([])
        sheet["rows"].insert(index, cells)
        for merge in sheet.get("merges", []):
            if merge.get("startRowIndex", 0) >= index:
                merge["startRowIndex"] = merge.get("startRowIndex", 0) + 1
            if merge.get("endRowIndex", 0) > index:
                merge["endRowIndex"] += 1
        return cells

    def new_month(self, month):
        template = max(self.book["sheets"], key=lambda s: s["properties"]["title"])
        used = {s["properties"]["sheetId"] for s in self.book["sheets"]}
        sid = secrets.randbelow(2**30) + 1
        while sid in used:
            sid = secrets.randbelow(2**30) + 1
        width = template["properties"]["gridProperties"]["columnCount"]
        sheet = {"properties": {"sheetId": sid, "title": month,
                               "gridProperties": {"rowCount": 1000, "columnCount": width, "frozenRowCount": 1}},
                 "rows": [[native_cell(c) for c in template["rows"][0]]], "merges": []}
        self.requests.append({"addSheet": {"properties": copy.deepcopy(sheet["properties"])}})
        widths = template.get("column_widths", [])
        sheet["column_widths"] = copy.deepcopy(widths)
        for col, dimensions in enumerate(widths):
            if dimensions.get("pixelSize"):
                self.requests.append({"updateDimensionProperties": {
                    "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
                    "properties": {"pixelSize": dimensions["pixelSize"]}, "fields": "pixelSize"}})
        self.requests.append({"updateCells": {"range": range_(sheet, 0),
                                             "rows": [{"values": sheet["rows"][0]}], "fields": CELL_FIELDS}})
        self.book["sheets"].append(sheet)
        self.structures.append("建立月份 " + month)
        return sheet

    def day_block(self, when):
        month = when.replace("-", "")[:6]
        sheet = next((s for s in self.book["sheets"] if s["properties"]["title"] == month), None)
        if sheet is None:
            sheet = self.new_month(month)
        groups = [e for e in entries(sheet) if e["kind"] == "day"]
        found = [e for e in groups if e["date"] == when and e["hospital"] == "高榮"]
        if len(found) > 1:
            raise ValueError(f"{month} 有重複高榮刀日 {when}，請先整理刀表。")
        if found:
            return sheet, found[0]["row"]
        next_group = next((e for e in groups if e["date"] and e["date"] > when), None)
        index = next_group["index"] if next_group else max(
            (i for i, r in enumerate(sheet["rows"]) if occupied(r)), default=0) + 1
        template = next((e["row"] for s in self.book["sheets"] for e in entries(s)
                         if e["kind"] == "day" and e["hospital"] == "高榮"), [])
        width, cols = sheet["properties"]["gridProperties"]["columnCount"], columns(sheet)
        cells = [{k: copy.deepcopy(v) for k, v in cell_at(template, i).items()
                  if k in {"userEnteredFormat", "dataValidation"}} for i in range(width)]
        cells[cols["date"]]["userEnteredValue"] = {"numberValue": (date.fromisoformat(when) - date(1899, 12, 30)).days}
        cells[cols["date"]].setdefault("userEnteredFormat", {})["numberFormat"] = {"type": "DATE", "pattern": "yyyy/m/d ddd"}
        cells[cols["hospital"]]["userEnteredValue"] = {"stringValue": "高榮"}
        cells[cols["mrn"]]["userEnteredValue"] = {"stringValue": "◆ 刀日"}
        self.insert(sheet, index, cells)
        self.requests.append({"updateCells": {"range": range_(sheet, index),
            "rows": [{"values": cells}], "fields": CELL_FIELDS}})
        self.structures.append(f"{month} 新增高榮刀日 {when}")
        return sheet, cells

    def patient_slot(self, sheet, header):
        start = row_location(sheet, header) + 1
        end = next((e["index"] for e in entries(sheet) if e["kind"] == "day" and e["index"] >= start), len(sheet["rows"]))
        for index in range(start, end):
            if not occupied(sheet["rows"][index]) and not any(
                m.get("startRowIndex", 0) <= index < m.get("endRowIndex", 0) for m in sheet.get("merges", [])):
                return sheet["rows"][index], index
        index = end
        row = self.insert(sheet, index, [])
        return row, index

    def initialize_patient(self, sheet, row):
        template = next((e["row"] for s in self.book["sheets"] for e in entries(s)
                         if e["kind"] == "patient" and e["fields"]["hospital"] == "高榮"), [])
        width = sheet["properties"]["gridProperties"]["columnCount"]
        cells = [{k: copy.deepcopy(v) for k, v in cell_at(template, i).items()
                  if k in {"userEnteredFormat", "dataValidation"}} for i in range(width)]
        row[:] = cells
        self.requests.append({"updateCells": {"range": range_(sheet, row_location(sheet, row)),
            "rows": [{"values": cells}], "fields": "userEnteredFormat,dataValidation"}})

    def move(self, source_sheet, source_row, target_sheet, header):
        if columns(source_sheet) != columns(target_sheet):
            raise ValueError("來源與目的月份欄位位置不同，請先對齊月表欄位後重新預覽。")
        source_headers, target_headers = source_sheet["rows"][0], target_sheet["rows"][0]
        managed = set(columns(source_sheet).values())
        for col in range(max(len(source_headers), len(target_headers))):
            if col not in managed and cell_text(cell_at(source_headers, col)) != cell_text(cell_at(target_headers, col)):
                raise ValueError("來源與目的月份的人工欄位不同，請先對齊欄位後再搬移。")
        destination, _ = self.patient_slot(target_sheet, header)
        source_index, target_index = row_location(source_sheet, source_row), row_location(target_sheet, destination)
        width = source_sheet["properties"]["gridProperties"]["columnCount"]
        if width > target_sheet["properties"]["gridProperties"]["columnCount"]:
            extra = width - target_sheet["properties"]["gridProperties"]["columnCount"]
            self.requests.append({"appendDimension": {"sheetId": target_sheet["properties"]["sheetId"],
                                                      "dimension": "COLUMNS", "length": extra}})
            target_sheet["properties"]["gridProperties"]["columnCount"] = width
        self.requests.append({"cutPaste": {"source": range_(source_sheet, source_index, end=width),
            "destination": {"sheetId": target_sheet["properties"]["sheetId"], "rowIndex": target_index, "columnIndex": 0},
            "pasteType": "PASTE_NORMAL"}})
        self.requests.append({"deleteDimension": {"range": {"sheetId": source_sheet["properties"]["sheetId"],
            "dimension": "ROWS", "startIndex": source_index, "endIndex": source_index + 1}}})
        target_sheet["rows"][target_index] = source_row
        source_sheet["rows"].pop(source_index)
        for merge in source_sheet.get("merges", []):
            if merge.get("startRowIndex", 0) > source_index:
                merge["startRowIndex"] -= 1
            if merge.get("endRowIndex", 0) > source_index:
                merge["endRowIndex"] -= 1
        source_sheet["properties"]["gridProperties"]["rowCount"] -= 1
        return source_row

    def apply_proposal(self, proposal):
        fields = proposal["fields"]
        exact, related = matches(self.book, fields)
        chosen = proposal.get("target", "")
        target = self.initial_refs.get(chosen) if chosen else None
        if chosen and not target:
            raise ValueError("所選刀表位置已變更，請重新預覽。")
        if target:
            sheet, row = target
            entry = next((e for e in entries(sheet) if e["row"] is row and e["kind"] == "patient"), None)
            if not entry or entry["fields"]["hospital"] != "高榮" or patient_key(entry["fields"]["mrn"]) != patient_key(fields["mrn"]):
                raise ValueError("指定列不是此病人的高榮排程。")
            if any(v["entry"]["row"] is not row for v in exact):
                raise ValueError("另有相同刀日、眼別與術式的病人列，請先整理重複排程。")
        elif len(exact) == 1:
            sheet, row = exact[0]["sheet"], exact[0]["entry"]["row"]
        elif exact or (related and not proposal.get("new_event")):
            options = [{"ref": v["ref"], "fields": v["entry"]["fields"]} for v in exact + related]
            return {"id": proposal["id"], "fields": fields, "status": "conflict",
                    "reason": "請選擇既有手術，或確認為另一次手術。", "options": options}
        else:
            sheet = row = None
        changes, action = [], "update"
        if row is not None:
            if any(watched_row is row for _, watched_row, _ in self.watched):
                raise ValueError("兩筆手術不可套用同一病人列，請分別指定手術位置。")
            index = row_location(sheet, row)
            if any(m.get("startRowIndex", 0) <= index < m.get("endRowIndex", 0)
                   for m in sheet.get("merges", [])):
                raise ValueError("病人列包含合併儲存格，請先在刀表整理後再預覽。")
        if row is None:
            sheet, header = self.day_block(fields["date"])
            row, _ = self.patient_slot(sheet, header)
            self.initialize_patient(sheet, row)
            action = "insert"
        else:
            entry = next(e for e in entries(sheet) if e["row"] is row and e["kind"] == "patient")
            if entry["fields"]["date"] != fields["date"]:
                old_position = row_label(sheet, row_location(sheet, row))
                destination, header = self.day_block(fields["date"])
                row = self.move(sheet, row, destination, header)
                sheet, action = destination, "move"
                changes.append({"field": "date", "label": "刀日／搬移", "before": entry["fields"]["date"],
                                "after": fields["date"], "from": old_position})
        cols = columns(sheet)
        selected = proposal.get("selected_fields", list(FIELD_NAMES))
        if action == "insert":
            selected = list(dict.fromkeys([*selected, "mrn", "side", "procedure"]))
        for field in selected:
            if field == "date":
                continue  # patient A contains arrival time, never the operation time.
            value = fields.get(field, "")
            if not value and field not in proposal.get("clear_fields", []):
                continue
            col = cols[field]
            while len(row) <= col:
                row.append({})
            cell = row[col]
            if cell.get("userEnteredValue", {}).get("formulaValue"):
                raise ValueError(f"{row_label(sheet, row_location(sheet, row))} 的 {HEADERS[field]} 是公式，請排除此欄。")
            validation = cell.get("dataValidation", {})
            if validation.get("strict") and validation.get("condition", {}).get("type") == "ONE_OF_LIST":
                allowed = [v.get("userEnteredValue") for v in validation["condition"].get("values", [])]
                if value and value not in allowed:
                    raise ValueError(f"{HEADERS[field]} 需為：{'、'.join(allowed)}")
            before = cell_text(cell)
            if value == before:
                continue
            if field == "mrn" and before and patient_key(before) == patient_key(value):
                # Still convert new writes to text; existing MRNs remain untouched.
                continue
            cell["userEnteredValue"] = {"stringValue": value}
            cell.pop("textFormatRuns", None)
            cell.pop("formattedValue", None)
            self.requests.append({"updateCells": {"range": range_(sheet, row_location(sheet, row), col, col + 1),
                "rows": [{"values": [{"userEnteredValue": {"stringValue": value}}]}],
                "fields": "userEnteredValue"}})
            changes.append({"field": field, "label": FIELD_NAMES[field], "before": before, "after": value, "column": col})
        item = {"id": proposal["id"], "fields": fields, "status": "ready", "action": action, "changes": changes}
        self.watched.append((sheet, row, item))
        return item

    def build(self):
        claimed = set()
        for proposal in self.proposals:
            identity = tuple(proposal["fields"].get(k) for k in ("mrn", "date", "side", "procedure"))
            if identity in claimed:
                raise ValueError("同一批更新包含重複的手術事件，請合併後再預覽。")
            claimed.add(identity)
            self.items.append(self.apply_proposal(proposal))
        expectations = []
        for sheet, row, item in self.watched:
            index = row_location(sheet, row)
            item["position"] = row_label(sheet, index)
            for change in item["changes"]:
                if "column" in change:
                    change["cell"] = f"{sheet['properties']['title']}!{column_name(change['column'])}{index + 1}"
            expectations.append({"sheet": sheet["properties"]["title"], "row": index,
                                 "cells": [native_cell(c) for c in row]})
        meta_id = secrets.randbelow(2**30) + 1
        existing_ids = {m.get("metadataId") for m in self.book.get("developerMetadata", [])}
        while meta_id in existing_ids:
            meta_id = secrets.randbelow(2**30) + 1
        self.requests.append({"createDeveloperMetadata": {"developerMetadata": {
            "metadataId": meta_id, "metadataKey": "vghks.opd.update", "metadataValue": self.id,
            "location": {"spreadsheet": True}, "visibility": "DOCUMENT"}}})
        return {"id": self.id, "created_at": timestamp(), "status": "preview",
                "spreadsheet_id": self.sheet_id, "baseline": fingerprint(self.original),
                "items": self.items, "structures": self.structures, "requests": self.requests,
                "expectations": expectations, "mrns": sorted({p["fields"]["mrn"] for p in self.proposals}),
                "source_records": sorted({r for p in self.proposals for r in p.get("source_records", [])}),
                "metadata_id": meta_id,
                "can_apply": bool(self.watched) and all(i["status"] == "ready" for i in self.items)}


def verify(book, preview):
    committed = any(m.get("metadataValue") == preview["id"] and m.get("metadataKey") == "vghks.opd.update"
                    for m in book.get("developerMetadata", []))
    if not committed:
        return "not_found"
    for expected in preview["expectations"]:
        sheet = next((s for s in book["sheets"] if s["properties"]["title"] == expected["sheet"]), None)
        if not sheet or len(sheet["rows"]) <= expected["row"]:
            return "changed"
        actual = sheet["rows"][expected["row"]]
        for index, cell in enumerate(expected["cells"]):
            # Verify values and manual content. Google may canonicalize styles.
            for key in ("userEnteredValue", "note", "textFormatRuns"):
                if cell.get(key) != cell_at(actual, index).get(key):
                    return "changed"
    return "verified"
