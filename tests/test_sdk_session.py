"""Real SDK protocol handling through the platform's ClinicalTransport."""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from vghks_sdk import ApplicationSessionExpiredError, ParseError, RequestPolicy
from vghks_sdk import __version__ as sdk_version
from vghks_sdk.core.errors import error_info

from vghks_bot.bot import BotApplication
from vghks_bot.bot_gateway import AccountGateway, NetworkGate
from vghks_bot.encoding import ClinicalTransport
from vghks_bot.scanner import create_sdk
from vghks_bot.selftest_bot import wait_task
from vghks_bot.selftest_session import MRN, PATIENT, SessionScenario
from vghks_bot.settings import Settings


class SDKIdentityBridgeTests(unittest.TestCase):
    def gateway(self, returned_id):
        sdk = create_sdk(Settings(username="SYNTHETIC", password="SYNTHETIC"))
        self.addCleanup(sdk.close)
        runtime = sdk._runtime
        runtime.auth.ensure = Mock()
        runtime.auth.hid_for = Mock(return_value="SYNTHETIC-HID")
        runtime.request_text = Mock(side_effect=[
            '<frameset><frame src="/Page/JSP/KS_Patient.jsp">'
            '<frame src="/QueryCaseList.do"></frameset>',
            f'<span id="pHistno">00012345</span><script>var url="&hidno={returned_id}&type=1";</script>',
        ])
        gateway = AccountGateway(lambda settings: sdk, NetworkGate())
        gateway.connection, gateway.online = sdk, True
        return gateway, runtime

    def test_real_sdk_resolves_verified_header_without_requiring_encounters(self):
        gateway, runtime = self.gateway("A123456789")
        self.assertEqual(gateway.resolve_identity("a123456789"), "00012345")
        self.assertEqual(gateway.context, "00012345")
        calls = runtime.request_text.call_args_list
        self.assertEqual([call.args[0].key for call in calls],
                         ["prq.patient_context", "prq.patient_identity"])
        self.assertEqual(calls[0].kwargs["data"],
                         {"id": "A123456789", "queryID": "A123456789", "queryPtID": "", "type": "2"})

    def test_real_sdk_rejects_header_id_mismatch_without_replaying_the_lookup(self):
        gateway, runtime = self.gateway("B123456789")
        with self.assertRaises(ParseError) as failed:
            gateway.resolve_identity("A123456789")
        self.assertEqual(failed.exception.info.code, "PRQ_PATIENT_ID_MISMATCH")
        self.assertIsNone(gateway.context)
        self.assertEqual(runtime.request_text.call_count, 2)


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

    def use_demographics_payloads(self, payloads):
        self.scenario.mode, self.scenario.expired = "healthy", False
        original = self.scenario.request
        pending = list(payloads)

        def request(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if url.endswith("/ajax/AJAXAction.do") and pending:
                response._content = json.dumps(pending.pop(0), ensure_ascii=False).encode("utf-8")
                response.headers["Content-Type"] = "application/json; charset=utf-8"
            return response

        self.gateway.connection._runtime.transport.session.request = request

    @staticmethod
    def login_timeout_envelope():
        return {"returnCode": "9999", "returnStatus": False,
                "returnMessage": "沒有正確登錄或是連線逾時。合成診斷內容"}

    def test_json_login_timeout_recovers_once_without_portal_login_and_keeps_raw_evidence(self):
        envelope = self.login_timeout_envelope()
        self.use_demographics_payloads([envelope])
        session = self.gateway.session_id
        with patch.object(self.gateway, "_reconnect_locked") as reconnect:
            task = self.resolve()
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["items"][0]["name"], PATIENT["patname"])
        self.assertEqual(self.gateway.session_id, session)
        self.assertEqual(self.gateway.recovery_count, 1)
        self.assertEqual(len(self.calls("/ajax/AJAXAction.do")), 2)
        self.assertEqual(len(self.calls("/WPSAutoLogon")), 1)
        reconnect.assert_not_called()
        self.assert_no_portal_password_or_platform_rebuild()
        incident, = self.work.diagnostics.query({"task_id": task["id"]})["items"]
        self.assertTrue(incident["recovered"])
        self.assertEqual(incident["error"]["cause"]["code"], "WEBMAAS_SESSION_TIMEOUT")
        with zipfile.ZipFile(io.BytesIO(self.work.diagnostics.export({"task_id": task["id"]}))) as archive:
            bodies = [archive.read(name) for name in archive.namelist() if name.endswith(".bin")]
            self.assertTrue(any(json.loads(body) == envelope for body in bodies))
            self.assertFalse(json.loads(archive.read("debug.json"))["export"]["missing_files"])

    def test_persistent_json_login_timeout_pauses_after_one_sso_without_saving_a_patient(self):
        envelope = self.login_timeout_envelope()
        self.use_demographics_payloads([envelope, envelope])
        with patch.object(self.gateway, "_reconnect_locked") as reconnect:
            task = self.resolve()
        self.assertEqual(task["status"], "paused")
        self.assertEqual(task["items"][0]["code"], "WEBMAAS_SESSION_TIMEOUT")
        self.assertIn("重新連線後續跑", task["items"][0]["message"])
        self.assertEqual(len(self.calls("/ajax/AJAXAction.do")), 2)
        self.assertEqual(len(self.calls("/WPSAutoLogon")), 1)
        self.assertFalse(self.gateway.online)
        self.assertIsNone(self.work.review.db.get("profile", MRN, required=False))
        reconnect.assert_not_called()
        self.assert_no_portal_password_or_platform_rebuild()

    def test_permission_or_unknown_envelopes_never_trigger_session_recovery(self):
        for envelope in (
            {"returnCode": "9999", "returnStatus": False, "returnMessage": "此帳號無讀取權限"},
            {"returnCode": "OTHER", "returnStatus": False, "returnMessage": "沒有正確登錄或是連線逾時。"},
            {"returnCode": "9999", "returnStatus": False, "returnMessage": "SESSION_EXPIRED"},
            {**PATIENT, "patname": "沒有正確登錄或是連線逾時。"},
        ):
            with self.subTest(envelope=envelope):
                self.use_demographics_payloads([envelope])
                sso_before = len(self.calls("/WPSAutoLogon"))
                with patch.object(self.gateway, "_reconnect_locked") as reconnect:
                    if "patname" in envelope:
                        self.assertEqual(self.gateway.invoke("patients", "get_demographics", MRN).mrn, MRN)
                    else:
                        self.assert_lookup_unconfirmed()
                self.assertEqual(len(self.calls("/WPSAutoLogon")), sso_before)
                self.assertTrue(self.gateway.online)
                reconnect.assert_not_called()
                self.assert_no_portal_password_or_platform_rebuild()

    def assert_lookup_unconfirmed(self):
        with self.assertRaises(ParseError) as caught:
            self.gateway.invoke("patients", "get_demographics", MRN)
        self.assertEqual(caught.exception.info.code, "PATIENT_LOOKUP_UNCONFIRMED")

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
        self.assertEqual(incident["sdk_version"], sdk_version)
        self.assertTrue(incident["transport"]["evidence"]["complete"])
        with zipfile.ZipFile(io.BytesIO(self.work.diagnostics.export({"task_id": task["id"]}))) as archive:
            manifest = json.loads(archive.read("sdk-context.json"))
            self.assertEqual(manifest["incidents"][0]["sdk_version"], sdk_version)
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


