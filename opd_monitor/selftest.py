"""Offline executable verification; reachable only through the CLI test flag."""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from vghks_sdk import OutpatientPatient, SoapRecord, VisitCase
from vghks_sdk.core.errors import error_info
from vghks_sdk.models import NumericHistoryReport, NumericTable

from .encoding import decode_response
from .scanner import create_sdk
from .server import Application, LocalServer
from .settings import load_defaults, today


class SyntheticSDK:
    observed_days = set()
    def __init__(self, settings):
        self.card = settings.username
        self.days = set()
        self.auth = SimpleNamespace(check=lambda **_: SimpleNamespace(ok=True))
        self.opd = SimpleNamespace(get_doctor_patients=self.patients)
        self.records = SimpleNamespace(get_visit_cases=self.visits, get_soap=self.soap)
        self.records.get_numeric_history = lambda mrn, _: NumericHistoryReport(
            mrn, (NumericTable("Va", ("日期", "OD", "OS"), (("2025-01-01", "0.4", "HM"),)),))
        self.orders = SimpleNamespace(get_order_history=lambda *_: [])
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def patients(self, card, day):
        self.days.add(day)
        self.observed_days.add(day)
        return [OutpatientPatient(f"TEST{i:03}", name, day, sex, age, "70", "02", card, True,
                                  sequence_no=f"{i:03}")
            for i, (name, sex, age) in enumerate((("測試病人甲", "女", "68歲"), ("測試病人乙", "男", "72歲"), ("測試病人丙", "女", "59歲"), ("測試病人丁", "男", "64歲")), 1)]

    def visits(self, mrn):
        if mrn == "TEST004":
            return []
        return [VisitCase(mrn, day, "O", f"CASE-{mrn}-{day}", "70", "眼科", doctor_name="測試醫師") for day in sorted(self.observed_days | {today() - timedelta(days=i) for i in range(30)})]

    def soap(self, case):
        plans = {
            "TEST001": "# Arrange CATA OD (IOL SN60WF +21.0 T-0.50) on 20260930\n- TEL: 0900-000-001\n- Explain the need for GL after surgery\n# APPLY Cataract OD",
            "TEST002": "# Arrange VT+MP OS (sharkskin+TWIN) on 20261002\n- TEL: 0900-000-002",
            "TEST003": "追蹤視力。Routine follow-up. <img src=x onerror=alert(1)>",
        }
        return SoapRecord(
            case, ("S: 離線測試用虛構病歷。\nBlurred vision.", "O: Test observations.", "A: Test assessment.", "P:\n" + plans[case.mrn]),
            subjective="離線測試用虛構病歷。\nBlurred vision.", objective="Test observations.",
            assessment="Test assessment.", plan=plans[case.mrn], present_sections=("S", "O", "A", "P"),
        )


