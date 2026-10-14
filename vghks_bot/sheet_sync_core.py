"""SOAP binding and three-way updates on top of the reviewable Sheets planner."""
from __future__ import annotations

import copy
import uuid

from .analysis_store import digest
from .google_sheets import SheetUncertain
from .settings import timestamp
from .sheet_identity import (
    MONTH_PATTERN,
    SYNC_ID_HEADER,
    SYNC_STATE_TITLE,
    header_columns,
    plan_identity_setup,
    schema_kind,
)
from .sheet_journal import OPD_KEY, confirm, reclaim
from .sheet_plan import (
    FUPlanner,
    Planner,
    cell_text,
    entries,
    fingerprint,
    matches,
    native_cell,
    patient_key,
    verify,
)
from .sheet_schema import HOSPITALS
from .sheet_service import account_hospital
from .sheet_sync_state import (
    field_hash,
    plan_sync_state,
    read_sync_state,
    route_merge,
    three_way_fields,
)
from .surgery_candidates import candidates, followup_candidates


def source_keys(row):
    # A single Arrange in an encounter remains the same source when its date or
    # clinical fields change. Multiple blocks require their explicit evidence.
    return list(dict.fromkeys(s.get("source_key") or digest([s["record_id"], s.get("field", "soap"), 0])
                             for s in row.get("sources", []) if s.get("block_count", 1) == 1))


def source_state(row, old=None):
    sources = row.get("sources", [])
    return {**copy.deepcopy(old or {}), "kind": "surgery", "updated_at": (old or {}).get("updated_at") or timestamp(),
            "mrn_hash": field_hash("mrn", row["fields"]["mrn"]),
            "source_keys": list(dict.fromkeys([*(old or {}).get("source_keys", []), *source_keys(row)])),
            "source_records": list(dict.fromkeys([*(old or {}).get("source_records", []),
                                                    *(s["record_id"] for s in sources)])),
            "source_version": digest([s.get("source_version", s.get("version", "")) for s in sources]),
            "source_bindings": [{k: s[k] for k in ("record_id", "field", "block_hash", "event_hash", "source_version", "block_count", "ordinal")
                                 if k in s} for s in sources]}


def pending(row, reason, proposal=None):
    return {"id": digest([row.get("id"), reason])[:32], "mrn": row["fields"]["mrn"],
            "name": row["fields"].get("name", ""), "reason": reason,
            "candidate_id": row.get("id", ""), "proposal": proposal}


def _setup_error(issues, fallback):
    protected = [issue["reason"] for issue in issues
                 if issue.get("code") == "protected_identity_range" and issue.get("reason")]
    return "；".join(protected[:8]) or fallback


def locate(book, uid):
    found = []
    for sheet in book["sheets"]:
        if MONTH_PATTERN.fullmatch(sheet["properties"]["title"]):
            found.extend(e for e in entries(sheet) if e["kind"] == "patient" and e["sync_id"] == uid)
    return found[0] if len(found) == 1 else None


def validate_fu_binding(book, uid, mrn_hash):
    """Validate a live FU tombstone without interpreting its editable Tag.

    Search actual UID cells as well as recognized schemas, so a renamed ID
    header or a row converted to a day header cannot masquerade as deletion.
    A genuinely missing row remains a permanent no-op binding.
    """
    found = []
    for sheet in book.get("sheets", []):
        if sheet["properties"]["title"] == SYNC_STATE_TITLE:
            continue
        for index, row in enumerate(sheet.get("rows", [])):
            found.extend((sheet, index, col) for col, cell in enumerate(row) if cell_text(cell).strip() == uid)
    if not found:
        return {"valid": True, "missing": True, "position": "", "expectation": None, "reason": ""}
    sheet, index, uid_col = found[0]
    position = f"{sheet['properties']['title']}!{index + 1}"

    def invalid(reason):
        return {"valid": False, "missing": False, "position": position, "expectation": None, "reason": reason}

    if len(found) != 1:
        return invalid("永久 FU 識別碼出現多次，請先核對；未新增追蹤。")
    kind, header_row = schema_kind(sheet)
    if kind not in {"fu", "archive"} or index <= header_row:
        return invalid("FU 綁定列已變為標題或其他分頁型態，請核對；未新增追蹤。")
    try:
        cols = header_columns(sheet, header_row)
    except ValueError:
        return invalid("FU 綁定列的欄位已變更或重複，請核對；未新增追蹤。")
    if cols.get(SYNC_ID_HEADER) != uid_col or "病歷號" not in cols:
        return invalid("FU 綁定列的識別碼或病歷號欄位已變更，請核對；未新增追蹤。")
    row = sheet["rows"][index]
    mrn = cell_text(row[cols["病歷號"]]).strip() if cols["病歷號"] < len(row) else ""
    archive_type = (cell_text(row[cols["ArchiveRowType"]]).strip().casefold()
                    if "ArchiveRowType" in cols and cols["ArchiveRowType"] < len(row) else "")
    if not mrn or patient_key(mrn) == "◆ 刀日" or archive_type not in {"", "patient"}:
        return invalid("FU 綁定列已變為刀日標題或無效患者列，請核對；未新增追蹤。")
    if field_hash("mrn", mrn) != mrn_hash:
        return invalid("FU 識別碼綁定的病人不符，請核對整列內容；未新增追蹤。")
    return {"valid": True, "missing": False, "position": position, "reason": "",
            "expectation": {"sheet": sheet["properties"]["title"], "row": index, "sync_id": uid,
                            "cells": [native_cell(cell) for cell in row]}}


