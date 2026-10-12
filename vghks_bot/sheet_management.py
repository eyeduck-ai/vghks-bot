"""Patient-list-independent Sheets management with durable, reviewable plans.

Public methods return JSON dictionaries. ``status`` reads local cache only;
``refresh`` reads Sheets. ``preview`` accepts ``action`` and nested ``params``
(the same parameters at top level are also accepted). ``apply``/``check`` take
``id``. Public plans omit requests, fingerprints and native clinical snapshots.
An uncertain write is never resent; ``check`` only reads its transaction marker.
Archive is explicitly two-phase: copied -> can_finalize -> a second apply.
"""
from __future__ import annotations

import copy
import re
import secrets
import uuid
from collections import Counter

from .analysis_numeric import norm
from .analysis_store import digest
from .google_sheets import SheetError, SheetUnavailable, SheetUncertain
from .settings import timestamp
from .sheet_identity import (
    EVENT_ID_HEADER,
    MONTH_PATTERN,
    SYNC_ID_HEADER,
    ensure_identity_columns,
    header_columns,
    plan_identity_setup,
    schema_kind,
)
from .sheet_plan import (
    CELL_FIELDS,
    Planner,
    cell_at,
    cell_text,
    columns,
    entries,
    fingerprint,
    range_,
)
from .sheet_schema import HOSPITALS, full_date, monthly_row

PLAN_KIND = "sheet_management_plan"
CACHE_KIND = "sheet_management_cache"
HISTORY_KIND = "sheet_management_history"
METADATA_KEY = "vghks.sheet.management"
STANDARD_HEADERS = (
    "日期／報到時間", "醫院", "病歷號", "姓名", "TEL", "GA", "側別", "診斷", "Grade", "術式",
    "IOL", "IOL Target", "IOL Final", "Axis", "Plan", "心得", "", "SN", "CDE", "Energy Time",
    "Energy %", "屈光數據", EVENT_ID_HEADER, SYNC_ID_HEADER,
)
STANDARD_WIDTHS = (115, 58, 76, 72, 96, 42, 46, 160, 82, 180, 96, 80, 76, 52, 260, 260,
                   65, 65, 65, 90, 72, 180, 120, 120)
PUBLIC_PLAN_FIELDS = {
    "id", "action", "params", "status", "spreadsheet_id", "created_at", "updated_at", "applied_at",
    "message", "changes", "issues", "stale", "phase", "can_apply", "can_finalize", "request_count",
    "cached_at", "blocking_plan_id",
}


def _month(value):
    if not isinstance(value, str) or not re.fullmatch(r"20[0-9]{2}-?(?:0[1-9]|1[0-2])", value):
        raise ValueError("月份請輸入 2000–2099 年的 YYYYMM 或 YYYY-MM。")
    return value.replace("-", "")


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", value) or full_date(value) != value:
        raise ValueError("刀日請輸入有效的 YYYY-MM-DD 純日期。")
    return value


def _intent(values):
    if not isinstance(values, dict):
        raise ValueError("刀表管理選項格式不正確。")
    action = values.get("action")
    nested = values.get("params", {})
    if not isinstance(nested, dict):
        raise ValueError("刀表管理參數格式不正確。")
    params = {key: value for key, value in values.items() if key not in {"action", "params"}}
    if any(key in params and params[key] != value for key, value in nested.items()):
        raise ValueError("同一刀表管理參數不可有兩種不同內容。")
    params.update(nested)
    allowed = {"create_month": {"month"}, "create_day": {"date", "hospital"}, "repair": set(), "archive": {"months"}}
    if action not in allowed or set(params) - allowed[action]:
        raise ValueError("請選擇建立月份、建立刀日、結構修復或封存月份。")
    if action == "create_month":
        params = {"month": _month(params.get("month"))}
    elif action == "create_day":
        hospital = norm(params.get("hospital", ""))
        if hospital not in HOSPITALS:
            raise ValueError("院區請選擇高榮或聯醫。")
        params = {"date": _date(params.get("date")), "hospital": hospital}
    elif action == "archive":
        months = params.get("months")
        if not isinstance(months, list) or not months or len(months) > 120:
            raise ValueError("請選擇 1–120 個要封存的月份。")
        params = {"months": sorted({_month(month) for month in months})}
    return action, params


