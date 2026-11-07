"""Account-local hourly fresh SOAP jobs, serialized with the normal task queue."""
from __future__ import annotations

import copy
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timedelta

from vghks_sdk import OutpatientPatient

from .analysis_store import digest
from .clinical_identity import classify_opd_registration
from .connection_state import should_pause
from .google_sheets import SheetError, SheetUnavailable
from .scanner import Cancelled, ScanState, _checked_list, safe_failure
from .settings import timestamp, today
from .sheet_service import account_hospital
from .storage import ACTIVE, StorageError
from .workflow import patient_value


class SheetSync:
    def __init__(self, app):
        self.app, self.db = app, app.review.db
        self.retry_lock = threading.Lock()

    def settings(self, values=None):
        current = self.db.get("preferences", "sheet_sync", required=False) or {
            "id": "sheet_sync", "enabled": False, "interval_minutes": 60,
            "last_run_at": "", "next_run_at": ""}
        if not values:
            return current
        if type(values.get("enabled")) is not bool:
            raise ValueError("請設定刀表排程開關。")
        minutes = values.get("interval_minutes", current["interval_minutes"])
        if type(minutes) is not int or not 5 <= minutes <= 1440:
            raise ValueError("排程間隔需為 5–1440 分鐘。")
        return self.db.save("preferences", {**current, "enabled": values["enabled"],
            "interval_minutes": minutes, "next_run_at": timestamp() if values["enabled"] else ""})

    def status(self, _=None):
        settings = self.settings()
        tasks = self.db.task_summaries(key=settings.get("last_task", "")) if settings.get("last_task") else []
        pending = self.db.all("sheet_sync_pending")
        return {"settings": settings, "online": self.app.gateway.online,
                "scheduler_error": getattr(self.app.root, "scheduler_errors", {}).get((self.app.account_id, "SheetSync"))
                                   or getattr(self.app.root, "scheduler_failure", None),
                "running": any(t["status"] in ACTIVE for t in tasks),
                "last_result": self.db.get("sheet_sync_result", "latest", required=False),
                "deferred": [{k: v for k, v in row.items() if k not in {"source_versions", "source_issues", "resolved_source_ids"}}
                             for row in self.db.all("sheet_sync_deferred") if row.get("status") != "completed"],
                "pending": [{**p, "can_retry": p.get("kind") == "source" and p.get("day") == today().isoformat()}
                            for p in pending if not p.get("dismissed")],
                "history": self.db.documents("sheet_sync_history", limit=30)}

    def schedule(self):
        if (self.app.closing or self.app.root.read_only or not self.app.entered
                or not self.app.gateway.online):
            return
        current = self.settings()
        if current["enabled"] and current["next_run_at"] <= timestamp():
            self.run({"automatic": True})

    def run(self, values=None, *, source_id=None):
        with self.app.root.lock, self.app.lock, self.app.task_manager.admission():
            self.app._available()
            if not self.app.gateway.online:
                raise ValueError("請先登入此帳號，再更新當天 SOAP 刀表。")
            if self.app.root.read_only:
                raise ValueError("唯讀檢閱不能更新刀表。")
            if self.status()["running"] or self.retry_lock.locked():
                if (values or {}).get("automatic") is True:
                    return None
                raise ValueError("此帳號的刀表更新仍在執行。")
            if not self.app.analysis.google.public()["configured"]:
                raise ValueError("請先在更新刀表工具匯入服務帳戶與設定刀表。")
            key, now, day = uuid.uuid4().hex, timestamp(), today()
            source = self.db.get("sheet_sync_pending", source_id) if source_id else None
            if source and (source.get("kind") != "source" or source.get("dismissed")
                           or source.get("day") != day.isoformat()):
                raise ValueError("只可重試今天仍待確認的 SOAP 來源。")
            task = {"id": key, "kind": "review", "sheet_sync": True,
                    "default_hospital": account_hospital(self.app),
                    "name": "當日 SOAP 更新刀表", "created_at": now,
                    "account_id": self.app.account_id, "members": [], "force": True,
                    "refresh": True, "mode": "registration", "cutoff": day.isoformat(),
                    "department_keywords": [], "categories": self.app.settings.public()["categories"],
                    "status": "queued", "message": "等待重新取得本人當日就診清單與 SOAP。",
                    "progress": {}, "error": "", "finished_at": ""}
            current = self.settings()
            if source:
                task.update(source_retry_id=source_id, name="重試待確認 SOAP 來源")
            else:
                due = datetime.fromisoformat(now) + timedelta(minutes=current["interval_minutes"])
                current.update(last_run_at=now, next_run_at=due.isoformat(timespec="seconds"))
            self.db.save_batch([("task", task), ("preferences", {**current, "last_task": key})])
            state = ScanState(day, day, id=key, kind="bot", account_id=self.app.account_id,
                              account=self.app.username, status="queued")
            self.app.states[key] = state
            self.app.idle.clear()
            self.app.queue.put((state, self.app.settings, {"bot_task": task, "sheet_sync": True}))
            return {"task_id": key}

    def execute(self, state, task):
        state.update(status="running", stage="registrations", message="正在重新取得本人當日門診清單…")
        day = today()
        if task["cutoff"] != day.isoformat():
            raise ValueError("來源日期已變更，請重新取得今天的門診清單。")
        source = self.db.get("sheet_sync_pending", task["source_retry_id"]) if task.get("source_retry_id") else None
        if source and (source.get("dismissed") or source.get("kind") != "source" or source.get("day") != task["cutoff"]):
            raise ValueError("此 SOAP 來源已離開今天的待確認清單。")
        state.check_cancel()
        with self.app.sdk_factory(self.app.settings) as sdk:
            patients = _checked_list(sdk.opd.get_doctor_patients(self.app.username, day), OutpatientPatient)
        if any(p.visit_date != day for p in patients):
            raise ValueError("門診清單日期與今天不符，未更新刀表。")
        self.app.store.library.save_list(self.app.username, day.isoformat(), [patient_value(p) for p in patients])
        grouped = defaultdict(list)
        for patient in patients:
            if (classify_opd_registration(patient, doctor_card=self.app.username) == "DEDICATED"
                    and patient.mrn.strip() and patient.section_code.strip()):
                grouped[patient.mrn].append(patient)
        if source:
            grouped = {mrn: rows for mrn, rows in grouped.items() if mrn == source["mrn"]}
        task["members"] = [{"mrn": mrn, "name": rows[0].name,
                            "account_id": self.app.account_id,
                            "registrations": [patient_value(p) for p in rows]} for mrn, rows in grouped.items()]
        task.update(status="running", progress=self.app.review.task_progress(task))
        self.db.save("task", task)
        state.count(patients_total=len(grouped))
        records, issues, resolved = [], [], []
        handoff = defaultdict(list)
        if source and not grouped:
            issues.append(self.db.save("sheet_sync_pending", {**source, "reason": "今天的本人門診清單已無此病人，請確認掛號來源。"}))
        for member in task["members"]:
            state.check_cancel()
            try:
                result = self.app.review.patient(task, member, state)
            except (Cancelled, StorageError):
                raise
            except Exception as exc:
                if not self.app.gateway.online or should_pause(exc):
                    raise
                message, code = safe_failure(exc)
                result = {**member, "status": "error", "records": [], "attempts": [], "message": message, "code": code}
            self.db.item(task["id"], member["mrn"], result)
            incomplete = [a for a in result.get("attempts", []) if a["status"] in {"error", "forbidden"}]
            pending_id = digest(["sheet-source", task["cutoff"], member["mrn"]])[:32]
            if result["status"] == "ready" and result["records"] and not incomplete:
                records.extend(result["records"])
                handoff[member["mrn"]].extend({"record_id": r["id"], "version": r["version"], "record": r,
                                                "review_task_id": task["id"]} for r in result["records"])
                resolved.append(pending_id)
            elif incomplete or source or result["status"] != "no_visit":
                reasons = [a.get("message", "") for a in incomplete] + [result.get("message", "")]
                issue = {"id": pending_id, "kind": "source", "day": task["cutoff"],
                         "mrn": member["mrn"], "name": member["name"],
                         "reason": " · ".join(dict.fromkeys(filter(None, reasons))) or "SOAP 來源尚未完整取得。"}
                issues.append(issue)
                self.db.save("sheet_sync_pending", issue)
            state.count(patients_done=1)
        state.check_cancel()
        if today() != day:
            raise ValueError("來源日期已變更，請重新取得今天的門診清單。")
        state.update(stage="sheet-sync", message="正在核對來源版本、人工修改與刀表位置…")
        cohort = {"id": uuid.uuid4().hex, "name": "當日 SOAP 刀表候選", "created_at": timestamp(),
                  "default_hospital": task.get("default_hospital", "高榮"),
                  "members": [{**m, "handoff_sources": handoff[m["mrn"]]}
                              for m in task["members"]]}
        self.app.analysis.store.save_document("analysis_cohorts", cohort)
        if records:
            result = self._apply_saved(task["id"], cohort, records, state,
                                       source_issues=issues, resolved_source_ids=resolved)
        else:
            result = {"id": task["id"], "created_at": timestamp(), "cohort_id": cohort["id"],
                      "updated": 0, "pending": issues, "source_issues": issues, "resolved_source_ids": []}
        self._save_result(result)
        state.update(status="partial" if result.get("pending") or result.get("deferred") else "completed", stage="done",
                     message=result.get("message") or f"刀表更新完成；{result.get('updated', 0)} 筆已核對，{len(result.get('pending', []))} 筆待核對。")

    def _apply_saved(self, key, cohort, records, state, *, intent=None, source_issues=(), resolved_source_ids=()):
        if intent is None:
            config = self.app.analysis.google.public()
            row = {"id": key, "cohort_id": cohort["id"], "created_at": timestamp(),
                   "default_hospital": cohort.get("default_hospital", account_hospital(self.app)),
                   "spreadsheet_id": config.get("spreadsheet_id", ""),
                   "source_issues": list(source_issues), "resolved_source_ids": list(resolved_source_ids),
                   "source_versions": {r["id"]: digest([r.get("soap", ""), r.get("soap_structure") or {}]) for r in records}}
        else:
            row = copy.deepcopy(intent)
            if row["id"] != key or row["cohort_id"] != cohort["id"]:
                raise ValueError("已保存候選與原操作不符，未更新刀表。")
        # Retry changes only its attempt state. Destination, source versions
        # and creation time remain the original durable evidence.
        row.update(status="pending", last_attempt_at=timestamp(), message="來源已保存，正在重新讀取線上刀表。")
        # Persist the intent before any Google read/write. A crash or network
        # outage never loses the already retrieved, immutable source cohort.
        self.db.save("sheet_sync_deferred", row)
        try:
            result = self.app.analysis.automatic_sheet_sync(records, cohort["id"], state,
                expected_spreadsheet_id=row["spreadsheet_id"], default_hospital=row.get("default_hospital", "高榮"))
        except (SheetError, ValueError) as exc:
            message = str(exc) + " 已保存本次 SOAP 與候選；恢復後可重新核對，尚未確認的批次不會重送。"
            row.update(status="offline" if isinstance(exc, SheetUnavailable) else "blocked", message=message)
            self.db.save("sheet_sync_deferred", row)
            result = {"updated": 0, "pending": [], "deferred": True, "message": message}
        else:
            self.db.save("sheet_sync_deferred", {**row, "status": "completed", "message": "已重新核對來源與刀表。"})
        issues = row.get("source_issues", [])
        return {**result, "id": key, "created_at": row["created_at"], "cohort_id": row["cohort_id"],
                "pending": [*result.get("pending", []), *issues], "source_issues": issues,
                "resolved_source_ids": row.get("resolved_source_ids", []) if intent is None else []}

    def _save_result(self, result):
        pending = result.get("pending", [])
        source_count = len(result.get("source_issues", []))
        result["message"] = result.get("message") or f"刀表更新已核對 {result.get('updated', 0)} 筆，{len(pending)} 筆待確認。"
        if source_count:
            result["message"] += f" 其中 {source_count} 位病人的 SOAP 來源未完整取得。"
        entries = [("sheet_sync_pending", {**row, "cohort_id": result["cohort_id"],
                    "id": row.get("id") or uuid.uuid4().hex}) for row in pending if row.get("kind") != "source"]
        for key in result.get("resolved_source_ids", []):
            previous = self.db.get("sheet_sync_pending", key, required=False)
            if previous:
                entries.append(("sheet_sync_pending", {**previous, "dismissed": True}))
        entries.extend([("sheet_sync_history", result), ("sheet_sync_result", {**result, "id": "latest"})])
        self.db.save_batch(entries)

    def retry(self, values):
        """Explicitly re-plan saved sources against fresh cloud state, never replay requests."""
        self.app._available()
        if self.app.root.read_only:
            raise ValueError("唯讀檢閱不能更新刀表。")
        with self.app.lock:
            if not self.app.idle.is_set() or not self.retry_lock.acquire(blocking=False):
                raise ValueError("請等待目前作業結束，再重新核對離線候選。")
        try:
            saved = self.db.get("sheet_sync_deferred", values.get("id"))
            if saved["status"] == "completed":
                return self.db.get("sheet_sync_history", saved["id"])
            if saved.get("spreadsheet_id") and saved["spreadsheet_id"] != self.app.analysis.google.public().get("spreadsheet_id"):
                raise ValueError("刀表設定已改變，請切回原刀表再核對。")
            cohort = self.app.analysis.store.document("analysis_cohorts", saved["cohort_id"])
            records = {source["record_id"]: source["record"] for member in cohort["members"]
                       for source in member.get("handoff_sources", [])}
            for key, version in saved["source_versions"].items():
                current = self.app.store.library.get_record(key)
                if key not in records or current is None or digest([current.get("soap", ""), current.get("soap_structure") or {}]) != version:
                    raise ValueError("已保存的來源 SOAP 版本已變更，請開啟候選重新確認，或重新取得當日 SOAP。")
            state = ScanState(today(), today(), id=saved["id"], kind="bot", account_id=self.app.account_id)
            result = self._apply_saved(saved["id"], cohort, list(records.values()), state, intent=saved)
            self._save_result(result)
            return result
        finally:
            self.retry_lock.release()

    def review(self, values):
        row = self.db.get("sheet_sync_pending", values.get("id"))
        if values.get("action") == "retry_source":
            return self.run(source_id=row["id"])
        if values.get("action") == "dismiss":
            self.db.save("sheet_sync_pending", {**row, "dismissed": True})
            return self.status()
        if values.get("action") != "preview" or not row.get("proposal"):
            raise ValueError("請開啟候選，確認來源與目的刀日後產生預覽。")
        return self.app.analysis.preview({"cohort_id": row["cohort_id"], "proposals": [row["proposal"]]})