def bind_candidate(book, row, states):
    keys = set(source_keys(row))
    if any(set(value.get("source_conflicted", [])).intersection(s["record_id"] for s in row.get("sources", []))
           for value in states.values()):
        return None, None, "來源曾變為多段手術標記，請人工重新確認來源綁定。"
    bound = [(uid, value) for uid, value in states.items()
             if value.get("kind") == "surgery" and (
                 keys.intersection(value.get("source_keys", [])) or any(
                     s.get("record_id") == b.get("record_id") and s.get("field") == b.get("field")
                     and (s.get("block_hash") and s.get("block_hash") == b.get("block_hash")
                          or s.get("event_hash") and s.get("event_hash") == b.get("event_hash"))
                     for s in row.get("sources", []) for b in value.get("source_bindings", [])))]
    if len(bound) > 1:
        return None, None, "同一來源綁定到多筆排程，請核對永久識別碼。"
    if bound:
        uid, state = bound[0]
        for source in row.get("sources", []):
            prior = [b for b in state.get("source_bindings", [])
                     if b.get("record_id") == source["record_id"] and b.get("field") == source.get("field")]
            if any(b.get("block_count", 1) > 1 for b in prior) and not any(
                    source.get("block_hash") == b.get("block_hash") or source.get("event_hash") == b.get("event_hash")
                    for b in prior):
                return None, state, "原來源含多筆手術，不能以單段序號重新配對，請人工核對。"
        if state.get("mrn_hash") != field_hash("mrn", row["fields"]["mrn"]):
            return None, state, "來源病人與排程 ID 綁定不符。"
        entry = locate(book, uid)
        if not entry:
            return None, state, "已綁定排程目前缺列、封存或位置無效，保留綁定並停止新增。"
        if field_hash("mrn", entry["fields"]["mrn"]) != state.get("mrn_hash"):
            return None, state, "排程 ID 綁定的病人不符，請核對整列貼上內容。"
        return entry, state, ""
    if not keys and any(s.get("block_count", 1) > 1 for s in row.get("sources", [])):
        return None, None, "同一來源有多段手術標記且尚未綁定，請先人工確認配對。"
    exact, related = matches(book, row["fields"])
    same = [item for item in exact + related if
            patient_key(item["entry"]["fields"]["mrn"]) == patient_key(row["fields"]["mrn"])
            and item["entry"]["fields"]["side"] == row["fields"]["side"]
            and item["entry"]["fields"]["procedure"].casefold() == row["fields"]["procedure"].casefold()]
    if len(exact) == 1:
        entry = exact[0]["entry"]
    elif len(same) == 1:
        entry = same[0]["entry"]
    elif exact or related:
        return None, None, "病人已有排程，無法唯一確認是否同一次手術。"
    else:
        return None, {}, ""
    old = states.get(entry["sync_id"], {})
    if (old.get("source_records") and not set(old["source_records"]).intersection(
            source["record_id"] for source in row.get("sources", []))
            and entry["fields"]["date"] != row["fields"]["date"]):
        return None, old, "不同就診來源與既有刀日不同，無法判定改期或另一次手術，請人工核對。"
    if old.get("mrn_hash") and old["mrn_hash"] != field_hash("mrn", row["fields"]["mrn"]):
        return None, old, "排程 ID 與來源病人不符。"
    if any(s.get("record_id") == b.get("record_id") and b.get("block_count", 1) > 1
           for s in row.get("sources", []) for b in old.get("source_bindings", [])):
        return None, old, "原來源含多筆手術，無法追溯改期，請重新確認綁定。"
    return entry, old, ""


