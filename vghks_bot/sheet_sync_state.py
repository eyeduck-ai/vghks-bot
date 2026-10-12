"""Shared, versioned SOAP→Sheet baselines containing hashes, never SOAP text.

State and patient writes are committed together in one Sheets batchUpdate. A
missing/corrupt baseline is conservative: only empty or already equal fields
are adopted. Last observed SOAP and last successfully written sheet values
are separate, so a repeated SOAP scan cannot erase a manual edit.
"""
from __future__ import annotations

import copy
import json
import re
import secrets

from .analysis_numeric import norm
from .analysis_store import digest
from .sheet_identity import SYNC_STATE_TITLE, UID_PATTERN, header_columns, text

STATE_HEADERS = ("ScheduleSyncId", "SchemaVersion", "StateJson")
STATE_VERSION = 1
MANAGED_FIELDS = frozenset({"mrn", "name", "tel", "ga", "side", "diagnosis", "grade", "procedure", "iol", "target", "plan"})
HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def field_hash(field, value):
    value = norm(str(value or ""))
    if field == "mrn" and value.isdigit():
        value = str(int(value))
    return digest(value)


def _route(value):
    return {key: str((value or {}).get(key) or "") for key in ("date", "hospital", "block_sync_id")}


def _valid_state(value):
    """Allowlist persisted metadata. Reject accidental raw clinical payloads."""
    allowed = {"fields", "source_fields", "route", "source_route", "source_version", "source_records",
               "source_keys", "source_bindings", "source_conflicted", "updated_at", "kind", "mrn_hash", "tag_hash"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("同步基準包含不支援的欄位。")
    result = copy.deepcopy(value)
    for label in ("fields", "source_fields"):
        values = result.get(label, {})
        if not isinstance(values, dict) or set(values) - MANAGED_FIELDS:
            raise ValueError("同步基準欄位不正確。")
        if any(not isinstance(v, str) or not HASH_PATTERN.fullmatch(v) for v in values.values()):
            raise ValueError("同步基準只能儲存欄位雜湊。")
    for label in ("route", "source_route"):
        if label in result:
            if not isinstance(result[label], dict) or set(result[label]) - {"date", "hospital", "block_sync_id"}:
                raise ValueError("同步基準路由不正確。")
            result[label] = _route(result[label])
    for label in ("source_version", "mrn_hash", "tag_hash"):
        if result.get(label) and (not isinstance(result[label], str) or not HASH_PATTERN.fullmatch(result[label])):
            raise ValueError("同步來源版本必須是雜湊。")
    for label in ("source_records", "source_keys", "source_conflicted"):
        if label in result and (not isinstance(result[label], list) or any(not isinstance(v, str) or len(v) > 180 for v in result[label])):
            raise ValueError("同步來源識別碼不正確。")
    bindings = result.get("source_bindings", [])
    if not isinstance(bindings, list):
        raise ValueError("同步來源綁定不正確。")
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) - {"record_id", "field", "block_hash", "event_hash", "source_version", "block_count", "ordinal"}:
            raise ValueError("同步來源綁定不可包含原始病歷。")
        if any(not isinstance(v, str) or len(v) > 180 for k, v in binding.items() if k not in {"block_count", "ordinal"}):
            raise ValueError("同步來源綁定不正確。")
        for key in ("block_hash", "event_hash", "source_version"):
            if binding.get(key) and not HASH_PATTERN.fullmatch(binding[key]):
                raise ValueError("同步來源綁定只能儲存雜湊。")
        if "field" in binding and binding["field"] not in {"soap", "assessment_plan", "assessment", "plan", "manual"}:
            raise ValueError("同步來源欄位不正確。")
        for key, minimum in (("block_count", 1), ("ordinal", 0)):
            if key in binding and (type(binding[key]) is not int or binding[key] < minimum):
                raise ValueError("同步來源段落數或位置不正確。")
    if result.get("kind") not in {None, "surgery", "fu"}:
        raise ValueError("同步基準類型不正確。")
    if "updated_at" in result and (not isinstance(result["updated_at"], str) or len(result["updated_at"]) > 50):
        raise ValueError("同步時間不正確。")
    return result