def self_test(report_path: Path) -> int:
    result = {"ok": False, "mode": "synthetic-only", "checks": []}
    try:
        settings = load_defaults()
        with create_sdk(replace(settings, username="TEST", password="synthetic")) as sdk:
            policy = sdk._runtime.transport.policy
            result["request_pacing"] = {"min_delay_seconds": policy.min_delay_seconds,
                "max_delay_seconds": policy.max_delay_seconds}
        result["checks"].append("SDK transport and Windows TLS initialization (no network)")
        raw = "<meta charset=big5>測試姓名 女 68歲".encode("cp950") + b"\xff"
        if "測試姓名 女 68歲" not in decode_response(raw):
            raise RuntimeError("encoding check failed")
        result["checks"].append("Big5 demographics survive unrelated malformed bytes")
        with tempfile.TemporaryDirectory() as directory:
            app = Application(settings, Path(directory), SyntheticSDK)
            try:
                with LocalServer(0, app) as server:
                    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
                    thread.start()
                    try:
                        for path in ("/", "/app.js", "/workspace.js", "/analysis.js", "/patient-tags.js", "/analysis.css", "/style.css", "/favicon.svg"):
                            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                            connection.request("GET", path)
                            response = connection.getresponse()
                            if response.status != 200 or not response.read():
                                raise RuntimeError("asset check failed")
                            connection.close()
                        result["checks"].append("bundled UI assets and local HTTP")
                        key = next(iter(app.accounts))
                        day = (today() - timedelta(days=1)).isoformat()
                        app.save_accounts({"accounts": [{"id":key,"username":"TEST","password":"synthetic", "start":day}]})
                        list_id = app.browse({"account_ids":[key]})["run_ids"][0]
                        if not app.idle.wait(10) or app.snapshot(list_id)["counts"]["soap_read"]:
                            raise RuntimeError("list stage check failed")
                        rows = app.patient_list({"account_id": key})["days"][0]["rows"]
                        selection = {"selections": [{"account_id": key, "rows": [{"day": r["day"], "id": r["id"]} for r in rows]}]}
                        run_id = app.fetch_selected(selection)["run_ids"][0]
                        if not app.idle.wait(10):
                            raise RuntimeError("scan timeout")
                        snapshot = app.snapshot(run_id)
                        if snapshot["status"] != "completed" or snapshot["counts"]["markers"] != 3 or len(snapshot["records"]) != 3:
                            raise RuntimeError("scan check failed")
                        fields = snapshot["records"][0]["matches"][0]["surgery"]
                        if fields["target"] != "-0.50" or fields["date_iso"] != "2026-09-30":
                            raise RuntimeError("surgery check failed")
                        result["checks"].append("patient ownership, same-day visits, SOAP and surgery fields")
                        if app.library_search({"tag": "__untagged", "q": "追蹤視力"})["total"] != 1:
                            raise RuntimeError("library search check failed")
                        result["checks"].append("SQLite archive retains untagged SOAP and supports offline full-text search")
                        cohort = app.analysis.save_cohort({"source": "library", "all": True, "name": "Self-test"})
                        analysis_id = app.analysis.start({"cohort_id": cohort["id"], "modules": ["retina"]})["run_ids"][0]
                        if not app.idle.wait(10) or app.analysis.store.document("analysis_runs", analysis_id)["status"] != "completed":
                            raise RuntimeError("analysis fetch failed")
                        measurements = app.analysis.results({"cohort_id": cohort["id"], "mrn": "TEST001"})
                        if measurements["numeric"][0]["cells"][1]["raw"] != "HM":
                            raise RuntimeError("special VA failed")
                        from .selftest_analysis import check_google_and_sheet
                        check_google_and_sheet(Path(directory))
                        result["checks"].append("analysis cohorts, historical numeric data, special VA, portable credentials, Google RSA signing and sheet planner")
                        manual = app.analysis.save_cohort({"source": "manual", "name": "Manual self-test",
                                                           "account_id": key, "mrns": "TEST001,TEST001"})
                        if len(manual["members"]) != 1 or manual["members"][0]["mrn"] != "TEST001":
                            raise RuntimeError("manual MRN check failed")
                        tag_reply = app.patient_tags_update({"mrns": "TEST003,0000099,0000099", "new_tag": "追蹤"})
                        manual_tag = tag_reply["settings"]["categories"][-1]["id"]
                        if app.tag_patients({"tag": manual_tag})["total"] != 2:
                            raise RuntimeError("manual tag patient group failed")
                        tagged_soap = app.library_search({"tag": manual_tag})
                        if tagged_soap["total"] != 1 or tagged_soap["records"][0]["matches"]:
                            raise RuntimeError("manual tags changed automatic SOAP matches")
                        tag_cohort = app.analysis.save_cohort({"source": "tags", "all": True,
                            "filters": {"tag": manual_tag}, "account_id": key, "name": "Tag self-test"})
                        if len(tag_cohort["members"]) != 2:
                            raise RuntimeError("tag group analysis selection failed")
                        future = (today() + timedelta(days=7)).isoformat()
                        future_id = app.browse({"account_ids": [key], "ranges": {key: {"start": future}}})["run_ids"][0]
                        if not app.idle.wait(10) or app.snapshot(future_id)["status"] != "completed":
                            raise RuntimeError("future registration check failed")
                        future_rows = app.patient_list({"account_id": key, "start": future})["days"][0]["rows"]
                        if not future_rows or any(r["soap_eligible"] for r in future_rows):
                            raise RuntimeError("future SOAP eligibility failed")
                        result["checks"].append("direct MRN selection and future registration lists with historical analysis dates kept separate")
                        result["counts"] = snapshot["counts"]
                        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                        connection.request("GET", "/api/history")
                        response = connection.getresponse()
                        if response.status != 401:
                            raise RuntimeError("session check failed")
                        response.read()
                        connection.close()
                        result["checks"].append("clinical data requires local session")
                    finally:
                        server.shutdown()
                        thread.join(2)
            finally:
                app.close()
            restored = Application(settings, Path(directory), SyntheticSDK)
            try:
                if len(restored.snapshot(run_id)["records"]) != 3 or any(a.password for a in restored.accounts.values()):
                    raise RuntimeError("persistence check failed")
                def no_network(_):
                    raise RuntimeError("unexpected network")
                restored.sdk_factory = no_network
                cached_list = restored.browse({"account_ids": [key]})["run_ids"][0]
                if not restored.idle.wait(10) or restored.snapshot(cached_list)["status"] != "completed":
                    raise RuntimeError("cached list check failed")
                cached_soap = restored.fetch_selected(selection)["run_ids"][0]
                if not restored.idle.wait(10) or restored.snapshot(cached_soap)["counts"].get("soap_cached") != 3:
                    raise RuntimeError("cached SOAP check failed")
                if restored.library_search({})["total"] != 3:
                    raise RuntimeError("deduplication check failed")
                if restored.tag_patients({"tag": manual_tag})["total"] != 2:
                    raise RuntimeError("manual tags lost on restart or cached SOAP fetch")
                restored.patient_tags_update({"mrns": "0000099", "tag_ids": [manual_tag], "operation": "remove"})
                if restored.tag_patients({"tag": manual_tag})["total"] != 1:
                    raise RuntimeError("manual tag removal failed")
                result["checks"].append("manual patient tags without SOAP, analysis selection, persistence and explicit removal")
                result["checks"].append("restart uses cached lists and SOAP with zero SDK connections, deduplicated by encounter")
                result["checks"].append("restart restores all SOAP without persisting passwords")
                cached_analysis = restored.analysis.start({"cohort_id": cohort["id"], "modules": ["retina"]})["run_ids"][0]
                if not restored.idle.wait(10) or restored.snapshot(cached_analysis)["status"] != "completed":
                    raise RuntimeError("analysis restart cache failed")
                result["checks"].append("restart restores analysis data with zero SDK connections")
            finally:
                restored.close()
        result["date"] = today().isoformat()
        result["ok"] = True
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error_code"] = error_info(exc).code
        result["cause_type"] = error_info(exc).cause_type
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1
