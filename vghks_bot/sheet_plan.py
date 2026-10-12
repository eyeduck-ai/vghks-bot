"""Pure Google Sheets change planner for month tabs containing 刀日 blocks."""
from __future__ import annotations

import copy
import re
import secrets
import uuid
from datetime import date

from .analysis_numeric import norm
from .analysis_store import digest
from .google_sheets import column_name
from .settings import timestamp
from .sheet_identity import (
    EVENT_ID_HEADER,
    MONTH_PATTERN,
    SYNC_ID_HEADER,
    UID_PATTERN,
    ensure_identity_columns,
    header_columns,
    scan_identities,
    set_row_identity,
)
from .sheet_schema import HOSPITALS, canonical_header, cell_text, full_date, monthly_row
from .surgery_candidates import FIELD_NAMES

HEADERS = {"date": "日期／報到時間", "hospital": "醫院", "mrn": "病歷號", "name": "姓名",
           "tel": "TEL", "ga": "GA", "side": "側別", "diagnosis": "診斷", "grade": "Grade",
           "procedure": "術式", "iol": "IOL", "target": "IOL Target", "plan": "Plan"}
CELL_FIELDS = "userEnteredValue,userEnteredFormat,dataValidation,note,textFormatRuns"


def patient_key(value):
    text = norm(value)
    return str(int(text)) if text.isdigit() else text


def typed_day(cell):
    return full_date(cell)


def native_cell(cell):
    return {k: copy.deepcopy(v) for k, v in cell.items() if k in CELL_FIELDS.split(",")}


def fingerprint(book):
    value = copy.deepcopy(book)
    for sheet in value["sheets"]:
        sheet["rows"] = [[native_cell(c) for c in r] for r in sheet.get("rows", [])]
    return digest(value)


def columns(sheet):
    headers = header_columns(sheet)
    result = {field: headers[label] for field, label in HEADERS.items() if label in headers}
    missing = set(HEADERS) - set(result)
    if missing:
        raise ValueError(f"{sheet['properties']['title']} 缺少欄位：{'、'.join(HEADERS[k] for k in sorted(missing))}")
    return result


def cell_at(row, index):
    return row[index] if index < len(row) else {}