def read_sync_state(book):
    """Return {states: {uid: metadata}, issues: [...], sheet_id: int|None}."""
    found = [s for s in book.get("sheets", []) if s["properties"]["title"] == SYNC_STATE_TITLE]
    if not found:
        return {"states": {}, "issues": [], "sheet_id": None}
    if len(found) != 1:
        return {"states": {}, "issues": [{"code": "duplicate_state_sheet"}], "sheet_id": None}
    sheet, states, issues, seen = found[0], {}, [], set()
    try:
        cols = header_columns(sheet)
        if set(cols) != set(STATE_HEADERS):
            raise ValueError("同步基準分頁欄位不正確。")
    except ValueError as exc:
        return {"states": {}, "issues": [{"code": "invalid_state_headers", "reason": str(exc)}],
                "sheet_id": sheet["properties"]["sheetId"]}
    for index, row in enumerate(sheet.get("rows", [])[1:], 1):
        values = {label: text(row[col]) if col < len(row) else "" for label, col in cols.items()}
        if not any(values.values()):
            continue
        uid = values["ScheduleSyncId"]
        if uid in seen:
            states.pop(uid, None)
            issues.append({"code": "duplicate_state_id", "sync_id": uid, "row": index})
            continue
        seen.add(uid)
        try:
            if not UID_PATTERN.fullmatch(uid) or values["SchemaVersion"] != str(STATE_VERSION):
                raise ValueError("同步基準 ID 或版本不正確。")
            states[uid] = _valid_state(json.loads(values["StateJson"]))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            issues.append({"code": "invalid_state", "sync_id": uid, "row": index, "reason": str(exc)})
    return {"states": states, "issues": issues, "sheet_id": sheet["properties"]["sheetId"]}


def plan_sync_state(book, updates):
    """Plan UID upserts, preserving other states. ``updates`` is uid→metadata.

    Malformed shared state is never overwritten silently; the caller reports it
    as pending. Return ``book``, ``requests``, ``states`` and ``issues``.
    """
    result, requests = copy.deepcopy(book), []
    read = read_sync_state(result)
    if read["issues"]:
        return {"book": result, "requests": [], "states": read["states"], "issues": read["issues"]}
    checked = {}
    for uid, value in updates.items():
        if not UID_PATTERN.fullmatch(uid):
            raise ValueError("同步基準需要有效的 ScheduleSyncId。")
        checked[uid] = _valid_state(value)
    if not checked:
        return {"book": result, "requests": [], "states": read["states"], "issues": []}
    sheet = next((s for s in result["sheets"] if s["properties"]["title"] == SYNC_STATE_TITLE), None)
    if sheet is None:
        used = {s["properties"]["sheetId"] for s in result["sheets"]}
        sid = secrets.randbelow(2**30) + 1
        while sid in used:
            sid = secrets.randbelow(2**30) + 1
        sheet = {"properties": {"sheetId": sid, "title": SYNC_STATE_TITLE, "hidden": True,
                 "gridProperties": {"rowCount": 1000, "columnCount": 3, "frozenRowCount": 1}},
                 "rows": [[{"userEnteredValue": {"stringValue": name}} for name in STATE_HEADERS]], "merges": []}
        result["sheets"].append(sheet)
        requests.append({"addSheet": {"properties": copy.deepcopy(sheet["properties"])}})
        requests.append({"updateCells": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                         "startColumnIndex": 0, "endColumnIndex": 3}, "rows": [{"values": copy.deepcopy(sheet["rows"][0])}],
                         "fields": "userEnteredValue"}})
    elif not sheet["properties"].get("hidden"):
        requests.append({"updateSheetProperties": {"properties": {"sheetId": sheet["properties"]["sheetId"], "hidden": True}, "fields": "hidden"}})
        sheet["properties"]["hidden"] = True
    cols = header_columns(sheet)
    positions = {text(row[cols["ScheduleSyncId"]]): index for index, row in enumerate(sheet["rows"][1:], 1)
                 if len(row) > cols["ScheduleSyncId"] and text(row[cols["ScheduleSyncId"]])}
    states = copy.deepcopy(read["states"])
    for uid, value in checked.items():
        if states.get(uid) == value:
            continue
        index = positions.get(uid, len(sheet["rows"]))
        if index >= sheet["properties"]["gridProperties"]["rowCount"]:
            extra = max(100, index + 1 - sheet["properties"]["gridProperties"]["rowCount"])
            requests.append({"appendDimension": {"sheetId": sheet["properties"]["sheetId"], "dimension": "ROWS", "length": extra}})
            sheet["properties"]["gridProperties"]["rowCount"] += extra
        row = [{} for _ in STATE_HEADERS]
        for label, raw in {"ScheduleSyncId": uid, "SchemaVersion": str(STATE_VERSION),
                           "StateJson": json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)}.items():
            row[cols[label]] = {"userEnteredValue": {"stringValue": raw}}
        if index == len(sheet["rows"]):
            sheet["rows"].append(row)
        else:
            sheet["rows"][index] = row
        positions[uid], states[uid] = index, value
        requests.append({"updateCells": {"range": {"sheetId": sheet["properties"]["sheetId"], "startRowIndex": index,
                         "endRowIndex": index + 1, "startColumnIndex": 0, "endColumnIndex": 3},
                         "rows": [{"values": copy.deepcopy(row)}], "fields": "userEnteredValue"}})
    return {"book": result, "requests": requests, "states": states, "issues": []}


