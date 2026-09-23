import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from vghks_sdk import LoginRejectedError, ParseError, SoapRecord

from opd_monitor.bot import BotApplication
from opd_monitor.databases import copy_database
from opd_monitor.scanner import create_sdk
from opd_monitor.selftest_bot import BotSyntheticSDK, wait_task
from opd_monitor.settings import Settings


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
        return self.task(kind="review", set_id=group["id"], department_confirmed=True, **values)

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
