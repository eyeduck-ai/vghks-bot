"""Saved patient sets and task-scoped, latest-specialty SOAP review."""
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

DEFAULT_DEPARTMENTS = [{"id": "oph", "name": "眼科", "codes": [], "names": [
    "眼科上午", "眼科下午", "眼科", "眼科約診", "眼科約診上午", "眼科約診下午"]}]
FOREGROUND_KINDS = {"numeric", "registrations", "resolve", "earnings_options"} | (APPROVAL_KINDS - {"approval_refresh", "approval_sync"})


def case_key(case):
    return hashlib.sha256(repr(case.identity).encode()).hexdigest()[:24]


def failure_status(exc):
    return "forbidden" if error_info(exc).http_status == 403 else "error"


class Review:
    def __init__(self, app):
        self.app = app
        self.db = WorkbenchStore(app.store.library)
        self.foreground = {}
        self.threads = {}
        self.lock = threading.RLock()
        self.db.get("preferences", "review", required=False) or self.db.save("preferences", {
            "id": "review", "departments": DEFAULT_DEPARTMENTS, "default_department": "oph"})

    def preferences(self, values=None):
        current = self.db.get("preferences", "review")
        if values is None:
            return current
        groups = values.get("departments")
        if not isinstance(groups, list) or not 1 <= len(groups) <= 30:
            raise ValueError("請設定 1–30 個科別群組。")
        seen = set()
        for group in groups:
            if not isinstance(group, dict) or any(not isinstance(group.get(k), str) or not 1 <= len(group[k]) <= 60 for k in ("id", "name")):
                raise ValueError("科別名稱或代碼不正確。")
            if group["id"] in seen:
                raise ValueError("科別群組代碼重複。")
            seen.add(group["id"])
            for key in ("codes", "names"):
                values_ = group.get(key)
                if not isinstance(values_, list) or len(values_) > 100 or any(not isinstance(v, str) or not v.strip() or len(v) > 100 for v in values_):
                    raise ValueError("科別代碼與名稱請每行一項。")
                group[key] = list(dict.fromkeys(v.strip() for v in values_))
            if not group["codes"] and not group["names"]:
                raise ValueError("每個科別至少填一個代碼或名稱。")
        if values.get("default_department") not in seen:
            raise ValueError("請選擇預設科別。")
        options = copy.deepcopy(values.get("review_options", current.get("review_options", {})))
        if not isinstance(options, dict):
            raise ValueError("檢閱設定格式不正確。")
        enums = {"department_filter": {"same", "all"}, "mode": {"latest", "registration"},
                 "cutoff_mode": {"today", "date"}, "retrieval": {"cache", "refresh", "force"}}
        if set(options) - set(enums) - {"department", "cutoff"}:
            raise ValueError("檢閱設定欄位不正確。")
        for key, allowed in enums.items():
            if key in options and (not isinstance(options[key], str) or options[key] not in allowed):
                raise ValueError("檢閱設定選項不正確。")
        if options.get("department") not in seen:
            options["department"] = values["default_department"]
        if options.get("cutoff_mode") == "date":
            parse_range({"start": options.get("cutoff"), "end": options.get("cutoff")})
        return self.db.save("preferences", {"id": "review", "departments": groups,
            "default_department": values["default_department"], "review_options": options})

    def save_set(self, values):
        if values.get("id"):
            old = self.db.get("set", values["id"])
            name = str(values.get("name", "")).strip()
            if not name or len(name) > 100:
                raise ValueError("集合名稱需為 1–100 字。")
            return self.db.save("set", {**old, "name": name})
        members = {}

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
        for mrn in parse_mrns(values["mrns"], limit=20000) if values.get("mrns") else []:
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

    def scope(self, values):
        """Check the chosen specialty against saved sources without hospital requests."""
        group = self.db.get("set", values.get("set_id"))
        department_filter = values.get("department_filter", "same")
        if not isinstance(department_filter, str) or department_filter not in {"same", "all"}:
            raise ValueError("科別篩選須為同科別或不限科別。")
        if department_filter == "all":
            return {"department": {"id": "__all__", "name": "不限科別", "codes": [], "names": []},
                    "unmatched": [], "needs_confirmation": False, "confirmation_key": ""}
        preferences = self.preferences()
        department = next((g for g in preferences["departments"]
            if g["id"] == values.get("department", preferences["default_department"])), None)
        if not department:
            raise ValueError("請先設定本次科別。")
        sources = {(str(r.get("section_code") or "").strip(), str(r.get("section_name") or "").strip())
            for member in group["members"] for r in member.get("registrations", [])}
        unmatched = [{"code": code, "name": name} for code, name in sorted(sources)
            if code not in department["codes"] and name not in department["names"]]
        # Registration mode selects the exact source visit, not a specialty group.
        needed = values.get("mode", "latest") != "registration" and bool(unmatched)
        return {"department": copy.deepcopy(department), "unmatched": unmatched,
                "needs_confirmation": needed, "confirmation_key": digest({
                    "set_id": group["id"], "department": department, "unmatched": unmatched}) if needed else ""}

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
            if kind not in {"review", "resolve", "numeric", "registrations", SURGERY_KIND} | APPROVAL_KINDS | EARNINGS_KINDS:
                raise ValueError("工具類型不正確。")
            task = {"id": uuid.uuid4().hex, "kind": kind, "created_at": timestamp(), "account_id": self.app.account_id,
                    "force": values.get("force") is True, "refresh": values.get("refresh") is True}
            if kind == "review":
                group = self.db.get("set", values.get("set_id"))
                scope = self.scope(values)
                department = scope["department"]
                mode = values.get("mode", "latest")
                if mode not in {"latest", "registration"}:
                    raise ValueError("SOAP 模式不正確。")
                cutoff = values.get("cutoff") or today().isoformat()
                parse_range({"start": cutoff, "end": cutoff})
                if scope["needs_confirmation"] and (values.get("department_confirmed") is not True
                        or values.get("department_confirmation") != scope["confirmation_key"]):
                    raise ValueError("來源含尚未對應至本次科別的掛號，請確認科別範圍後再開始。")
                task.update(name=group["name"], members=copy.deepcopy(group["members"]), set_id=group["id"],
                    department=copy.deepcopy(department), department_scope=scope,
                    department_filter=values.get("department_filter", "same"), mode=mode, cutoff=cutoff,
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
            total, unit = task.get("case_total"), "件"
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
        return progress(len(counted), total, unit=unit,
                        failed=sum(not i.get("processing") and i.get("status") in {"error", "partial", "forbidden"} for i in items),
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
                        result["message"] = "就診索引的病人身分不符；重新核對後仍未確認。可單獨重試或查看診斷。"
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
        if not isinstance(cases, list) or any(not isinstance(c, VisitCase) or c.mrn != mrn for c in cases):
            raise ValueError("就診索引病人不符或格式不正確。")
        saved = self.db.save("visits", {"id": mrn, "cases": clean(cases)})
        return cases, saved["updated_at"]

    def patient(self, task, member, state):
        mrn = member["mrn"]
        result = {**member, "records": [], "attempts": [], "status": "ready", "message": ""}
        if task["mode"] == "registration" and not member["registrations"]:
            return {**result, "status": "no_source", "message": "未指定掛號來源，請改用最新同科或從門診清單選取。"}
        if task["mode"] == "registration" and all(r["visit_date"] > today().isoformat() for r in member["registrations"]):
            return {**result, "status": "future", "message": "尚未到診，可使用最新同科模式。"}
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
        group = task["department"]
        all_departments = task.get("department_filter", "same") == "all"
        def same_section(case):
            return case.section_code.strip() in group["codes"] or case.section_name.strip() in group["names"]
        eligible = [c for c in cases if c.case_type == "O" and c.visit_date and c.visit_date.isoformat() <= task["cutoff"]]
        ambiguous = any(c.case_type == "O" and (not c.visit_date or not all_departments and not c.section_code and not c.section_name) for c in cases)
        if task["mode"] == "registration":
            keys = {(r["visit_date"], r["section_code"]) for r in member["registrations"] if r["visit_date"] <= today().isoformat()}
            eligible = [c for c in eligible if (c.visit_date.isoformat(), c.section_code) in keys]
        elif not all_departments:
            eligible = [c for c in eligible if same_section(c)]
        if not eligible:
            message = ("索引日期或科別不完整，無法判定" if ambiguous else "查無掛號當次就診" if task["mode"] == "registration"
                       else "查無門診歷史（可能初診）" if all_departments else "查無同科歷史（可能初診）")
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
                        if not isinstance(soap, SoapRecord) or soap.case.identity != case.identity:
                            from vghks_sdk import ParseError

                            raise ParseError("SOAP identity mismatch", code="BOT_SOAP_CASE_MISMATCH")
                        if not soap.full_text.strip():
                            result["attempts"].append({**attempt, "status": "empty", "message": "SOAP 尚無內容"})
                            continue
                        record = {"id": key, "mrn": mrn, "name": member.get("name", ""), "sex": member.get("sex", ""),
                            "age": member.get("age", ""), "date": day.isoformat(), "section": case.section_name,
                            "section_code": case.section_code, "case_no": case.case_no, "doctor": case.doctor_name,
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
                case = VisitCase(mrn, date.fromisoformat(record["date"]), "O", record["case_no"], record["section_code"], record["section"])
                report = sdk.records.get_numeric_report(case)
                if report.case.identity != case.identity:
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