def entries(sheet):
    cols, context = columns(sheet), None
    identity_cols = header_columns(sheet)
    uid_col = identity_cols.get(SYNC_ID_HEADER)
    result = []
    for index, row in enumerate(sheet["rows"][1:], 1):
        uid = cell_text(cell_at(row, uid_col)).strip() if uid_col is not None else ""
        parsed = monthly_row(row, identity_cols)
        if parsed["kind"] == "invalid_header":
            raise ValueError(f"{row_label(sheet, index)} {parsed['reason']}")
        if parsed["kind"] == "day":
            context = {"date": parsed["date"], "hospital": parsed["hospital"],
                       "index": index, "block_sync_id": uid}
            result.append({"kind": "day", "row": row, "index": index, "sync_id": uid, **context})
        elif parsed["kind"] == "patient":
            if not context:
                raise ValueError(f"{row_label(sheet, index)} 病人列上方沒有有效刀日，請先整理刀表。")
            item = {k: cell_text(cell_at(row, col)) for k, col in cols.items()}
            # These two fields belong to the preceding ◆ 刀日 block. A patient's
            # date cell is arrival time; old per-patient hospitals may be stale
            # after a full-row drag and must not override the block route.
            item.update(date=context["date"], hospital=context["hospital"])
            result.append({"kind": "patient", "row": row, "index": index, "fields": item,
                           "day_index": context["index"], "sync_id": uid,
                           "block_sync_id": context["block_sync_id"],
                           "route": {k: context[k] for k in ("date", "hospital", "block_sync_id")}})
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
        if not MONTH_PATTERN.fullmatch(sheet["properties"]["title"]):
            continue
        for entry in entries(sheet):
            if entry["kind"] != "patient":
                continue
            target = entry["fields"]
            same_mrn = patient_key(target["mrn"]) == patient_key(fields["mrn"])
            # A decorated/cancelled MRN is a possible match, never a new-row shortcut.
            decorated = bool(re.search(r"(?<!\d)0*" + re.escape(patient_key(fields["mrn"])) + r"(?!\d)", target["mrn"])) if fields["mrn"].isdigit() else False
            if not (same_mrn or decorated):
                continue
            item = {"sheet": sheet, "entry": entry, "ref": row_label(sheet, entry["index"])}
            if (same_mrn and target["hospital"] == (fields.get("hospital") or "高榮")
                    and target["date"] == fields["date"] and target["side"] == fields["side"]
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
        self.initial_day_refs = {}
        self.months = [s for s in self.book["sheets"] if MONTH_PATTERN.fullmatch(s["properties"]["title"])]
        # A whole-row move can remove the last populated cell of an unnamed
        # manual column. Keep the original layout for this atomic plan, including
        # a freshly cloned month before its first patient row is moved into it.
        self._header_widths = {s["properties"]["sheetId"]: len(self.header_signature(s)) for s in self.months}
        self.identity_diagnostics = scan_identities(self.book)
        self.initial_uids = {}
        for sheet in self.months:
            columns(sheet)
            for entry in entries(sheet):
                if entry["kind"] == "day":
                    self.initial_day_refs[row_label(sheet, entry["index"])] = (sheet, entry["row"])
                if entry["kind"] == "patient":
                    self.initial_refs[row_label(sheet, entry["index"])] = (sheet, entry["row"])
                    if entry["sync_id"]:
                        self.initial_uids.setdefault(entry["sync_id"], []).append((sheet, entry["row"]))
        if not self.months:
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
        template = max(self.months, key=lambda s: s["properties"]["title"])
        signature = self._signature(template)
        if any(self._signature(sheet) != signature for sheet in self.months):
            raise ValueError("各月份欄位位置或人工欄位不同，請先對齊所有月表後再建立新月份。")
        used = {s["properties"]["sheetId"] for s in self.book["sheets"]}
        sid = secrets.randbelow(2**30) + 1
        while sid in used:
            sid = secrets.randbelow(2**30) + 1
        width = template["properties"]["gridProperties"]["columnCount"]
        sheet = {"properties": {"sheetId": sid, "title": month,
                               "gridProperties": {"rowCount": max(1000, template["properties"]["gridProperties"]["rowCount"]),
                                                  "columnCount": width, "frozenRowCount": 1}},
                 "rows": [copy.deepcopy(template["rows"][0])], "merges": []}
        self._header_widths[sid] = len(signature)
        self.requests.append({"addSheet": {"properties": copy.deepcopy(sheet["properties"])}})
        widths = template.get("column_widths", [])
        sheet["column_widths"] = copy.deepcopy(widths)
        for col, dimensions in enumerate(widths):
            properties = {k: dimensions[k] for k in ("pixelSize", "hiddenByUser") if k in dimensions}
            if properties:
                self.requests.append({"updateDimensionProperties": {
                    "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
                    "properties": properties, "fields": ",".join(properties)}})
        row_metadata = template.get("row_metadata", [])
        if row_metadata and "pixelSize" in row_metadata[0]:
            dimensions = {"pixelSize": row_metadata[0]["pixelSize"]}
            sheet["row_metadata"] = [copy.deepcopy(dimensions)]
            self.requests.append({"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                "properties": dimensions, "fields": "pixelSize"}})
        rules = copy.deepcopy(template.get("conditionalFormats", []))
        if rules:
            sheet["conditionalFormats"] = rules
        has_source_reference = False
        source_title = re.escape(template["properties"]["title"])
        for index, rule in enumerate(rules):
            for grid_range in rule.get("ranges", []):
                grid_range["sheetId"] = sid
            values = rule.get("booleanRule", {}).get("condition", {}).get("values", [])
            has_source_reference |= any(re.search(r"(?<![\w])(?:'" + source_title + r"'|" + source_title + r")!",
                                                 str(value.get("userEnteredValue", ""))) is not None
                                        for value in values)
            self.requests.append({"addConditionalFormatRule": {"rule": copy.deepcopy(rule), "index": index}})
        if has_source_reference:
            self.structures.append("條件格式保留對既有分頁 " + template["properties"]["title"] + " 的公式引用，請核對新月份是否適用。")
        self.requests.append({"updateCells": {"range": range_(sheet, 0),
                                             "rows": [{"values": [native_cell(c) for c in sheet["rows"][0]]}],
                                             "fields": CELL_FIELDS}})
        self.book["sheets"].append(sheet)
        self.months.append(sheet)
        self.structures.append("建立月份 " + month)
        return sheet

    def day_block(self, when, hospital="高榮", block_sync_id=None):
        when, hospital = full_date(when), norm(hospital)
        if not when or hospital not in HOSPITALS:
            raise ValueError("請指定有效純日期刀日與高榮或聯醫。")
        if isinstance(block_sync_id, str) and block_sync_id.startswith("ref:"):
            # The location picker may read before missing IDs are migrated.
            # Resolve its fresh snapshot address to a row object, which remains
            # stable after earlier proposals insert/move rows in this batch.
            target = self.initial_day_refs.get(block_sync_id[4:])
            if not target:
                raise ValueError("指定刀日區塊位置已變更，請重新讀取目的地。")
            sheet, row = target
            entry = next((e for e in entries(sheet) if e["kind"] == "day" and e["row"] is row), None)
            if not entry or entry["date"] != when or entry["hospital"] != hospital:
                raise ValueError("指定刀日區塊的日期或醫院不符，請重新選擇目的地。")
            uid = entry["sync_id"] or set_row_identity(sheet, row_location(sheet, row), self.requests)
            locations = scan_identities(self.book)["by_id"].get(uid, [])
            if not UID_PATTERN.fullmatch(uid) or len(locations) != 1:
                raise ValueError("指定刀日區塊識別碼重複或無效，請先整理刀表。")
            return sheet, row
        month = when.replace("-", "")[:6]
        sheet = next((s for s in self.book["sheets"] if s["properties"]["title"] == month), None)
        if sheet is None:
            sheet = self.new_month(month)
        groups = [e for e in entries(sheet) if e["kind"] == "day"]
        found = [e for e in groups if e["date"] == when and e["hospital"] == hospital]
        if block_sync_id:
            found = [e for e in found if e["sync_id"] == block_sync_id]
            global_locations = scan_identities(self.book)["by_id"].get(block_sync_id, [])
            if len(global_locations) != 1 or len(found) != 1:
                raise ValueError("指定刀日區塊已變更或識別碼重複，請重新預覽。")
        if len(found) > 1:
            raise ValueError(f"{month} 有重複{hospital}刀日 {when}，請指定區塊或先整理刀表。")
        if found:
            return sheet, found[0]["row"]
        next_group = next((e for e in groups if e["date"] and e["date"] > when), None)
        index = next_group["index"] if next_group else max(
            (i for i, r in enumerate(sheet["rows"]) if occupied(r)), default=0) + 1
        template_sheet, template = next(((s, e["row"]) for s in self.months for e in entries(s)
                         if e["kind"] == "day" and e["hospital"] == hospital), (sheet, []))
        width, cols = sheet["properties"]["gridProperties"]["columnCount"], columns(sheet)
        # Use a template only when all headers align, including manual fields.
        if self._signature(template_sheet) != self._signature(sheet):
            template = []
        cells = [{k: copy.deepcopy(v) for k, v in cell_at(template, i).items()
                  if k in {"userEnteredFormat", "dataValidation"}} for i in range(width)]
        cells[cols["date"]]["userEnteredValue"] = {"numberValue": (date.fromisoformat(when) - date(1899, 12, 30)).days}
        cells[cols["date"]].setdefault("userEnteredFormat", {})["numberFormat"] = {"type": "DATE", "pattern": "yyyy/m/d ddd"}
        cells[cols["hospital"]]["userEnteredValue"] = {"stringValue": hospital}
        cells[cols["mrn"]]["userEnteredValue"] = {"stringValue": "◆ 刀日"}
        self.insert(sheet, index, cells)
        self.requests.append({"updateCells": {"range": range_(sheet, index),
            "rows": [{"values": cells}], "fields": CELL_FIELDS}})
        set_row_identity(sheet, index, self.requests)
        self.structures.append(f"{month} 新增{hospital}刀日 {when}")
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
        template = next((e["row"] for s in self.months for e in entries(s)
                         if e["kind"] == "patient" and self._signature(s) == self._signature(sheet)), [])
        width = sheet["properties"]["gridProperties"]["columnCount"]
        cells = [{k: copy.deepcopy(v) for k, v in cell_at(template, i).items()
                  if k in {"userEnteredFormat", "dataValidation"}} for i in range(width)]
        row[:] = cells
        self.requests.append({"updateCells": {"range": range_(sheet, row_location(sheet, row)),
            "rows": [{"values": cells}], "fields": "userEnteredFormat,dataValidation"}})
        set_row_identity(sheet, row_location(sheet, row), self.requests)

    @staticmethod
    def header_signature(sheet, minimum_width=0):
        """Semantic header positions, with formula diagnosis summaries normalized."""
        managed = {index: field for field, index in columns(sheet).items()}
        headers = sheet["rows"][0]
        labels = [managed.get(index, canonical_header(cell)) for index, cell in enumerate(headers)]
        content_width = max((index + 1 for row in sheet.get("rows", []) for index, cell in enumerate(row)
                             if any(value != "" for value in cell.get("userEnteredValue", {}).values())
                             or cell.get("note") or cell.get("textFormatRuns")), default=0)
        width = max(content_width, minimum_width)
        while len(labels) > width and not labels[-1]:
            labels.pop()
        labels.extend([""] * max(0, width - len(labels)))
        return labels

    def _signature(self, sheet):
        return self.header_signature(sheet, self._header_widths.get(sheet["properties"]["sheetId"], 0))

    def move(self, source_sheet, source_row, target_sheet, header):
        # PASTE_NORMAL preserves every manual/technical cell. Never attempt a
        # partial remap when column positions drift between month templates.
        if self._signature(source_sheet) != self._signature(target_sheet):
            raise ValueError("來源與目的月份欄位位置或人工欄位不同，請先對齊欄位後再搬移。")
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
        uid = proposal.get("target_sync_id", "")
        if uid:
            locations = self.identity_diagnostics["by_id"].get(uid, [])
            targets = self.initial_uids.get(uid, [])
            if not UID_PATTERN.fullmatch(uid) or len(locations) != 1 or len(targets) != 1:
                raise ValueError("手術識別碼遺失、重複或不在有效月表區塊，請重新預覽。")
            target = targets[0]
            if chosen and self.initial_refs.get(chosen) != target:
                raise ValueError("所選位置與手術識別碼不一致，請重新預覽。")
        else:
            target = self.initial_refs.get(chosen) if chosen else None
        if chosen and not target:
            raise ValueError("所選刀表位置已變更，請重新預覽。")
        if target:
            sheet, row = target
            entry = next((e for e in entries(sheet) if e["row"] is row and e["kind"] == "patient"), None)
            if not entry or patient_key(entry["fields"]["mrn"]) != patient_key(fields["mrn"]):
                raise ValueError("指定列不是此病人的有效排程。")
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
        previous = next((e for e in entries(sheet) if e["row"] is row and e["kind"] == "patient"), None) if sheet else None
        previous_route = copy.deepcopy(previous["route"]) if previous else None
        previous_position = next((ref for ref, (s, r) in self.initial_refs.items() if s is sheet and r is row), "")
        changes, action = [], "update"
        selected = proposal.get("selected_fields", list(FIELD_NAMES))
        destination = proposal.get("destination")
        if destination is not None and not isinstance(destination, dict):
            raise ValueError("目的刀日與醫院格式不正確。")
        route_requested = destination is not None or "date" in selected or "hospital" in selected or bool(chosen and not uid)
        if row is not None:
            if any(watched_row is row for _, watched_row, _ in self.watched):
                raise ValueError("兩筆手術不可套用同一病人列，請分別指定手術位置。")
            index = row_location(sheet, row)
            if any(m.get("startRowIndex", 0) <= index < m.get("endRowIndex", 0)
                   for m in sheet.get("merges", [])):
                raise ValueError("病人列包含合併儲存格，請先在刀表整理後再預覽。")
        if row is None:
            desired = destination or {"date": fields["date"], "hospital": fields.get("hospital") or "高榮"}
            sheet, header = self.day_block(desired["date"], desired.get("hospital") or "高榮", desired.get("block_sync_id"))
            row, _ = self.patient_slot(sheet, header)
            self.initialize_patient(sheet, row)
            action = "insert"
        else:
            entry = next(e for e in entries(sheet) if e["row"] is row and e["kind"] == "patient")
            desired = destination or {"date": fields.get("date") or entry["fields"]["date"],
                                      "hospital": fields.get("hospital") or entry["fields"]["hospital"]}
            resolved_destination = None
            if isinstance(desired.get("block_sync_id"), str) and desired["block_sync_id"].startswith("ref:"):
                resolved_destination = self.day_block(desired["date"], desired["hospital"], desired["block_sync_id"])
                ref_sheet, ref_header = resolved_destination
                ref_entry = next(e for e in entries(ref_sheet) if e["kind"] == "day" and e["row"] is ref_header)
                desired = {**desired, "block_sync_id": ref_entry["sync_id"]}
            differs = (entry["fields"]["date"] != desired.get("date")
                       or entry["fields"]["hospital"] != desired.get("hospital")
                       or bool(desired.get("block_sync_id") and desired["block_sync_id"] != entry["block_sync_id"]))
            if route_requested and differs:
                old_position = row_label(sheet, row_location(sheet, row))
                set_row_identity(sheet, row_location(sheet, row), self.requests)
                target_sheet, header = resolved_destination or self.day_block(desired["date"], desired["hospital"], desired.get("block_sync_id"))
                row = self.move(sheet, row, target_sheet, header)
                sheet, action = target_sheet, "move"
                changes.append({"field": "date", "label": "刀日／搬移", "before": entry["fields"]["date"],
                                "after": desired["date"], "before_hospital": entry["fields"]["hospital"],
                                "after_hospital": desired["hospital"], "from": old_position})
        cols = columns(sheet)
        if action == "insert":
            selected = list(dict.fromkeys([*selected, "mrn", "side", "procedure"]))
        for field in selected:
            if field in {"date", "hospital"}:
                continue  # patient A contains arrival time, never the operation time.
            if field not in cols or field not in FIELD_NAMES:
                raise ValueError("刀表更新包含不支援的欄位。")
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
        current = next(e for e in entries(sheet) if e["row"] is row and e["kind"] == "patient")
        if uid or action in {"insert", "move"}:
            # Hospital is block-owned. After confirming UID+MRN, normalize a
            # stale/blank copied patient hospital to the containing block.
            col, value = cols["hospital"], current["fields"]["hospital"]
            while len(row) <= col:
                row.append({})
            before = cell_text(row[col])
            if before != value:
                if row[col].get("userEnteredValue", {}).get("formulaValue"):
                    raise ValueError("病人醫院欄為公式，請先在刀表整理。")
                row[col]["userEnteredValue"] = {"stringValue": value}
                row[col].pop("formattedValue", None)
                self.requests.append({"updateCells": {"range": range_(sheet, row_location(sheet, row), col, col + 1),
                                      "rows": [{"values": [{"userEnteredValue": {"stringValue": value}}]}], "fields": "userEnteredValue"}})
                changes.append({"field": "hospital", "label": "醫院", "before": before, "after": value, "column": col})
        item = {"id": proposal["id"], "fields": fields, "status": "ready", "action": action, "changes": changes,
                "sync_id": current["sync_id"], "route": current["route"],
                "previous_route": previous_route, "previous_position": previous_position}
        self.watched.append((sheet, row, item))
        return item

    def build(self):
        claimed = set()
        for proposal in self.proposals:
            identity = (proposal["fields"].get("hospital") or "高榮", *(
                proposal["fields"].get(k) for k in ("mrn", "date", "side", "procedure")))
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
                                 "sync_id": item.get("sync_id", ""), "cells": [native_cell(c) for c in row]})
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
                "identity_issues": self.identity_diagnostics["issues"],
                "can_apply": bool(self.watched) and all(i["status"] == "ready" for i in self.items)}


