"""Two-stage collection with lazy login: cache hits make zero intranet calls."""
from __future__ import annotations

from collections import defaultdict
from contextlib import ExitStack
from dataclasses import asdict, fields
from datetime import date
from types import SimpleNamespace

from vghks_sdk import AuthenticationError, OutpatientPatient

from .scanner import _checked_list, _collect, _read_patients, create_sdk
from .settings import parse_range
from .tags import classification


def patient_value(patient):
    return {**asdict(patient), "visit_date": patient.visit_date.isoformat()}


def patient_model(value):
    valid = {f.name for f in fields(OutpatientPatient)}
    return OutpatientPatient(**{k: date.fromisoformat(v) if k == "visit_date" else v for k, v in value.items() if k in valid})


def run_workflow(state, settings, library, *, rows=None, force=False, sdk_factory=create_sdk):
    with ExitStack() as stack:
        connection = None

        def sdk():
            nonlocal connection
            if connection is None:
                state.check_cancel()
                if not settings.username or not settings.password:
                    raise ValueError("需要連線取得新資料，請輸入此帳號的密碼。")
                state.update(status="running", stage="auth", message="正在驗證登入…")
                connection = stack.enter_context(sdk_factory(settings))
                report = connection.auth.check(only=("prq",))
                state.check_cancel()
                if not report.ok:
                    raise AuthenticationError("Authentication failed", code="AUTH_CHECK_FAILED")
            return connection

        state.check_cancel()
        state.update(status="running", stage="registrations" if rows is None else "soap", message="正在讀取本機資料…")
        state.data["counts"].update(days_cached=0, soap_cached=0)
        if rows is None:
            def doctor_patients(account, day):
                cached = None if force else library.cached_list(account, day.isoformat())
                if cached is not None:
                    state.count(days_cached=1)
                    return [patient_model(value) for value in cached["rows"]]
                result = _checked_list(sdk().opd.get_doctor_patients(account, day), OutpatientPatient)
                # An unexpected date must never become a permanent historical cache.
                if all(row.visit_date == day for row in result):
                    library.save_list(account, day.isoformat(), [patient_value(row) for row in result])
                return result

            start, end = parse_range(state.data, allow_future=True)
            _collect(SimpleNamespace(opd=SimpleNamespace(get_doctor_patients=doctor_patients)), state, settings, start, end)
        else:
            grouped = defaultdict(list)
            for value in rows:
                grouped[value["mrn"]].append(value)
            state.count(patients_total=len(grouped), registrations=len(rows), eligible=len(rows))
            for mrn, patient_rows in grouped.items():
                state.check_cancel()
                missing = []
                for value in patient_rows:
                    state.archive("registrations", value)
                    cached = None if force else library.completed_registration(settings.username, value)
                    if cached is None:
                        missing.append(patient_model(value))
                        continue
                    state.count(with_visit=int(bool(cached)), without_visit=int(not cached))
                    for record in cached:
                        record.update(classification(record, settings))
                        before = state.data["counts"]["soap_read"]
                        state.add_record(record)
                        if state.data["counts"]["soap_read"] > before:
                            state.count(soap_cached=1, soap_total=1, soap_done=1)
                if missing:
                    _read_patients(sdk(), state, settings, {mrn: missing}, library=library, force=force)
                else:
                    state.count(patients_done=1)
                    state.update(message=f"已讀取本機病歷：{state.data['counts']['patients_done']} / {len(grouped)} 位病人")
        state.check_cancel()
        incomplete = bool(state.data["counts"]["errors"])
        state.update(status="partial" if incomplete else "completed", stage="done",
            message="部分資料未能取得，請查看明細。" if incomplete else "門診清單已保存，可勾選病人分析歷年檢查或抓取已就診 SOAP。" if rows is None else "SOAP 已保存。")