def source_route(fields, state=None, entry=None, *, hospital_default=False):
    hospital = fields.get("hospital") or "高榮"
    if hospital_default and entry is not None:
        # Account campus is a default for new schedules, not evidence of a
        # hospital change. Date changes must retain the original source campus.
        hospital = (state or {}).get("source_route", {}).get("hospital") or entry["route"]["hospital"]
    return {"date": fields["date"], "hospital": hospital, "block_sync_id": ""}


def automatic_proposal(book, row, states):
    fields = row["fields"]
    proposal = {"id": row["id"], "fields": fields, "source_records": [s["record_id"] for s in row["sources"]]}
    if (row.get("cancelled_hint") or row.get("ambiguities") or row.get("differences")
            or not fields.get("date") or fields.get("side") not in {"OD", "OS", "OU"}
            or not fields.get("procedure")):
        return None, None, [pending(row, "來源包含取消、缺值或多義內容，請人工核對。", proposal)]
    entry, old, reason = bind_candidate(book, row, states)
    if reason:
        return None, None, [pending(row, reason, proposal)]
    state = source_state(row, old)
    if entry is None:
        if fields.get("hospital") not in HOSPITALS:
            return None, None, [pending(row, "帳號院區未對應高榮或聯醫，請手動選擇院區。", proposal)]
        proposal.update(new_event=True, selected_fields=list(fields),
                        destination={"date": fields["date"], "hospital": fields.get("hospital") or "高榮"})
        merged = three_way_fields({}, fields, state)
        state = merged["baseline"]
        state["source_route"] = proposal["destination"]
        return proposal, state, []
    proposal["target_sync_id"] = entry["sync_id"]
    if any(fields.get(k) and fields[k] != entry["fields"].get(k) for k in ("side", "procedure")):
        return None, None, [pending(row, "眼別或術式變更需人工核對。", proposal)]
    merged = three_way_fields(entry["fields"], fields, state)
    state = merged["baseline"]
    route = route_merge(entry["route"], source_route(fields, state, entry, hospital_default=row.get("hospital_default", False)), state)
    state = route["baseline"]
    # Record the observed source separately even when its route is not accepted.
    # A repeat of an old SOAP must never restore a manually moved row.
    state.setdefault("source_route", source_route(fields, state, entry, hospital_default=row.get("hospital_default", False)))
    proposal["selected_fields"] = list(merged["updates"])
    if route["action"] == "move":
        proposal["destination"] = route["destination"]
    issues = []
    if merged["conflicts"]:
        issues.append(pending(row, "保留人工修改欄位：" + "、".join(merged["conflicts"]), proposal))
    if route["action"] == "pending":
        issues.append(pending(row, "SOAP 刀日與人工位置衝突；一般欄位仍可獨立更新。", proposal))
    return proposal, state, issues


