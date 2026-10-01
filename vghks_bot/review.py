"""Saved patient sets and task-scoped SOAP review."""
from __future__ import annotations

import copy
import hashlib
import threading
import uuid
from collections import defaultdict
from datetime import date

from vghks_sdk import SoapRecord, VisitCase
from vghks_sdk.core.errors import error_info
from vghks_sdk.identifiers import normalize_mrn

from .analysis_fetch import clean, model, validate_patient
from .analysis_store import digest
from .approvals import KINDS as APPROVAL_KINDS
from .bot_store import WorkbenchStore, fresh
from .bounded_search import search as regex_search
from .earnings import KINDS as EARNINGS_KINDS
from .library import current_cache, registration_id
from .progress import progress
from .scanner import Cancelled, ScanState, safe_failure
from .settings import parse_mrns, parse_range, timestamp, today
from .soap_data import snapshot as soap_snapshot
from .storage import StorageError
from .surgery_schedule import KIND as SURGERY_KIND
from .tags import classification

FOREGROUND_KINDS = {"numeric", "registrations", "resolve", "history", "earnings_options"} | (APPROVAL_KINDS - {"approval_refresh", "approval_sync"})


def case_key(case):
    return hashlib.sha256(repr(case.identity).encode()).hexdigest()[:24]


def failure_status(exc):
    return "forbidden" if error_info(exc).http_status == 403 else "error"


