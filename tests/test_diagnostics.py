import http.client
import io
import json
import tempfile
import threading
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from vghks_sdk import LoginRejectedError, ParseError, SoapRecord
from vghks_sdk.models import OrderHistoryFilter
from vghks_sdk.parsing.clinical import parse_clinical_orders

from vghks_bot.bot import BotApplication
from vghks_bot.bot_server import BotServer
from vghks_bot.databases import copy_database
from vghks_bot.scanner import create_sdk
from vghks_bot.selftest_bot import BotSyntheticSDK, wait_task
from vghks_bot.settings import Settings, today

UNSUPPORTED_ORDERS_HTML = '''<script>
    var orderStr = unsupportedHospitalFunction("SYNTHETIC_PATIENT_BODY");
    new KSCase("", orderStr, "2026-09-11", "2026-09-11", "", "");
    </script>'''


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.app = BotApplication(Settings(), self.path / "source", BotSyntheticSDK)
        self.key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.key)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def task(self, **values):
        return wait_task(self.work, self.work.review.start(values)["task_id"])

    def review(self, **values):
        self.task(kind="resolve", identifiers="TEST001")
        group = self.work.review.save_set({"mrns": "TEST001"})
        return self.task(kind="review", set_id=group["id"], **values)

    def analysis(self, **values):
        cohort = self.work.analysis.save_cohort({"source": "manual", "account_id": self.key,
                                                "name": "合成白內障", "mrns": "TEST001"})
        key = self.work.analysis.start({"cohort_id": cohort["id"], "modules": ["cataract"],
                                        "mrn": "TEST001", **values})["run_ids"][0]
        self.assertTrue(self.work.idle.wait(8))
        return self.work.snapshot(key)

    def unsupported_orders(self, mrn, _filters):
        return parse_clinical_orders(UNSUPPORTED_ORDERS_HTML, mrn=mrn)

    def test_index_failures_identify_both_queries_and_preserve_completed_data_on_resume(self):
        with (patch.object(self.work.gateway.connection.orders, "get_order_history", side_effect=self.unsupported_orders),
              patch.object(self.work.gateway.connection.orders, "get_case_orders", return_value=[], create=True)):
            task = self.analysis()
        self.assertEqual(task["status"], "partial")
        self.assertTrue(self.work.gateway.online)
        saved = self.work.diagnostics.query({"task_id": task["id"]})
        self.assertEqual(len(saved["failures"]), 2)
        self.assertEqual({item["query"]["category"] for item in saved["failures"]}, {"*", "OR"})
        self.assertEqual({item["key"] for item in saved["saved_attempts"]}, {"orders-history:*", "orders-history:OR"})
        self.assertTrue(all("醫囑索引" in item["label"] for item in saved["failures"]))
        self.assertTrue(all(item["parser_context"]["variable"] == "orderStr" for item in saved["failures"]))
        self.assertTrue(all("[字串]" in item["parser_context"]["expression_shape"] for item in saved["failures"]))
        self.assertTrue(all("靜態解析" in item["reason"] for item in saved["failures"]))
        self.assertIn("SYNTHETIC_PATIENT_BODY", json.dumps(saved))
        self.assertIn("unsupportedHospitalFunction", json.dumps(saved))
        self.assertTrue(saved["contains_medical_values"])
        self.assertTrue(any(event["service"] == "auth" and event["status"] == "ok" for event in saved["sdk_events"]))
        store = self.work.analysis.store
        self.assertIsNotNone(store.step("TEST001", "numeric-history"))
        self.assertIsNotNone(store.step("TEST001", "cataract-soap"))
        self.assertIsNone(store.step("TEST001", "orders-history:*"))
        calls = list(BotSyntheticSDK.calls)
        with patch.object(self.work.gateway.connection.orders, "get_case_orders", return_value=[], create=True):
            resumed = self.analysis(resume=task["id"])
        self.assertEqual(resumed["status"], "completed")
        added = BotSyntheticSDK.calls[len(calls):]
        self.assertFalse(any(item[1] in {"numeric-history", "soap"} for item in added))
        self.assertEqual(self.work.diagnostics.query({"task_id": task["id"]})["items"], saved["items"])
        self.assertEqual(saved["task"]["analysis_name"], "白內障術前分析")
        history = self.work.history()["runs"]
        self.assertEqual(next(row for row in history if row["id"] == task["id"])["modules"], ["cataract"])

    def test_legacy_analysis_keeps_unknown_query_category_and_restores_module_name_offline(self):
        from vghks_bot.settings import timestamp

        state, _, _ = self.work.analysis.start({"cohort_id": self.work.analysis.save_cohort({
            "source": "manual", "account_id": self.key, "name": "舊分析", "mrns": "TEST001"})["id"],
            "modules": ["cataract"]}, enqueue=False)
        state.data.pop("modules")
        state.data.pop("name")
        for _ in range(2):
            state.issue("order_index", "院內回傳格式無法辨識，這筆資料尚未確認。",
                        code="JS_EXPRESSION_UNSUPPORTED", mrn="TEST001")
            self.work.review.db.save("patient_diagnostic", {
                "task_id": state.data["id"], "recorded_at": timestamp(), "schema_version": 1,
                "mrn": "TEST001", "phase": "orders.get_order_history", "sdk_version": "0.20.11",
                "error": {"code": "JS_EXPRESSION_UNSUPPORTED", "category": "PARSE",
                          "stack": [{"file": "jsliteral.py", "function": "evaluated_string_assignments", "line": 192}]}})
        state.update(status="partial")
        self.work.analysis.finish(state)
        self.work.states.pop(state.data["id"])
        self.app.logout(self.key)
        self.app.offline(self.key)
        saved = self.work.diagnostics.query({"task_id": state.data["id"]})
        self.assertEqual(len(saved["failures"]), 2)
        self.assertTrue(all("類別未記錄" in item["label"] for item in saved["failures"]))
        self.assertTrue(all("無法" in item["detail_note"] for item in saved["failures"]))
        self.assertTrue(all("靜態解析" in item["reason"] for item in saved["failures"]))
        self.assertTrue(all(not item["parser_context"] for item in saved["failures"]))
        self.assertEqual(saved["task"]["modules"], ["cataract"])
        self.assertEqual(saved["task"]["analysis_name"], "白內障術前分析")
        self.assertEqual(self.work.history()["runs"][0]["analysis_name"], "白內障術前分析")
        self.assertTrue(all("索引" in item["label"] for item in saved["saved_attempts"]))
        other = self.app.login({"username": "SECOND", "password": "synthetic"})["account"]["id"]
        self.assertFalse(self.app.workspace(other).diagnostics.query({"task_id": state.data["id"]})["failures"])

    def test_failing_request_metadata_is_bound_to_its_operation_without_sensitive_values(self):
        def factory(settings):
            sdk = create_sdk(settings)
            sdk.auth.check = lambda **_: SimpleNamespace(ok=True)
            return sdk
        self.work.gateway.factory = factory
        self.work.gateway.login(Settings(username="TEST", password="SECRET_PASSWORD"))
        recorder = self.work.gateway.recorder
        operation_id = recorder.start_operation(name="get_order_history", app_key="prq")
        request_id = recorder.record_http_request(method="POST", url="https://synthetic.invalid/PRQWeb/QueryOrderResult.do?hid=SECRET_TOKEN",
            attempt=1, max_attempts=2, throttle_delay_seconds=0, tls_verification_enabled=True,
            kwargs={"data": {"password": "SECRET_PASSWORD", "mrn": "TEST001"}})
        response = requests.Response()
        response.status_code, response.url = 200, "https://synthetic.invalid/PRQWeb/QueryOrderResult.do"
        response._content = UNSUPPORTED_ORDERS_HTML.encode("utf-8")
        recorder.record_http_response(request_id=request_id, response=response, elapsed_seconds=.01)
        try:
            self.unsupported_orders("TEST001", None)
        except ParseError as exc:
            recorder.finish_operation(operation_id=operation_id, name="get_order_history", status="ERROR", exc=exc)
            with self.work.gateway.task_context("http-failure", "analysis"):
                self.work.gateway.record_failure(exc, "orders", "get_order_history",
                                                 ("TEST001", OrderHistoryFilter(category="OR")))
        saved = self.work.diagnostics.query({"task_id": "http-failure"})
        failure = saved["failures"][0]
        self.assertEqual(failure["http_status"], 200)
        self.assertEqual(failure["endpoint_path"], "/PRQWeb/QueryOrderResult.do")
        self.assertEqual(failure["transport"]["operation_id"], operation_id)
        self.assertEqual(failure["transport"]["request_id"], request_id)
        self.assertEqual(failure["query"]["category"], "OR")
        self.assertEqual(failure["query"]["lookback_days"], 4000)
        self.assertNotIn("SECRET", json.dumps(saved))
        self.assertTrue(saved["contains_raw_response"])
        self.assertTrue(saved["contains_medical_values"])
        raw = failure["transport"]["evidence"]["files"]
        self.assertEqual(len(raw), 2)
        archive = self.work.diagnostics.export({"task_id": "http-failure"})
        with zipfile.ZipFile(io.BytesIO(archive)) as zip_file:
            body = zip_file.read(next(name for name in zip_file.namelist() if name.endswith(".html"))).decode("utf-8")
            self.assertEqual(body, UNSUPPORTED_ORDERS_HTML)
            with self.assertRaises(ParseError) as caught:
                parse_clinical_orders(body, mrn="TEST001")
            self.assertEqual(caught.exception.info.code, "JS_EXPRESSION_UNSUPPORTED")
            self.assertFalse(json.loads(zip_file.read("debug.json"))["export"]["missing_files"])
        next_id = recorder.start_operation(name="get_soap", app_key="prq")
        recorder.finish_operation(operation_id=next_id, name="get_soap", status="OK")
        self.assertFalse(recorder.failure_transport(ParseError("SECRET", code="JS_EXPRESSION_UNSUPPORTED")))
        recorder.finalize(command="synthetic-debug", status="ERROR", exit_code=1)
        self.app.logout(self.key)
        source = self.app.directory
        self.app.close()
        destination = self.path / "evidence-copy"
        copy_database(source, destination, name="原始诊斷副本")
        self.app = BotApplication(Settings(), destination, BotSyntheticSDK)
        self.app.offline(self.key)
        self.work = self.app.workspace(self.key)
        with zipfile.ZipFile(io.BytesIO(self.work.diagnostics.export({"task_id": "http-failure"}))) as zip_file:
            self.assertEqual(body, zip_file.read(next(name for name in zip_file.namelist() if name.endswith(".html"))).decode("utf-8"))
            self.assertTrue(json.loads(zip_file.read("debug.json"))["contains_raw_response"])

    def test_debug_export_rejects_paths_outside_account_evidence_and_notes_missing_files(self):
        self.work.review.db.save("patient_diagnostic", {"task_id": "forged", "mrn": "TEST001",
            "sdk_run_id": "../another-account", "transport": {"evidence": {"files": [
                {"name": "../private.html", "sha256": "invalid"}]}}, "error": {"code": "PARSE_ERROR"}})
        self.work.review.db.save("patient_diagnostic", {"task_id": "forged", "mrn": "TEST001",
            "sdk_run_id": "a" * 32, "transport": {"evidence": {"files": [
                {"name": "../../private.html", "sha256": "invalid"},
                {"name": "response-1-1.html", "sha256": "invalid"}]}}, "error": {"code": "PARSE_ERROR"}})
        with zipfile.ZipFile(io.BytesIO(self.work.diagnostics.export({"task_id": "forged"}))) as zip_file:
            self.assertEqual(set(zip_file.namelist()), {"debug.json", "README.txt"})
            exported = json.loads(zip_file.read("debug.json"))["export"]
            self.assertEqual(exported["evidence_files"], [])
            self.assertEqual(len(exported["missing_files"]), 1)

    def test_debug_zip_http_download_is_authenticated_account_scoped_and_readonly_compatible(self):
        from vghks_bot.selftest_diagnostics import check_order_index_evidence

        check_order_index_evidence(self.work)
        second = self.app.login({"username": "SECOND", "password": "synthetic"})["account"]["id"]
        self.app.read_only = True
        with BotServer(0, self.app) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def read(account, authenticated=True):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    headers = {"Cookie": "opd_session=" + self.app.session_token} if authenticated else {}
                    connection.request("GET", "/api/accounts/" + account +
                                       "/reviews/diagnostics/export?task_id=selftest-order-index", headers=headers)
                    response = connection.getresponse()
                    result = response.status, dict(response.getheaders()), response.read()
                    connection.close()
                    return result
                self.assertEqual(read(self.key, False)[0], 401)
                status, headers, content = read(self.key)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], "application/zip")
                self.assertIn("attachment;", headers["Content-Disposition"])
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    self.assertTrue(any(name.endswith(".bin") for name in archive.namelist()))
                with zipfile.ZipFile(io.BytesIO(read(second)[2])) as archive:
                    self.assertFalse(json.loads(archive.read("debug.json"))["items"])
                    self.assertEqual(set(archive.namelist()), {"debug.json", "README.txt"})
            finally:
                server.shutdown()
                thread.join(timeout=3)

    def test_soap_errors_have_task_visit_and_safe_stack_surviving_retry(self):
        with patch.object(self.work.gateway.connection.records, "get_soap", side_effect=ParseError(
                "SECRET_BODY", code="PRQ_SOAP_STRUCTURE_MISSING", http_status=200,
                endpoint_path="/PRQWeb/QueryBillingSOAP.do?hid=SECRET_TOKEN")):
            task = self.review()
        self.assertEqual(task["status"], "partial")
        saved = self.work.diagnostics.query({"mrn": "TEST001", "task_id": task["id"]})
        self.assertTrue(saved["items"])
        for row in saved["items"]:
            self.assertEqual(row["task_kind"], "review")
            self.assertEqual(row["phase"], "records.get_soap")
            self.assertTrue(row["visit"]["case_no"])
            self.assertTrue(row["error"]["stack"])
            self.assertEqual(row["error"]["http_status"], 200)
        self.assertNotIn("SECRET", json.dumps(saved))
        self.assertEqual(self.task(resume=task["id"], retry_only=["TEST001"])["status"], "completed")
        self.assertEqual(saved["items"], self.work.diagnostics.query({"task_id": task["id"]})["items"])

    def test_soap_identity_validation_failure_is_saved_without_wrong_soap(self):
        with patch.object(self.work.gateway.connection.records, "get_soap",
                side_effect=lambda case: SoapRecord(replace(case, mrn="OTHER"), ("WRONG SOAP",))):
            task = self.review()
        saved = self.work.diagnostics.query({"task_id": task["id"]})
        self.assertTrue(all(r["error"]["code"] == "BOT_SOAP_CASE_MISMATCH" for r in saved["items"]))
        self.assertTrue(saved["items"])
        self.assertFalse(self.work.review.results({"id": task["id"]})["patients"][0]["records"])

    def test_lazy_numeric_failure_has_its_own_task_and_error_code(self):
        reviewed = self.review()
        record = reviewed["items"][0]["records"][0]
        with patch.object(self.work.gateway.connection.records, "get_numeric_report", side_effect=ParseError(
                "SECRET", code="PRQ_NUMERIC_STRUCTURE_MISSING")):
            task = self.task(kind="numeric", mrn="TEST001", record_id=record["id"])
        saved = self.work.diagnostics.query({"task_id": task["id"], "mrn": "TEST001"})
        self.assertEqual(task["status"], "failed")
        self.assertEqual(saved["items"][0]["phase"], "records.get_numeric_report")
        self.assertEqual(saved["task"]["error_code"], "PRQ_NUMERIC_STRUCTURE_MISSING")
        self.assertFalse(self.work.diagnostics.query({"task_id": reviewed["id"]})["items"])

    def test_sdk_activity_covers_each_system_without_credentials_or_response_bodies(self):
        login = self.work.review.db.sdk_sessions()[0]
        self.assertEqual(login["status"], "completed")
        self.assertEqual(self.work.diagnostics.query({"session_id": login["id"]})["sdk_events"][0]["method"], "login")
        reviewed = self.review()
        surgery = self.task(kind="surgery_schedule", start=today().isoformat(), end=today().isoformat(), department="OPH")
        approval = self.task(kind="approval_sync")
        self.work.earnings.save_credentials({"national_id": "A123456789", "password": "secret-salary", "remember": False})
        earnings = self.task(kind="earnings_options")
        for task, service in ((reviewed, "records"), (surgery, "surgery"),
                              (approval, "reviews"), (earnings, "earnings")):
            events = self.work.diagnostics.query({"task_id": task["id"]})["sdk_events"]
            self.assertTrue(any(event["service"] == service for event in events), task["kind"])
            self.assertNotIn("secret-salary", json.dumps(events))
            self.assertNotIn("sensitive-context", json.dumps(events))

    def test_national_id_failure_is_logged_without_recording_input_or_password(self):
        with patch.object(self.work.gateway.connection.patients, "resolve_identity", side_effect=LoginRejectedError(
                "SECRET_PASSWORD", code="AUTH_LOGIN_REJECTED")):
            task = self.task(kind="resolve", identifier_kind="national_id", identifiers="A123456789")
        saved = self.work.diagnostics.query({"task_id": task["id"]})
        self.assertEqual(task["status"], "paused")
        self.assertEqual(saved["items"][0]["phase"], "patients.resolve_identity")
        self.assertNotIn("A123456789", json.dumps(saved))
        self.assertNotIn("SECRET_PASSWORD", json.dumps(saved))
        self.assertFalse(self.work.gateway.online)

    def test_sdk_trace_is_attached_redacted_and_copied_with_the_database(self):
        def factory(settings):
            sdk = create_sdk(settings)
            sdk.auth.check = lambda **_: SimpleNamespace(ok=True)
            return sdk
        self.work.gateway.factory = factory
        self.work.gateway.login(Settings(username="TEST", password="SECRET_PASSWORD"))
        recorder = self.work.gateway.recorder
        self.assertIs(recorder, self.work.gateway.connection._runtime.transport.diagnostics)
        request_id = recorder.record_http_request(method="POST", url="https://synthetic.invalid/PRQWeb/QueryBillingSOAP.do?hid=SECRET_TOKEN",
            attempt=1, max_attempts=2, throttle_delay_seconds=0, tls_verification_enabled=True,
            kwargs={"data": {"password": "SECRET_PASSWORD", "mrn": "TEST001"}})
        response = requests.Response()
        response.status_code, response.url = 200, "https://synthetic.invalid/PRQWeb/QueryBillingSOAP.do"
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response._content = b'<div id="data"><div class="soap"><pre>SECRET_SOAP</pre></div></div>'
        recorder.record_http_response(request_id=request_id, response=response, elapsed_seconds=.01)
        with self.work.gateway.task_context("trace-task", "numeric"):
            self.work.gateway.record_diagnostic({"mrn": "TEST001", "phase": "test", "recovered": False})
        self.app.logout(self.key)
        relative = recorder.directory.relative_to(self.app.directory)
        trace = recorder.trace_path.read_text(encoding="utf-8")
        self.assertIn('"status_code": 200', trace)
        self.assertIn('"#data .soap pre": 1', trace)
        self.assertNotIn("SECRET", trace)
        other = self.app.login({"username": "SECOND", "password": "synthetic"})["account"]["id"]
        self.assertFalse(self.app.workspace(other).diagnostics.query({"mrn": "TEST001"})["items"])
        source = self.app.directory
        self.app.close()
        destination = self.path / "copy"
        copy_database(source, destination, name="診斷副本")
        self.assertEqual(trace, (destination / relative / "diagnostics.jsonl").read_text(encoding="utf-8"))
        self.app = BotApplication(Settings(), destination, BotSyntheticSDK)
        self.app.offline(self.key)
        self.work = self.app.workspace(self.key)
        self.assertEqual(self.work.diagnostics.query({"mrn": "TEST001"})["items"][0]["sdk_run_id"], recorder.run_id)

    def test_old_task_error_summary_is_available_without_inventing_details(self):
        db = self.work.review.db
        db.save("task", {"id": "legacy", "status": "partial", "kind": "review"})
        db.item("legacy", "TEST001", {"mrn": "TEST001", "status": "error", "code": "OLD_PARSE_ERROR"})
        saved = self.work.diagnostics.query({"task_id": "legacy", "mrn": "TEST001"})
        self.assertFalse(saved["items"])
        self.assertEqual(saved["saved_attempts"][0]["code"], "OLD_PARSE_ERROR")