FU_HEADERS = ("病歷號", "姓名", "TEL", "醫院", "Tag", "Condition", "日期", "Plan", "心得",
              EVENT_ID_HEADER, SYNC_ID_HEADER)


class FUPlanner:
    """Idempotently append minimal follow-up drafts selected by configured tags.

    SOAP text is deliberately not accepted. Dates and clinical FU details are
    entered by people in the sheet; repeat MRN+Tag scans preserve them all.
    """
    def __init__(self, book, spreadsheet_id, drafts):
        self.original, self.book = book, copy.deepcopy(book)
        self.sheet_id, self.proposals = spreadsheet_id, drafts
        self.requests, self.items, self.structures, self.watched = [], [], [], []
        self.id = uuid.uuid4().hex

    def _sheet(self):
        sheet = next((s for s in self.book["sheets"] if s["properties"]["title"] == "FU"), None)
        if sheet is None:
            sheet = next((s for s in self.book["sheets"] if s["properties"]["title"] == "All"), None)
        if sheet is None:
            if any(s["properties"]["title"] == "OP" for s in self.book["sheets"]):
                raise ValueError("刀表只有 OP 追蹤分頁，請先人工確認內容並將 OP 重新命名為 FU；未另建分頁。")
            used = {s["properties"]["sheetId"] for s in self.book["sheets"]}
            sid = secrets.randbelow(2**30) + 1
            while sid in used:
                sid = secrets.randbelow(2**30) + 1
            sheet = {"properties": {"sheetId": sid, "title": "FU", "gridProperties": {
                     "rowCount": 1000, "columnCount": len(FU_HEADERS), "frozenRowCount": 1}},
                     "rows": [[{"userEnteredValue": {"stringValue": label}} for label in FU_HEADERS]], "merges": []}
            self.book["sheets"].append(sheet)
            self.requests.append({"addSheet": {"properties": copy.deepcopy(sheet["properties"])}})
            self.requests.append({"updateCells": {"range": range_(sheet, 0), "rows": [{"values": copy.deepcopy(sheet["rows"][0])}],
                                  "fields": "userEnteredValue"}})
            self.structures.append("建立 FU 追蹤分頁")
        cols = header_columns(sheet)
        missing = set(FU_HEADERS[:-2]) - set(cols)
        if missing:
            raise ValueError("FU 缺少欄位：" + "、".join(sorted(missing)))
        return sheet, ensure_identity_columns(sheet, self.requests)

    def build(self):
        if not self.proposals:
            return {"id": self.id, "status": "preview", "spreadsheet_id": self.sheet_id,
                    "baseline": fingerprint(self.original), "items": [], "requests": [], "expectations": [],
                    "structures": [], "mrns": [], "source_records": [], "can_apply": False}
        sheet, cols = self._sheet()
        claimed = set()
        for draft in self.proposals:
            fields = draft["fields"]
            mrn, tag = str(fields.get("mrn") or "").strip(), norm(fields.get("tag") or "FU")
            if not mrn or not patient_key(mrn):
                raise ValueError("新增 FU 需有病歷號。")
            key = patient_key(mrn), tag.casefold()
            if key in claimed:
                continue  # Several qualifying encounters still mean one MRN+Tag draft.
            claimed.add(key)
            matching = []
            for index, row in enumerate(sheet["rows"][1:], 1):
                if (patient_key(cell_text(cell_at(row, cols["病歷號"]))) == key[0]
                        and norm(cell_text(cell_at(row, cols["Tag"]))).casefold() == key[1]):
                    matching.append((index, row))
            if len(matching) > 1:
                self.items.append({"id": draft["id"], "fields": fields, "status": "conflict",
                                   "reason": "FU 已有相同病歷號與 Tag 的多列，請先整理。", "options": [
                                       {"ref": row_label(sheet, index)} for index, _ in matching]})
                continue
            if matching:
                index, row = matching[0]
                uid = cell_text(cell_at(row, cols[SYNC_ID_HEADER])).strip()
                if uid and (not UID_PATTERN.fullmatch(uid) or len(scan_identities(self.book)["by_id"].get(uid, [])) != 1):
                    self.items.append({"id": draft["id"], "fields": fields, "status": "conflict",
                                       "reason": "FU 手術識別碼重複或格式錯誤，請先整理。"})
                    continue
                uid = set_row_identity(sheet, index, self.requests)
                item = {"id": draft["id"], "fields": fields, "status": "ready", "action": "keep", "changes": [], "sync_id": uid}
            else:
                index = max((i for i, row in enumerate(sheet["rows"]) if occupied(row)), default=0) + 1
                if any(m.get("startRowIndex", 0) <= index < m.get("endRowIndex", 0) for m in sheet.get("merges", [])):
                    raise ValueError("FU 新增位置包含合併儲存格，請先整理。")
                grid = sheet["properties"]["gridProperties"]
                if index >= grid["rowCount"]:
                    extra = max(20, index + 1 - grid["rowCount"])
                    self.requests.append({"appendDimension": {"sheetId": sheet["properties"]["sheetId"], "dimension": "ROWS", "length": extra}})
                    grid["rowCount"] += extra
                while len(sheet["rows"]) <= index:
                    sheet["rows"].append([])
                template = next((row for row in sheet["rows"][1:] if cell_text(cell_at(row, cols["病歷號"]))), [])
                row = [{k: copy.deepcopy(v) for k, v in cell_at(template, col).items()
                        if k in {"userEnteredFormat", "dataValidation"}} for col in range(grid["columnCount"])]
                changes = []
                for field, label, value in (("mrn", "病歷號", mrn), ("name", "姓名", fields.get("name") or ""),
                                           ("hospital", "醫院", fields.get("hospital") or "高榮"), ("tag", "Tag", tag)):
                    if value:
                        row[cols[label]]["userEnteredValue"] = {"stringValue": str(value)}
                        changes.append({"field": field, "label": label, "before": "", "after": str(value), "column": cols[label]})
                sheet["rows"][index] = row
                self.requests.append({"updateCells": {"range": range_(sheet, index), "rows": [{"values": copy.deepcopy(row)}], "fields": CELL_FIELDS}})
                uid = set_row_identity(sheet, index, self.requests)
                item = {"id": draft["id"], "fields": fields, "status": "ready", "action": "insert", "changes": changes, "sync_id": uid}
            item["position"] = row_label(sheet, index)
            self.items.append(item)
            self.watched.append((sheet, row, item))
        expectations = [{"sheet": sheet["properties"]["title"], "row": row_location(sheet, row),
                         "sync_id": item["sync_id"], "cells": [native_cell(cell) for cell in row]}
                        for sheet, row, item in self.watched]
        meta_id = secrets.randbelow(2**30) + 1
        existing = {m.get("metadataId") for m in self.book.get("developerMetadata", [])}
        while meta_id in existing:
            meta_id = secrets.randbelow(2**30) + 1
        self.requests.append({"createDeveloperMetadata": {"developerMetadata": {"metadataId": meta_id,
            "metadataKey": "vghks.opd.update", "metadataValue": self.id,
            "location": {"spreadsheet": True}, "visibility": "DOCUMENT"}}})
        return {"id": self.id, "created_at": timestamp(), "status": "preview", "spreadsheet_id": self.sheet_id,
                "baseline": fingerprint(self.original), "items": self.items, "requests": self.requests,
                "expectations": expectations, "structures": self.structures, "metadata_id": meta_id,
                "mrns": sorted({str(p["fields"]["mrn"]) for p in self.proposals}),
                "source_records": sorted({r for p in self.proposals for r in p.get("source_records", [])}),
                "can_apply": bool(self.watched) and all(item["status"] == "ready" for item in self.items)}


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
