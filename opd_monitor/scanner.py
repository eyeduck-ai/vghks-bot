from __future__ import annotations

import copy
import hashlib
import threading
from collections import defaultdict
from dataclasses import asdict
from datetime import date, timedelta

from vghks_sdk import (
    OutpatientPatient,
    ParseError,
    PortalCredentials,
    RequestPolicy,
    SDKSettings,
    SoapRecord,
    VghksSDK,
    VisitCase,
)
from vghks_sdk.core.connections import TLSConnectionManager
from vghks_sdk.core.errors import error_info
from vghks_sdk.core.tls import create_requests_session

from .clinical_identity import classify_opd_registration, select_registration_visits
from .encoding import ClinicalTransport
from .progress import run_progress
from .settings import Settings, timestamp
from .soap_data import snapshot as soap_snapshot
from .storage import StorageError
from .tags import classification


class Cancelled(Exception):
    pass


class ScanState:
    def __init__(self, start: date, end: date, *, account: str = "", **metadata):
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.journal = None
        self.matched_mrns = set()
        self.record_ids = set()
        self.data = {
            "status": "running", "stage": "auth", "message": "正在連線並驗證登入…",
            "start": start.isoformat(), "end": end.isoformat(), "account": account,
            "created_at": timestamp(), "schema_version": 2,
            "revision": 0, "records": [], "issues": [],
            "counts": dict.fromkeys((
                "days_done", "days_failed", "registrations", "eligible", "shared", "excluded",
                "patients_total", "patients_done", "with_visit", "without_visit", "unknown",
                "soap_total", "soap_done", "soap_read", "soap_missing", "matched_patients", "matched_visits",
                "markers", "errors",
            ), 0),
        }
        self.data["counts"]["days_total"] = (end - start).days + 1
        self.data.update(metadata)

    def snapshot(self, *, detail: bool = True) -> dict:
        with self.lock:
            data = {k: v for k, v in self.data.items() if detail or k not in {"records", "issues"}}
            return copy.deepcopy({**data, "progress": run_progress(data)})

    def update(self, **values):
        with self.lock:
            if values.get("status") in {"completed", "partial", "failed", "cancelled"}:
                values.setdefault("finished_at", timestamp())
            self.data.update(values)
            self.data["revision"] += 1
            self.checkpoint()

    def checkpoint(self):
        if self.journal:
            self.journal.checkpoint(self.snapshot(detail=False))

    def archive(self, kind, value):
        if self.journal:
            self.journal.append(kind, value)

    def count(self, **values):
        with self.lock:
            for key, value in values.items():
                self.data["counts"][key] += value
            self.data["revision"] += 1

    def issue(self, stage: str, message: str, *, code: str = "", mrn: str = "", day: str = ""):
        with self.lock:
            issue = {
                "stage": stage, "message": message, "code": code, "mrn": mrn, "date": day,
            }
            self.archive("issues", issue)
            self.data["issues"].append(issue)
            self.count(errors=1)
            self.checkpoint()

    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()

    def add_record(self, record: dict):
        with self.lock:
            if record["id"] in self.record_ids:
                return
            self.record_ids.add(record["id"])
            self.data["records"].append(record)
            if record["matches"]:
                self.matched_mrns.add(record["mrn"])
            self.data["counts"]["matched_patients"] = len(self.matched_mrns)
            self.count(matched_visits=int(bool(record["matches"])), markers=len(record["matches"]), soap_read=1)
            self.archive("records", record)
            self.checkpoint()


def create_sdk(settings: Settings):
    policy = RequestPolicy(
        min_delay_seconds=settings.min_delay_seconds,
        max_delay_seconds=settings.max_delay_seconds,
        connect_timeout_seconds=settings.connect_timeout_seconds,
        read_timeout_seconds=settings.read_timeout_seconds,
        max_attempts=settings.max_attempts,
    )
    sdk_settings = SDKSettings(request_policy=policy, allow_unverified_tls=settings.allow_unverified_tls)
    session, _ = create_requests_session(ca_bundle=sdk_settings.ca_bundle)
    connections = TLSConnectionManager(sdk_settings)
    connections.apply_all(session)
    transport = ClinicalTransport(policy=policy, verify=sdk_settings.requests_verify,
        session=session, connections=connections, response_encoding=settings.response_encoding)
    return VghksSDK(
        settings=sdk_settings, transport=transport,
        credentials=PortalCredentials(settings.username, settings.password),
    )


