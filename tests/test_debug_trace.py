"""Failure evidence is replayable, scoped, credential-redacted and bounded."""
import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from vghks_sdk import AuthExpiredError, ParseError
from vghks_sdk.core.config import SDKSettings
from vghks_sdk.core.operations import operation_spec
from vghks_sdk.parsing.webmaas import parse_query_form
from vghks_sdk.runtime import SDKRuntime

from vghks_bot.bot import BotApplication
from vghks_bot.debug_bundle import selected_trace
from vghks_bot.debug_evidence import BUFFER_LIMIT, EVENT_LIMIT, RESPONSE_LIMIT, safe_path
from vghks_bot.debug_trace import TraceRecorder
from vghks_bot.diagnostics import failure_details
from vghks_bot.selftest_bot import BotSyntheticSDK
from vghks_bot.settings import Settings

URL = "https://synthetic.invalid/webmaas/RSV/RSV11W001.do"


def response(body, *, status=200, url=URL, headers=None, history=()):
    value = requests.Response()
    value.status_code, value.url = status, url
    value._content = body.encode("utf-8") if isinstance(body, str) else body
    value.headers.update(headers or {"Content-Type": "text/html; charset=utf-8"})
    value.history = list(history)
    return value


def exchange(recorder, body, *, url=URL, status=200, attempt=1, kwargs=None, history=()):
    request = recorder.record_http_request(method="GET", url=url, attempt=attempt, max_attempts=2,
        throttle_delay_seconds=0, tls_verification_enabled=True, kwargs=kwargs or {})
    recorder.record_http_response(request_id=request, response=response(body, status=status, url=url, history=history),
                                  elapsed_seconds=.01)
    return request


class DebugTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.recorder = TraceRecorder(self.root / "trace")

    def tearDown(self):
        self.recorder.finalize(command="synthetic", status="CLOSED", exit_code=0)
        self.temp.cleanup()

    def test_successes_do_not_parse_html_or_write_raw_http_and_release_memory(self):
        with patch("vghks_bot.debug_trace.response_shape") as shape:
            for _ in range(120):
                key = self.recorder.start_operation(name="get_soap", app_key="prq")
                exchange(self.recorder, "<html><table>SUCCESS_CLINICAL_BODY</table></html>")
                self.recorder.finish_operation(operation_id=key, name="get_soap", status="OK")
                self.recorder.release_success()
            shape.assert_not_called()
        self.assertFalse(list(self.recorder.directory.glob("response-*")))
        self.assertFalse(list(self.recorder.directory.glob("operation-*")))
        self.assertLess(self.recorder.trace_path.stat().st_size, 4000)
        self.assertEqual(self.recorder.http_local.last["buffer_bytes"], 0)
        self.assertFalse(self.recorder.http_local.last["events"])
        self.assertNotIn("SUCCESS_CLINICAL_BODY", self.recorder.trace_path.read_text())

    def test_failure_probe_keeps_navigation_readiness_body_and_replayable_redacted_page(self):
        self.recorder.set_context({"interaction_id": "abc", "service": "patients", "method": "get_demographics"})
        check = self.recorder.start_operation(name="auth_check", app_key="portal")
        exchange(self.recorder, "PORTAL_ALIVE", url="https://synthetic.invalid/sessionCheck.do")
        self.recorder.finish_operation(operation_id=check, name="auth_check", status="OK")
        probe = self.recorder.start_operation(name="patient_session_check", app_key="webmaas")
        redirect = response("", status=302, headers={"Location": "/webmaas/common/sessionExpired.jsp?hid=HID_SECRET"})
        body = '''<html><title>登入逾時</title><input name="password" value="PW_SECRET">
                  <script>var csrfToken="CSRF_SECRET";</script> COOKIE_SECRET 病歷內容保留</html>'''
        request = self.recorder.record_http_request(method="GET", url=URL, attempt=1, max_attempts=1,
            throttle_delay_seconds=0, tls_verification_enabled=True,
            kwargs={"params": {"hid": "HID_SECRET"}, "data": {"password": "PW_SECRET"},
                    "headers": {"Cookie": "sid=COOKIE_SECRET"}})
        self.recorder.record_http_response(request_id=request, elapsed_seconds=.01,
            response=response(body, url="https://synthetic.invalid/webmaas/common/sessionExpired.jsp?hid=HID_SECRET", history=[redirect]))
        error = ParseError("SECRET", code="WEBMAAS_QUERY_FORM_MISSING")
        self.recorder.finish_operation(operation_id=probe, name="patient_session_check", status="ERROR", exc=error)
        transport = self.recorder.failure_transport(error)
        source = (self.recorder.directory / f"response-{probe}-{request}.html").read_text(encoding="utf-8")
        self.assertIn("登入逾時", source)
        self.assertIn("病歷內容保留", source)
        self.assertNotIn("_SECRET", source)
        self.assertNotIn("_SECRET", json.dumps(transport))
        self.assertEqual(transport["navigation"]["path"], "/webmaas/common/sessionExpired.jsp")
        self.assertEqual(transport["redirects"][0]["location"]["path"], "/webmaas/common/sessionExpired.jsp")
        self.assertTrue(any(entry["supporting_readiness_response"] for entry in transport["evidence"]["responses"]))
        with self.assertRaises(ParseError) as caught:
            parse_query_form(source, "RSV11WForm")
        self.assertEqual(caught.exception.info.code, error.info.code)
        events = [json.loads(line) for line in (self.recorder.directory / f"operation-{probe}.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["sequence"] for event in events], sorted(event["sequence"] for event in events))
        self.assertTrue(any(event.get("response", {}).get("html_shape", {}).get("selector_counts", {}).get("form#RSV11WForm") == 0 for event in events))

    def test_response_and_event_limits_are_explicit_without_unbounded_buffers(self):
        key = self.recorder.start_operation(name="get_soap", app_key="prq")
        exchange(self.recorder, b"X" * (RESPONSE_LIMIT + 100))
        self.assertLessEqual(self.recorder._frame()["buffer_bytes"], BUFFER_LIMIT)
        for _ in range(EVENT_LIMIT + 20):
            request = self.recorder.record_http_request(method="GET", url=URL, attempt=1, max_attempts=1,
                throttle_delay_seconds=0, tls_verification_enabled=True, kwargs={})
        self.recorder.record_http_response(request_id=request, response=response(b"X" * (RESPONSE_LIMIT + 100)), elapsed_seconds=.01)
        self.assertLessEqual(len(self.recorder._frame()["events"]), EVENT_LIMIT)
        error = ParseError("synthetic oversized reply")
        self.recorder.finish_operation(operation_id=key, name="get_soap", status="ERROR", exc=error)
        evidence = self.recorder.failure_transport(error)["evidence"]
        self.assertFalse(evidence["complete"])
        self.assertGreater(evidence["dropped_events"], 0)
        self.assertTrue(evidence["responses"][0]["truncated"])
        self.assertTrue(all(file["size_bytes"] <= RESPONSE_LIMIT for file in evidence["files"] if file["name"].endswith((".bin", ".html"))))

    def test_error_without_response_never_uses_another_requests_http_status(self):
        key = self.recorder.start_operation(name="get_soap", app_key="prq")
        exchange(self.recorder, "earlier page")
        request = self.recorder.record_http_request(method="GET", url=URL, attempt=1, max_attempts=1,
            throttle_delay_seconds=0, tls_verification_enabled=True, kwargs={})
        self.recorder.record_network_error(request_id=request, exc=TimeoutError(), elapsed_seconds=1, will_retry=False)
        error = ParseError("no response")
        self.recorder.finish_operation(operation_id=key, name="get_soap", status="ERROR", exc=error)
        transport = self.recorder.failure_transport(error)
        self.assertEqual(transport["request_id"], request)
        self.assertNotIn("http_status", transport)
        self.assertFalse(transport["evidence"]["responses"])

    def test_http_retry_keeps_failed_body_only_and_queue_does_not_hold_success_bytes(self):
        key = self.recorder.start_operation(name="get_soap", app_key="prq")
        request = exchange(self.recorder, "<html>SERVICE_UNAVAILABLE</html>", status=503)
        self.recorder.record_retry(request_id=request, reason="HTTP_503", next_attempt=2, delay_seconds=.1)
        exchange(self.recorder, "SUCCESS_BODY" * 10000, attempt=2)
        self.recorder.finish_operation(operation_id=key, name="get_soap", status="OK")
        self.assertTrue(all(not snapshot["_raw"] for frame in self.recorder._pending_incidents
                            for snapshot in frame["snapshots"].values()))
        incident, = self.recorder.consume_incidents()
        self.assertEqual(incident["code"], "HTTP_503")
        self.assertEqual(incident["http_status"], 503)
        self.assertEqual(incident["transport"]["http_status"], 200)
        sources = b"".join(path.read_bytes() for path in self.recorder.directory.glob("response-*.html"))
        self.assertIn(b"SERVICE_UNAVAILABLE", sources)
        self.assertNotIn(b"SUCCESS_BODY", sources)

    def test_late_auth_check_recovery_identifies_expired_page_instead_of_final_success(self):
        key = self.recorder.start_operation(name="auth_check", app_key="portal")
        request = self.recorder.record_http_request(method="GET", url="https://synthetic.invalid/sessionCheck.do",
            attempt=1, max_attempts=1, throttle_delay_seconds=0, tls_verification_enabled=True, kwargs={})
        self.recorder.record_http_response(request_id=request, response=response(
            '<form action="login.do"><input name="HID" value="EXPIRED_HID"><input name="password" value="PW_SECRET"></form>',
            url="https://synthetic.invalid/index.do"), elapsed_seconds=.01)
        exchange(self.recorder, '<form action="login.do"><input name="HID" value="NORMAL_LOGIN_ENTRY"></form>',
                 url="https://synthetic.invalid/index.do")
        exchange(self.recorder, "FINAL_PORTAL_SUCCESS", url="https://synthetic.invalid/sessionCheck.do")
        self.recorder.record_reauthentication(operation_id=key, app_key="portal")
        self.recorder.finish_operation(operation_id=key, name="auth_check", status="OK")
        incident, = self.recorder.consume_incidents()
        self.assertEqual([entry["request_id"] for entry in incident["transport"]["evidence"]["responses"]], [request])
        sources = b"".join(path.read_bytes() for path in self.recorder.directory.glob("response-*.html"))
        self.assertNotIn(b"FINAL_PORTAL_SUCCESS", sources)
        self.assertNotIn(b"NORMAL_LOGIN_ENTRY", sources)
        self.assertNotIn(b"PW_SECRET", sources)
        self.assertNotIn(b"EXPIRED_HID", sources)

    def test_late_recovery_with_unknown_expired_page_reports_missing_evidence(self):
        key = self.recorder.start_operation(name="auth_check", app_key="portal")
        exchange(self.recorder, "FINAL_SUCCESS_ONLY", url="https://synthetic.invalid/sessionCheck.do")
        self.recorder.record_reauthentication(operation_id=key, app_key="portal")
        self.recorder.finish_operation(operation_id=key, name="auth_check", status="OK")
        incident, = self.recorder.consume_incidents()
        evidence = incident["transport"]["evidence"]
        self.assertFalse(evidence["complete"])
        self.assertEqual(evidence["capture_error"], "RECOVERY_RESPONSE_NOT_IDENTIFIED")
        self.assertFalse(evidence["responses"])

    def test_post_sdk_validation_failure_is_saved_once_and_keeps_stable_file_hashes(self):
        key = self.recorder.start_operation(name="get_soap", app_key="prq")
        exchange(self.recorder, "<html>INCOMPLETE_CLINICAL_BODY</html>")
        self.recorder.finish_operation(operation_id=key, name="get_soap", status="OK")
        error = ParseError("incomplete parsed result", code="ACQUISITION_PARTIAL")
        first = self.recorder.failure_transport(error)
        second = self.recorder.failure_transport(error)
        self.assertEqual(first, second)
        trace = (self.recorder.directory / f"operation-{key}.jsonl").read_text(encoding="utf-8")
        self.assertEqual(trace.count('"event": "validation_failed"'), 1)
        self.assertTrue(first["evidence"]["responses"])

    def test_evidence_write_failure_is_reported_without_changing_query_error(self):
        key = self.recorder.start_operation(name="patient_session_check", app_key="webmaas")
        exchange(self.recorder, "<html>expired</html>")
        error = ParseError("missing form")
        with patch.object(Path, "write_bytes", side_effect=OSError("synthetic disk full")):
            self.recorder.finish_operation(operation_id=key, name="patient_session_check", status="ERROR", exc=error)
        evidence = self.recorder.failure_transport(error)["evidence"]
        self.assertFalse(evidence["complete"])
        self.assertEqual(evidence["capture_error"], "RAW_EVIDENCE_SAVE_FAILED")

    def test_nested_operations_keep_child_requests_and_restore_parent_request_context(self):
        parent = self.recorder.start_operation(name="get_history", app_key="prq")
        child = self.recorder.start_operation(name="get_soap", app_key="prq")
        request = exchange(self.recorder, "<html>child reply</html>")
        self.recorder.finish_operation(operation_id=child, name="get_soap", status="OK")
        self.assertEqual(self.recorder._local.operation_id, parent)
        error = ParseError("outer validation")
        self.recorder.finish_operation(operation_id=parent, name="get_history", status="ERROR", exc=error)
        transport = self.recorder.failure_transport(error)
        self.assertEqual(transport["request_id"], request)
        self.assertEqual(transport["children"][0]["parent_operation_id"], parent)
        self.assertTrue(any(entry["source_operation_id"] == child for entry in transport["evidence"]["responses"]))
        content = (self.recorder.directory / f"operation-{parent}.jsonl").read_text()
        self.assertIn('"event": "http_response"', content)

    def test_unknown_sensitive_paths_stay_masked_and_known_error_route_is_preserved(self):
        self.assertEqual(safe_path("https://synthetic.invalid/webmaas/common/sessionExpired.jsp?hid=SECRET"),
                         "/webmaas/common/sessionExpired.jsp")
        self.assertNotIn("00012345", safe_path("https://synthetic.invalid/PRQWeb/00012345/report.pdf"))

    def test_sdk_capture_operation_reference_is_forwarded_to_failure_trace(self):
        key = self.recorder.start_operation(name="get_patient_demographics", app_key="webmaas")
        exchange(self.recorder, "<html>synthetic failure</html>")
        error = ParseError("synthetic failure")
        self.recorder.finish_operation(operation_id=key, name="get_patient_demographics", status="ERROR", exc=error,
                                       capture_operation_id="op-000107")
        trace = (self.recorder.directory / f"operation-{key}.jsonl").read_text(encoding="utf-8")
        self.assertIn('"capture_operation_id": "op-000107"', trace)

    def test_many_nested_operations_keep_parent_response_count_and_evidence_bounded(self):
        parent = self.recorder.start_operation(name="get_history", app_key="prq")
        for _ in range(12):
            child = self.recorder.start_operation(name="get_soap", app_key="prq")
            exchange(self.recorder, "child body")
            self.recorder.finish_operation(operation_id=child, name="get_soap", status="OK")
        self.assertEqual(len(self.recorder._frame()["snapshots"]), 8)
        child = self.recorder.start_operation(name="get_soap", app_key="prq")
        exchange(self.recorder, "failed child")
        error = ParseError("synthetic child error")
        with patch("vghks_bot.debug_trace.EVIDENCE_LIMIT", 100):
            self.recorder.finish_operation(operation_id=child, name="get_soap", status="ERROR", exc=error)
            self.assertLessEqual(self.recorder._frame()["evidence_bytes"], 100)
            self.assertEqual(self.recorder._frame()["capture_error"], "EVIDENCE_LIMIT_REACHED")
        self.recorder.finish_operation(operation_id=parent, name="get_history", status="ERROR", exc=error)
        self.assertFalse(self.recorder.failure_transport(error)["evidence"]["complete"])

    def test_simultaneous_operations_do_not_mix_thread_responses(self):
        barrier = threading.Barrier(2)
        results = []

        def fail(marker):
            key = self.recorder.start_operation(name="get_soap", app_key="prq")
            barrier.wait()
            exchange(self.recorder, "<html>" + marker + "</html>")
            error = ParseError("synthetic")
            self.recorder.finish_operation(operation_id=key, name="get_soap", status="ERROR", exc=error)
            results.append((marker, self.recorder.failure_transport(error)))

        threads = [threading.Thread(target=fail, args=(marker,)) for marker in ("FIRST_ONLY", "SECOND_ONLY")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(results), 2)
        for marker, transport in results:
            source = next(file["name"] for file in transport["evidence"]["files"] if file["name"].endswith(".html"))
            self.assertIn(marker, (self.recorder.directory / source).read_text())
            self.assertEqual(len(transport["evidence"]["responses"]), 1)

    def test_legacy_trace_export_remains_readable_and_reports_capacity_limit(self):
        self.recorder.trace_path.write_text('{"event":"run_started"}\n{"event":"http_request"}\n', encoding="utf-8")
        content, truncated = selected_trace(self.recorder.trace_path, set(), 1000)
        self.assertIn(b"http_request", content)
        self.assertFalse(truncated)
        _, truncated = selected_trace(self.recorder.trace_path, set(), 10)
        self.assertTrue(truncated)


class DebugIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = BotApplication(Settings(), Path(self.temp.name), BotSyntheticSDK)
        self.account = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.account)
        self.recorder = self.work.diagnostics.recorder()
        self.work.gateway.recorder = self.recorder

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_sdk_recovery_is_saved_even_when_final_query_succeeds(self):
        auth = SimpleNamespace(generation=1, ensure=Mock())
        auth.login = Mock(side_effect=lambda **kwargs: setattr(auth, "generation", auth.generation + 1))
        runtime = SDKRuntime(settings=SDKSettings(), transport=SimpleNamespace(), auth=auth, diagnostics=self.recorder)
        self.work.gateway.connection._runtime = runtime
        self.work.gateway.observe_auth()
        attempts = []

        def operation():
            attempts.append(True)
            exchange(self.recorder, "<html>SESSION_EXPIRED</html>" if len(attempts) == 1 else "SUCCESS_BODY_NOT_SAVED")
            if len(attempts) == 1:
                raise AuthExpiredError("synthetic expiry")
            return []

        self.work.gateway.connection.records.get_probe = lambda: runtime.execute(
            operation_spec("webmaas.demographics"), operation, operation_name="get_probe")
        with self.work.gateway.task_context("sdk-recovered", "review"):
            self.assertEqual(self.work.gateway.invoke("records", "get_probe"), [])
        report = self.work.diagnostics.query({"task_id": "sdk-recovered"})
        self.assertEqual(len(report["items"]), 1)
        self.assertTrue(report["items"][0]["recovered"])
        self.assertEqual(report["items"][0]["error"]["code"], "SDK_SESSION_RECOVERED")
        auth.login.assert_called_once_with(force=True)
        self.assertEqual(len(attempts), 2)
        with zipfile.ZipFile(io.BytesIO(self.work.diagnostics.export({"task_id": "sdk-recovered"}))) as archive:
            self.assertIn("sdk-context.json", archive.namelist())
            sources = [archive.read(name) for name in archive.namelist() if name.endswith(".html")]
            self.assertEqual(len(sources), 1)
            self.assertIn(b"SESSION_EXPIRED", sources[0])
            self.assertNotIn(b"SUCCESS_BODY_NOT_SAVED", b"".join(archive.read(name) for name in archive.namelist()))
            trace = next(archive.read(name).decode() for name in archive.namelist() if "/operation-" in name)
            self.assertIn('"event": "reauthentication_started"', trace)
            self.assertEqual(trace.count('"event": "http_request"'), 2)

    def test_export_includes_only_selected_incident_and_successes_stay_compact(self):
        for task, marker in (("FIRST", "FIRST_RESPONSE"), ("SECOND", "SECOND_RESPONSE")):
            key = self.recorder.start_operation(name="get_soap", app_key="prq")
            exchange(self.recorder, "<html>" + marker + "</html>")
            error = ParseError("synthetic parser failure")
            self.recorder.finish_operation(operation_id=key, name="get_soap", status="ERROR", exc=error)
            self.work.diagnostics.save({"task_id": task, "sdk_run_id": self.recorder.run_id,
                "phase": "records.get_soap", "error": failure_details(error),
                "transport": self.recorder.failure_transport(error)})
        with zipfile.ZipFile(io.BytesIO(self.work.diagnostics.export({"task_id": "FIRST"}))) as archive:
            all_content = b"".join(archive.read(name) for name in archive.namelist())
            self.assertIn(b"FIRST_RESPONSE", all_content)
            self.assertNotIn(b"SECOND_RESPONSE", all_content)
            self.assertFalse(json.loads(archive.read("debug.json"))["export"]["missing_files"])


if __name__ == "__main__":
    unittest.main()
