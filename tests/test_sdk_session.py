"""Real SDK 0.22.6 protocol handling through the platform's ClinicalTransport."""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from vghks_sdk import ParseError
from vghks_sdk.core.errors import error_info

from vghks_bot.bot import BotApplication
from vghks_bot.selftest_bot import wait_task
from vghks_bot.selftest_session import MRN, PATIENT, SessionScenario
from vghks_bot.settings import Settings


class NativeSDKSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.scenario = SessionScenario()
        self.app = BotApplication(Settings(), Path(self.temp.name), self.scenario.factory)
        self.account = self.app.login({"username": "TEST", "password": "SECRET_PASSWORD"})["account"]["id"]
        self.work = self.app.workspace(self.account)
        self.gateway = self.work.gateway

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def resolve(self):
        task = self.work.review.start({"kind": "resolve", "identifiers": MRN})["task_id"]
        return wait_task(self.work, task)

    def calls(self, suffix):
        return [entry for entry in self.scenario.requests if entry["url"].endswith(suffix)]

    def assert_no_portal_password_or_platform_rebuild(self):
        self.assertEqual(len(self.scenario.connections), 1)
        self.assertFalse(any(entry["method"] == "POST" and "/webmaas/" not in entry["url"]
                             for entry in self.scenario.requests))
        self.assertEqual(self.gateway.connection._runtime.auth.generation, 1)

    def test_manual_patient_uses_native_sso_recovery_without_changing_account_session(self):
        session = self.gateway.session_id
        sectord = self.gateway.connection._runtime.auth._apps["sectord"]
        with patch.object(self.gateway, "_reconnect_locked") as reconnect:
            task = self.resolve()
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["items"][0]["mrn"], MRN)
        self.assertEqual(task["items"][0]["name"], PATIENT["patname"])
        self.assertEqual(self.gateway.session_id, session)
        self.assertIs(self.gateway.connection._runtime.auth._apps["sectord"], sectord)
        self.assertTrue(self.gateway.connection._runtime.auth.portal_authenticated)
        self.assertEqual(self.gateway.recovery_count, 1)
        self.assertEqual(len(self.calls("/ajax/AJAXAction.do")), 2)
        self.assertEqual(len(self.calls("/so.do")), 1)
        self.assertEqual(len(self.calls("/WPSAutoLogon")), 1)
        self.assertFalse(self.calls("/RSV/RSV11W001.do"))
        reconnect.assert_not_called()
        self.assert_no_portal_password_or_platform_rebuild()

    def test_native_recovery_debug_keeps_original_timeout_and_same_operation_success(self):
        task = self.resolve()
        incident, = self.work.diagnostics.query({"task_id": task["id"]})["items"]
        self.assertTrue(incident["recovered"])
        self.assertEqual(incident["error"]["code"], "SDK_APPLICATION_SESSION_RECOVERED")
        self.assertEqual(incident["error"]["cause"]["code"], "WEBMAAS_SESSION_TIMEOUT")
        self.assertEqual(incident["sdk_version"], "0.22.6")
        self.assertTrue(incident["transport"]["evidence"]["complete"])
        with zipfile.ZipFile(io.BytesIO(self.work.diagnostics.export({"task_id": task["id"]}))) as archive:
            manifest = json.loads(archive.read("sdk-context.json"))
            self.assertEqual(manifest["incidents"][0]["sdk_version"], "0.22.6")
            self.assertFalse(json.loads(archive.read("debug.json"))["export"]["missing_files"])
            trace = next(archive.read(name).decode() for name in archive.namelist() if "/operation-" in name)
            events = [json.loads(line) for line in trace.splitlines()]
            recovery, = [row for row in events if row["event"] == "application_session_recovery_started"]
            finished, = [row for row in events if row["event"] == "operation_finished"]
            self.assertEqual(recovery["operation_id"], finished["operation_id"])
            self.assertEqual(finished["status"], "OK")
            self.assertEqual(recovery["issue"]["http_status"], 200)
            sources = [archive.read(name) for name in archive.namelist() if name.endswith(".html")]
            self.assertEqual(len(sources), 1)
            self.assertIn(b"Page time out", sources[0])
            self.assertNotIn(PATIENT["patname"].encode(), b"".join(sources))
            self.assertIn("/webmaas/comm/pageTimeOut.do", trace)

    def test_persistent_timeout_stops_after_one_sso_and_keeps_sdk_code(self):
        self.scenario.mode = "persistent_timeout"
        with patch.object(self.gateway, "_reconnect_locked") as reconnect:
            task = self.resolve()
        self.assertEqual(task["status"], "paused")
        self.assertEqual(task["items"][0]["code"], "WEBMAAS_SESSION_TIMEOUT")
        self.assertEqual(len(self.calls("/WPSAutoLogon")), 1)
        self.assertFalse(self.gateway.online)
        self.assertEqual(self.gateway.recovery_count, 0)
        incident, = self.work.diagnostics.query({"task_id": task["id"]})["items"]
        self.assertFalse(incident["recovered"])
        self.assertFalse(incident["error"]["retry_safe"])
        reconnect.assert_not_called()
        self.assert_no_portal_password_or_platform_rebuild()

    def test_sso_dns_failure_keeps_unsafe_outer_flag_and_does_not_submit_password(self):
        self.scenario.mode = "sso_network"
        task = self.resolve()
        self.assertEqual(task["status"], "paused")
        self.assertEqual(task["items"][0]["code"], "WEBMAAS_SSO_RECOVERY_FAILED")
        self.assertEqual(self.gateway.connection_issue["root_cause"]["code"], "NETWORK_DNS_FAILED")
        self.assertFalse(self.gateway.connection_issue["retry_safe"])
        self.assertFalse(self.gateway.connection_issue["auto_reconnect"])
        self.assertEqual(len(self.calls("/so.do")), 1)
        self.assertFalse(self.calls("/WPSAutoLogon"))
        self.assert_no_portal_password_or_platform_rebuild()

    def test_unknown_page_is_parse_error_without_sso_or_portal_recovery(self):
        self.scenario.mode = "unknown"
        task = self.resolve()
        self.assertEqual(task["status"], "partial")
        incident, = self.work.diagnostics.query({"task_id": task["id"]})["items"]
        self.assertEqual(incident["error"]["category"], "PARSE")
        self.assertNotEqual(task["items"][0]["code"], "PATIENT_NOT_FOUND")
        self.assertFalse(self.calls("/WPSAutoLogon"))
        self.assertEqual(len(self.calls("/ajax/AJAXAction.do")), 1)
        self.assertTrue(self.gateway.online)
        self.assert_no_portal_password_or_platform_rebuild()

    def test_explicit_readiness_uses_native_fresh_form_validation_and_keeps_parse_code(self):
        for mode, code in (("form_missing", "WEBMAAS_QUERY_FORM_MISSING"),
                           ("token_missing", "WEBMAAS_QUERY_TOKEN_MISSING")):
            with self.subTest(mode=mode):
                self.scenario.mode = mode
                auth = self.gateway.connection._runtime.auth
                if "webmaas" not in auth._apps:
                    from vghks_sdk.adapters.auth import AppSession

                    auth._webmaas_page = "RSV11W001"
                    auth._apps["webmaas"] = AppSession("webmaas", "SYNTHETIC-HID",
                        "https://synthetic.invalid/webmaas/RSV/RSV11W001.do")
                with self.assertRaises(ParseError) as caught:
                    self.gateway.invoke("auth", "check", only=("webmaas",))
                self.assertEqual(error_info(caught.exception).code, code)
                self.assertNotIn("webmaas", auth._apps)
                self.assertTrue(self.gateway.online)
                self.assertFalse(self.calls("/WPSAutoLogon"))
                self.assert_no_portal_password_or_platform_rebuild()

    def test_complete_basic_info_recovers_original_role_and_uses_fresh_query_token(self):
        self.gateway.close()
        self.scenario = SessionScenario(page="QUY15W001")
        self.gateway.factory = self.scenario.factory
        self.gateway.login(Settings(username="TEST", password="SECRET_PASSWORD"))
        value = self.gateway.invoke("patients", "get_basic_info", MRN)
        self.assertEqual(value.mrn, MRN)
        self.assertEqual(value.name, PATIENT["patname"])
        sso, = self.calls("/WPSAutoLogon")
        self.assertEqual(sso["params"]["externalRoles"], "maas_QRY15")
        submitted, = [entry for entry in self.calls("/QUY/QUY15W001.do") if entry["method"] == "POST"]
        self.assertEqual(submitted["data"]["org.apache.struts.taglib.html.TOKEN"], "fresh-token")
        self.assertEqual(self.gateway.recovery_count, 1)
        self.assert_no_portal_password_or_platform_rebuild()

    def test_normal_reads_keep_success_recording_compact_and_do_not_count_recovery(self):
        self.scenario.mode, self.scenario.expired = "healthy", False
        self.assertEqual(self.gateway.invoke("patients", "get_demographics", MRN).mrn, MRN)
        self.assertFalse(self.work.diagnostics.query({"mrn": MRN})["items"])
        self.assertEqual(self.gateway.recovery_count, 0)
        self.assertFalse(list(self.gateway.recorder.directory.glob("response-*")))
        self.assertFalse(list(self.gateway.recorder.directory.glob("operation-*")))
        self.assertEqual(self.gateway.recorder.http_local.last["buffer_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