def _content(cell):
    result = {}
    value = cell.get("userEnteredValue", {})
    if value and value != {"stringValue": ""}:
        result["userEnteredValue"] = copy.deepcopy(value)
    for key in ("note", "textFormatRuns"):
        if cell.get(key):
            result[key] = copy.deepcopy(cell[key])
    return result


def _rows(sheet):
    result = [[_content(cell) for cell in row] for row in sheet.get("rows", [])]
    for row in result:
        while row and not row[-1]:
            row.pop()
    while result and not result[-1]:
        result.pop()
    return result


def _expected(book):
    checks = []
    for sheet in book.get("sheets", []):
        kind, _ = schema_kind(sheet)
        if not kind:
            continue
        props = sheet["properties"]
        cols = header_columns(sheet, 5 if kind == "archive" else 0)
        checks.append({"sheet_id": props["sheetId"], "title": props["title"], "rows": _rows(sheet),
                       "row_count": props["gridProperties"]["rowCount"],
                       "column_count": props["gridProperties"]["columnCount"],
                       "frozen_rows": props["gridProperties"].get("frozenRowCount", 0),
                       "hidden_columns": [cols[label] for label in (EVENT_ID_HEADER, SYNC_ID_HEADER)
                                          if label in cols and cols[label] < len(sheet.get("column_widths", []))
                                          and sheet["column_widths"][cols[label]].get("hiddenByUser")]})
    return checks


def _verify(book, checks):
    for check in checks:
        sheet = next((sheet for sheet in book.get("sheets", []) if sheet["properties"]["sheetId"] == check["sheet_id"]), None)
        if sheet is None or sheet["properties"]["title"] != check["title"] or _rows(sheet) != check["rows"]:
            return False
        grid = sheet["properties"]["gridProperties"]
        if (grid["rowCount"] < check["row_count"] or grid["columnCount"] < check["column_count"]
                or grid.get("frozenRowCount", 0) < check["frozen_rows"]):
            return False
        metadata = sheet.get("column_widths", [])
        if any(col >= len(metadata) or not metadata[col].get("hiddenByUser") for col in check["hidden_columns"]):
            return False
    return True


def _summary(book):
    months, issues = [], []
    for sheet in sorted(book.get("sheets", []), key=lambda sheet: sheet["properties"]["title"]):
        title = sheet["properties"]["title"]
        if not MONTH_PATTERN.fullmatch(title):
            continue
        try:
            parsed = entries(sheet)
            days = [{"date": entry["date"], "hospital": entry["hospital"], "row": entry["index"] + 1,
                     "sync_id": entry["sync_id"]} for entry in parsed if entry["kind"] == "day"]
            count = sum(entry["kind"] == "patient" for entry in parsed)
            months.append({"month": title, "patient_count": count, "days": days})
        except (ValueError, KeyError) as exc:
            months.append({"month": title, "patient_count": None, "days": []})
            issues.append({"sheet": title, "code": "invalid_month", "reason": str(exc)})
    setup = plan_identity_setup(book)
    issues.extend(setup["issues"])
    return months, issues


def _new_id(book):
    used = {sheet["properties"]["sheetId"] for sheet in book.get("sheets", [])}
    result = secrets.randbelow(2**30) + 1
    while result in used:
        result = secrets.randbelow(2**30) + 1
    return result