def checked_department_keyword(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100 or "\n" in value or "\r" in value:
        raise ValueError("科別篩選字串需為 1–100 字，且只能輸入一行。")
    return value.strip()


def checked_department_keywords(value):
    if not isinstance(value, list):
        raise ValueError("科別關鍵字格式不正確。")
    keywords, seen = [], set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError("科別關鍵字格式不正確。")
        if not item.strip():
            continue
        keyword = checked_department_keyword(item)
        if keyword.casefold() not in seen:
            keywords.append(keyword)
            seen.add(keyword.casefold())
            if len(keywords) > 10:
                raise ValueError("科別關鍵字最多十個。")
    if not keywords:
        raise ValueError("請輸入至少一個科別關鍵字。")
    return keywords


class Review:
    def __init__(self, app):
        self.app = app
        self.db = WorkbenchStore(app.store.library)
        from .review_history import ReviewHistory
        self.history = ReviewHistory(self)
        self.foreground = {}
        self.threads = {}
        self.lock = threading.RLock()
        self.db.get("preferences", "review", required=False) or self.db.save("preferences", {
            "id": "review", "review_options": {"department_keyword": "眼科"}})

    def preferences(self, values=None):
        current = self.db.get("preferences", "review")
        if values is None:
            return current
        options = copy.deepcopy(values.get("review_options", current.get("review_options", {})))
        if not isinstance(options, dict):
            raise ValueError("檢閱設定格式不正確。")
        enums = {"mode": {"latest", "registration"},
                 "cutoff_mode": {"today", "date"}, "retrieval": {"cache", "refresh", "force"}}
        if set(options) - set(enums) - {"department_keyword", "department_keywords", "cutoff"}:
            raise ValueError("檢閱設定欄位不正確。")
        for key, allowed in enums.items():
            if key in options and (not isinstance(options[key], str) or options[key] not in allowed):
                raise ValueError("檢閱設定選項不正確。")
        if "department_keywords" in options:
            options["department_keywords"] = checked_department_keywords(options["department_keywords"])
        else:
            options["department_keyword"] = checked_department_keyword(options.get("department_keyword", "眼科"))
        if options.get("cutoff_mode") == "date":
            parse_range({"start": options.get("cutoff"), "end": options.get("cutoff")})
        return self.db.save("preferences", {"id": "review", "review_options": options})

    def save_set(self, values):
        if values.get("id"):
            old = self.db.get("set", values["id"])
            name = str(values.get("name", "")).strip()
            if not name or len(name) > 100:
                raise ValueError("集合名稱需為 1–100 字。")
            return self.db.save("set", {**old, "name": name})
        members = {}
        requested_mrns = parse_mrns(values["mrns"], limit=20000) if values.get("mrns") else []
        source_set_id = values.get("source_set_id")
        if source_set_id:
            if not isinstance(source_set_id, str):
                raise ValueError("來源集合識別碼不正確。")
            source_set = self.db.get("set", source_set_id)
            requested = set(requested_mrns)
            for member in source_set["members"]:
                if member["mrn"] in requested:
                    members[member["mrn"]] = copy.deepcopy(member)

        def add(mrn, name="", registration=None, record=None, origin="manual"):
            mrn = normalize_mrn(mrn)
            profile = self.db.get("profile", mrn, required=False) or {}
            member = members.setdefault(mrn, {"mrn": mrn, "name": name or profile.get("name", ""),
                "sex": profile.get("sex", ""), "age": profile.get("age", ""), "birthday": profile.get("birthday", ""),
                "registrations": [], "source_records": [], "origins": []})
            if registration and registration not in member["registrations"]:
                member["registrations"].append(registration)
                for key in ("name", "sex", "age"):
                    member[key] = member[key] or registration.get(key, "")
            if record and record not in member["source_records"]:
                member["source_records"].append(record)
            if origin not in member["origins"]:
                member["origins"].append(origin)

        refs = values.get("registrations", [])
        if not isinstance(refs, list) or len(refs) > 20000:
            raise ValueError("門診選取格式不正確。")
        cached_days = {}
        for ref in refs:
            day = ref.get("day", "")
            if day not in cached_days:
                cache = self.app.store.library.cached_list(self.app.username, day, allow_stale=True)
                cached_days[day] = {registration_id(r): r for r in cache["rows"]} if cache else {}
            row = cached_days[day].get(ref.get("id"))
            if not row:
                raise ValueError("門診清單已變動，請重新載入。")
            add(row["mrn"], row.get("name", ""), registration=row, origin="registration")
        for mrn in requested_mrns:
            if mrn in members:
                continue
            profile = self.db.get("profile", mrn, required=False)
            if not profile or profile.get("status") != "resolved":
                raise ValueError("手動病人須先取得基本資料並核對。")
            add(mrn)
        source = values.get("source")
        if source == "tags":
            patients = self.app.tag_patients(values.get("filters", {})) if not values.get("all") else self.app.store.library.tag_patients(values.get("filters", {}), self.app.settings, all_results=True)
            selected = set(values.get("selected", []))
            for patient in patients["patients"]:
                if values.get("all") or patient["mrn"] in selected:
                    add(patient["mrn"], patient["name"], origin="tag")
        elif source == "library":
            records = self.app.store.library.search(values.get("filters", {}), self.app.settings, all_results=True)["records"]
            if values.get("filters", {}).get("task_id"):
                source_task = values["filters"]["task_id"]
                self.db.get("task", source_task)
                allowed = {r["id"] for p in self.db.items(source_task) for r in p.get("records", [])}
                records = [r for r in records if r["id"] in allowed]
            selected = set(values.get("selected", []))
            for record in records:
                if values.get("all") or record["id"] in selected:
                    add(record["mrn"], record.get("name", ""), record=record["id"], origin="library")
        elif source == "review":
            result = self.results({"id": values.get("task_id"), **values.get("filters", {})})
            selected = set(values.get("selected", []))
            for patient in result["patients"]:
                if values.get("all") or patient["mrn"] in selected:
                    add(patient["mrn"], patient.get("name", ""), origin="review")
                    for registration in patient.get("registrations", []):
                        add(patient["mrn"], registration=registration, origin="registration")
                    for record in patient.get("records", []):
                        add(patient["mrn"], record=record["id"], origin="review")
        if not 1 <= len(members) <= 20000:
            raise ValueError("請選取 1–20000 位已確認的病人。")
        name = str(values.get("name") or f"{today().isoformat()} 病人集合 · {len(members)} 位")[:100]
        return self.db.save("set", {"name": name, "members": list(members.values()), "created_at": timestamp(),
            "source_range": values.get("source_range", {}), "account_id": self.app.account_id})

    def start(self, values, *, foreground=False):
        with self.app.root.lock:
            return self._start(values, foreground=foreground)

    def _start(self, values, *, foreground=False):
        if self.app.root.read_only:
            raise ValueError("唯讀資料庫不能啟動網路任務。")
        if self.app.closing:
            raise ValueError("程式正在結束。")
        if not self.app.gateway.online:
            raise ValueError("請先登入此帳號再啟動網路任務。")
        if values.get("resume"):
            task = self.db.get("task", values["resume"])
            if task["status"] in {"queued", "running", "cancelling"}:
                raise ValueError("此任務仍在執行。")
            if task["kind"] in {"approval_refresh", "approval_sync"}:
                self.app.approvals.tracker.require_idle()
            elif task["kind"] in EARNINGS_KINDS:
                self.app.earnings.require_idle()
            elif task["kind"] == SURGERY_KIND:
                self.app.surgery_schedule.require_idle()
            if task["kind"] in FOREGROUND_KINDS:
                foreground = True
            task.pop("retry_only", None)
            if "retry_only" in values:
                retry = values["retry_only"]
                known = task.get("identifiers", []) if task["kind"] == "resolve" else [m["mrn"] for m in task.get("members", [])]
                if task["kind"] not in {"resolve", "review"} or not isinstance(retry, list) or not retry or any(i not in known for i in retry):
                    raise ValueError("請選擇原任務中的識別碼重試。")
                task["retry_only"] = retry
        else:
            kind = values.get("kind", "review")
            if kind not in {"review", "resolve", "numeric", "registrations", "history", SURGERY_KIND} | APPROVAL_KINDS | EARNINGS_KINDS:
                raise ValueError("工具類型不正確。")
            task = {"id": uuid.uuid4().hex, "kind": kind, "created_at": timestamp(), "account_id": self.app.account_id,
                    "force": values.get("force") is True, "refresh": values.get("refresh") is True}
            if kind == "review":
                group = self.db.get("set", values.get("set_id"))
                mode = values.get("mode", "latest")
                if mode not in {"latest", "registration"}:
                    raise ValueError("SOAP 模式不正確。")
                cutoff = values.get("cutoff") or today().isoformat()
                parse_range({"start": cutoff, "end": cutoff})
                preferred = self.preferences().get("review_options", {})
                if "department_keywords" in values:
                    department = {"department_keywords": checked_department_keywords(values["department_keywords"])}
                elif "department_keyword" in values:
                    department = {"department_keyword": checked_department_keyword(values["department_keyword"])}
                elif "department_keywords" in preferred:
                    department = {"department_keywords": checked_department_keywords(preferred["department_keywords"])}
                else:
                    department = {"department_keyword": checked_department_keyword(preferred.get("department_keyword", "眼科"))}
                task.update(name=group["name"], members=copy.deepcopy(group["members"]), set_id=group["id"],
                    **department, mode=mode, cutoff=cutoff,
                    categories=self.app.settings.public()["categories"])
            elif kind in APPROVAL_KINDS:
                foreground = kind in FOREGROUND_KINDS
                task = self.app.approvals.prepare(task, values)
            elif kind in EARNINGS_KINDS:
                foreground = kind in FOREGROUND_KINDS
                task = self.app.earnings.prepare(task, values)
            elif kind == SURGERY_KIND:
                task = self.app.surgery_schedule.prepare(task, values)
            elif kind == "resolve":
                foreground = True
                input_kind = values.get("identifier_kind", "mrn")
                if input_kind not in {"mrn", "national_id", "auto"}:
                    raise ValueError("請選擇病歷號或身分證字號。")
                identifiers = parse_mrns(values.get("identifiers"))
                task.update(name="病人基本資料", identifier_kind=input_kind, identifiers=identifiers)
            elif kind == "history":
                task.update(self.history.prepare(values))
                foreground = True
            else:
                mrn = normalize_mrn(values.get("mrn"))
                task.update(name="本次數值報告" if kind == "numeric" else "掛號紀錄", mrn=mrn)
                if kind == "numeric":
                    record = self.app.library_record(values.get("record_id"))
                    if record["mrn"] != mrn:
                        raise ValueError("病人與就診不符。")
                    task["record_id"] = record["id"]
                foreground = True
        task.update(status="queued", message="等待處理", error="", finished_at="")
        task["progress"] = self.task_progress(task)
        self.db.save("task", task)
        if task["kind"] in EARNINGS_KINDS:
            self.app.earnings.monitor.started(task)
        self.app.approvals.tracker.started(task)
        state = ScanState(today(), today(), id=task["id"], kind="bot", account_id=self.app.account_id,
                          account=self.app.username, status="queued")
        if foreground:
            with self.lock:
                self.foreground[task["id"]] = state
                thread = threading.Thread(target=self._foreground, args=(state, task), daemon=True)
                self.threads[task["id"]] = thread
                thread.start()
        else:
            with self.app.lock:
                self.app.states[state.data["id"]] = state
                self.app.idle.clear()
                self.app.queue.put((state, self.app.settings, {"bot_task": task}))
        return {"task_id": task["id"]}

    def _foreground(self, state, task):
        try:
            with self.app.gateway.foreground(), self.app.gateway.task_context(task["id"], task["kind"]):
                self.execute(state, task)
        except Cancelled:
            state.update(status="cancelled", message="已暫停，待續跑")
        except Exception as exc:
            message, code = safe_failure(exc)
            state.issue("查詢", message, code=code)
            state.update(status="failed", message=message)
        finally:
            try:
                self.finish(state)
            except StorageError:
                self.app._storage_failure(state)
            finally:
                with self.lock:
                    self.foreground.pop(task["id"], None)
                    self.threads.pop(task["id"], None)

    def finish(self, state):
        task = self.db.get("task", state.data["id"], required=False)
        if task:
            status = state.data["status"]
            if status in {"cancelled", "cancelling"} or not self.app.gateway.online and status == "failed":
                status = "paused"
            self.db.save("task", {**task, "status": status, "message": state.data.get("message", ""),
                                 "error_code": next((i["code"] for i in reversed(state.data.get("issues", [])) if i.get("code")), ""),
                                 "finished_at": state.data.get("finished_at") or timestamp(),
                                 "progress": self.task_progress(task, stage=state.data.get("message", ""))})

    def task_progress(self, task, *, stage=None):
        items = self.db.items(task["id"])
        kind, unit = task["kind"], "項"
        total = 1
        counted = [i for i in items if not i.get("processing")]
        if kind in {"review", "resolve"}:
            total, unit = len(task.get("members", task.get("identifiers", []))), "位病人"
        elif kind == "approval_refresh":
            total, unit = len(task["references"]), "件"
        elif kind == "approval_sync":
            total = (task["case_total"] + task["order_total"]
                     if "case_total" in task and "order_total" in task else None)
            unit = "項作業"
            counted = [i for i in counted if i.get("checked_at") == task.get("sync_checked_at")]
        elif kind == "approval_case":
            total = len(task["parts"])
        elif kind == "earnings_options":
            total, unit = len(task["report_kinds"]), "種報表"
            counted = [i for i in items if i.get("status") == "ready"]
        elif kind == "earnings_capture":
            total, unit = len(task["reports"]), "份報表"
            if task["all_available"] and set(task.get("discovered", [])) != set(task["report_kinds"]):
                total = None
            counted = [i for i in items if "period" in i]
        elif kind == "history" and task.get("resource") == "scans" and task.get("backfill"):
            unit = "次眼科就診"
            try:
                total = len(self.app.scans._eye_cases(task["mrn"]))
            except (ValueError, TypeError, KeyError):
                total = None
            counted = [i for i in items if i.get("reference")]
        failed_items = counted if kind == "approval_sync" else items
        return progress(len(counted), total, unit=unit,
                        failed=sum(not i.get("processing") and i.get("status") in {"error", "partial", "forbidden"} for i in failed_items),
                        stage=task.get("message", "") if stage is None else stage)

    def report(self, state, task, *, stage):
        value = self.task_progress(task, stage=stage)
        # Preserve dynamically discovered months and other durable task fields.
        current = self.db.get("task", task["id"])
        self.db.save("task", {**current, "progress": value, "message": stage})
        state.update(progress=value, message=stage)

    def stop(self, key):
        with self.lock:
            state = self.foreground.get(key)
            if state:
                state.cancel.set()
            else:
                self.app.stop(key)
        return {"ok": True}

    def execute(self, state, task):
        state.check_cancel()
        task = self.db.save("task", {**task, "status": "running", "message": "正在取得資料"})
        state.update(status="running")
        self.report(state, task, stage="正在取得資料")
        if task["kind"] == "review":
            for member in task["members"]:
                state.check_cancel()
                if task.get("retry_only") and member["mrn"] not in task["retry_only"]:
                    continue
                old = self.db.item(task["id"], member["mrn"])
                if old and old["status"] not in {"error", "partial", "forbidden"}:
                    continue
                self.report(state, task, stage="檢閱病人 " + member["mrn"])
                try:
                    result = self.patient(task, member, state)
                except (Cancelled, StorageError):
                    raise
                except Exception as exc:
                    if not self.app.gateway.online:
                        raise
                    message, code = safe_failure(exc)
                    status = failure_status(exc)
                    result = {**member, "status": status, "message": "此帳號沒有讀取權限" if status == "forbidden" else message, "code": code, "records": []}
                    if code == "PRQ_CASE_PATIENT_MISMATCH":
                        result["message"] = "就診索引的病人身分不符；重新核對後仍未確認。可單獨重試，或到爬蟲紀錄查看 DEBUG 資訊。"
                self.db.item(task["id"], member["mrn"], result)
                self.report(state, task, stage="病歷已處理")
        elif task["kind"] in APPROVAL_KINDS:
            self.app.approvals.execute(state, task)
        elif task["kind"] in EARNINGS_KINDS:
            self.app.earnings.execute(state, task)
        elif task["kind"] == SURGERY_KIND:
            self.app.surgery_schedule.execute(state, task)
        elif task["kind"] == "resolve":
            for identifier in task["identifiers"]:
                state.check_cancel()
                if task.get("retry_only") and identifier not in task["retry_only"]:
                    continue
                old = self.db.item(task["id"], identifier)
                if old and old["status"] == "resolved":
                    continue
                self.report(state, task, stage="核對病人基本資料")
                input_kind = task["identifier_kind"]
                if input_kind == "auto":
                    input_kind = "national_id" if identifier[:1].isascii() and identifier[:1].isalpha() else "mrn"
                try:
                    mrn = self.app.gateway.resolve_identity(identifier) if input_kind == "national_id" else normalize_mrn(identifier)
                    result = self.profile(mrn, force=task["force"])
                except StorageError:
                    raise
                except Exception as exc:
                    if not self.app.gateway.online:
                        raise
                    message, code = safe_failure(exc)
                    result = {"status": "error", "message": message, "code": code}
                self.db.item(task["id"], identifier, {**result, "input": identifier, "identifier_kind": input_kind})
                self.report(state, task, stage="病人資料已處理")
        elif task["kind"] == "history":
            state.check_cancel()
            result = self.history.execute(task, state)
            self.db.item(task["id"], task["mrn"], result)
        else:
            state.check_cancel()
            result = self.extension(task)
            self.db.item(task["id"], task["mrn"], result)
        state.check_cancel()
        partial = any(r.get("status") in {"error", "partial", "forbidden"} for r in self.db.items(task["id"]))
        self.report(state, task, stage="部分資料未取得，可續跑" if partial else "完成，資料已保存")
        state.update(status="partial" if partial else "completed", message="部分資料未取得，可續跑" if partial else "完成，資料已保存")

    def profile(self, mrn, *, force=False):
        cached = self.db.get("profile", mrn, required=False)
        if cached and not force and fresh(cached["updated_at"], 86400):
            return cached
        with self.app.sdk_factory(self.app.settings) as sdk:
            value = clean(sdk.patients.get_demographics(mrn))
        validate_patient(value, mrn)
        if value.get("mrn") != mrn or not value.get("name", "").strip():
            raise ValueError("未取得可確認的病人基本資料。")
        birthday = value.get("birthday", "")
        return self.db.save("profile", {"id": mrn, "mrn": mrn, "status": "resolved", "name": value["name"],
            "sex": value.get("sex", ""), "birthday": birthday, "age": value.get("age", ""),
            "mobile_phone": value.get("mobile_phone", ""), "home_phone": value.get("home_phone", "")})

    def cases(self, mrn, force=False):
        cached = self.db.get("visits", mrn, required=False)
        if cached and not force and fresh(cached["updated_at"], 300):
            return [model(VisitCase, c) for c in cached["cases"]], cached["updated_at"]
        with self.app.sdk_factory(self.app.settings) as sdk:
            cases = sdk.records.get_visit_cases(mrn)
        if not isinstance(cases, list) or any(not isinstance(c, VisitCase) or c.patient_mrn != mrn for c in cases):
            raise ValueError("就診索引病人不符或格式不正確。")
        saved = self.db.save("visits", {"id": mrn, "cases": clean(cases)})
        return cases, saved["updated_at"]

    def patient(self, task, member, state):
        mrn = member["mrn"]
        result = {**member, "records": [], "attempts": [], "status": "ready", "message": ""}
        if task["mode"] == "registration" and not member["registrations"]:
            return {**result, "status": "no_source", "message": "未指定掛號來源，請改用最新 SOAP 或從門診清單選取。"}
        if task["mode"] == "registration" and all(r["visit_date"] > today().isoformat() for r in member["registrations"]):
            return {**result, "status": "future", "message": "尚未到診，可使用最新 SOAP 模式。"}
        checkpoint = self.db.item(task["id"], mrn) or {}
        if "index_cases" in checkpoint:
            cases = [model(VisitCase, c) for c in checkpoint["index_cases"]]
            checked = checkpoint["index_checked_at"]
        else:
            cases, checked = self.cases(mrn, task["force"] or task["refresh"])
        result["index_cases"] = clean(cases)
        result["index_checked_at"] = checked
        self.db.item(task["id"], mrn, {**result, "status": "partial", "processing": True})
        adopted = {r["id"]: r for r in checkpoint.get("records", [])}
        keyword = task.get("department_keyword")
        keywords = task.get("department_keywords")
        needles = keywords if keywords is not None else [keyword] if keyword is not None else []
        legacy_all = not needles and task.get("department_filter", "same") == "all"
        def same_section(case):
            if needles:
                name, code = case.section_name.casefold(), case.section_code.casefold()
                return any(needle.casefold() in name or needle.casefold() in code for needle in needles)
            if legacy_all:
                return True
            group = task["department"]
            return case.section_code.strip() in group["codes"] or case.section_name.strip() in group["names"]
        eligible = [c for c in cases if c.case_type == "O" and c.visit_date and c.visit_date.isoformat() <= task["cutoff"]]
        ambiguous = any(c.case_type == "O" and (not c.visit_date or not legacy_all and not c.section_code and not c.section_name) for c in cases)
        if task["mode"] == "registration":
            keys = {(r["visit_date"], r["section_code"]) for r in member["registrations"] if r["visit_date"] <= today().isoformat()}
            eligible = [c for c in eligible if (c.visit_date.isoformat(), c.section_code) in keys]
        if needles or task["mode"] != "registration" and not legacy_all:
            eligible = [c for c in eligible if same_section(c)]
        if not eligible:
            message = ("索引日期或科別不完整，無法判定" if ambiguous else
                "查無符合科別篩選的掛號當次就診" if task["mode"] == "registration" and needles else
                "查無掛號當次就診" if task["mode"] == "registration" else
                "查無科別含任一關鍵字（" + "、".join(needles) + "）的門診歷史（可能初診）" if keywords is not None else
                f"查無科別含「{keyword}」的門診歷史（可能初診）" if keyword is not None else
                "查無門診歷史（可能初診）" if legacy_all else "查無同科歷史（可能初診）")
            return {**result, "status": "unknown" if ambiguous else "no_visit", "message": message}
        by_day = defaultdict(list)
        for case in {c.identity: c for c in eligible}.values():
            by_day[case.visit_date].append(case)
        settings = self.app.settings.update({"categories": task["categories"]})
        for day in sorted(by_day, reverse=True):
            found_day = False
            for case in by_day[day]:
                state.check_cancel()
                key = case_key(case)
                attempt = {"date": day.isoformat(), "case_no": case.case_no, "section": case.section_name, "record_id": key}
                if self.db.deleted_since(key, task["created_at"]):
                    result["attempts"].append({**attempt, "status": "deleted"})
                    continue
                try:
                    if key in adopted:
                        result["records"].append(adopted[key])
                        result["attempts"].append({**attempt, "status": "ready"})
                        found_day = True
                        self.db.item(task["id"], mrn, {**result, "status": "partial", "processing": True})
                        continue
                    record = None if task["force"] else self.app.store.library.get_record(key)
                    cache_hit = bool(record and current_cache(day.isoformat(), record["updated_at"]))
                    if not cache_hit:
                        with self.app.sdk_factory(self.app.settings) as sdk:
                            soap = sdk.records.get_soap(case)
                        if (not isinstance(soap, SoapRecord) or soap.case.identity != case.identity
                                or soap.case.patient_mrn != mrn):
                            from vghks_sdk import ParseError

                            raise ParseError("SOAP identity mismatch", code="BOT_SOAP_CASE_MISMATCH")
                        if not soap.full_text.strip():
                            result["attempts"].append({**attempt, "status": "empty", "message": "SOAP 尚無內容"})
                            continue
                        record = {"id": key, "mrn": mrn, "name": member.get("name", ""), "sex": member.get("sex", ""),
                            "age": member.get("age", ""), "date": day.isoformat(), "section": case.section_name,
                            "section_code": case.section_code, "case_no": case.case_no,
                            "source_mrn": case.mrn, "case_index": case.index, "doctor": case.doctor_name,
                            "doctor_card": case.doctor_card, **soap_snapshot(soap)}
                        self.app.store.library.save_record(record, self.app.username, task["id"])
                        record = self.app.store.library.get_record(key)
                    record = {k: v for k, v in record.items() if k not in {"versions", "matches", "manual_tags"}}
                    record.update(case=clean(case), cached=cache_hit,
                                  version=digest([record["soap"], record.get("soap_structure")]),
                                  **classification(record, settings))
                    result["records"].append(record)
                    result["attempts"].append({**attempt, "status": "ready"})
                    found_day = True
                    # Checkpoint after each SOAP, including all same-day entries.
                    self.db.item(task["id"], mrn, {**result, "status": "partial", "processing": True})
                except (Cancelled, StorageError):
                    raise
                except Exception as exc:
                    if not getattr(self.app.gateway.local, "diagnostic_emitted", False):
                        self.app.diagnostics.failure(exc, task_id=task["id"], mrn=mrn,
                            phase="soap_validation", visit=attempt)
                    if not self.app.gateway.online:
                        raise
                    message, code = safe_failure(exc)
                    status = failure_status(exc)
                    result["attempts"].append({**attempt, "status": status, "message": "此帳號沒有讀取權限" if status == "forbidden" else message, "code": code})
            if found_day and task["mode"] == "latest":
                break
        failed = any(a["status"] in {"error", "forbidden"} for a in result["attempts"])
        result["status"] = "partial" if failed else "ready" if result["records"] else "missing"
        result["fallback"] = bool(result["records"] and result["records"][0]["date"] < max(by_day).isoformat())
        result["message"] = "較新 SOAP 未取得，顯示上一份可讀病歷" if result["fallback"] else "" if result["records"] else "有就診，但未取得非空 SOAP"
        return result

    def extension(self, task):
        mrn, kind = task["mrn"], task["kind"]
        key = task.get("record_id", mrn)
        cached = self.db.get(kind, key, required=False)
        if kind == "numeric":
            record = self.app.library_record(key)
            if self.db.deleted_since(key, task["created_at"]):
                return {"status": "deleted", "record_id": key}
            valid = bool(cached and current_cache(record["date"], cached["updated_at"]))
        else:
            valid = bool(cached and fresh(cached["updated_at"], 60))
        if valid and not task["force"]:
            return {**cached, "cached": True}
        with self.app.sdk_factory(self.app.settings) as sdk:
            if kind == "numeric":
                source_mrn = record.get("source_mrn") or mrn
                case = VisitCase(source_mrn, date.fromisoformat(record["date"]), "O",
                                 record["case_no"], record["section_code"], record["section"],
                                 index=record.get("case_index"), lookup_mrn=mrn)
                report = sdk.records.get_numeric_report(case)
                if report.case.identity != case.identity or report.case.patient_mrn != mrn:
                    raise ValueError("數值報告就診不符。")
                payload = clean(report)
            else:
                payload = clean(sdk.patients.get_registration_history(mrn))
                validate_patient(payload, mrn)
        return self.db.save(kind, {"id": key, "mrn": mrn, "record_id": key if kind == "numeric" else "", "status": "ready", "payload": payload, "cached": False, "queried_on": today().isoformat()})

    def task(self, key):
        task = self.db.get("task", key)
        items = self.db.items(key)
        value = self.task_progress(task)
        return {**task, "items": items, "progress": value, "done": value["done"], "total": value["total"]}

    def results(self, values):
        task = self.db.get("task", values.get("id"))
        if task["kind"] != "review":
            raise ValueError("此任務不是病歷檢閱。")
        saved = {p["mrn"]: p for p in self.db.items(task["id"])}
        patients = [saved.get(m["mrn"], {**m, "status": "pending", "records": [], "message": "待處理"}) for m in task["members"]]
        manual = self.app.store.library.manual_tags(self.app.settings)
        query, mode, tag = values.get("q", ""), values.get("search_mode", "text"), values.get("tag", "")
        if not isinstance(query, str) or len(query) > 500 or mode not in {"text", "regex"}:
            raise ValueError("搜尋格式不正確。")
        texts, refs = [], []
        for patient in patients:
            patient["manual_tags"] = manual.get(patient["mrn"], [])
            for record in patient.get("records", []):
                texts.append(record["soap"])
                refs.append(record)
        matches = regex_search(query, texts) if query and mode == "regex" else {}
        if query and mode == "text":
            for index, text in enumerate(texts):
                offset = text.casefold().find(query.casefold())
                if offset >= 0:
                    matches[index] = [[offset, offset + len(query)]]
        for index, record in enumerate(refs):
            record["highlights"] = matches.get(index, [])
            record["search_hit"] = not query or index in matches
        counts = defaultdict(int)
        filtered = []
        for patient in patients:
            ids = {t["id"] for t in patient["manual_tags"]} | {m["category"] for r in patient.get("records", []) for m in r.get("matches", [])}
            for tag_id in ids:
                counts[tag_id] += 1
            if tag and tag not in ids:
                continue
            if query and not any(r["search_hit"] for r in patient.get("records", [])):
                continue
            filtered.append(patient)
        return {"task": task, "patients": filtered, "total": len(filtered), "all_total": len(task["members"]), "tag_counts": dict(counts),
                "scope_issue_count": sum(bool(record.get("tag_scope_issues")) for record in refs)}

    def _note_task(self, values):
        task = self.db.get("task", values.get("task_id"))
        if task["kind"] != "review":
            raise ValueError("此任務不是病歷檢閱。")
        return task

    def _registration_sequence(self, member):
        sources = [row for row in member.get("registrations", []) if isinstance(row, dict) and row.get("visit_date")]
        if not sources:
            return ""
        latest = max(row["visit_date"] for row in sources)
        sources = [row for row in sources if row["visit_date"] == latest]
        if len(sources) != 1:
            return ""
        source = sources[0]
        if (source.get("mrn") and source["mrn"] != member["mrn"]) or not source.get("room") or not (
            source.get("section_code") or source.get("section_name")
        ):
            return ""
        if source.get("sequence_no"):
            return str(source["sequence_no"])
        saved = self.db.get("registrations", member["mrn"], required=False) or {}
        numbers = set()
        history = saved.get("payload")
        if not isinstance(history, list):
            return ""
        for row in history:
            if not isinstance(row, dict):
                continue
            if row.get("mrn") and row["mrn"] != member["mrn"]:
                continue
            if row.get("visit_date") != latest or row.get("room") != source["room"]:
                continue
            source_code, row_code = source.get("section_code"), row.get("section_code")
            if source_code and row_code:
                same_section = source_code == row_code
            else:
                same_section = bool(source.get("section_name") and source["section_name"] == row.get("section_name"))
            if same_section and row.get("sequence_no"):
                numbers.add(str(row["sequence_no"]))
        return next(iter(numbers)) if len(numbers) == 1 else ""

    def read_notes(self, values):
        task = self._note_task(values)
        patients = []
        for member in task["members"]:
            mrn = member["mrn"]
            note = self.db.get("review_note", task["id"] + ":" + mrn, required=False) or {}
            patients.append({"mrn": mrn, "name": member.get("name", ""),
                "sequence_no": self._registration_sequence(member), "text": note.get("text", ""),
                "updated_at": note.get("updated_at", "")})
        return {"task_id": task["id"], "patients": patients}

    def save_note(self, values):
        task = self._note_task(values)
        mrn = normalize_mrn(values.get("mrn", ""))
        if mrn not in {member["mrn"] for member in task["members"]}:
            raise ValueError("病人不在此檢閱任務中。")
        text = values.get("text")
        if not isinstance(text, str) or len(text) > 2000:
            raise ValueError("備註需為 2,000 字以內的文字。")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        key = task["id"] + ":" + mrn
        if not text.strip():
            self.db.delete("review_note", key)
            return {"task_id": task["id"], "mrn": mrn, "text": "", "updated_at": ""}
        saved = self.db.save("review_note", {"id": key, "text": text})
        return {"task_id": task["id"], "mrn": mrn, "text": saved["text"], "updated_at": saved["updated_at"]}

    def reclassify(self, key):
        task = self.db.get("task", key)
        if task["status"] in {"running", "queued"}:
            raise ValueError("請等待目前任務完成，再套用新規則。")
        for patient in self.db.items(key):
            for record in patient.get("records", []):
                record.update(classification(record, self.app.settings))
            self.db.item(key, patient["mrn"], patient)
        self.db.save("task", {**task, "categories": self.app.settings.public()["categories"]})
        return {"ok": True}

    def cohort(self, values):
        group = self.db.get("set", values.get("set_id"))
        members = [{"mrn": m["mrn"], "name": m["name"], "account_id": self.app.account_id,
                    "accounts": [self.app.username], "source_records": m["source_records"],
                    "source_registrations": m["registrations"], "origins": m["origins"]} for m in group["members"]]
        value = {"id": uuid.uuid4().hex, "name": group["name"], "members": members, "created_at": timestamp(),
                 "source_set": group["id"]}
        self.app.analysis.store.save_document("analysis_cohorts", value)
        return value