def safe_failure(exc: Exception) -> tuple[str, str]:
    info = error_info(exc)
    earnings_messages = {
        "EARNINGS_PASSWORD_REJECTED": "薪資系統登入未通過，請更新身分證字號及薪資密碼。",
        "EARNINGS_SESSION_EXPIRED": "薪資系統工作階段已過期，可按續跑重新登入。",
        "EARNINGS_CONTEXT_EXPIRED": "薪資查詢階段已過期，可按續跑重新取得月份。",
    }
    if info.code in earnings_messages:
        return earnings_messages[info.code], info.code
    messages = {
        "AUTHENTICATION": "登入或授權失敗，請檢查帳密與內網權限。",
        "NETWORK": "無法連線至院內系統，請檢查院內網路或 VPN。",
        "HTTP": "院內系統暫時無法完成請求。",
        "PARSE": "院內回傳格式無法辨識，這筆資料尚未確認。",
        "CONFIGURATION": "登入或連線設定有誤，請檢查設定。",
    }
    return messages.get(info.category, "查詢未完成，請依錯誤代碼檢查。"), info.code


def _checked_list(value, model):
    if not isinstance(value, list) or not all(isinstance(item, model) for item in value):
        raise ParseError("invalid query result", code="QUERY_RESULT_INVALID")
    return value


def run_scan(state: ScanState, settings: Settings, start: date, end: date, sdk_factory=create_sdk):
    try:
        state.check_cancel()
        state.update(status="running", stage="auth", message="正在驗證登入…")
        with sdk_factory(settings) as sdk:
            report = sdk.auth.check(only=("prq",))
            state.check_cancel()
            if not report.ok:
                failed = next((target for target in report.targets if target.status != "OK"), None)
                code = failed.error_code if failed else "AUTH_CHECK_FAILED"
                state.issue("登入", "院內登入或 PRQ 連線未成功，請檢查帳密、內網與權限。", code=code)
                state.update(status="failed", message="連線驗證未通過，尚未查詢病歷。")
                return
            registrations = _collect(sdk, state, settings, start, end)
            _read_patients(sdk, state, settings, registrations)
            state.check_cancel()
        with state.lock:
            state.check_cancel()
            incomplete = bool(state.data["counts"]["errors"])
            state.update(
                status="partial" if incomplete else "completed", stage="done",
                message="查詢結束；部分資料尚未確認，請查看查詢明細。" if incomplete else "查詢完成。",
            )
    except Cancelled:
        state.update(status="cancelled", message="已停止查詢，保留停止前已取得的結果。")
    except StorageError:
        # Do not recurse through a failing journal. Keep the in-memory result
        # available for JSON download; previously flushed lines remain durable.
        with state.lock:
            state.journal = None
            state.issue("保存", "保存失敗，已停止查詢；可下載目前 JSON。", code="STORAGE_FAILED")
            state.update(status="failed", message="保存失敗，請檢查資料夾權限與磁碟空間。")
    except Exception as exc:
        message, code = safe_failure(exc)
        state.issue("查詢", message, code=code)
        state.update(status="failed", message=message)


def _collect(sdk, state, settings, start, end):
    grouped = defaultdict(list)
    seen = set()
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        state.check_cancel()
        state.update(stage="registrations", message=f"正在取得 {day.isoformat()} 門診清單…")
        try:
            rows = _checked_list(
                sdk.opd.get_doctor_patients(settings.username, day), OutpatientPatient,
            )
        except StorageError:
            raise
        except Exception as exc:
            message, code = safe_failure(exc)
            state.issue("門診清單", message, code=code, day=day.isoformat())
            state.count(days_failed=1, days_done=1)
            if error_info(exc).category == "AUTHENTICATION":
                raise
            continue
        for row in rows:
            state.check_cancel()
            identity = (day, row.mrn, row.name, row.visit_date, row.section_code, row.room, row.doctor_card)
            if identity in seen:
                continue
            seen.add(identity)
            state.count(registrations=1)
            scope = classify_opd_registration(row, doctor_card=settings.username)
            state.archive("registrations", {**asdict(row), "visit_date": row.visit_date.isoformat(), "query_date": day.isoformat(), "scope": scope})
            if any("\ufffd" in field for field in (row.name, row.sex, row.age)):
                state.issue("文字編碼", "病人欄位仍有無法解碼的字元，可調整此帳號的文字編碼後重查。", code="TEXT_DECODE_INCOMPLETE", mrn=row.mrn, day=day.isoformat())
            if row.visit_date != day:
                state.count(unknown=1)
                state.issue("門診清單", "回傳日期與查詢日期不同，未讀取病歷。", mrn=row.mrn, day=day.isoformat())
            elif scope == "SHARED":
                state.count(shared=1)
            elif scope != "DEDICATED":
                state.count(excluded=1)
                if not row.doctor_card.strip():
                    state.issue("門診清單", "未能確認門診醫師歸屬，未讀取病歷。", mrn=row.mrn, day=day.isoformat())
            elif not row.mrn.strip() or not row.section_code.strip():
                state.count(unknown=1)
                state.issue("門診清單", "病歷號或科別缺漏，無法確認當日就診。", mrn=row.mrn, day=day.isoformat())
            else:
                grouped[row.mrn].append(row)
                state.count(eligible=1)
        state.count(days_done=1)
        state.checkpoint()
    state.count(patients_total=len(grouped))
    return grouped