def three_way_fields(remote, desired, baseline=None, *, selected_fields=None, clear_fields=()):
    """Return safe updates/conflicts plus a baseline for accepted fields only."""
    state = copy.deepcopy(baseline or {})
    written, sources = state.setdefault("fields", {}), state.setdefault("source_fields", {})
    updates, conflicts, unchanged, preserved = {}, {}, [], []
    selected = selected_fields if selected_fields is not None else desired.keys()
    for field in selected:
        if field not in MANAGED_FIELDS or field not in desired:
            continue
        value = desired[field]
        if not value and field not in clear_fields:
            continue  # An absent SOAP value is never a request to clear the sheet.
        current = remote.get(field, "")
        before_hash, wanted_hash = field_hash(field, current), field_hash(field, value)
        if before_hash == wanted_hash:
            written[field], sources[field] = wanted_hash, wanted_hash
            unchanged.append(field)
        elif sources.get(field) == wanted_hash:
            # A repeated source cannot undo a later manual edit, including blanking.
            preserved.append(field)
        elif (field in sources and written.get(field) == before_hash) or (field not in written and not current):
            updates[field] = value
            written[field], sources[field] = wanted_hash, wanted_hash
        else:
            conflicts[field] = {"before": current, "after": value, "reason": "刀表已有人工修改或沒有可信的同步基準。"}
    return {"updates": updates, "conflicts": conflicts, "unchanged": unchanged,
            "preserved": preserved, "baseline": state}


def route_merge(remote, desired, baseline=None, *, explicit=False):
    """Decide a route separately from cell fields and patient arrival time.

    Existing UID rows keep their sheet route unless the source route changed
    against a trusted prior route, or a person explicitly confirms a move.
    """
    state, current, wanted = copy.deepcopy(baseline or {}), _route(remote), _route(desired)
    old, old_source = state.get("route"), state.get("source_route")
    # Block UID adds ambiguity protection but a source need only specify date/hospital.
    equal = all(current[k] == wanted[k] for k in ("date", "hospital")) and (
        not wanted["block_sync_id"] or current["block_sync_id"] == wanted["block_sync_id"])
    if equal:
        state["route"], state["source_route"] = current, wanted
        return {"action": "keep", "baseline": state, "conflict": None}
    if explicit:
        state["route"], state["source_route"] = wanted, wanted
        return {"action": "move", "destination": wanted, "baseline": state, "conflict": None}
    if old_source and _route(old_source) == wanted:
        return {"action": "preserve", "baseline": state, "conflict": None}
    if old and old_source and _route(old) == current:
        state["route"], state["source_route"] = wanted, wanted
        return {"action": "move", "destination": wanted, "baseline": state, "conflict": None}
    return {"action": "pending", "baseline": state,
            "conflict": {"before": current, "after": wanted, "reason": "刀表位置已由人工變更，或缺少可信的原位置基準。"}}
