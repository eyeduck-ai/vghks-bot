"""Synthetic verification, also executed inside the frozen Windows binary."""
from __future__ import annotations

import http.client
import json
import shutil
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from vghks_sdk import OutpatientPatient, SoapRecord, VisitCase
from vghks_sdk import __version__ as sdk_version
from vghks_sdk.models import (
    NumericHistoryReport,
    NumericReport,
    NumericTable,
    PatientDemographics,
    RegistrationRecord,
    SurgeryRecord,
    SurgeryScheduleProcedure,
)
from vghks_sdk.models.documents import EarningsReportContext, FormSnapshot, HtmlDocument, HtmlTable
from vghks_sdk.models.review import ReviewCase, ReviewCasePart, ReviewCaseRef

from .bot import BotApplication
from .bot_server import BotServer
from .bounded_search import search
from .encoding import decode_response
from .selftest_portable import check_portable_credentials
from .selftest_soap import synthetic_soap
from .settings import Settings, today


class BotSyntheticSDK:
    """No network calls. Assert patient context before returning clinical data."""
    calls = []
    fail_case = ""
    revision = "original"
    on_soap = None

    def __init__(self, settings):
        self.card, self.context = settings.username, None
        self.auth = SimpleNamespace(check=lambda **_: SimpleNamespace(ok=settings.password != "invalid"))
        self.opd = SimpleNamespace(get_doctor_patients=self.listing)
        self.patients = SimpleNamespace(get_demographics=self.demographics,
            get_registration_history=self.registrations, resolve_identity=self.resolve)
        self.records = SimpleNamespace(get_visit_cases=self.visits, get_soap=self.soap,
            get_numeric_report=self.numeric, get_numeric_history=self.history)
        self.orders = SimpleNamespace(get_order_history=lambda *_: [], get_case_orders=lambda *_: [])
        self.surgery = SimpleNamespace(get_patient_info=lambda mrn: {"mrn": mrn}, get_schedule=self.surgery_schedule)
        self.reviews = SimpleNamespace(get_cases=self.review_cases, get_case=self.review_case,
            get_options=self.review_options, get_doctors=self.review_doctors,
            get_orders=lambda ref: self.review_part(ref, "orders"),
            get_attachments=lambda ref: self.review_part(ref, "attachments"),
            get_pacs=lambda ref: self.review_part(ref, "pacs"))
        self.earnings = SimpleNamespace(open_performance=lambda c: self.earnings_open(c, "performance"),
            open_bonus=lambda c: self.earnings_open(c, "payroll"), get_report=self.earnings_report)

    def surgery_schedule(self, card, start, end, **filters):
        self.calls.append((self.card, "surgery-schedule", card, start.isoformat(), end.isoformat(), filters))
        rows = []
        for index, offset in enumerate((-3, 0, 1, 14)):
            day = today() + timedelta(days=offset)
            if start <= day <= end:
                rows.append(SurgeryRecord(patient_mrn=filters.get("mrn") or f"0000000{index+1}",
                    case_no=f"SYNTH-{index}", surgery_date=day.isoformat(),
                    start_time="" if index == 3 else "0830", end_time="" if index == 3 else "0930",
                    room="OP-01", doctor_card=card, doctor_name=card+" 合成醫師",
                    procedure="Phaco-IOL OD" if index % 2 else "VT OS", status="已取消" if offset < 0 else "已排程",
                    patient_name=f"合成病人{index+1}", patient_sex="女", ward="OPD", department="OPH",
                    anesthesia="GA", category="常規", schedule_time="TF1" if index == 3 else "0830",
                    time_status="UNCONFIRMED" if index == 3 else "CLOCK_TIME",
                    request_no=f"REQ-{index}", sequence_no=str(index), case_type="O",
                    procedures=(SurgeryScheduleProcedure(1, "SYNTH-OP", "Phaco-IOL OD" if index % 2 else "VT OS"),),
                    diagnosis_codes=("H26",), diagnosis_text="合成資料", extra={"orpatnam": "舊欄位姓名"}))
        return rows

    def earnings_open(self, credentials, kind):
        from vghks_sdk import AuthenticationError
        self.calls.append((self.card, "earnings-open", kind))
        if credentials.password == "invalid":
            raise AuthenticationError("synthetic rejection", code="EARNINGS_PASSWORD_REJECTED")
        return EarningsReportContext(kind, 1, FormSnapshot("report", (("SSO", "never-persist-this-token"),),
            {"BEGYM": (("11508", "115 年 08 月"), ("11509", "115 年 09 月"))}, "sensitive-context"))

    def earnings_report(self, context, period):
        self.calls.append((self.card, "earnings-report", context.kind, period))
        html = ('<table id="合成報表"><tr><th rowspan="2">項目</th><th colspan="2">明細</th></tr>'
                '<tr><th>數值</th><th>備註</th></tr><tr><td>績點</td><td>1,234.50</td><td>合成資料</td></tr>'
                '<tr><td>扣項</td><td>(200)</td><td>調整</td></tr><tr><td>比率</td><td>12.5%</td>'
                '<td>&lt;script&gt;不可執行&lt;/script&gt;</td></tr><tr><td>代碼</td><td>00123</td>'
                '<td>=SUM(A1:A2)</td></tr></table>')
        return HtmlDocument((HtmlTable("合成報表", (("項目", "數值"), ("績點", "1,234.50"))),),
                            f"合成 {self.card} {context.kind} {period} {self.revision}", html)

    def _review_rows(self):
        result = []
        for seq, mrn, code in (("1001", "TEST001", "4"), ("1002", "TEST001", "1"), ("1003", "TEST002", "9")):
            fields = {"ApplySeq": seq, "PatNo": mrn, "PatName": "合成病人 " + mrn,
                      "ApplyStatus": "已送件", "ApplyFinishFlag": "已處理", "VerifyCode": code,
                      "ReviewComment": "合成審查資料 <script>不得執行</script>"}
            result.append(ReviewCase(ReviewCaseRef(seq), today().isoformat(), mrn, fields["PatName"],
                self.card, "70", code, fields["ApplyStatus"], fields["ApplyFinishFlag"], fields))
        return result

    def review_cases(self, filters):
        self.calls.append((self.card, "review-cases", filters.mrn, filters.verify_code))
        if filters.mrn == "FAIL":
            raise ValueError("synthetic review unavailable")
        return [r for r in self._review_rows() if (not filters.mrn or r.mrn == filters.mrn)
                and (not filters.verify_code or r.verify_code == filters.verify_code)]

    def review_case(self, ref):
        self.calls.append((self.card, "review-detail", ref.apply_seq))
        return next(r for r in self._review_rows() if r.reference == ref)

    def review_part(self, ref, kind):
        self.calls.append((self.card, "review-" + kind, ref.apply_seq))
        row = {"ApplySeq": ref.apply_seq, "項目": "合成 " + kind}
        if kind == "orders":
            row["OrderName"] = "合成醫囑 " + ref.apply_seq
        return ReviewCasePart(ref, kind, (row,), 1)

    def review_options(self):
        self.calls.append((self.card, "review-options"))
        return {"InsuSectNo": [{"Value": "70", "Text": "眼科"}], "VSDrID": self.review_doctors("70"),
                "VerifyCode": [{"Value": "4", "Text": "補件"}, {"Value": "1", "Text": "同意備查"}],
                "ApplyMode": [{"Value": "1", "Text": "事前審查"}]}

    def review_doctors(self, department):
        self.calls.append((self.card, "review-doctors", department))
        return [{"Value": self.card, "Text": "合成醫師"}]

    def __enter__(self):
        self.calls.append((self.card, "login"))
        return self

    def __exit__(self, *_):
        pass

    def listing(self, card, day):
        self.calls.append((self.card, "list", day.isoformat()))
        return [OutpatientPatient(f"TEST{i:03}", f"合成病人{i}", day, "女", "68歲", "70", "02", doctor, True,
                                  sequence_no=f"{i:03}")
                for i, doctor in ((1, card), (2, card), (3, "OTHER"), (4, ""))]

    def demographics(self, mrn):
        self.calls.append((self.card, "profile", mrn))
        if mrn == "BAD":
            return PatientDemographics("MISMATCH", "不符")
        return PatientDemographics(mrn, "合成病人 " + mrn, "0900000000", birthday="1958-01-01", sex="女")

    def resolve(self, identifier):
        self.calls.append((self.card, "identity", identifier))
        return "NEW000"  # A verified identity without any encounters.

    def visits(self, mrn):
        self.context = mrn
        self.calls.append((self.card, "index", mrn))
        if mrn == "FAIL":
            raise ValueError("synthetic index failure")
        if mrn in {"NEW000", "TEST004"}:
            return []
        return [VisitCase(mrn, today() - timedelta(days=offset), "O", case, "70", section,
                          doctor_name="另一位醫師", doctor_card="OTHER")
                for offset, case, section in ((0, "EMPTY", "眼科上午"), (1, "ONE", "眼科約診"),
                                             (1, "TWO", "眼科下午"), (2, "OLD", "眼科"), (0, "OTHER", "內科"))]

    def soap(self, case):
        if self.context != case.patient_mrn:
            raise AssertionError("wrong patient context")
        self.calls.append((self.card, "soap", case.mrn, case.case_no))
        if type(self).on_soap:
            type(self).on_soap(case)
        if case.case_no == self.fail_case:
            raise ValueError("synthetic SOAP failure")
        if case.case_no == "EMPTY":
            return SoapRecord(case, ())
        return synthetic_soap(case, f"{self.card} {self.revision} {case.case_no}")

    def numeric(self, case):
        assert self.context == case.patient_mrn
        self.calls.append((self.card, "numeric", case.mrn))
        return NumericReport(case, (NumericTable("Va", ("OD", "OS"), (("0.4", "HM"),)),))

    def history(self, mrn, _):
        self.context = mrn
        self.calls.append((self.card, "numeric-history", mrn))
        return NumericHistoryReport(mrn, (NumericTable("Va", ("日期", "OD", "OS"), (("2025-01-01", "0.4", "HM"),)),))

    def registrations(self, mrn):
        self.context = mrn
        self.calls.append((self.card, "registrations", mrn))
        return [RegistrationRecord({}, mrn=mrn, visit_date=today()+timedelta(days=7), section_name="眼科", status=""),
                RegistrationRecord({}, mrn=mrn, visit_date=today()+timedelta(days=1), section_name="眼科", cancelled_at="已取消")]


