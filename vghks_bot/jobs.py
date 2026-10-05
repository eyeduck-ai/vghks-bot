from __future__ import annotations

import copy
import queue
import secrets
import threading
import uuid
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from vghks_sdk import __version__ as sdk_version

from . import __version__
from .clinical_identity import classify_opd_registration
from .connection_state import should_pause
from .library import current_cache, registration_id
from .scanner import Cancelled, ScanState, create_sdk, run_scan, safe_failure
from .settings import Account, Settings, parse_mrns, parse_range, today
from .storage import ACTIVE, StorageError, Store
from .tags import classification
from .workflow import patient_model, run_workflow


class BusyError(Exception):
    pass


class Application:
    def __init__(self, settings: Settings, data_dir: Path, sdk_factory=create_sdk):
        self.store = Store(data_dir)
        self.settings = settings
        self.accounts = {}
        try:
            config = self.store.load_config()
            if config:
                saved = config["settings"]
                self.settings = settings.update(saved)
                for value in config["accounts"]:
                    account = Account(value["id"]).update(value)
                    self.accounts[account.id] = account
            if not self.accounts:
                account = Account(uuid.uuid4().hex, username=settings.username, password=settings.password)
                self.accounts[account.id] = account
        except Exception:
            self.store.close()
            raise
        self.lock = threading.RLock()
        self.launch_token = secrets.token_urlsafe(32)
        self.session_token = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self.states = {}
        self.queue = queue.Queue()
        from .task_manager import TaskManager

        self.task_manager = TaskManager(self.queue)
        self.idle = threading.Event()
        self.idle.set()
        self.closing = False
        self.sdk_factory = sdk_factory
        from .analysis import Analysis
        from .library_data import LibraryData

        try:
            self.store.library.prune_patient_tags(self.settings)
            self.analysis = Analysis(self)
            self.library_data = LibraryData(self)
        except Exception:
            self.store.close()
            raise
        self.worker = threading.Thread(target=self._work, daemon=True, name="account-query-queue")
        self.worker.start()

    def _available(self):
        if self.closing:
            raise BusyError("程式正在結束。")

    def bootstrap(self):
        with self.lock:
            return {
                "today": today().isoformat(), "csrf": self.csrf_token,
                "settings": self.settings.public(), "accounts": [a.public() for a in self.accounts.values()],
                "data_dir": str(self.store.directory), "warnings": self.store.warnings,
                "version": __version__, "sdk_version": sdk_version,
                "library": self.store.library.stats(),
            }

    def history(self, values=None):
        from .pagination import slice_rows

        with self.lock:
            rows = {r["id"]: r for r in self.store.summaries()}
            rows.update({key: state.snapshot(detail=False) for key, state in self.states.items()})
            self.analysis.label_history(rows.values())
            ordered = sorted(rows.values(), key=lambda r: (r["created_at"], r["id"]), reverse=True)
            return {"runs": slice_rows(ordered, values or {}), "total": len(ordered), "warnings": self.store.warnings}

    def status(self, values):
        watched = list(dict.fromkeys(str(values.get("run", "")).split(",")))[:20]
        with self.lock:
            rows = {key: state.snapshot(detail=False) for key, state in self.states.items()}
        for key in watched:
            if key and key not in rows:
                rows[key] = self.store.metadata(key)
        return {"runs": list(rows.values()), "tasks": [],
                "active_count": sum(row["status"] in ACTIVE for row in rows.values()), "warnings": self.store.warnings}

    def snapshot(self, run_id, revision=""):
        with self.lock:
            state = self.states.get(run_id)
            if state:
                with state.lock:
                    if str(state.data["revision"]) == revision:
                        return {"unchanged": True}
                    return state.snapshot()
            if revision and str(self.store.metadata(run_id)["revision"]) == revision:
                return {"unchanged": True}
            return self.store.load(run_id)

    def save_settings(self, values):
        with self.lock:
            self._available()
            if set(values) - set(self.settings.public()):
                raise ValueError("設定欄位不正確。")
            settings = self.settings.update(values)
            self.store.save_config(settings, self.accounts.values())
            self.settings = settings
            self.store.library.prune_patient_tags(settings)
            return settings.public()

    def patient_tags_update(self, values):
        with self.lock:
            self._available()
            mrns = parse_mrns(values.get("mrns"), limit=20000)
            ids, name, operation = values.get("tag_ids", []), values.get("new_tag", ""), values.get("operation", "add")
            if not isinstance(ids, list) or len(ids) > 50 or any(not isinstance(k, str) for k in ids):
                raise ValueError("請勾選有效 tag。")
            if not isinstance(name, str) or len(name.strip()) > 40 or operation not in {"add", "remove"}:
                raise ValueError("tag 名稱或操作不正確。")
            name = name.strip()
            if set(ids) - {t.id for t in self.settings.categories}:
                raise ValueError("tag 已刪除，請重新載入。")
            if name:
                if operation != "add":
                    raise ValueError("移除標記時不可建立新 tag。")
                if any(t.name.casefold() == name.casefold() for t in self.settings.categories):
                    raise ValueError("名稱已存在，請勾選現有 tag。")
                key = uuid.uuid4().hex
                self.save_settings({"categories": [*self.settings.public()["categories"],
                                                   {"id": key, "name": name, "keywords": []}]})
                ids = [*ids, key]
            if not ids:
                raise ValueError("請勾選 tag，或輸入新的 tag 名稱。")
            changed = self.store.library.set_patient_tags(mrns, list(dict.fromkeys(ids)), remove=operation == "remove")
            return {"patients": len(mrns), "changed": changed, "settings": self.settings.public()}

    def tag_patients(self, values):
        with self.lock:
            return self.store.library.tag_patients(values, self.settings)

    def save_accounts(self, values):
        items = values.get("accounts")
        if not isinstance(items, list) or not 1 <= len(items) <= 50:
            raise ValueError("請設定 1–50 個帳號。")
        with self.lock:
            self._available()
            updated = dict(self.accounts)
            seen = set()
            saved = []
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("帳號格式不正確。")
                key = item.get("id") or uuid.uuid4().hex
                if key in seen:
                    raise ValueError("帳號設定重複。")
                if not isinstance(key, str) or len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
                    raise ValueError("帳號代碼不正確。")
                account = updated.get(key, Account(key)).update(item)
                updated[key] = account
                saved.append(account.public())
                seen.add(key)
            if len(updated) > 50:
                raise ValueError("最多可設定 50 個帳號。")
            self.store.save_config(self.settings, updated.values())
            self.accounts = updated
            return {"accounts": saved}

    def delete_account(self, key):
        with self.lock:
            self._available()
            if self._active_account(key):
                raise BusyError("請先停止此帳號的查詢。")
            updated = {k: v for k, v in self.accounts.items() if k != key}
            self.store.save_config(self.settings, updated.values())
            self.accounts = updated
            return {"ok": True}

    def _active_account(self, key):
        return any(s.snapshot(detail=False)["status"] in ACTIVE and s.data.get("account_id") == key for s in self.states.values())

    def _account(self, key):
        account = self.accounts.get(key) if isinstance(key, str) else None
        if not account or not account.username:
            raise ValueError("請先儲存登入帳號。")
        return account

    @staticmethod
    def _force(values):
        force = values.get("force", False)
        if not isinstance(force, bool):
            raise ValueError("重新抓取設定不正確。")
        return force

    def _enqueue_workflows(self, prepared):
        with self.task_manager.admission(len(prepared)):
            return self._enqueue_prepared(prepared)

    def _enqueue_prepared(self, prepared):
        jobs = []
        try:
            for account, start, end, rows, force in prepared:
                settings = replace(self.settings, username=account.username, password=account.password, response_encoding=account.response_encoding)
                state = ScanState(start, end, account=account.username, id=uuid.uuid4().hex,
                    account_id=account.id, account_label=account.label, mode=account.mode,
                    kind="list" if rows is None else "fetch", response_encoding=account.response_encoding,
                    categories=settings.public()["categories"], status="queued", stage="queued", message="等待處理…")
                state.journal = self.store.create(state.snapshot(detail=False))
                jobs.append((state, settings, {"workflow": True, "rows": rows, "force": force}))
        except StorageError:
            for state, _, _ in jobs:
                state.update(status="failed", message="尚未連線，無法建立作業。")
            raise
        self.idle.clear()
        for job in jobs:
            self.states[job[0].data["id"]] = job[0]
            self.queue.put(job)
        return {"run_ids": [job[0].data["id"] for job in jobs]}

    def browse(self, values):
        keys = values.get("account_ids")
        if not isinstance(keys, list) or not 1 <= len(keys) <= 50 or any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys):
            raise ValueError("請選擇要查看的帳號。")
        force = self._force(values)
        with self.lock:
            self._available()
            prepared = []
            for key in keys:
                account = self._account(key)
                if self._active_account(key):
                    raise BusyError("此帳號仍在處理中。")
                ranges = values.get("ranges", {})
                if not isinstance(ranges, dict) or not isinstance(ranges.get(key, {}), dict):
                    raise ValueError("日期設定不正確。")
                start, end = parse_range(ranges.get(key) or account.persisted(), allow_future=True)
                missing = force or any(self.store.library.cached_list(account.username, (start + timedelta(days=i)).isoformat()) is None for i in range((end - start).days + 1))
                if missing and not account.password:
                    raise ValueError("此日期尚無有效快取，請輸入此帳號的密碼。")
                prepared.append((account, start, end, None, force))
            return self._enqueue_workflows(prepared)

    def patient_list(self, values):
        with self.lock:
            account = self._account(values.get("account_id"))
            start, end = parse_range({"start": values.get("start") or account.start, "end": values.get("end") or values.get("start") or account.end}, allow_future=True)
            days = []
            saved_rows = self.store.library.registration_statuses(account.username, start.isoformat(), end.isoformat())
            manual_tags = self.store.library.manual_tags(self.settings)
            for offset in range((end - start).days + 1):
                day = (start + timedelta(days=offset)).isoformat()
                cached = self.store.library.cached_list(account.username, day, allow_stale=True)
                rows = []
                for row in cached["rows"] if cached else []:
                    model = patient_model(row)
                    eligible = bool(model.mrn.strip() and model.section_code.strip() and model.visit_date.isoformat() == day and classify_opd_registration(model, doctor_card=account.username) == "DEDICATED")
                    saved = saved_rows.get(registration_id(row))
                    rows.append({**row, "id": registration_id(row), "day": day, "eligible": eligible,
                        "ownership": classify_opd_registration(model, doctor_card=account.username),
                        "manual_tags": manual_tags.get(row["mrn"], []),
                        "soap_eligible": eligible and day <= today().isoformat(),
                        "saved": saved is not None, "record_ids": saved or [],
                        "status": "未來掛號 · 可分析歷年檢查" if eligible and day > today().isoformat() else "已保存" if saved else "無當日就診" if saved is not None else "未抓取" if eligible else "非專屬診或資料不全"})
                days.append({"day": day, "cached": cached is not None, "fresh": bool(cached and current_cache(day, cached["fetched_at"])), "fetched_at": cached["fetched_at"] if cached else "", "rows": rows})
            return {"account_id": account.id, "account": account.username, "start": start.isoformat(), "end": end.isoformat(), "days": days}

    def fetch_selected(self, values):
        selections = values.get("selections")
        if not isinstance(selections, list) or not 1 <= len(selections) <= 50:
            raise ValueError("請勾選要抓取的病人。")
        force = self._force(values)
        with self.lock:
            self._available()
            prepared, seen = [], set()
            for selection in selections:
                if not isinstance(selection, dict):
                    raise ValueError("病人選擇格式不正確。")
                account = self._account(selection.get("account_id"))
                if account.id in seen:
                    raise ValueError("帳號重複。")
                seen.add(account.id)
                if self._active_account(account.id):
                    raise BusyError("此帳號仍在處理中。")
                refs = selection.get("rows")
                if not isinstance(refs, list) or not 1 <= len(refs) <= 20000:
                    raise ValueError("請勾選要抓取的病人。")
                rows, row_seen, day_rows = [], set(), {}
                for ref in refs:
                    if not isinstance(ref, dict) or not isinstance(ref.get("day"), str) or not isinstance(ref.get("id"), str):
                        raise ValueError("病人選擇格式不正確。")
                    parse_range({"start": ref["day"], "end": ref["day"]}, allow_future=True)
                    if ref["day"] > today().isoformat():
                        raise ValueError("未來掛號尚無當日 SOAP，請改用「加入分析清單」查詢歷年檢查。")
                    if ref["day"] not in day_rows:
                        cached = self.store.library.cached_list(account.username, ref["day"], allow_stale=True)
                        day_rows[ref["day"]] = {registration_id(r): r for r in cached["rows"]} if cached else {}
                    row = day_rows[ref["day"]].get(ref["id"])
                    if row is None or row["visit_date"] != ref["day"]:
                        raise ValueError("門診清單已變動，請重新載入後勾選。")
                    model = patient_model(row)
                    if not model.mrn.strip() or not model.section_code.strip() or classify_opd_registration(model, doctor_card=account.username) != "DEDICATED":
                        raise ValueError("只能抓取可確認歸屬此帳號的門診病人。")
                    if ref["id"] not in row_seen:
                        rows.append(row)
                        row_seen.add(ref["id"])
                if not account.password and (force or any(self.store.library.completed_registration(account.username, row) is None for row in rows)):
                    raise ValueError("需要抓取新病歷，請輸入此帳號的密碼。")
                start, end = parse_range({"start": min(r["visit_date"] for r in rows), "end": max(r["visit_date"] for r in rows)})
                prepared.append((account, start, end, rows, force))
            return self._enqueue_workflows(prepared)

    def library_search(self, values):
        settings = self.settings
        return {**self.store.library.search(values, settings), "stats": self.store.library.stats(), "categories": settings.public()["categories"]}

    def library_record(self, key):
        record = self.store.library.get_record(key)
        if record is None:
            raise ValueError("此病歷已刪除或不存在。")
        record.update(classification(record, self.settings))
        record["manual_tags"] = self.store.library.manual_tags(self.settings).get(record["mrn"], [])
        for version in record["versions"]:
            version.update(classification(version, self.settings))
            version["manual_tags"] = record["manual_tags"]
        return record

    def delete_records(self, values):
        with self.lock:
            self._available()
            if not self.idle.is_set() or self.analysis.sheet_busy:
                raise BusyError("請先等待或停止目前作業，再刪除病歷。")
            mrns = {r["mrn"] for key in values.get("ids", [])
                    if (r := self.store.library.get_record(key))}
            self.analysis.store.invalidate_previews(mrns)
            result = self.store.delete_records(values.get("ids"))
            self.library_data.soap_cleared(values["ids"], mrns)
            for state in self.states.values():
                state.data["records"] = [r for r in state.data["records"] if r["id"] not in values["ids"]]
                matched = [r for r in state.data["records"] if r.get("matches")]
                state.data["counts"].update(soap_read=len(state.data["records"]), matched_visits=len(matched),
                    matched_patients=len({r["mrn"] for r in matched}), markers=sum(len(r["matches"]) for r in matched))
                state.data["revision"] += 1
            return {**result, "mrns": sorted(mrns), "categories": ["soap"]}

    def delete_list(self, values):
        with self.lock:
            self._available()
            account = self._account(values.get("account_id"))
            if not self.idle.is_set():
                raise BusyError("請先等待或停止目前作業，再刪除快取。")
            start, end = parse_range(values, allow_future=True)
            rows = self.library_data.read({"start": start.isoformat(), "end": end.isoformat()})["lists"]
            rows = [{"account": row["account"], "day": row["day"]} for row in rows if row["account"] == account.username]
            if not rows:
                return {"ok": True, "deleted": 0}
            return self.library_data.delete({"lists": rows}, compatibility=True)

    def start(self, values):
        keys = values.get("account_ids")
        if not isinstance(keys, list) or not keys or len(keys) > 50 or any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys):
            raise ValueError("請選擇要查詢的帳號。")
        with self.lock, self.task_manager.admission(len(keys)):
            self._available()
            accounts = []
            for key in keys:
                account = self.accounts.get(key)
                if not account or not account.username or not account.password:
                    raise ValueError("請填妥所選帳號的登入帳號與密碼。")
                if self._active_account(key):
                    raise BusyError(f"{account.label or account.username} 已在查詢佇列中。")
                parse_range(account.persisted())
                accounts.append(account)
            jobs = []
            try:
                for account in accounts:
                    start, end = parse_range(account.persisted())
                    settings = replace(self.settings, username=account.username, password=account.password, response_encoding=account.response_encoding)
                    state = ScanState(start, end, account=account.username, id=uuid.uuid4().hex,
                        account_id=account.id, account_label=account.label, mode=account.mode,
                        response_encoding=account.response_encoding, categories=settings.public()["categories"],
                        status="queued", stage="queued", message="等待查詢…")
                    state.journal = self.store.create(state.snapshot(detail=False))
                    jobs.append((state, settings, None))
            except StorageError:
                for state, _, _ in jobs:
                    state.update(status="failed", message="未能建立全部查詢，尚未連線。")
                raise
            self.idle.clear()
            for job in jobs:
                self.states[job[0].data["id"]] = job[0]
                self.queue.put(job)
            return {"run_ids": [s.data["id"] for s, _, _ in jobs]}

    def reclassify(self, run_id):
        with self.lock, self.task_manager.admission():
            self._available()
            source = self.snapshot(run_id)
            if source["status"] in ACTIVE:
                raise BusyError("請等待此查詢結束後再重新分類。")
            start, end = parse_range(source)
            state = ScanState(start, end, account=source["account"], id=uuid.uuid4().hex,
                account_id=source.get("account_id", ""), account_label=source.get("account_label", ""),
                response_encoding=source.get("response_encoding", "auto"), source_id=run_id,
                categories=self.settings.public()["categories"], status="queued", stage="queued", message="等待重新分類…")
            state.journal = self.store.create(state.snapshot(detail=False))
            self.states[state.data["id"]] = state
            self.idle.clear()
            self.queue.put((state, replace(self.settings, username="", password=""), source))
            return {"run_ids": [state.data["id"]]}

    def _reclassify(self, state, settings, source):
        state.check_cancel()
        state.update(status="running", stage="classify", message="正在套用目前分類…")
        state.data["counts"] = copy.deepcopy(source["counts"])
        state.data["counts"].update(soap_read=0, matched_visits=0, matched_patients=0, markers=0, errors=0)
        for issue in source["issues"]:
            state.issue(issue["stage"], issue["message"], code=issue.get("code", ""), mrn=issue.get("mrn", ""), day=issue.get("date", ""))
        for record in source["records"]:
            state.check_cancel()
            updated = {**record, **classification(record, settings)}
            state.add_record(updated)
        complete = source["status"] == "completed" and not state.data["counts"]["errors"]
        state.update(status="completed" if complete else "partial", stage="done",
            message="已重新分類。" if complete else "已重新分類；來源查詢有未完成或異常資料。")

    def _work(self):
        while True:
            job = self.queue.get()
            if job is None:
                self.queue.task_done()
                self.analysis.cataract_queue.notify()
                break
            state, settings, source = job
            try:
                context = self.gateway.task_context(state.data["id"], (source or {}).get("bot_task", {}).get("kind", state.data.get("kind", "")), state.cancel) if hasattr(self, "gateway") else nullcontext()
                with context:
                    if source and source.get("bot_task"):
                        self.review.execute(state, source["bot_task"])
                    elif source and source.get("analysis"):
                        self.analysis.execute(state, source)
                    elif source and source.get("workflow"):
                        run_workflow(state, settings, self.store.library, rows=source["rows"], force=source["force"], sdk_factory=self.sdk_factory)
                    elif source is None:
                        start, end = parse_range(state.data)
                        run_scan(state, settings, start, end, self.sdk_factory)
                    else:
                        self._reclassify(state, settings, source)
            except Cancelled:
                try:
                    state.update(status="cancelled", message="已停止，先前資料已保留。")
                except StorageError:
                    self._storage_failure(state)
            except StorageError:
                self._storage_failure(state)
            except Exception as exc:
                try:
                    message, code = safe_failure(exc)
                    state.issue("查詢", message, code=code)
                    state.update(status="paused" if should_pause(exc) else "failed", message=message)
                except StorageError:
                    self._storage_failure(state)
            finally:
                try:
                    if source and source.get("bot_task"):
                        self.review.finish(state)
                    self.analysis.finish(state)
                except StorageError:
                    self._storage_failure(state)
                # Release credentials as soon as this job ends.
                job = settings = source = None
                self.queue.task_done()
                self.analysis.cataract_queue.notify()
                with self.lock:
                    if (state.journal is not None or state.data.get("kind") == "bot") and state.data["status"] not in ACTIVE:
                        self.states.pop(state.data["id"], None)
                    if not any(s.snapshot(detail=False)["status"] in ACTIVE for s in self.states.values()):
                        self.idle.set()
                state = None
        self.store.close()

    @staticmethod
    def _storage_failure(state):
        with state.lock:
            state.journal = None
            state.issue("保存", "保存失敗；可下載目前 JSON。", code="STORAGE_FAILED")
            state.update(status="failed", message="保存失敗，已停止查詢。")

    def stop(self, run_id):
        with self.lock:
            state = self.states.get(run_id)
            if state:
                with state.lock:
                    if state.data["status"] in ACTIVE:
                        state.cancel.set()
                        queued = self.task_manager.remove_queued(run_id)
                        if queued is not None:
                            state.update(status="cancelled", message="已停止，先前資料已保留。")
                            source = queued[2]
                            if source and source.get("bot_task"):
                                self.review.finish(state)
                            self.analysis.finish(state)
                            self.states.pop(run_id, None)
                            if not any(s.data["status"] in ACTIVE for s in self.states.values()):
                                self.idle.set()
                            return {"ok": True}
                        state.update(status="cancelling", message="正在停止…")
            return {"ok": True}

    def request_close(self):
        self.analysis.cataract_queue.close()
        self.task_manager.cancel_pending()
        with self.lock:
            if self.closing:
                return
            self.closing = True
            for key in list(self.states):
                try:
                    self.stop(key)
                except StorageError:
                    self._storage_failure(self.states[key])
            self.accounts = {key: replace(a, password="") for key, a in self.accounts.items()}
            self.settings = replace(self.settings, password="")
            self.queue.put(None)

    def close(self):
        self.request_close()
        self.worker.join(2)