def _standard_month(book, month):
    sid = _new_id(book)
    sheet = {"properties": {"sheetId": sid, "title": month,
                             "gridProperties": {"rowCount": 1000, "columnCount": len(STANDARD_HEADERS), "frozenRowCount": 1}},
             "rows": [[{"userEnteredValue": {"stringValue": label},
                        "userEnteredFormat": {"textFormat": {"fontFamily": "Arial", "fontSize": 10, "bold": True}}}
                       for label in STANDARD_HEADERS]], "merges": [], "column_widths": []}
    requests = [{"addSheet": {"properties": copy.deepcopy(sheet["properties"])}},
                {"updateCells": {"range": range_(sheet, 0), "rows": [{"values": copy.deepcopy(sheet["rows"][0])}], "fields": CELL_FIELDS}}]
    for col, width in enumerate(STANDARD_WIDTHS):
        dimensions = {"pixelSize": width, "hiddenByUser": col >= len(STANDARD_HEADERS) - 2}
        sheet["column_widths"].append(dimensions)
        requests.append({"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
                        "properties": copy.deepcopy(dimensions), "fields": "pixelSize,hiddenByUser"}})
    book["sheets"].append(sheet)
    return sheet, requests


def _report_candidate(sheet, row, index, route, cols, field_cols):
    fields = {field: cell_text(cell_at(row, col)) for field, col in field_cols.items()}
    mrn_cell = cell_at(row, cols["病歷號"])
    entered_line = mrn_cell.get("userEnteredFormat", {}).get("textFormat", {}).get("strikethrough", False)
    cancelled = bool(mrn_cell.get("effectiveFormat", {}).get("textFormat", {}).get("strikethrough", entered_line))
    final_col, uid_col = cols.get("IOL Final"), cols.get(SYNC_ID_HEADER)
    values = {label: _content(cell_at(row, col)) for label, col in cols.items()
              if label not in {"ArchiveSourceMonth", "ArchiveRowType"}}
    return {**{field: fields.get(field, "") for field in ("mrn", "name", "side", "procedure", "iol")},
            **route, "iol_target": fields.get("target", ""),
            "iol_final": cell_text(cell_at(row, final_col)) if final_col is not None else "",
            "position": f"{sheet['properties']['title']}!{index + 1}",
            "sync_id": cell_text(cell_at(row, uid_col)).strip() if uid_col is not None else "",
            "_cancelled": cancelled, "_signature": digest([values, route, cancelled])}


def _report_rows(book):
    result, issues = [], []
    for sheet in book.get("sheets", []):
        kind, header = schema_kind(sheet)
        if kind not in {"month", "archive"}:
            continue
        title = sheet["properties"]["title"]
        try:
            cols = header_columns(sheet, header)
            field_cols = columns({"properties": sheet["properties"], "rows": [sheet["rows"][header]]})
            if kind == "month":
                for entry in entries(sheet):
                    if entry["kind"] == "patient":
                        result.append(_report_candidate(sheet, entry["row"], entry["index"],
                            {field: entry["fields"][field] for field in ("date", "hospital")}, cols, field_cols))
                continue
            if not {"ArchiveSourceMonth", "ArchiveRowType"} <= cols.keys():
                raise ValueError("封存缺少 ArchiveSourceMonth 或 ArchiveRowType。")
            month, context = "", None
            for index, row in enumerate(sheet["rows"][header + 1:], header + 1):
                source = cell_text(cell_at(row, cols["ArchiveSourceMonth"])).strip()
                row_type = cell_text(cell_at(row, cols["ArchiveRowType"])).strip().upper()
                if not row_type and not source and not any(_content(cell) for cell in row):
                    continue
                valid_source = bool(MONTH_PATTERN.fullmatch(source) and source[:4] == title[-4:])
                if row_type == "MONTH_HEADER":
                    month, context = source if valid_source else "", None
                    if valid_source:
                        continue
                elif source == month and valid_source:
                    parsed = monthly_row(row, cols)
                    if row_type == "DATE_HEADER":
                        context = ({"date": parsed["date"], "hospital": parsed["hospital"]}
                                   if parsed["kind"] == "day" and parsed["date"].replace("-", "")[:6] == month else None)
                        if context:
                            continue
                    elif row_type == "PATIENT" and context and parsed["kind"] == "patient":
                        result.append(_report_candidate(sheet, row, index, context, cols, field_cols))
                        continue
                context = None
                issues.append({"code": "invalid_archive_context", "sheet": title, "row": index,
                               "reason": "封存來源月份、列類型或上方刀日無效；此列不納入統計。"})
        except (ValueError, KeyError, IndexError) as exc:
            issues.append({"sheet": title, "code": "invalid_report_sheet", "reason": str(exc)})
    grouped = {}
    for item in result:
        grouped.setdefault(item["sync_id"] or item["position"], []).append(item)
    unique = []
    for items in grouped.values():
        if len(items) > 1:
            same = len({item["_signature"] for item in items}) == 1
            issues.append({"code": "duplicate_report_sync_id" if same else "conflicting_report_sync_id",
                           "positions": [item["position"] for item in items],
                           "reason": "來源與封存含相同排程 ID，暫計一次；封存流程完成前統計尚待核對。" if same
                                     else "同一排程 ID 的內容不一致，相關列全部排除並待核對。"})
            if not same:
                continue
        unique.append(items[0])
    return unique, issues


