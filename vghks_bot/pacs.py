"""Explicit workstation PACS handoff, independent of report/JPEG acquisition."""
from __future__ import annotations

from vghks_sdk.core.errors import SDKError
from vghks_sdk.integrations.smartiris import SmartIrisClient
from vghks_sdk.models import (
    OrderDetailRef,
    OrderReportRef,
    PacsLaunchContext,
    PacsLaunchRef,
    to_jsonable,
)

from .analysis_fetch import model, unique_refs, validate_patient
from .analysis_store import digest
from .jobs import BusyError
from .review_history import ORDER_LIST_RESOURCES
from .scanner import safe_failure
from .storage import StorageError

PACS_BATCH_LIMIT = 20


def availability_message(value):
    if value.status == "UNSUPPORTED":
        return "本地 PACS 需要在 Windows 工作站執行本程式。"
    if value.owner == "OTHER_PROCESS":
        return "PACS 連接埠由其他程式使用，請確認 DownloadApp 的執行狀態。"
    if value.installation == "MISSING":
        return r"找不到 C:\TEDPCOCX\DownloadApp.exe，請確認本機已安裝院方 PACS 檢閱器。"
    if value.process == "NOT_RUNNING" or value.listener == "NOT_LISTENING":
        return "請先啟動本機院方 PACS 檢閱器／DownloadApp，再按「開啟本地 PACS」。"
    return "無法確認本機 DownloadApp 的程序及連接埠持有者，請確認安裝與執行權限。"


def _batch_counts(total):
    return {"selected": total, "sent": 0, "no_pacs": 0, "selection_required": 0,
            "duplicate": 0, "failed": 0, "unknown": 0, "pending": total}


def _batch_outcome(counts, problem):
    messages = []
    if counts["sent"]:
        messages.append(f'已交接 {counts["sent"]} 份 PACS 檢查，請在檢閱器核對病人與影像。')
    if counts["duplicate"]:
        messages.append(f'{counts["duplicate"]} 筆醫囑指向已交接的相同檢查。')
    if counts["no_pacs"]:
        messages.append(f'{counts["no_pacs"]} 筆醫囑無原生 PACS 入口，可查看 JPG／PDF。')
    if counts["selection_required"]:
        messages.append(f'{counts["selection_required"]} 筆醫囑含多份檢查，請從各列選擇。')
    if counts["failed"]:
        messages.append(f'{counts["failed"]} 筆醫囑未完成交接。')
    if counts["unknown"]:
        messages.append("有一份檢查的交接結果不明。請先查看檢閱器，再決定是否重試。")
    if counts["pending"]:
        messages.append(f'{counts["pending"]} 筆醫囑尚未處理。')
    if problem:
        messages.append(problem)

    if counts["unknown"]:
        status = "OUTCOME_UNKNOWN"
    elif counts["sent"]:
        status = "HANDOFF_SENT" if counts["sent"] + counts["duplicate"] == counts["selected"] else "PARTIAL"
    elif counts["no_pacs"] == counts["selected"]:
        status = "NO_PACS"
    else:
        status = "FAILED"
    return {"status": status, "counts": counts, "viewer_loaded": None, "message": "".join(messages)}