class SheetSyncCore:
    def __init__(self, analysis):
        self.analysis = analysis

    @staticmethod
    def read(client):
        return client.read_all() if callable(getattr(client, "read_all", None)) else client.read()

    @staticmethod
    def _manual_surgery_plan(book, spreadsheet_id, proposals, states):
        """Keep surgery row references tied to the original monthly snapshot."""
        for proposal in proposals:
            row = {"id": proposal["id"], "fields": proposal["fields"],
                   "sources": proposal.get("source_bindings") or [{"record_id": k} for k in proposal.get("source_records", [])]}
            uid = proposal.get("target_sync_id")
            entry = locate(book, uid) if uid else None
            old = states.get(uid, {}) if uid else {}
            if not entry and not proposal.get("new_event") and not proposal.get("target"):
                entry, old, reason = bind_candidate(book, row, states)
                if reason and old is not None:
                    raise ValueError(reason)
                if entry:
                    proposal["target_sync_id"] = entry["sync_id"]
            if old and old.get("mrn_hash") != field_hash("mrn", proposal["fields"]["mrn"]):
                raise ValueError("排程識別碼綁定的病人不符。")
            if entry and not proposal.get("destination"):
                route = route_merge(entry["route"], source_route(proposal["fields"], old, entry,
                                    hospital_default=proposal.get("hospital_default", False)), old)
                proposal["selected_fields"] = [k for k in proposal.get("selected_fields", proposal["fields"])
                                                if k not in {"date", "hospital"}]
                if route["action"] == "move":
                    proposal["destination"] = route["destination"]
        planner = Planner(book, spreadsheet_id, proposals)
        preview = planner.build()
        updates = {}
        for proposal, item in zip(proposals, preview["items"], strict=True):
            if item["status"] != "ready":
                continue
            uid = item["sync_id"]
            row = {"fields": proposal["fields"], "sources": proposal.get("source_bindings", [])}
            if not row["sources"]:
                row["sources"] = [{"record_id": key} for key in proposal.get("source_records", [])]
            previous = states.get(uid, {})
            if previous.get("mrn_hash") and previous["mrn_hash"] != field_hash("mrn", proposal["fields"]["mrn"]):
                raise ValueError("排程識別碼綁定的病人不符。")
            state = source_state(row, previous)
            state["source_conflicted"] = [key for key in state.get("source_conflicted", [])
                                          if key not in proposal.get("source_records", [])]
            selected = proposal.get("selected_fields", proposal["fields"].keys())
            for field in selected:
                if field in {"date", "hospital"}:
                    continue
                value = proposal["fields"].get(field, "")
                if value or field in proposal.get("clear_fields", []):
                    state.setdefault("fields", {})[field] = field_hash(field, value)
                    source_value = proposal.get("source_fields", proposal["fields"]).get(field, "")
                    if source_value:
                        state.setdefault("source_fields", {})[field] = field_hash(field, source_value)
            state["route"] = item["route"]
            source_fields = proposal.get("source_fields", proposal["fields"])
            if proposal.get("source_records") and source_fields.get("date"):
                state["source_route"] = source_route(source_fields, previous, locate(book, uid),
                    hospital_default=proposal.get("hospital_default", False))
            updates[uid] = state
        return planner.book, preview, updates

    @staticmethod
    def _manual_fu_state(proposal, previous=None):
        """Persist only source identifiers/versions, never FU SOAP paragraphs."""
        previous = previous or {}
        versions = {row["record_id"]: row.get("version", "") for row in proposal.get("source_versions", [])}
        sources = proposal.get("source_bindings") or [{"record_id": key} for key in proposal.get("source_records", [])]
        bindings = []
        for source in sources:
            binding = {key: source[key] for key in ("record_id", "field", "block_hash", "event_hash", "source_version", "block_count", "ordinal")
                       if key in source}
            version = source.get("source_version") or versions.get(source["record_id"])
            if version:
                binding["source_version"] = version
            bindings.append(binding)
        return {**copy.deepcopy(previous), "kind": "fu",
                "mrn_hash": field_hash("mrn", proposal["fields"]["mrn"]),
                "tag_hash": digest(proposal["fields"].get("tag") or "FU"),
                "updated_at": previous.get("updated_at") or timestamp(),
                "source_records": list(dict.fromkeys([*previous.get("source_records", []),
                    *proposal.get("source_records", []), *(row["record_id"] for row in bindings)])),
                "source_version": digest([row.get("source_version", "") for row in bindings]),
                "source_bindings": bindings}

    def build_manual(self, original, spreadsheet_id, proposals):
        """Build one atomic preview for monthly surgery updates and FU drafts.

        A permanent FU binding is a tombstone as well as a live-row identity.
        Tag edits, archiving and deletion cannot turn it into a new FU draft.
        """
        setup = plan_identity_setup(original)
        shared = read_sync_state(setup["book"])
        if setup["issues"] or shared["issues"]:
            raise ValueError(_setup_error(setup["issues"], "刀表識別碼、欄位或同步基準有異常，請先查看待核對。"))
        proposals = copy.deepcopy(proposals)
        if not proposals or len({row["id"] for row in proposals}) != len(proposals):
            raise ValueError("請提供候選，且同一批預覽不可重複選取相同候選。")
        surgery = [row for row in proposals if row.get("kind") != "fu"]
        followup = [row for row in proposals if row.get("kind") == "fu"]
        book, updates, previews, items, bound_expectations = setup["book"], {}, [], {}, []
        if surgery:
            book, preview, surgery_states = self._manual_surgery_plan(book, spreadsheet_id, surgery, shared["states"])
            previews.append(preview)
            updates.update(surgery_states)
            for item in preview["items"]:
                item["kind"] = "surgery"
                items[item["id"]] = item
        states = {**shared["states"], **updates}
        unbound, aliases, first_fu = [], {}, {}
        for proposal in followup:
            mrn = str(proposal["fields"].get("mrn") or "").strip()
            if not mrn:
                raise ValueError("FU 候選需要病歷號。")
            canonical = patient_key(mrn)
            if canonical in first_fu:
                aliases[proposal["id"]] = first_fu[canonical]
                continue
            first_fu[canonical] = proposal["id"]
            bound = [(uid, state) for uid, state in states.items()
                     if state.get("kind") == "fu" and state.get("mrn_hash") == field_hash("mrn", mrn)]
            if len(bound) > 1:
                items[proposal["id"]] = {"id": proposal["id"], "kind": "fu", "fields": proposal["fields"],
                    "status": "conflict", "reason": "此病歷號有多個永久 FU 綁定，請先核對；未新增追蹤。"}
            elif bound:
                uid, state = bound[0]
                binding = validate_fu_binding(book, uid, state["mrn_hash"])
                if not binding["valid"]:
                    items[proposal["id"]] = {"id": proposal["id"], "kind": "fu", "fields": proposal["fields"],
                        "status": "conflict", "sync_id": uid, "position": binding["position"], "reason": binding["reason"]}
                else:
                    items[proposal["id"]] = {"id": proposal["id"], "kind": "fu", "fields": proposal["fields"],
                        "status": "ready", "action": "keep", "changes": [], "sync_id": uid, "position": binding["position"],
                        "reason": "已有永久 FU 綁定；保留人工內容，改標籤、封存或刪除後皆不重複新增。"}
                    updates[uid] = state
                    if binding["expectation"]:
                        bound_expectations.append(binding["expectation"])
            else:
                unbound.append(proposal)
        if unbound:
            planner = FUPlanner(book, spreadsheet_id, unbound)
            preview = planner.build()
            previews.append(preview)
            book = planner.book
            by_id = {row["id"]: row for row in unbound}
            for item in preview["items"]:
                item["kind"] = "fu"
                items[item["id"]] = item
                if item["status"] != "ready":
                    continue
                uid, proposal = item["sync_id"], by_id[item["id"]]
                previous = states.get(uid, {})
                if previous.get("mrn_hash") and previous["mrn_hash"] != field_hash("mrn", proposal["fields"]["mrn"]):
                    raise ValueError("FU 識別碼綁定的病人不符。")
                if previous.get("kind") not in {None, "fu"}:
                    raise ValueError("FU 識別碼已綁定其他排程，請先核對。")
                updates[uid] = self._manual_fu_state(proposal, previous)
        for alias, primary in aliases.items():
            items[alias] = {**copy.deepcopy(items[primary]), "id": alias}
            if items[alias]["status"] == "ready":
                items[alias]["action"] = "keep"
                items[alias]["changes"] = []
                items[alias]["reason"] = "此病歷號已包含在同批 FU 預覽，僅新增或保留一列。"
        planned = plan_sync_state(book, updates)
        if planned["issues"]:
            raise ValueError("共享同步基準異常，未建立更新。")
        preview_id = previews[0]["id"] if previews else uuid.uuid4().hex
        metadata_id = previews[0]["metadata_id"] if previews else int(uuid.uuid4().hex[:7], 16)
        used_ids = {row.get("metadataId") for row in original.get("developerMetadata", [])}
        while metadata_id in used_ids:
            metadata_id = int(uuid.uuid4().hex[:7], 16)
        requests = setup["requests"] + [request for preview in previews for request in preview["requests"]
                                         if "createDeveloperMetadata" not in request] + planned["requests"]
        requests.append({"createDeveloperMetadata": {"developerMetadata": {"metadataId": metadata_id,
            "metadataKey": "vghks.opd.update", "metadataValue": preview_id,
            "location": {"spreadsheet": True}, "visibility": "DOCUMENT"}}})
        ordered = [items[row["id"]] for row in proposals]
        return {"id": preview_id, "created_at": timestamp(), "status": "preview", "spreadsheet_id": spreadsheet_id,
                "baseline": fingerprint(original), "items": ordered, "requests": requests,
                "structures": [value for preview in previews for value in preview["structures"]],
                "expectations": [value for preview in previews for value in preview["expectations"]] + bound_expectations,
                "metadata_id": metadata_id, "identity_issues": [],
                "mrns": sorted({row["fields"]["mrn"] for row in proposals}),
                "source_records": sorted({key for row in proposals for key in row.get("source_records", [])}),
                "source_versions": list({(source["record_id"], source.get("version", "")): {
                    "record_id": source["record_id"], "version": source.get("version", "")}
                    for row in proposals for source in row.get("source_versions", [])}.values()),
                "sync_states": {uid: planned["states"][uid] for uid in updates},
                "can_apply": bool(ordered) and all(item["status"] == "ready" for item in ordered)}

    def commit(self, client, original, planner, state, prefix, *, checkpoint=lambda: None):
        preview = planner.build()
        if not preview["can_apply"]:
            raise ValueError("目的刀日區塊不唯一或排程配對有衝突。")
        item = preview["items"][0]
        if item.get("route") and item.get("action") in {"insert", "move"}:
            state["route"] = item["route"]
        shared = plan_sync_state(planner.book, {item["sync_id"]: state})
        if shared["issues"]:
            raise ValueError("雲端同步基準無效，未寫入。")
        if not prefix and not shared["requests"] and all("createDeveloperMetadata" in r for r in preview["requests"]):
            return {**preview, "status": "unchanged", "message": "已核對，無需更新。"}
        library = getattr(self.analysis.store, "library", None)
        if library:
            for source in state.get("source_bindings", []):
                current = library.get_record(source["record_id"])
                if (current is None or source.get("source_version") and source["source_version"] !=
                        digest([current.get("soap", ""), current.get("soap_structure") or {}])):
                    raise ValueError("來源 SOAP 版本已在處理期間變更，停止此筆更新。")
        preview.update(requests=prefix + preview["requests"] + shared["requests"],
                       baseline=fingerprint(original), sync_states={item["sync_id"]: shared["states"][item["sync_id"]]},
                       automatic=True, status="applying")
        self.analysis.store.save_document("sheet_previews", preview)
        if fingerprint(self.read(client)) != preview["baseline"]:
            preview.update(status="stale", message="刀表出現並行異動，已停止此筆更新。")
            self.analysis.store.save_document("sheet_previews", preview)
            raise ValueError(preview["message"])
        try:
            checkpoint()
            client.write(preview["requests"])
            readback = self.read(client)
            ok = verify(readback, preview) == "verified" and all(
                read_sync_state(readback)["states"].get(uid) == value for uid, value in preview["sync_states"].items())
            preview.update(status="applied" if ok else "uncertain",
                           message="已寫入並核對。" if ok else "讀回資料有變動，請核對此批次。")
            if ok:
                confirm(preview, preview["id"])
        except SheetUncertain:
            preview.update(status="uncertain", message="寫入結果尚未確認，請先核對此批次。")
        finally:
            self.analysis.store.save_document("sheet_previews", preview)
        if preview["status"] == "applied":
            reclaim(client, readback, [(preview, OPD_KEY, [preview["id"]],
                                       lambda p: self.analysis.store.save_document("sheet_previews", p))])
        return preview

    def automatic(self, client, records, cohort_id, scan_state, *, default_hospital=None):
        from .sheet_plan import FUPlanner

        result = {"updated": 0, "pending": [], "preview_ids": []}
        hospital = account_hospital(self.analysis.app) if default_hospital is None else default_hospital
        rows = candidates(records, structured_only=True, strict=True, default_hospital=hospital)
        # Remember an ambiguous intermediate version even if no patient fields
        # can be written. Otherwise single A → A+B → single B could reuse key 0.
        multiple = {s["record_id"] for row in rows for s in row["sources"] if s.get("block_count", 1) > 1}
        if multiple:
            original = self.read(client)
            shared = read_sync_state(original)
            updates = {}
            for uid, state in shared["states"].items():
                overlap = multiple.intersection(state.get("source_records", []))
                if overlap - set(state.get("source_conflicted", [])):
                    updates[uid] = {**state, "source_conflicted": sorted(set(state.get("source_conflicted", [])) | overlap)}
            if updates and not shared["issues"]:
                planned = plan_sync_state(original, updates)
                key = uuid.uuid4().hex
                metadata_id = int(key[:7], 16)
                requests = planned["requests"] + [{"createDeveloperMetadata": {"developerMetadata": {
                    "metadataId": metadata_id, "metadataKey": "vghks.opd.update", "metadataValue": key,
                    "location": {"spreadsheet": True}, "visibility": "DOCUMENT"}}}]
                preview = {"id": key, "automatic": True, "spreadsheet_id": client.key, "status": "applying",
                           "created_at": timestamp(), "items": [], "structures": [], "expectations": [],
                           "mrns": [], "source_records": sorted(multiple), "can_apply": False,
                           "baseline": fingerprint(original), "metadata_id": metadata_id, "requests": requests,
                           "sync_states": {uid: planned["states"][uid] for uid in updates}}
                self.analysis.store.save_document("sheet_previews", preview)
                try:
                    if fingerprint(self.read(client)) != preview["baseline"]:
                        raise ValueError("刀表出現並行異動，請重新核對多段來源。")
                    scan_state.check_cancel()
                    client.write(requests)
                    readback = self.read(client)
                    checked = read_sync_state(readback)["states"]
                    ok = verify(readback, preview) == "verified" and all(checked.get(uid) == state for uid, state in preview["sync_states"].items())
                    preview["status"] = "applied" if ok else "uncertain"
                    if ok:
                        confirm(preview, key)
                except SheetUncertain:
                    preview["status"] = "uncertain"
                finally:
                    self.analysis.store.save_document("sheet_previews", preview)
                if preview["status"] == "applied":
                    reclaim(client, readback, [(preview, OPD_KEY, [key], lambda p: self.analysis.store.save_document("sheet_previews", p))])
        fu = followup_candidates(records, self.analysis.app.settings, default_hospital=hospital)
        seen_fu = set()
        for row in rows + fu:
            scan_state.check_cancel()
            is_fu = row in fu
            if is_fu and row["id"] in seen_fu:
                continue
            seen_fu.add(row["id"])
            original = self.read(client)
            setup = plan_identity_setup(original)
            book, shared = setup["book"], read_sync_state(setup["book"])
            if setup["issues"] or shared["issues"]:
                result["pending"].append(pending(row, _setup_error(setup["issues"], "刀表欄位、識別碼或共享基準有異常，未寫入。")))
                continue
            unresolved = [p for p in self.analysis.store.documents("sheet_previews")
                          if p.get("automatic") and p.get("status") in {"applying", "uncertain"}
                          and set(p.get("source_records", [])).intersection(row.get("source_records", [s["record_id"] for s in row["sources"]]))]
            if unresolved:
                result["pending"].append(pending(row, "此來源有尚未確認的舊批次，請先核對更新結果。"))
                continue
            try:
                if is_fu:
                    mrn_hash = field_hash("mrn", row["fields"]["mrn"])
                    bound = [(uid, s) for uid, s in shared["states"].items() if s.get("kind") == "fu" and s.get("mrn_hash") == mrn_hash]
                    if len(bound) > 1:
                        result["pending"].append(pending(row, "此病歷號有多個永久 FU 綁定，請先核對；未新增追蹤。"))
                        continue
                    if bound:
                        binding = validate_fu_binding(book, bound[0][0], mrn_hash)
                        if not binding["valid"]:
                            result["pending"].append(pending(row, binding["reason"]))
                        # A live correct row or a genuinely missing/archived
                        # row still has a permanent binding; never recreate it.
                        continue
                    state = {"kind": "fu", "mrn_hash": mrn_hash, "tag_hash": digest("FU"),
                             "source_records": row["source_records"], "updated_at": timestamp(),
                             "source_bindings": [{"record_id": r["id"], "source_version": digest([
                                 r.get("soap", ""), r.get("soap_structure") or {}])}
                                 for r in records if r["id"] in row["source_records"]]}
                    planner = FUPlanner(book, client.key, [row])
                else:
                    proposal, state, issues = automatic_proposal(book, row, shared["states"])
                    result["pending"].extend(issues)
                    if proposal is None:
                        continue
                    planner = Planner(book, client.key, [proposal])
                preview = self.commit(client, original, planner, state, setup["requests"], checkpoint=scan_state.check_cancel)
                result["preview_ids"].append(preview["id"])
                if preview["status"] == "applied":
                    result["updated"] += 1
                elif preview["status"] != "unchanged":
                    result["pending"].append(pending(row, preview["message"]))
            except ValueError as exc:
                result["pending"].append(pending(row, str(exc)))
        return result
