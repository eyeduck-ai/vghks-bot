"""PACS is an explicit, patient-bound workstation command; JPEG stays primary."""
from __future__ import annotations

import http.client
import json
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from async_helpers import wait_workspace_task as wait_task
from test_review_history import ALIAS, DETAIL, MRN, REPORT, HistorySDK
from vghks_sdk import ParseError
from vghks_sdk.integrations.smartiris import SmartIrisClient, SmartIrisLaunchResult
from vghks_sdk.integrations.smartiris_availability import SmartIrisAvailability
from vghks_sdk.models import PacsLaunchContext, PacsLaunchRef
from vghks_sdk.models.pacs_launch import SMARTWONDER_BASE_URL, SMARTWONDER_WADO_URL

from vghks_bot.analysis_fetch import clean
from vghks_bot.analysis_store import digest
from vghks_bot.bot import BotApplication
from vghks_bot.bot_server import BotServer
from vghks_bot.jobs import BusyError
from vghks_bot.settings import Settings


def launch_context(ref):
    return PacsLaunchContext(ref, "TEST", "192.0.2.10", "1.2.3.4", "SK", "STUDY", "",
        SMARTWONDER_BASE_URL, SMARTWONDER_WADO_URL + "?" + urlencode({
            "requestType": "WADO", "contentType": "application/dicom;text/plain",
            "DicomServer": "SK", "PID": "vwrIris", "Dcm00110010": "TEST",
            "Dcm00110011": "192.0.2.10"}))


class PacsSDK(HistorySDK):
    native_refs = (PacsLaunchRef(ALIAS, "NATIVE1"),)
    prepare_error = None

    def __init__(self, settings):
        super().__init__(settings)
        self.orders.prepare_pacs_launch = self.prepare_pacs_launch

    def order_detail(self, reference):
        return replace(super().order_detail(reference), pacs_launch_refs=type(self).native_refs)

    def order_report(self, reference):
        return replace(super().order_report(reference), pacs_launch_refs=type(self).native_refs)

    def prepare_pacs_launch(self, reference):
        self.calls.append((self.card, "prepare-pacs", reference))
        if type(self).prepare_error:
            raise type(self).prepare_error
        return launch_context(reference)


class PacsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        PacsSDK.calls = []
        PacsSDK.native_refs = (PacsLaunchRef(ALIAS, "NATIVE1"),)
        PacsSDK.prepare_error = None
        for name in ("expire_once", "expire_pdf_once", "reject_new_sessions", "fail_scan_history",
                     "mismatched_scan_history", "fail_visits"):
            setattr(PacsSDK, name, False)
        PacsSDK.fail_scan_case = PacsSDK.cataract_order_name = ""
        self.app = BotApplication(Settings(), Path(self.temp.name), PacsSDK)
        self.key = self.app.login({"username": "TEST", "password": "synthetic", "remember": False})["account"]["id"]
        self.work = self.app.workspace(self.key)
        self.run_task(kind="resolve", identifiers=MRN)
        group = self.work.review.save_set({"mrns": MRN})
        review = self.run_task(kind="review", set_id=group["id"])
        self.scope = {"review_task_id": review["id"], "mrn": MRN, "resource": "orders", "reference": ""}
        self.run_task(kind="history", **self.scope)
        order = self.work.review.history.read(self.scope)["data"][0]
        self.scope.update(resource="order_report", reference=order["id"])
        self.viewer_patch = patch("vghks_bot.pacs.SmartIrisClient")
        self.factory = self.viewer_patch.start()
        self.viewer = self.factory.return_value.__enter__.return_value
        self.viewer.check_availability.return_value = SmartIrisAvailability("AVAILABLE")
        self.viewer.open_study.return_value = SmartIrisLaunchResult("HANDOFF_SENT")

    def tearDown(self):
        self.viewer_patch.stop()
        self.app.close()
        self.temp.cleanup()

    def run_task(self, **values):
        return wait_task(self.work, self.work.review.start(values)["task_id"])

    def test_jpeg_report_never_inspects_or_opens_native_viewer(self):
        task = self.run_task(kind="history", **self.scope)
        self.assertEqual(task["status"], "completed")
        data = self.work.review.history.read(self.scope)["data"]
        self.assertEqual(len(data["assets"]), 2)
        self.factory.assert_not_called()
        self.assertFalse(any(call[1] == "prepare-pacs" for call in PacsSDK.calls))

    def test_open_uses_native_ref_and_cached_branches_without_downloading_jpeg(self):
        self.run_task(kind="history", **self.scope)
        before = len(PacsSDK.calls)
        result = self.work.pacs.open(self.scope)
        self.assertEqual(result["status"], "HANDOFF_SENT")
        self.assertIsNone(result["viewer_loaded"])
        self.assertEqual([call[1] for call in PacsSDK.calls[before:]], ["prepare-pacs"])
        self.assertEqual(self.viewer.open_study.call_args.args[0].reference, PacsLaunchRef(ALIAS, "NATIVE1"))
        self.assertNotIn("study_uid", json.dumps(result))
        self.assertNotIn("192.0.2.10", json.dumps(result))
        # A second explicit click is a new handoff, never a deduplicated success.
        self.work.pacs.open(self.scope)
        self.assertEqual(self.viewer.open_study.call_count, 2)

    def test_legacy_cache_refreshes_only_selected_metadata_once(self):
        for method, ref in (("get_order_detail", DETAIL), ("get_order_report", REPORT)):
            value = clean(getattr(HistorySDK, "order_detail" if method.endswith("detail") else "order_report")(
                self.work.gateway.connection, ref))
            value.pop("pacs_launch_refs", None)
            self.work.analysis.store.save_step(MRN, "history-asset:" + method + ":" + digest(clean(ref)),
                "report", value, "TEST")
        PacsSDK.calls.clear()
        self.assertEqual(self.work.pacs.open(self.scope)["status"], "HANDOFF_SENT")
        self.assertEqual([call[1] for call in PacsSDK.calls if call[1] != "index"],
                         ["order-detail", "order-report", "prepare-pacs"])
        PacsSDK.calls.clear()
        self.work.pacs.open(self.scope)
        self.assertEqual([call[1] for call in PacsSDK.calls], ["prepare-pacs"])

    def test_jpeg_only_reference_is_never_promoted_to_native_pacs(self):
        PacsSDK.native_refs = ()
        result = self.work.pacs.open(self.scope)
        self.assertEqual(result["status"], "NO_PACS")
        self.viewer.open_study.assert_not_called()
        self.assertFalse(any(call[1] == "prepare-pacs" for call in PacsSDK.calls))

    def test_multiple_studies_require_explicit_choice_and_reject_foreign_choice(self):
        PacsSDK.native_refs = (PacsLaunchRef(ALIAS, "NATIVE1"), PacsLaunchRef(ALIAS, "NATIVE2"))
        result = self.work.pacs.open(self.scope)
        self.assertEqual(result["status"], "SELECTION_REQUIRED")
        self.assertEqual(len(result["choices"]), 2)
        self.viewer.open_study.assert_not_called()
        with self.assertRaises(ValueError):
            self.work.pacs.open({**self.scope, "launch_id": "forged"})
        result = self.work.pacs.open({**self.scope, "launch_id": result["choices"][1]["id"]})
        self.assertEqual(result["status"], "HANDOFF_SENT")
        self.assertEqual(self.viewer.open_study.call_args.args[0].reference.request_no, "NATIVE2")

    def test_unavailable_unknown_and_other_owner_stop_before_hospital_requests(self):
        PacsSDK.calls.clear()
        for availability in (SmartIrisAvailability("UNAVAILABLE", installation="MISSING"),
                SmartIrisAvailability("UNAVAILABLE", process="NOT_RUNNING", listener="NOT_LISTENING"),
                SmartIrisAvailability("UNAVAILABLE", owner="OTHER_PROCESS"),
                SmartIrisAvailability("UNKNOWN"), SmartIrisAvailability("UNSUPPORTED")):
            with self.subTest(availability=availability):
                self.viewer.check_availability.return_value = availability
                result = self.work.pacs.open(self.scope)
                self.assertEqual(result["status"], "UNAVAILABLE")
                self.assertTrue(result["message"])
        self.assertEqual(PacsSDK.calls, [])
        self.viewer.open_study.assert_not_called()

    def test_foreign_patient_order_and_scope_cannot_inspect_workstation(self):
        for scope in ({**self.scope, "mrn": "OTHER"}, {**self.scope, "reference": "forged"},
                {**self.scope, "resource": "numeric"}, {**self.scope, "cohort_id": "forged"}):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                self.work.pacs.open(scope)
        self.factory.assert_not_called()

    def test_offline_readonly_and_concurrent_requests_do_not_send(self):
        self.work.gateway.online = False
        with self.assertRaises(ValueError):
            self.work.pacs.open(self.scope)
        self.work.gateway.online = True
        self.app.read_only = True
        with self.assertRaises(ValueError):
            self.work.pacs.open(self.scope)
        self.app.read_only = False
        self.app.pacs_lock.acquire()
        try:
            with self.assertRaises(BusyError):
                self.work.pacs.open(self.scope)
        finally:
            self.app.pacs_lock.release()
        self.viewer.check_availability.assert_not_called()
        self.viewer.open_study.assert_not_called()

    def test_mismatched_native_patient_stops_before_prepare(self):
        PacsSDK.native_refs = (PacsLaunchRef("OTHER", "NATIVE1"),)
        with self.assertRaises(ValueError):
            self.work.pacs.open(self.scope)
        self.viewer.open_study.assert_not_called()
        self.assertFalse(any(call[1] == "prepare-pacs" for call in PacsSDK.calls))

    def test_cataract_scope_uses_same_verified_order_and_account(self):
        PacsSDK.cataract_order_name = "DBR, free charge"
        order_scope = {**self.scope, "resource": "orders", "reference": ""}
        self.run_task(kind="history", **order_scope, force=True)
        order = next(row for row in self.work.review.history.read(order_scope)["data"] if row["name"] == "DBR, free charge")
        cohort = self.work.analysis.save_cohort({"source": "manual", "account_id": self.key,
                                               "mrns": MRN, "name": "合成術前分析"})
        scope = {"cohort_id": cohort["id"], "mrn": MRN, "resource": "order_report", "reference": order["id"]}
        self.assertEqual(self.work.pacs.open(scope)["status"], "HANDOFF_SENT")
        with patch.object(self.work.gateway.connection.orders, "prepare_pacs_launch",
                return_value=launch_context(PacsLaunchRef(ALIAS, "WRONG-STUDY"))):
            with self.assertRaises(ValueError):
                self.work.pacs.open(scope)
        self.viewer.open_study.assert_called_once()

    def test_retina_scope_accepts_verified_non_cataract_orders_only_for_assigned_account(self):
        cohort = self.work.analysis.save_cohort({"source": "manual", "account_id": self.key,
                                               "mrns": MRN, "name": "合成視網膜分析"})
        scope = {key: value for key, value in self.scope.items() if key != "review_task_id"}
        scope.update(cohort_id=cohort["id"], module="retina")
        self.assertEqual(self.work.pacs.open(scope)["status"], "HANDOFF_SENT")
        with patch.object(self.work.analysis, "member", return_value={"account_id": "OTHER"}):
            with self.assertRaises(ValueError):
                self.work.pacs.open(scope)
        self.viewer.open_study.assert_called_once()

    def test_failed_prepare_and_uncertain_handoff_are_not_retried(self):
        PacsSDK.prepare_error = ParseError("synthetic mismatch", code="PACS_LAUNCH_PATIENT_MISMATCH")
        result = self.work.pacs.open(self.scope)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(sum(call[1] == "prepare-pacs" for call in PacsSDK.calls), 1)
        self.viewer.open_study.assert_not_called()
        PacsSDK.prepare_error = None
        self.viewer.open_study.return_value = SmartIrisLaunchResult("OUTCOME_UNKNOWN", error_code="SMARTIRIS_TIMEOUT")
        result = self.work.pacs.open(self.scope)
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        self.assertIn("未自動重送", result["message"])
        self.viewer.open_study.assert_called_once()
        self.assertFalse(self.app.pacs_lock.locked())

    def test_route_requires_session_csrf_and_current_database_context(self):
        server = BotServer(0, self.app)
        server.database_manager = SimpleNamespace()
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            path = f"/api/accounts/{self.key}/reviews/pacs/open"
            for auth, csrf, context, status in ((False, True, True, 401), (True, False, True, 403),
                                               (True, True, False, 400), (True, True, True, 200)):
                headers = {"Content-Type": "application/json"}
                if auth:
                    headers["X-VGHKS-Session"] = self.app.session_token
                if csrf:
                    headers["X-CSRF-Token"] = self.app.csrf_token
                if context:
                    headers["X-Database-Context"] = self.app.context_token
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                try:
                    connection.request("POST", path, json.dumps(self.scope), headers)
                    response = connection.getresponse()
                    self.assertEqual(response.status, status)
                    response.read()
                finally:
                    connection.close()
            self.viewer.open_study.assert_called_once()
        finally:
            server.shutdown()
            thread.join(2)
            server.server_close()

    def test_actual_sdk_handoff_sends_four_isolated_localhost_commands(self):
        requests = []

        class Bridge(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                requests.append((urlsplit(self.path), dict(self.headers)))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        bridge = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        thread = threading.Thread(target=bridge.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        client = SmartIrisClient(bridge_url=f"http://127.0.0.1:{bridge.server_port}")
        try:
            with patch.object(client, "check_availability", return_value=SmartIrisAvailability("AVAILABLE")), \
                    patch("vghks_bot.pacs.SmartIrisClient", return_value=client):
                result = self.work.pacs.open(self.scope)
            self.assertEqual(result["status"], "HANDOFF_SENT")
            self.assertEqual([url.path for url, _ in requests], ["/login", "/wonderurl", "/setanonymousmode", "/cmove"])
            for _, headers in requests:
                self.assertNotIn("Cookie", headers)
                self.assertNotIn("Authorization", headers)
            move = parse_qs(requests[-1][0].query)
            self.assertEqual(move["uid"], ["1.2.3.4"])
            nested = parse_qs(urlsplit(move["cmoveurl"][0]).query)
            self.assertEqual(nested["PatID"], [ALIAS])
            self.assertEqual(nested["AccNo"], ["NATIVE1"])
        finally:
            client.close()
            bridge.shutdown()
            thread.join(2)
            bridge.server_close()

    def test_browser_pacs_control_requires_gesture_and_keeps_patient_scope(self):
        subprocess.run(["node", "tests/pacs_ui.js"], cwd=Path(__file__).resolve().parents[1],
                       check=True, capture_output=True, text=True, timeout=20)