class PacsLauncher:
    def __init__(self, app):
        self.app = app

    def _scope(self, values):
        history = self.app.review.history
        if values.get("resource") in ORDER_LIST_RESOURCES:
            return history.order_selection(values, limit=PACS_BATCH_LIMIT)
        if values.get("resource") != "order_report":
            raise ValueError("請從所選醫囑報告開啟 PACS。")
        if values.get("cohort_id"):
            mrn, reference = values.get("mrn"), values.get("reference")
            if (values.get("review_task_id") or not isinstance(mrn, str)
                    or not isinstance(reference, str) or len(reference) > 128
                    or values.get("module", "cataract") not in {"retina", "cataract"}):
                raise ValueError("分析醫囑範圍不正確。")
            member = self.app.analysis.member(values)
            if member.get("account_id") != self.app.account_id:
                raise ValueError("此病人未指定給目前抓取帳號。")
            return mrn, history.order(mrn, reference,
                cataract=values.get("module", "cataract") == "cataract")
        _, mrn, _, reference = history.scope(values)
        return mrn, history.order(mrn, reference)

    def _references(self, mrn, variants, *, cataract=False):
        history, store = self.app.review.history, self.app.analysis.store
        visits = (self.app.analysis.cataract_sources(mrn)[1] if cataract else
                  history._visits(mrn)[0] or [])
        allowed = history._allowed_mrns(mrn, visits=visits)
        refs, reports = [], [order.get("report_ref") for order in variants]

        def accept(raw, source):
            for item in raw:
                validate_patient(item, source)
                ref = model(PacsLaunchRef, item)
                if ref.mrn != source or source not in allowed:
                    raise ValueError("PACS 檢查不在已核對的病人範圍。")
                refs.append(to_jsonable(ref))

        def branch(cls, method, raw):
            source = raw.get("mrn")
            if source not in allowed:
                raise ValueError("醫囑參照病歷號未經就診清單核對。")
            # Old SDK caches omitted this field. Refresh only the selected
            # branch once; JPEG references are never converted to native refs.
            key = method + ":" + digest(raw)
            saved = [store.step(mrn, key), store.step(mrn, "history-asset:" + key)]
            native_metadata = any(item and "pacs_launch_refs" in item["payload"] for item in saved)
            value = self.app.resources.branch(mrn, cls, method, raw,
                verified_cases=visits, force=not native_metadata).value
            validate_patient(value, source)
            if value.get("reference") != to_jsonable(model(cls, raw)):
                raise ValueError("醫囑回傳參照不符。")
            accept(value.get("pacs_launch_refs") or [], source)
            return value

        for order in variants:
            if order.get("pacs_launch_ref"):
                accept([order["pacs_launch_ref"]], order["mrn"])
        for raw in unique_refs([order.get("detail_ref") for order in variants]):
            value = branch(OrderDetailRef, "get_order_detail", raw)
            reports.extend(value.get("report_refs") or [])
        for raw in unique_refs(reports):
            branch(OrderReportRef, "get_order_report", raw)
        return unique_refs(refs)

    def _launch_context(self, reference):
        # Keep URLs, account bindings and UIDs in memory on the workstation.
        context = self.app.gateway.invoke("orders", "prepare_pacs_launch", reference)
        if type(context) is not PacsLaunchContext or context.reference != reference:
            raise ValueError("PACS 啟動資訊與所選檢查不符。")
        return context

    def _open_batch(self, mrn, orders, viewer):
        counts = _batch_counts(len(orders))
        references, studies = {}, set()
        problem = ""
        for variants in orders:
            counts["pending"] -= 1
            try:
                refs = self._references(mrn, variants)
                if not refs:
                    counts["no_pacs"] += 1
                    continue
                if len(refs) > 1:
                    counts["selection_required"] += 1
                    continue
                reference = model(PacsLaunchRef, refs[0])
                if reference in references:
                    counts["duplicate" if references[reference] else "failed"] += 1
                    continue
                references[reference] = False
                context = self._launch_context(reference)
            except (BusyError, SDKError, StorageError, TypeError, ValueError) as exc:
                counts["failed"] += 1
                if not problem:
                    cause = (safe_failure(exc)[0] if isinstance(exc, (BusyError, SDKError))
                             else "無法核對所選檢查的 PACS 啟動資訊。")
                    problem = "PACS 準備未完成：" + cause
                if isinstance(exc, (BusyError, StorageError)) or not self.app.gateway.online:
                    break
                continue

            study = (context.dicom_server, context.study_uid)
            if study in studies:
                counts["duplicate"] += 1
                references[reference] = True
                continue
            try:
                result = viewer.open_study(context)
            except SDKError:
                counts["unknown"] += 1
                break
            if result.status == "HANDOFF_SENT":
                counts["sent"] += 1
                references[reference] = True
                studies.add(study)
                continue
            # The bridge can fail after receiving part of a command sequence.
            # Stop the batch here and let the operator confirm the viewer.
            if result.status == "FAILED":
                counts["failed"] += 1
                problem = "請確認本地檢閱器／DownloadApp 的執行狀態。"
            else:
                counts["unknown"] += 1
            break
        return _batch_outcome(counts, problem)

    def open(self, values):
        # Validate account/cohort, patient, visit and selected order before any
        # workstation inspection or hospital request.
        self._scope(values)
        if self.app.root.read_only:
            raise ValueError("目前是唯讀檢閱；請建立可編輯副本後操作。")
        lock = self.app.root.pacs_lock
        if not lock.acquire(blocking=False):
            raise BusyError("正在開啟另一份 PACS 檢查，請待交接完成後再操作。")
        try:
            with self.app.session(), SmartIrisClient() as viewer:
                mrn, selection = self._scope(values)
                if not self.app.gateway.online:
                    raise ValueError("請先登入此帳號，再開啟本地 PACS。")
                batch = values.get("resource") in ORDER_LIST_RESOURCES
                availability = viewer.check_availability()
                if availability.status != "AVAILABLE":
                    result = {"status": "UNAVAILABLE", "availability": to_jsonable(availability),
                              "message": availability_message(availability)}
                    if batch:
                        result.update(counts=_batch_counts(len(selection)), viewer_loaded=None)
                    return result
                if batch:
                    return self._open_batch(mrn, selection, viewer)
                refs = self._references(mrn, selection, cataract=bool(values.get("cohort_id"))
                                        and values.get("module", "cataract") == "cataract")
                if not refs:
                    return {"status": "NO_PACS", "message": "此醫囑未提供原生 PACS 檢閱入口，可繼續檢閱已存 JPG／PDF 報告。"}
                selected = values.get("launch_id", "")
                if not isinstance(selected, str):
                    raise ValueError("PACS 檢查選擇不正確。")
                if not selected and len(refs) > 1:
                    return {"status": "SELECTION_REQUIRED", "message": "此醫囑有多份 PACS 檢查，請選擇要開啟的一份。",
                            "choices": [{"id": digest(ref), "label": "檢查單 " + ref["request_no"]} for ref in refs]}
                selected = selected or digest(refs[0])
                ref = next((ref for ref in refs if digest(ref) == selected), None)
                if ref is None:
                    raise ValueError("所選 PACS 檢查不在此醫囑，請重新選擇。")
                context = self._launch_context(model(PacsLaunchRef, ref))
                result = viewer.open_study(context)
                if result.status == "HANDOFF_SENT":
                    message = "已交接本地 PACS，請在檢閱器確認病人與影像。"
                elif result.status == "OUTCOME_UNKNOWN":
                    message = "PACS 交接結果不明，請先查看本地檢閱器；系統未自動重送。"
                else:
                    message = "PACS 交接未完成，請確認本地檢閱器／DownloadApp 的執行狀態。"
                return {"status": result.status, "code": result.error_code,
                        "viewer_loaded": result.viewer_loaded, "message": message}
        except SDKError as exc:
            message, code = safe_failure(exc)
            return {"status": "FAILED", "code": code, "message": "PACS 準備未完成：" + message}
        finally:
            lock.release()