def wait_task(workspace, key, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = workspace.review.task(key)
        if task["status"] not in {"queued", "running", "cancelling"}:
            return task
        time.sleep(.02)
    raise AssertionError("synthetic task timeout")


def self_test(report_path: Path) -> int:
    result = {"ok": False, "mode": "synthetic-only", "sdk_version": sdk_version, "checks": []}
    try:
        from vghks_sdk.parsing.prq import parse_visit_cases

        active = 'new KSCase("QueryCaseDetail.do?hhisnum=TEST001&caseDT=2026-09-21&caseType=O&caseNo=1&caseSec=70", "2026-09-21", type, section, "測試醫師");'
        inactive = active.replace("TEST001", "TEST002")
        cases = parse_visit_cases(f"<script>if (true) {{{active}}} else {{{inactive}}}</script>", "TEST001")
        assert len(cases) == 1 and cases[0].mrn == "TEST001"
        result["checks"].append("SDK active visit branch excludes inactive foreign patient links")
        BotSyntheticSDK.calls = []
        with tempfile.TemporaryDirectory() as directory:
            check_portable_credentials(Path(directory))
            result["checks"].append("portable account and Google credentials, database-only relocation without DPAPI")
            path = Path(directory) / "original"
            path.mkdir()
            app = BotApplication(Settings(), path, BotSyntheticSDK)
            try:
                first = app.login({"username": "TEST", "password": "synthetic-password"})["account"]["id"]
                second = app.login({"username": "SECOND", "password": "synthetic-second"})["account"]["id"]
                workspace = app.workspace(first)
                other = app.workspace(second)
                assert app.registry.password(first) == "synthetic-password"
                assert b"synthetic-password" not in (path / "accounts.sqlite3").read_bytes()
                assert "synthetic-password" not in json.dumps(app.bootstrap())
                result["checks"].append("portable credentials and public account metadata")
                resolve = workspace.review.start({"kind": "resolve", "identifiers": "TEST001,TEST002"})["task_id"]
                assert wait_task(workspace, resolve)["status"] == "completed"
                group = workspace.review.save_set({"mrns": "TEST001,TEST002,TEST001"})
                assert len(group["members"]) == 2
                review = workspace.review.start({"set_id": group["id"]})["task_id"]
                task = wait_task(workspace, review)
                assert task["status"] == "completed", task
                assert all(len(p["records"]) == 2 and p["fallback"] for p in task["items"])
                assert workspace.review.results({"id": review, "q": "# APPLY", "search_mode": "regex"})["total"] == 2
                from .selftest_efficiency import check_efficiency

                check_efficiency(workspace, review)
                result["checks"].append("scoped SQL search, rebuildable tag indexes, body-free progress, immutable membership, inventory pages and 4/64 task limits")
                assert other.library_search({})["total"] == 0
                record = task["items"][0]["records"][0]
                structured = record["soap_structure"]
                assert structured["subjective"] and structured["objective"]
                assert "# Arrange" in structured["assessment_plan"]
                assert structured["medications"][0]["frequency"] == "BID"
                assert structured["orders"][0]["quantity"] == "1.00"
                assert structured["chronic_prescription_periods"][0]["start_date"] == "2026-09-21"
                from .tags import classification

                scoped = Settings().update({"categories": [{"id": "ap", "name": "AP", "keywords": ["# Arrange"], "scope": "ap"}]})
                hits = classification(record, scoped)
                assert hits["matches"][0]["source_field"] == "assessment_plan" and not hits["tag_scope_issues"]
                assert workspace.library_search({})["records"][0]["ap_preview"][0]["label"] == "A+P"
                result["checks"].append("structured SOAP, printed orders/medications, chronic period, scoped tags and AP preview")
                assert not any(c[1] in {"numeric", "registrations"} for c in BotSyntheticSDK.calls)
                result["checks"].append("same-day SOAP, cross-doctor aliases, fallback, task scope, account isolation and lazy extensions")
                numeric = workspace.review.start({"kind": "numeric", "mrn": "TEST001", "record_id": task["items"][0]["records"][0]["id"]})["task_id"]
                assert wait_task(workspace, numeric)["items"][0]["payload"]["tables"][0]["rows"][0][1] == "HM"
                future = (today() + timedelta(days=1)).isoformat()
                run = workspace.browse({"account_ids": [first], "ranges": {first: {"start": future}}})["run_ids"][0]
                assert workspace.idle.wait(10) and workspace.snapshot(run)["status"] == "completed"
                assert workspace.patient_list({"account_id": first, "start": future})["days"][0]["rows"]
                identity = workspace.review.start({"kind": "resolve", "identifier_kind": "national_id", "identifiers": "A123456789"})["task_id"]
                assert wait_task(workspace, identity)["items"][0]["mrn"] == "NEW000"
                result["checks"].append("future registrations and verified ID resolution with zero encounters")
                cohort = workspace.review.cohort({"set_id": group["id"]})
                surgical = workspace.analysis.surgery_candidates({"cohort_id": cohort["id"]})["candidates"]
                assert len(surgical) == 2 and all(c["fields"]["procedure"] == "Phaco-IOL" for c in surgical)
                assert all(len(c["sources"]) == 2 for c in surgical)
                result["checks"].append("inline P: Arrange candidates and same-day source merging")
                run = workspace.analysis.start({"cohort_id": cohort["id"], "modules": ["retina", "cataract"]})["run_ids"][0]
                assert workspace.idle.wait(10) and workspace.snapshot(run)["status"] == "completed"
                result["checks"].append("retina and cataract reuse the account-scoped analysis core")
                auto = workspace.review.start({"kind": "resolve", "identifier_kind": "auto", "identifiers": "00012345\na123456789"})["task_id"]
                people = wait_task(workspace, auto)["items"]
                assert {p["mrn"] for p in people} == {"00012345", "NEW000"}
                all_group = workspace.review.save_set({"mrns": "00012345"})
                internal = wait_task(workspace, workspace.review.start({"set_id": all_group["id"], "department_keyword": "內科"})["task_id"])
                assert [r["case_no"] for r in internal["items"][0]["records"]] == ["OTHER"]
                result["checks"].append("automatic identifier routing and latest outpatient SOAP by department substring")
                schedule = wait_task(workspace, workspace.review.start({"kind": "surgery_schedule",
                    "start": (today()-timedelta(days=7)).isoformat()})["task_id"])
                assert schedule["status"] == "completed" and schedule["progress"]["done"] == 1
                before = list(BotSyntheticSDK.calls)
                saved_schedule = workspace.surgery_schedule.overview()["result"]
                assert len(saved_schedule["rows"]) == 4
                assert {r["group"] for r in saved_schedule["rows"]} == {"past", "upcoming"}
                assert other.surgery_schedule.overview()["result"] is None
                assert before == BotSyntheticSDK.calls
                result["checks"].append("account surgery schedule, date buckets, original status, durable snapshots and offline reads")
                sync = wait_task(workspace, workspace.review.start({"kind": "approval_sync"})["task_id"])
                assert sync["status"] == "completed" and sync["finished_at"]
                assert sum(bool(i.get("version")) and "case" not in i for i in sync["items"]) == 3
                assert sum(bool(i.get("order_names")) for i in sync["items"]) == 3
                assert workspace.approvals.cases()["total"] == 3
                from vghks_sdk.parsing.documents import parse_document
                for table_id in ("first", "second"):
                    workspace.earnings.store_report("performance", "202610", "2026/10", parse_document(
                        '<table id="' + table_id + '"><tr><th>績點</th><td>1234</td></tr></table>'))
                report = workspace.earnings.overview()["reports"][0]
                assert report["version_count"] == 1
                assert len(workspace.earnings.detail({"id": report["id"]})["fetches"]) == 2
                from .clinical_identity import doctor_identity
                assert doctor_identity("AB42B") == "AB42" != doctor_identity("AB420")
                result["checks"].append("v6.2 full-list sync, content references, semantic salary versions and physician suffixes")
                with workspace.gateway.task_context("selftest-diagnostic", "review"):
                    try:
                        workspace.gateway.invoke("records", "get_visit_cases", "FAIL")
                    except ValueError:
                        pass
                diagnostic = workspace.diagnostics.query({"mrn": "FAIL", "task_id": "selftest-diagnostic"})
                assert diagnostic["items"][0]["attempts"][0]["error"]["stack"]
                recorder = workspace.diagnostics.recorder()
                recorder.record_http_request(method="GET", url="https://synthetic.invalid/QueryCaseList.do?hid=SECRET",
                    attempt=1, max_attempts=1, throttle_delay_seconds=0, tls_verification_enabled=True, kwargs={})
                recorder.finalize(command="selftest", status="OK", exit_code=0)
                assert "SECRET" not in recorder.trace_path.read_text(encoding="utf-8")
                result["checks"].append("portable failure diagnostics, SDK trace recording and credential-value exclusion")
                from .selftest_diagnostics import (
                    check_failure_trace_policy,
                    check_order_index_evidence,
                )

                check_order_index_evidence(workspace)
                result["checks"].append("order-index query scopes, full clinical failure evidence ZIP and offline SDK replay")
                check_failure_trace_policy(workspace)
                result["checks"].append("compact successful HTTP logging, bounded session failure evidence, SDK manifest and probe replay")
                from .selftest_connection import check_connection_contract

                check_connection_contract(workspace)
                result["checks"].append("SDK acquisition states, nested recovery causes, password countdown and no outer login replay")
                from .selftest_session import check_sdk_session_recovery

                check_sdk_session_recovery(Path(directory) / "native-session")
                result["checks"].append("native SDK WebMAAS SSO recovery without account rebuild or Portal password POST, original timeout diagnostics")
                from .selftest_cataract import check_numeric_prefetch
                check_numeric_prefetch(workspace)
                result["checks"].append("durable decimal refraction/KM parsing, serial background wait and selected-patient priority")
                from .selftest_library_data import check_clinical_clearing
                check_clinical_clearing(workspace, other)
                result["checks"].append("offline classified cache clearing, shared attachment retention, cache-only patients and account isolation")
                from .selftest_exports import check_saved_export
                before = list(BotSyntheticSDK.calls)
                check_saved_export(workspace, other)
                assert before == BotSyntheticSDK.calls
                result["checks"].append("portable offline cataract ZIP, grouped numeric, SOAP, original PDF/JPG and shared attachment deduplication")
                with BotServer(0, app) as server:
                    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
                    thread.start()
                    try:
                        for url in ("/", "/bot.js", "/bot.css", "/databases.js", "/approvals.js", "/tool-workspace.js", "/monitor-ui.js", "/earnings.js", "/surgery-system.js", "/surgery-system.css", "/progress.js", "/progress.css", "/clinical-ui.js", "/clinical-ui.css", "/library-data.js", "/cataract-export.js", "/tools", "/tool-bridge.js", "/analysis.js", "/analysis.css", "/app.js", "/workspace.js", "/patient-tags.js", "/style.css", "/favicon.svg"):
                            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                            connection.request("GET", url)
                            reply = connection.getresponse()
                            assert reply.status == 200 and reply.read(), url
                            connection.close()
                        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                        connection.request("GET", f"/api/accounts/{first}/workbench")
                        reply = connection.getresponse()
                        assert reply.status == 401
                        reply.read()
                        connection.close()
                    finally:
                        server.shutdown()
                        thread.join(2)
                result["checks"].append("bundled UI assets, scoped HTTP and local session authentication")
                from .selftest_analysis import check_google_and_sheet
                check_google_and_sheet(workspace.store.directory)
                result["checks"].append("Google RSA credentials and sheet diff planner")
            finally:
                app.close()
            copied = Path(directory) / "relocated"
            shutil.copytree(path, copied)
            app = BotApplication(Settings(), copied, BotSyntheticSDK)
            try:
                app.offline(first)
                before = list(BotSyntheticSDK.calls)
                assert app.workspace(first).library_data.cleared("CACHEONLY")
                assert app.workspace(first).review.results({"id": review})["total"] == 2
                assert BotSyntheticSDK.calls == before
                app.login({"id": first})
                result["checks"].append("relocated workspaces, offline restart and remembered login without DPAPI")
            finally:
                app.close()
        from .selftest_databases import check_databases
        with tempfile.TemporaryDirectory() as directory:
            check_databases(Path(directory))
        result["checks"].append("database snapshots, portable copies, read-only source preservation, HTTP mutation guards and saved approval queries")
        result["checks"].append("approval follow-up, real task progress, both MIS earnings reports, portable secondary credentials, archive CSV and read-only API")
        assert "測試姓名" in decode_response("測試姓名 女".encode("cp950"))
        assert search("# Arrange", ["P: # Arrange CATA"])
        try:
            search("(a+)+$", ["a" * 60000 + "!"], timeout=.6)
            raise AssertionError("regex timeout missing")
        except ValueError:
            pass
        result["checks"].append("Chinese encoding and isolated regex timeout in frozen runtime")
        result["ok"] = True
    except Exception as exc:
        result.update(error_type=type(exc).__name__, error=str(exc)[:1500])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1
