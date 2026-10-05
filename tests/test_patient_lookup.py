"""Patient validation uses public SDK calls and never supplies session recovery."""
import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from vghks_sdk import (
    AuthExpiredError,
    AuthorizationError,
    LoginRejectedError,
    ParseError,
    RequestError,
)
from vghks_sdk.core.errors import error_info
from vghks_sdk.core.operations import operation_spec
from vghks_sdk.parsing.webmaas import parse_patient_demographics

from vghks_bot.bot import BotApplication
from vghks_bot.connection_state import readiness_error
from vghks_bot.patient_lookup import check_session
from vghks_bot.scanner import create_sdk
from vghks_bot.selftest_bot import BotSyntheticSDK, wait_task
from vghks_bot.settings import Settings

PATIENT = {"patno": "00012345", "patname": "合成病人", "birthday": "047/01/01", "hsex": "女"}


class PatientLookupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.actions, self.reads, self.checks, self.connections = [], [], [], []
        self.health = lambda only: None
        self.app = BotApplication(Settings(), Path(self.temp.name), self.factory)
        self.key = self.app.login({"username": "TEST", "password": "SECRET_PASSWORD"})["account"]["id"]
        self.work = self.app.workspace(self.key)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def factory(self, settings):
        sdk = create_sdk(settings)
        self.connections.append(sdk)

        def check(*, only):
            self.checks.append(only)
            exc = self.health(only)
            return SimpleNamespace(ok=exc is None, reauthenticated=False, targets=(
                SimpleNamespace(status="ERROR", issue=error_info(exc)),) if exc else ())

        def demographics(mrn):
            def operation():
                self.reads.append(mrn)
                recorder = sdk._runtime.diagnostics
                request = recorder.record_http_request(method="POST",
                    url="https://synthetic.invalid/webmaas/ajax/AJAXAction.do?hid=SECRET_TOKEN", attempt=1,
                    max_attempts=1, throttle_delay_seconds=0, tls_verification_enabled=True,
                    kwargs={"data": {"patno": mrn}, "headers": {"Cookie": "SECRET_COOKIE"}})
                payload = self.actions.pop(0) if self.actions else {**PATIENT, "patno": mrn}
                if isinstance(payload, Exception):
                    raise payload
                response = requests.Response()
                response.status_code, response.url = 200, "https://synthetic.invalid/webmaas/ajax/AJAXAction.do"
                response.headers["Content-Type"] = "application/json; charset=utf-8"
                response._content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                recorder.record_http_response(request_id=request, response=response, elapsed_seconds=.01)
                return parse_patient_demographics(payload, mrn)
            return sdk._runtime.execute(operation_spec("webmaas.demographics"), operation,
                                        operation_name="get_patient_demographics")

        def login(*, force):
            sdk._runtime.auth._generation += 1

        sdk.auth.check = check
        sdk._runtime.auth._generation = 1
        sdk._runtime.auth.ensure = Mock()
        sdk._runtime.auth.login = Mock(side_effect=login)
        sdk.patients.get_demographics = demographics
        return sdk

    def resolve(self, mrn="00012345", **values):
        key = self.work.review.start({"kind": "resolve", "identifiers": mrn, **values})["task_id"]
        return wait_task(self.work, key)

    def test_sdk_restores_expired_read_without_rebuilding_the_account(self):
        self.actions = [AuthExpiredError("synthetic expiry"), PATIENT]
        original_session = self.work.gateway.session_id
        with patch.object(self.work.gateway, "_reconnect_locked") as reconnect:
            task = self.resolve()
        reconnect.assert_not_called()
        self.assertEqual(task["status"], "completed")
        self.assertEqual(len(self.connections), 1)
        self.assertEqual(len(self.reads), 2)
        self.assertEqual(self.work.gateway.session_id, original_session)
        self.assertEqual(self.work.gateway.recovery_count, 1)
        self.connections[0]._runtime.auth.login.assert_called_once_with(force=True)
        self.assertTrue(self.work.diagnostics.query({"task_id": task["id"]})["items"][0]["recovered"])

    def test_missing_form_and_token_are_not_permission_to_reconnect_or_retry(self):
        for code in ("WEBMAAS_QUERY_FORM_MISSING", "WEBMAAS_QUERY_TOKEN_MISSING"):
            with self.subTest(code=code):
                self.actions = [ParseError("synthetic unknown page", code=code)]
                before = len(self.reads)
                with patch.object(self.work.gateway, "_reconnect_locked") as reconnect:
                    task = self.resolve(force=True)
                self.assertEqual(task["items"][0]["code"], code)
                self.assertEqual(len(self.reads), before + 1)
                reconnect.assert_not_called()
                self.connections[0]._runtime.auth.login.assert_not_called()
                self.assertTrue(self.work.gateway.online)

    def test_sdk_recovery_failure_is_not_followed_by_an_outer_login(self):
        self.actions = [AuthExpiredError("synthetic expiry"), AuthExpiredError("still expired")]
        with patch.object(self.work.gateway, "_reconnect_locked") as reconnect:
            task = self.resolve()
        self.assertEqual(task["status"], "paused")
        self.assertEqual(task["items"][0]["code"], "AUTH_RELOGIN_FAILED")
        self.assertEqual(len(self.connections), 1)
        self.assertEqual(len(self.reads), 2)
        self.connections[0]._runtime.auth.login.assert_called_once_with(force=True)
        reconnect.assert_not_called()

    def test_successful_reads_do_not_run_a_platform_idle_probe(self):
        before = len(self.checks)
        for _ in range(3):
            self.assertEqual(self.resolve(force=True)["status"], "completed")
        self.assertEqual(len(self.checks), before)
        self.assertEqual(len(self.connections), 1)
        self.connections[0]._runtime.auth.login.assert_not_called()

    def test_confirmed_empty_requires_public_readiness_and_second_empty_read(self):
        self.actions = [[], []]
        task = self.resolve()
        self.assertEqual(task["items"][0]["code"], "PATIENT_NOT_FOUND")
        self.assertEqual(len(self.reads), 2)
        self.assertEqual(self.checks.count(("webmaas",)), 1)
        self.assertEqual(len(self.connections), 1)
        self.assertTrue(self.work.gateway.online)

    def test_empty_then_valid_patient_keeps_verification_failure_and_final_success(self):
        self.actions = [[], PATIENT]
        task = self.resolve()
        self.assertEqual(task["status"], "completed")
        self.assertEqual(len(self.reads), 2)
        self.assertTrue(self.work.diagnostics.query({"task_id": task["id"]})["items"][0]["recovered"])

    def test_failed_public_readiness_cannot_confirm_a_missing_patient(self):
        self.actions = [[]]
        self.health = lambda only: RequestError("SECRET", code="NETWORK_TIMEOUT") if only == ("webmaas",) else None
        task = self.resolve()
        self.assertEqual(task["status"], "paused")
        self.assertEqual(task["items"][0]["code"], "NETWORK_TIMEOUT")
        self.assertEqual(len(self.reads), 1)
        self.assertEqual(len(self.connections), 1)

    def test_unrecognized_metadata_never_guesses_session_expiry_or_missing_patient(self):
        self.actions = [{"status": "UNKNOWN", "code": "SESSION_EXPIRED"}]
        task = self.resolve()
        self.assertEqual(task["items"][0]["code"], "PATIENT_LOOKUP_UNCONFIRMED")
        self.assertEqual(len(self.reads), 1)
        self.assertTrue(self.work.gateway.online)
        self.connections[0]._runtime.auth.login.assert_not_called()

    def test_patient_name_looking_like_an_error_remains_clinical_data(self):
        self.actions = [{**PATIENT, "patname": "SESSION_EXPIRED"}]
        task = self.resolve()
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["items"][0]["name"], "SESSION_EXPIRED")
        self.connections[0]._runtime.auth.login.assert_not_called()

    def test_other_patient_and_duplicate_matches_are_not_missing_patients(self):
        for payload, code in (([{**PATIENT, "patno": "OTHER"}], "PATIENT_LOOKUP_UNCONFIRMED"),
                              ([PATIENT, PATIENT], "WEBMAAS_DEMOGRAPHICS_DUPLICATE")):
            with self.subTest(code=code):
                self.actions = [payload]
                task = self.resolve(force=True)
                self.assertEqual(task["items"][0]["code"], code)
                self.assertIsNone(self.work.review.db.get("profile", "00012345", required=False))

    def test_sdk_login_rejection_pauses_without_replaying_credentials(self):
        self.actions = [LoginRejectedError("SECRET")]
        task = self.resolve()
        self.assertEqual(task["status"], "paused")
        self.assertEqual(task["items"][0]["input"], "00012345")
        self.assertEqual(task["items"][0]["code"], "AUTH_LOGIN_REJECTED")
        self.assertEqual(len(self.reads), 1)
        self.assertFalse(self.work.gateway.online)
        self.connections[0]._runtime.auth.login.assert_not_called()
        self.assertFalse(self.work.diagnostics.query({"task_id": task["id"]})["items"][0]["recovered"])

    def test_sdk_permission_denied_does_not_relogin_or_disconnect(self):
        self.actions = [AuthorizationError("SECRET", code="DEMOGRAPHICS_PERMISSION_DENIED")]
        task = self.resolve()
        self.assertEqual(task["items"][0]["code"], "DEMOGRAPHICS_PERMISSION_DENIED")
        self.assertEqual(len(self.reads), 1)
        self.assertTrue(self.work.gateway.online)
        self.connections[0]._runtime.auth.login.assert_not_called()

    def test_network_failure_pauses_without_another_password_submission(self):
        self.actions = [RequestError("SECRET", code="NETWORK_TIMEOUT")]
        task = self.resolve()
        self.assertEqual(task["status"], "paused")
        self.assertEqual(task["items"][0]["code"], "NETWORK_TIMEOUT")
        self.assertEqual(len(self.reads), 1)
        self.assertFalse(self.work.gateway.online)
        self.connections[0]._runtime.auth.login.assert_not_called()

    def test_server_error_is_distinct_from_network_and_missing_patient(self):
        self.actions = [RequestError("SECRET", status_code=503)]
        task = self.resolve()
        self.assertEqual(task["status"], "paused")
        self.assertIn("暫時無法", task["items"][0]["message"])
        self.assertTrue(self.work.gateway.online)

    def test_debug_export_keeps_failed_response_and_excludes_request_secrets(self):
        payload = {"status": "UNKNOWN", "returnValue": "SYNTHETIC_RESPONSE"}
        self.actions = [payload]
        task = self.resolve()
        details = self.work.diagnostics.query({"task_id": task["id"]})
        self.assertNotIn("SECRET", json.dumps(details))
        with zipfile.ZipFile(io.BytesIO(self.work.diagnostics.export({"task_id": task["id"]}))) as archive:
            clinical = [name for name in archive.namelist() if name.endswith(".bin")]
            self.assertEqual(len(clinical), 1)
            self.assertEqual(json.loads(archive.read(clinical[0])), payload)
            self.assertFalse(json.loads(archive.read("debug.json"))["export"]["missing_files"])
        self.assertIsNone(self.work.review.db.get("profile", "00012345", required=False))

    def test_mismatched_model_is_rejected_without_retry(self):
        self.work.gateway.close()
        self.work.gateway.factory = BotSyntheticSDK
        self.work.gateway.login(Settings(username="TEST", password="synthetic"))
        value = self.work.gateway.connection.patients.get_demographics("00012345")
        self.work.gateway.connection.patients.get_demographics = Mock(return_value=replace(value, mrn="OTHER"))
        task = self.resolve()
        self.assertEqual(task["items"][0]["code"], "DEMOGRAPHICS_PATIENT_MISMATCH")
        self.work.gateway.connection.patients.get_demographics.assert_called_once()

    def test_rejected_reconnect_readiness_prompts_for_password(self):
        self.work.gateway.online = False
        self.health = lambda only: LoginRejectedError("SECRET")
        result = self.app.activate(self.key)
        self.assertEqual(result["status"], "needs_password")
        self.assertEqual(result["error_code"], "AUTH_LOGIN_REJECTED")
        self.assertNotIn("SECRET", json.dumps(result))


class ReadinessTests(unittest.TestCase):
    def test_readiness_keeps_sdk_network_permission_and_login_categories(self):
        for exc in (RequestError("SECRET", code="NETWORK_TIMEOUT"),
                    AuthorizationError("SECRET"), LoginRejectedError("SECRET")):
            with self.subTest(code=error_info(exc).code):
                report = SimpleNamespace(targets=(SimpleNamespace(status="ERROR", issue=error_info(exc)),))
                self.assertEqual(error_info(readiness_error(report)), error_info(exc))

    def test_public_readiness_never_adds_a_private_runtime_page_probe(self):
        runtime = Mock()
        connection = SimpleNamespace(auth=SimpleNamespace(check=Mock(return_value=SimpleNamespace(ok=True))), _runtime=runtime)
        check_session(connection)
        connection.auth.check.assert_called_once_with(only=("webmaas",))
        runtime.assert_not_called()
        self.assertFalse(runtime.mock_calls)

    def test_frontend_rejected_credentials_stop_background_login_attempts(self):
        subprocess.run(["node", "tests/connection_recovery_ui.js"], cwd=Path(__file__).resolve().parents[1],
                       check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
