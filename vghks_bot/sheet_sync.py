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
from .google_sheets import SheetError, SheetUnavailable
from .scanner import ScanState, _checked_list
from .settings import timestamp, today
from .storage import ACTIVE
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
                "running": any(t["status"] in ACTIVE for t in tasks),
                "last_result": self.db.get("sheet_sync_result", "latest", required=False),
                "deferred": [{k: v for k, v in row.items() if k != "source_versions"}
                             for row in self.db.all("sheet_sync_deferred") if row.get("status") != "completed"],
                "pending": [p for p in pending if not p.get("dismissed")],
                "history": sorted(self.db.all("sheet_sync_history"),
                                  key=lambda v: v.get("created_at", ""), reverse=True)[:30]}

    def schedule(self):
        if (self.app.closing or self.app.root.read_only or not self.app.entered
                or not self.app.gateway.online):
            return
        current = self.settings()
        if current["enabled"] and current["next_run_at"] <= timestamp():
            self.run({"automatic": True})

    def run(self, _=None):
        with self.app.root.lock, self.app.lock, self.app.task_manager.admission():
            self.app._available()
            if not self.app.gateway.online:
                raise ValueError("請先登入此帳號，再更新當天 SOAP 刀表。")
            if self.app.root.read_only:
                raise ValueError("唯讀檢閱不能更新刀表。")
            if self.status()["running"]:
                raise ValueError("此帳號的刀表更新仍在執行。")
            if not self.app.analysis.google.public()["configured"]:
                raise ValueError("請先在更新刀表工具匯入服務帳戶與設定刀表。")
            key, now = uuid.uuid4().hex, timestamp()
            task = {"id": key, "kind": "review", "sheet_sync": True,
                    "name": "當日 SOAP 更新刀表", "created_at": now,
                    "account_id": self.app.account_id, "members": [], "force": True,
                    "refresh": True, "mode": "registration", "cutoff": today().isoformat(),
                    "department_keywords": [], "categories": self.app.settings.public()["categories"],
                    "status": "queued", "message": "等待重新取得本人當日就診清單與 SOAP。",
                    "progress": {}, "error": "", "finished_at": ""}
            self.db.save("task", task)
            current = self.settings()
            due = (datetime.fromisoformat(now) + timedelta(minutes=current["interval_minutes"])).isoformat(timespec="seconds")
            self.db.save("preferences", {**current, "last_task": key, "last_run_at": now, "next_run_at": due})
            state = ScanState(today(), today(), id=key, kind="bot", account_id=self.app.account_id,
                              account=self.app.username, status="queued")
            self.app.states[key] = state
            self.app.idle.clear()
            self.app.queue.put((state, self.app.settings, {"bot_task": task, "sheet_sync": True}))
            return {"task_id": key}

    def execute(self, state, task):
        state.update(status="running", stage="registrations", message="正在重新取得本人當日門診清單…")
        day = today()
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
        task["members"] = [{"mrn": mrn, "name": rows[0].name,
                            "account_id": self.app.account_id,
                            "registrations": [patient_value(p) for p in rows]} for mrn, rows in grouped.items()]
        task.update(status="running", progress=self.app.review.task_progress(task))
        self.db.save("task", task)
        state.count(patients_total=len(grouped))
        records = []
        for member in task["members"]:
            state.check_cancel()
            result = self.app.review.patient(task, member, state)
            self.db.item(task["id"], member["mrn"], result)
            records.extend(result["records"])
            state.count(patients_done=1)
        state.check_cancel()
        state.update(stage="sheet-sync", message="正在核對來源版本、人工修改與刀表位置…")
        cohort = {"id": uuid.uuid4().hex, "name": "當日 SOAP 刀表候選", "created_at": timestamp(),
                  "members": [{**copy.deepcopy(m), "handoff_sources": [
                      {"record_id": r["id"], "version": r["version"], "record": r,
                       "review_task_id": task["id"]} for r in records if r["mrn"] == m["mrn"]]}
                              for m in task["members"]]}
        self.app.analysis.store.save_document("analysis_cohorts", cohort)
        result = self._apply_saved(task["id"], cohort, records, state)
        self._save_result(result)
        state.update(status="partial" if result.get("pending") or result.get("deferred") else "completed", stage="done",
                     message=result.get("message") or f"刀表更新完成；{result.get('updated', 0)} 筆已核對，{len(result.get('pending', []))} 筆待核對。")

    def _apply_saved(self, key, cohort, records, state, *, intent=None):
        if intent is None:
            config = self.app.analysis.google.public()
            row = {"id": key, "cohort_id": cohort["id"], "created_at": timestamp(),
                   "spreadsheet_id": config.get("spreadsheet_id", ""),
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
                expected_spreadsheet_id=row["spreadsheet_id"])
        except (SheetError, ValueError) as exc:
            message = str(exc) + " 已保存本次 SOAP 與候選；恢復後可重新核對，尚未確認的批次不會重送。"
            row.update(status="offline" if isinstance(exc, SheetUnavailable) else "blocked", message=message)
            self.db.save("sheet_sync_deferred", row)
            result = {"updated": 0, "pending": [], "deferred": True, "message": message}
        else:
            self.db.save("sheet_sync_deferred", {**row, "status": "completed", "message": "已重新核對來源與刀表。"})
        return {**result, "id": key, "created_at": row["created_at"], "cohort_id": row["cohort_id"]}

    def _save_result(self, result):
        for row in result.get("pending", []):
            self.db.save("sheet_sync_pending", {**row, "cohort_id": result["cohort_id"],
                         "id": row.get("id") or uuid.uuid4().hex})
        self.db.save("sheet_sync_history", result)
        self.db.save("sheet_sync_result", {**result, "id": "latest"})

    def retry(self, values):
        """Explicitly re-plan saved sources against fresh cloud state, never replay requests."""
        self.app._available()
        if self.app.root.read_only:
            raise ValueError("唯讀檢閱不能更新刀表。")
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
        if values.get("action") == "dismiss":
            self.db.save("sheet_sync_pending", {**row, "dismissed": True})
            return self.status()
        if values.get("action") != "preview" or not row.get("proposal"):
            raise ValueError("請開啟候選，確認來源與目的刀日後產生預覽。")
        return self.app.analysis.preview({"cohort_id": row["cohort_id"], "proposals": [row["proposal"]]})