class SheetManagement:
    def __init__(self, app):
        self.app, self.db = app, app.review.db

    def _config(self):
        public = self.app.analysis.google.public()
        return {"spreadsheet_id": public.get("spreadsheet_id", ""), "configured": bool(public.get("configured"))}

    @staticmethod
    def _read(client, *, archive=False):
        if archive and hasattr(client, "read_for_archive"):
            return client.read_for_archive()
        return client.read_all() if hasattr(client, "read_all") else client.read()

    def _cache(self, key):
        return self.db.get(CACHE_KIND, key or "unconfigured", required=False) or {}

    def _cache_success(self, key, book):
        now = timestamp()
        return self.db.save(CACHE_KIND, {"id": key or "unconfigured", "spreadsheet_id": key,
            "book": copy.deepcopy(book), "cached_at": now, "last_attempt": now, "online": True,
            "message": "已重新讀取雲端刀表。"})

    def _cache_error(self, key, message):
        return self.db.save(CACHE_KIND, {**self._cache(key), "id": key or "unconfigured", "spreadsheet_id": key,
            "last_attempt": timestamp(), "online": False, "message": message})

    @staticmethod
    def _public(plan):
        return {key: copy.deepcopy(value) for key, value in plan.items() if key in PUBLIC_PLAN_FIELDS}

    def _save(self, plan, **changes):
        plan.update(changes)
        plan["can_apply"] = plan["status"] == "preview" and not plan.get("stale") and bool(plan.get("requests"))
        plan["can_finalize"] = plan["status"] == "copied" and not plan.get("stale")
        plan["request_count"] = len(plan.get("requests", []))
        saved = self.db.save(PLAN_KIND, plan)
        plan.update(saved)
        return self._public(plan)

    def _history(self, plan):
        self.db.save(HISTORY_KIND, {**self._public(plan), "id": uuid.uuid4().hex, "plan_id": plan["id"], "at": timestamp()})

    def _unresolved(self, key, exclude=""):
        """Keep unresolved batches exclusive across the whole spreadsheet."""
        plans = [plan for plan in self.db.all(PLAN_KIND)
                 if plan.get("spreadsheet_id") == key and plan["id"] != exclude
                 and (plan["status"] in {"pending", "uncertain", "failed", "copied"}
                      or plan.get("action") == "archive" and plan.get("submitted_at")
                      and plan.get("status") in {"applied_changed", "blocked"})]
        return min(plans, key=lambda plan: (plan.get("created_at", ""), plan["id"])) if plans else None

    def _blocked_by(self, plan, other):
        return self._save(plan, status="blocked", stale=True, requests=[], blocking_plan_id=other["id"],
            message=f"此刀表尚有未確認或未完成的批次（{other['id']}）；請先核對並完成原批次，再產生新預覽。")

    def _status(self, config, cache, *, stale=True):
        months, issues = _summary(cache["book"]) if cache.get("book") else ([], [])
        key = config["spreadsheet_id"]
        message = cache.get("message") or ("請先設定刀表與服務帳戶；可先保存管理草稿。" if not config["configured"] else "請重新讀取雲端刀表。")
        if stale and cache.get("book"):
            message += " 顯示最近成功讀取的本機快取，套用前仍須重新讀取雲端。"
        connection = {**config, "online": bool(cache.get("online")) and config["configured"], "stale": stale,
                      "cached_at": cache.get("cached_at", ""), "last_attempt": cache.get("last_attempt", ""), "message": message}
        plans = sorted((plan for plan in self.db.all(PLAN_KIND) if plan.get("spreadsheet_id") in {key, ""}),
                       key=lambda plan: plan.get("created_at", ""), reverse=True)[:30]
        history = sorted((row for row in self.db.all(HISTORY_KIND) if row.get("spreadsheet_id") == key),
                         key=lambda row: row.get("at", ""), reverse=True)[:30]
        return {**connection, "connection": connection, "months": months, "issues": issues,
                "plans": [self._public(plan) for plan in plans], "history": history}

    def status(self, values=None):
        config = self._config()
        return self._status(config, self._cache(config["spreadsheet_id"]))

    def refresh(self, values=None):
        config = self._config()
        if not config["configured"]:
            return self.status()
        try:
            def work(client):
                book = self._read(client)
                cache = self._cache_success(client.key, book)
                return self._status({**config, "spreadsheet_id": client.key}, cache, stale=False)
            return self.app.analysis.sheet_operation(work)
        except SheetError as exc:
            return self._status(config, self._cache_error(config["spreadsheet_id"], str(exc)))

    @staticmethod
    def _marker(book, token):
        found = [item for item in book.get("developerMetadata", [])
                 if item.get("metadataKey") == METADATA_KEY and item.get("metadataValue") == token]
        return len(found) == 1

    @staticmethod
    def _add_marker(book, requests, token):
        used = {item.get("metadataId") for item in book.get("developerMetadata", [])}
        metadata_id = secrets.randbelow(2**30) + 1
        while metadata_id in used:
            metadata_id = secrets.randbelow(2**30) + 1
        requests.append({"createDeveloperMetadata": {"developerMetadata": {"metadataId": metadata_id,
            "metadataKey": METADATA_KEY, "metadataValue": token, "location": {"spreadsheet": True}, "visibility": "DOCUMENT"}}})
        return metadata_id

    def _build(self, original, plan):
        action, params = plan["action"], plan["params"]
        if action == "archive":
            from .sheet_archive import build_archive
            archive = build_archive(original, params["months"], transaction_id=plan["id"])
            summary = archive.get("summary", {})
            changes = [f"封存 {item['month']} 至 {item['target']}：{item['patients']} 筆病人、{item['blocks']} 個刀日區塊。"
                       for item in summary.get("months", [])]
            changes.append("先保留原生備份並核對封存副本；第二步確認後才移除原月份。")
            changes.extend(summary[key] for key in ("formula_policy", "format_policy") if summary.get(key))
            plan.update(archive_plan=archive, phase="copy", requests=archive["requests"],
                        changes=changes, expected=[], marker=plan["id"] + ":copy")
            return
        setup = plan_identity_setup(original)
        if setup["issues"]:
            plan.update(issues=setup["issues"], status="blocked", message="請先核對刀表欄位、刀日與既有保護權限。")
            return
        book, requests, changes = setup["book"], setup["requests"], []
        if requests:
            changes.append("補齊並隱藏系統識別欄；保留既有 ID 與人工內容。")
        month_sheets = [sheet for sheet in book["sheets"] if MONTH_PATTERN.fullmatch(sheet["properties"]["title"])]
        if action in {"create_month", "create_day"}:
            month = params.get("month") or params["date"].replace("-", "")[:6]
            if not month_sheets:
                _, extra = _standard_month(book, month)
                requests.extend(extra)
                changes.append(f"建立標準空月份 {month}（不複製病人）。")
            planner = Planner(book, plan["spreadsheet_id"], [])
            if action == "create_month":
                if not any(sheet["properties"]["title"] == month for sheet in planner.months):
                    sheet = planner.new_month(month)
                    ensure_identity_columns(sheet, planner.requests)
                elif month_sheets:
                    changes.append(f"月份 {month} 已存在，保留既有內容。")
            else:
                planner.day_block(params["date"], params["hospital"])
            requests.extend(planner.requests)
            changes.extend(planner.structures)
            book = planner.book
        elif action == "repair":
            for sheet in book["sheets"]:
                kind, header_row = schema_kind(sheet)
                if kind is None:
                    continue
                count = header_row + 1
                if sheet["properties"]["gridProperties"].get("frozenRowCount", 0) < count:
                    sheet["properties"]["gridProperties"]["frozenRowCount"] = count
                    requests.append({"updateSheetProperties": {"properties": {"sheetId": sheet["properties"]["sheetId"],
                        "gridProperties": {"frozenRowCount": count}}, "fields": "gridProperties.frozenRowCount"}})
                    changes.append(f"凍結 {sheet['properties']['title']} 表頭（前 {count} 列）。")
        plan.update(requests=requests, expected=_expected(book), changes=changes, marker=plan["id"])

    def preview(self, values):
        action, params = _intent(values)
        config = self._config()
        plan = {"id": uuid.uuid4().hex, "action": action, "params": params, "spreadsheet_id": config["spreadsheet_id"],
                "created_at": timestamp(), "status": "draft", "changes": [], "issues": [], "stale": True,
                "message": "管理意圖已保存；重新連線並產生預覽後才可套用。", "requests": []}
        self._save(plan)
        if not config["configured"]:
            return self._public(plan)
        try:
            def work(client):
                unresolved = self._unresolved(client.key, plan["id"])
                if unresolved:
                    self._blocked_by(plan, unresolved)
                    if (unresolved["action"], unresolved["params"]) == (action, params):
                        return self._public(unresolved)
                    return self._public(plan)
                original = self._read(client, archive=action == "archive")
                cache = self._cache_success(client.key, original)
                plan.update(spreadsheet_id=client.key, baseline=fingerprint(original), stale=False,
                            status="preview", cached_at=cache["cached_at"])
                try:
                    self._build(original, plan)
                except ValueError as exc:
                    plan.update(status="blocked", message=str(exc), requests=[])
                if plan["status"] == "preview":
                    if plan["requests"]:
                        plan["metadata_id"] = self._add_marker(original, plan["requests"], plan["marker"])
                        plan["message"] = "請核對結構變更，再套用此預覽。"
                    else:
                        plan.update(status="unchanged", message="目前結構已符合這項操作，無需寫入。")
                return self._save(plan)
            return self.app.analysis.sheet_operation(work)
        except SheetError as exc:
            self._cache_error(config["spreadsheet_id"], str(exc))
            return self._save(plan, status="draft" if isinstance(exc, SheetUnavailable) else "blocked", stale=True, message=str(exc))

    def _plan(self, values):
        if not isinstance(values, dict) or not isinstance(values.get("id"), str):
            raise ValueError("請選擇已保存的刀表管理預覽。")
        return self.db.get(PLAN_KIND, values["id"])

    def _same_sheet(self, client, plan):
        if client.key != plan["spreadsheet_id"]:
            raise ValueError("刀表設定已改變，請切回原刀表或重新產生預覽。")

    def _check_work(self, client, plan, book=None):
        self._same_sheet(client, plan)
        book = book if book is not None else self._read(client, archive=plan["action"] == "archive")
        plan["cached_at"] = self._cache_success(client.key, book)["cached_at"]
        if not self._marker(book, plan.get("marker")):
            if plan["status"] in {"pending", "uncertain", "failed"}:
                return self._save(plan, status="uncertain", stale=False,
                    message="尚未找到此批次完成標記；不會自動重送，請核對雲端內容。")
            if plan["status"] == "copied":
                return self._save(plan, status="blocked", stale=False,
                    message="無法確認封存副本的完成標記，保留原月份，請人工核對。")
            return self._public(plan)
        if plan["action"] == "archive":
            from .sheet_archive import verify_archive, verify_archive_complete
            verify = verify_archive_complete if plan["phase"] == "delete" else verify_archive
            valid = verify(plan["archive_plan"], book) == "verified"
            if valid and plan["phase"] == "copy":
                result = self._save(plan, status="copied", stale=False,
                    message="封存副本已寫入並核對。確認後可移除原月份；目前原分頁仍保留。")
            else:
                result = self._save(plan, status="applied" if valid else "applied_changed", stale=False,
                    applied_at=timestamp(), message="封存完成並核對。" if valid else "批次已送達，但封存內容或來源已變動，保留供人工核對。")
        else:
            valid = _verify(book, plan["expected"])
            result = self._save(plan, status="applied" if valid else "applied_changed", stale=False, applied_at=timestamp(),
                message="已寫入並核對。" if valid else "批次已送達，但讀回結構或內容有變動，請人工核對。")
        self._history(plan)
        return result

    def check(self, values):
        plan = self._plan(values)
        if not plan.get("marker"):
            return self._public(plan)
        try:
            def work(client):
                nonlocal plan
                # Another caller may have completed copy/delete while this
                # operation was waiting for the shared sheet lock.
                plan = self._plan(values)
                try:
                    return self._check_work(client, plan) if plan.get("marker") else self._public(plan)
                except SheetError as exc:
                    self._cache_error(plan["spreadsheet_id"], str(exc))
                    return self._save(plan, stale=True, message=str(exc))
            return self.app.analysis.sheet_operation(work)
        except SheetError as exc:
            self._cache_error(plan["spreadsheet_id"], str(exc))
            # Client creation can fail before taking the callback's lock. Do
            # not overwrite a phase that another caller may have advanced.
            return {**self._public(self._plan(values)), "stale": True, "message": str(exc),
                    "can_apply": False, "can_finalize": False}

    def apply(self, values):
        plan = self._plan(values)
        if getattr(getattr(self.app, "root", self.app), "read_only", False):
            raise ValueError("唯讀檢閱不能套用刀表管理變更。")
        if plan["status"] in {"pending", "uncertain", "applied", "applied_changed", "failed"}:
            return self.check(values)
        if plan["status"] not in {"preview", "copied"} or plan.get("stale"):
            raise ValueError("請重新連線並產生可套用的預覽。")
        try:
            def work(client):
                nonlocal plan
                plan = self._plan(values)
                self._same_sheet(client, plan)
                if plan["status"] in {"pending", "uncertain", "applied", "applied_changed", "failed"}:
                    return self._check_work(client, plan)
                if plan["status"] not in {"preview", "copied"} or plan.get("stale"):
                    raise ValueError("請重新連線並產生可套用的預覽。")
                unresolved = self._unresolved(client.key, plan["id"])
                if unresolved:
                    return self._blocked_by(plan, unresolved)
                book = self._read(client, archive=plan["action"] == "archive")
                plan["cached_at"] = self._cache_success(client.key, book)["cached_at"]
                if self._marker(book, plan["marker"]):
                    if plan["status"] != "copied":
                        return self._check_work(client, plan, book)
                if plan["status"] == "copied":
                    if not self._marker(book, plan["marker"]):
                        return self._save(plan, status="blocked", stale=False,
                            message="無法確認封存副本的完成標記，保留原月份，請人工核對。")
                    from .sheet_archive import build_archive_delete
                    try:
                        archive = build_archive_delete(plan["archive_plan"], book)
                    except ValueError as exc:
                        return self._save(plan, status="blocked", message=str(exc), requests=[])
                    plan.update(archive_plan=archive, phase="delete", requests=archive["requests"], marker=plan["id"] + ":delete")
                    plan["metadata_id"] = self._add_marker(book, plan["requests"], plan["marker"])
                    plan["baseline"] = fingerprint(book)
                if fingerprint(book) != plan["baseline"]:
                    return self._save(plan, status="stale", stale=True,
                        message="雲端刀表已改變，未套用；請重新讀取並產生預覽。")
                self._save(plan, status="pending", stale=False, submitted_at=timestamp(),
                           message="批次已保存，正在送出結構變更…")
                try:
                    client.write(plan["requests"])
                except SheetUncertain as exc:
                    return self._save(plan, status="uncertain", message=str(exc))
                except SheetError as exc:
                    # The transport/ambiguous cases above are check-only.
                    # A definite rejection may be freshly planned after the
                    # cause is fixed; it must not lock all future management.
                    if plan["action"] == "archive" and plan.get("phase") == "delete":
                        plan.update(phase="copy", marker=plan["id"] + ":copy")
                        plan["archive_plan"]["phase"] = "copy"
                        return self._save(plan, status="copied",
                            message=str(exc) + " 原月份仍保留；請修正權限或設定、核對封存副本後再確認移除。")
                    return self._save(plan, status="rejected", can_apply=False,
                        message=str(exc) + " 此批次已明確拒絕；修正後請重新讀取並產生新預覽。")
                try:
                    return self._check_work(client, plan)
                except SheetError as exc:
                    self._cache_error(client.key, str(exc))
                    return self._save(plan, status="uncertain", stale=True,
                        message="批次已送出，但讀回失敗；請核對結果，系統不會重送。")
            def locked_work(client):
                try:
                    return work(client)
                except SheetError as exc:
                    self._cache_error(plan["spreadsheet_id"], str(exc))
                    return self._save(plan, stale=True, message=str(exc))
            return self.app.analysis.sheet_operation(locked_work)
        except SheetError as exc:
            self._cache_error(plan["spreadsheet_id"], str(exc))
            return {**self._public(self._plan(values)), "stale": True, "message": str(exc),
                    "can_apply": False, "can_finalize": False}

    def report(self, values):
        values = values or {}
        if not isinstance(values, dict) or set(values) - {"start", "end", "hospital"}:
            raise ValueError("IOL 統計篩選格式不正確。")
        start = _date(values["start"]) if values.get("start") else ""
        end = _date(values["end"]) if values.get("end") else ""
        hospital = norm(values.get("hospital", ""))
        if start and end and start > end or hospital and hospital not in HOSPITALS:
            raise ValueError("請確認日期範圍及高榮或聯醫院區。")
        state = self.refresh()
        cache = self._cache(state["spreadsheet_id"])
        candidates, issues = _report_rows(cache.get("book", {}))
        items, excluded = [], 0
        for item in candidates:
            if (start and item["date"] < start or end and item["date"] > end
                    or hospital and item["hospital"] != hospital):
                continue
            if item["_cancelled"]:
                excluded += 1
                continue
            items.append({key: value for key, value in item.items() if not key.startswith("_")})
        items.sort(key=lambda item: (item["date"], item["hospital"], item["position"]))
        counts = Counter(item["iol"] or "未填" for item in items)
        return {"spreadsheet_id": state["spreadsheet_id"], "online": state["online"], "stale": state["stale"],
                "cached_at": state["cached_at"], "message": state["message"], "filters": {"start": start, "end": end, "hospital": hospital},
                "items": items, "models": [{"model": model, "count": count} for model, count in sorted(counts.items())],
                "total": len(items), "excluded_cancelled": excluded, "count_basis": "排程列數；OU 不自動推算為兩片 IOL。",
                "issues": issues + state["issues"], "complete": not issues and not state["issues"] and not state["stale"]}