def _read_patients(sdk, state, settings, grouped, *, library=None, force=False):
    for patient_index, (mrn, rows) in enumerate(grouped.items(), 1):
        state.check_cancel()
        errors_before = state.data["counts"]["errors"]
        state.update(stage="soap", message=f"正在核對就診與 SOAP：{patient_index} / {len(grouped)} 位病人…")
        try:
            cases = _checked_list(sdk.records.get_visit_cases(mrn), VisitCase)
            if any(case.mrn != mrn for case in cases):
                raise ParseError("visit patient mismatch", code="VISIT_PATIENT_MISMATCH")
        except Exception as exc:
            message, code = safe_failure(exc)
            state.issue("就診紀錄", message, code=code, mrn=mrn)
            state.count(unknown=len(rows), patients_done=1)
            if error_info(exc).category == "AUTHENTICATION":
                raise
            continue
        selected = select_registration_visits(rows, cases, doctor_card=settings.username)
        for case in cases:
            # detail_params can contain session context; never persist them.
            state.archive("visits", {"mrn": case.mrn, "date": case.visit_date.isoformat() if case.visit_date else "", "case_type": case.case_type, "case_no": case.case_no, "section_code": case.section_code, "section_name": case.section_name, "doctor_name": case.doctor_name, "doctor_card": case.doctor_card, "selected": case in selected})
        for row in rows:
            matching = select_registration_visits([row], selected, doctor_card=settings.username)
            ambiguous = any(
                case.mrn == mrn and case.case_type.strip().upper() == "O"
                and case.section_code.strip() in {row.section_code.strip(), ""}
                and (case.visit_date is None or (case.visit_date == row.visit_date and not case.section_code.strip()))
                for case in cases
            )
            if ambiguous:
                state.count(unknown=1)
                state.issue("就診紀錄", "就診日期或科別有缺漏，保留可確認的紀錄，其餘無法判定。", mrn=mrn, day=row.visit_date.isoformat())
            elif matching:
                state.count(with_visit=1)
            else:
                state.count(without_visit=1)
        state.count(soap_total=len(selected))
        # Keep PRQ's patient context unchanged from the case query through SOAP reads.
        for case in selected:
            state.check_cancel()
            try:
                key = hashlib.sha256(repr(case.identity).encode()).hexdigest()[:24]
                from .library import current_cache

                cached = library.get_record(key) if library and not force else None
                if cached and not current_cache(case.visit_date.isoformat(), cached["updated_at"]):
                    cached = None
                if cached:
                    cached.pop("versions", None)
                    cached.update(classification(cached, settings))
                    state.add_record(cached)
                    state.count(soap_cached=1)
                    continue
                soap = sdk.records.get_soap(case)
                if not isinstance(soap, SoapRecord) or soap.case.identity != case.identity:
                    raise ParseError("SOAP case mismatch", code="SOAP_CASE_MISMATCH")
                text = soap.full_text
                if not text.strip():
                    state.count(soap_missing=1)
                    state.issue("SOAP", "有當日就診紀錄，但 SOAP 尚無內容。", mrn=mrn, day=case.visit_date.isoformat())
                    continue
                payload = soap_snapshot(soap)
                sources = [row for row in rows if select_registration_visits([row], [case], doctor_card=settings.username)]
                patient = sources[0]
                state.add_record({
                        "id": key,
                        "mrn": mrn, "name": patient.name, "sex": patient.sex, "age": patient.age,
                        "date": case.visit_date.isoformat(), "section": case.section_name or case.section_code,
                        "section_code": case.section_code, "case_no": case.case_no,
                        "rooms": sorted({row.room for row in sources if row.room}),
                        "registration_doctor": patient.doctor_card,
                        "doctor": case.doctor_name, "doctor_card": case.doctor_card,
                        **payload, **classification(payload, settings),
                })
                if "\ufffd" in text:
                    state.issue("文字編碼", "SOAP 含無法解碼的字元，請核對原文。", code="TEXT_DECODE_INCOMPLETE", mrn=mrn, day=case.visit_date.isoformat())
            except StorageError:
                raise
            except Exception as exc:
                message, code = safe_failure(exc)
                state.issue("SOAP", message, code=code, mrn=mrn, day=case.visit_date.isoformat())
                if error_info(exc).category == "AUTHENTICATION":
                    raise
            finally:
                state.count(soap_done=1)
        state.count(patients_done=1)
        state.checkpoint()
        if library and state.data["counts"]["errors"] == errors_before:
            for row in rows:
                ids = [hashlib.sha256(repr(case.identity).encode()).hexdigest()[:24]
                    for case in select_registration_visits([row], selected, doctor_card=settings.username)]
                if all(library.get_record(key) for key in ids):
                    library.save_check(settings.username, {**asdict(row), "visit_date": row.visit_date.isoformat()}, ids)