class WebmaasJSONTimeoutGuardTests(unittest.TestCase):
    base = "https://synthetic.invalid/webmaas"

    def setUp(self):
        self.transport = ClinicalTransport(policy=RequestPolicy(), webmaas_base_url=self.base)
        self.addCleanup(self.transport.close)

    def response(self, payload, *, url=None, method="POST", querymethod="CHECK_PAT"):
        request = requests.Request(method, url or self.base + "/ajax/AJAXAction.do",
                                   params={"querymethod": querymethod}).prepare()
        response = requests.Response()
        response.status_code, response.url, response.request = 200, request.url, request
        response.headers["Content-Type"] = "application/json; charset=utf-8"
        response._content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return response

    def test_configured_check_pat_envelope_becomes_a_fixed_sdk_session_error(self):
        payload = NativeSDKSessionTests.login_timeout_envelope()
        with self.assertRaises(ApplicationSessionExpiredError) as failed:
            self.transport.json(self.response(payload))
        info = error_info(failed.exception)
        self.assertEqual((info.code, info.app, info.endpoint_path, info.http_status),
                         ("WEBMAAS_SESSION_TIMEOUT", "webmaas", "/webmaas/ajax/AJAXAction.do", 200))
        self.assertNotIn("合成診斷內容", str(failed.exception))

    def test_other_origins_endpoints_methods_and_query_operations_are_unchanged(self):
        payload = NativeSDKSessionTests.login_timeout_envelope()
        cases = (
            {"url": "https://other.invalid/webmaas/ajax/AJAXAction.do"},
            {"url": "http://synthetic.invalid/webmaas/ajax/AJAXAction.do"},
            {"url": self.base + "/other/AJAXAction.do"},
            {"method": "GET"},
            {"querymethod": "OTHER_OPERATION"},
        )
        for options in cases:
            with self.subTest(options=options):
                self.assertEqual(self.transport.json(self.response(payload, **options)), payload)
        response = self.response(payload)
        response.url = "https://other.invalid/webmaas/ajax/AJAXAction.do"
        self.assertEqual(self.transport.json(response), payload)
        response = self.response(payload)
        response.request = None
        self.assertEqual(self.transport.json(response), payload)

    def test_only_the_exact_error_envelope_shape_can_trigger_recovery(self):
        envelope = NativeSDKSessionTests.login_timeout_envelope()
        cases = (
            None, {}, [], [envelope], {"data": envelope},
            {**envelope, "returnCode": 9999}, {**envelope, "returnStatus": 0},
            {**envelope, "returnStatus": True}, {**envelope, "returnStatus": "false"},
            {**envelope, "returnStatus": None},
            {**envelope, "returnMessage": "此帳號無讀取權限"},
            {**envelope, "patname": "合成病人"}, {**envelope, "patno": MRN},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(self.transport.json(self.response(payload)), payload)

    def test_unconfigured_transport_does_not_classify_an_arbitrary_server(self):
        transport = ClinicalTransport(policy=RequestPolicy())
        self.addCleanup(transport.close)
        payload = NativeSDKSessionTests.login_timeout_envelope()
        self.assertEqual(transport.json(self.response(payload)), payload)

    def test_sdk_factory_wires_the_configured_webmaas_origin(self):
        sdk = create_sdk(Settings(username="SYNTHETIC", password="SYNTHETIC"))
        self.addCleanup(sdk.close)
        self.assertIsNotNone(sdk._runtime.transport.webmaas_origin)
        self.assertTrue(sdk._runtime.transport.webmaas_ajax_path.endswith("/webmaas/ajax/AJAXAction.do"))


if __name__ == "__main__":
    unittest.main()
