"""SDK 0.22 acquisition, session recovery and outer retry safety integration."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vghks_sdk import (
    ApplicationSessionExpiredError,
    AuthenticationError,
    AuthExpiredError,
    AuthorizationError,
    LoginRejectedError,
    NotAuthenticatedError,
    NotFoundError,
    PasswordChangeRequiredError,
    RequestError,
    SDKSettings,
    assess_data,
)
from vghks_sdk.core.errors import error_info
from vghks_sdk.core.operations import operation_spec
from vghks_sdk.models import NumericTable, PasswordStatus
from vghks_sdk.runtime import SDKRuntime

from vghks_bot.bot import BotApplication
from vghks_bot.connection_state import failure_state, readiness_error, should_pause
from vghks_bot.diagnostics import failure_details, failure_summary
from vghks_bot.review import failure_status
from vghks_bot.scanner import safe_failure
from vghks_bot.selftest_bot import BotSyntheticSDK, wait_task
from vghks_bot.settings import Settings


class FailureClassificationTests(unittest.TestCase):
    def test_webmaas_readiness_retains_sdk_application_expiry_type_and_code(self):
        issue = ApplicationSessionExpiredError("synthetic timeout", code="WEBMAAS_SESSION_TIMEOUT", app="webmaas",
                                               http_status=200, endpoint_path="/webmaas/comm/pageTimeOut.do")
        report = SimpleNamespace(targets=(SimpleNamespace(status="ERROR", issue=error_info(issue)),))
        exc = readiness_error(report)
        self.assertIsInstance(exc, ApplicationSessionExpiredError)
        self.assertEqual(error_info(exc), error_info(issue))
        self.assertNotIn("重建", safe_failure(exc)[0])
        issue.with_context(attempt=2, retry_safe=False)
        self.assertIn("SDK 重建", safe_failure(issue)[0])

    def test_readiness_preserves_structured_cause_and_unsafe_outer_flag(self):
        inner = RequestError("SECRET_COOKIE", code="NETWORK_DNS_FAILED", retry_safe=True,
                             retry_recommended=True)
        outer = AuthenticationError("SECRET_PASSWORD", code="AUTH_RELOGIN_FAILED",
                                    phase="REAUTHENTICATION", cause=error_info(inner))
        report = SimpleNamespace(targets=(SimpleNamespace(status="ERROR", issue=error_info(outer)),))
        exc = readiness_error(report)
        self.assertEqual(error_info(exc), error_info(outer))
        self.assertEqual(error_info(exc).root_cause.code, "NETWORK_DNS_FAILED")
        state = failure_state(exc)
        self.assertEqual(state["action"], "network")
        self.assertFalse(state["retry_safe"])
        self.assertFalse(state["auto_reconnect"])
        self.assertIn("DNS", safe_failure(exc)[0])
        summary = failure_summary({"error": failure_details(exc), "phase": "orders.get_order_history"})
        self.assertEqual(summary["root_cause"]["code"], "NETWORK_DNS_FAILED")
        self.assertIn("網路問題", summary["reason"])
        self.assertNotIn("SECRET", json.dumps(summary))

    def test_authentication_cases_keep_distinct_actions_and_types(self):
        for exc, action in [(LoginRejectedError("SECRET"), "credentials"),
                            (LoginRejectedError("SECRET", code="PORTAL_LOGIN_REJECTED"), "credentials"),
                            (NotAuthenticatedError("SECRET"), "login"),
                            (PasswordChangeRequiredError("SECRET"), "password_change"),
                            (AuthExpiredError("SECRET"), "reconnect")]:
            with self.subTest(code=error_info(exc).code):
                report = SimpleNamespace(targets=(SimpleNamespace(status="ERROR", issue=error_info(exc)),))
                self.assertIsInstance(readiness_error(report), AuthenticationError)
                self.assertEqual(failure_state(exc)["action"], action)
                self.assertTrue(should_pause(exc))
                self.assertFalse(failure_state(exc)["auto_reconnect"])
                self.assertNotIn("SECRET", safe_failure(exc)[0])

    def test_legacy_diagnostic_keeps_its_recorded_code_as_root_cause(self):
        summary = failure_summary({"code": "AUTH_LOGIN_REJECTED", "message": "old safe summary"})
        self.assertEqual(summary["code"], "AUTH_LOGIN_REJECTED")
        self.assertEqual(summary["root_cause"]["code"], "AUTH_LOGIN_REJECTED")
        self.assertIn("院方限制", summary["next_step"])

    def test_http_denied_is_not_confirmed_permission_or_missing_patient(self):
        denied = AuthenticationError("SECRET", code="AUTH_HTTP_DENIED", http_status=403)
        self.assertEqual(failure_status(denied), "error")
        self.assertEqual(failure_status(AuthorizationError("SECRET", http_status=403)), "forbidden")
        missing_endpoint = RequestError("SECRET", code="HTTP_404", status_code=404, retry_safe=True)
        self.assertFalse(should_pause(missing_endpoint))
        self.assertIn("尚不能判定", safe_failure(missing_endpoint)[0])
        self.assertEqual(error_info(missing_endpoint).category, "HTTP")
        self.assertEqual(failure_state(NotFoundError("SECRET"))["action"], "lookup")

    def test_dns_tls_proxy_and_timeout_have_specific_messages(self):
        for code, hint in [("NETWORK_DNS_FAILED", "DNS"), ("TLS_VERIFY_FAILED", "憑證"),
                           ("NETWORK_PROXY_FAILED", "代理"), ("NETWORK_READ_TIMEOUT", "回應逾時")]:
            exc = RequestError("SECRET", code=code, retry_safe=True)
            self.assertEqual(failure_state(exc)["action"], "network")
            self.assertIn(hint, safe_failure(exc)[0])
            self.assertTrue(should_pause(exc))

    def test_partial_numeric_warning_remains_distinct_from_parsing_failure(self):
        aligned = NumericTable(title="Va", headers=("日期", "OD", "OS"), header_rows=(("日期", "Va"), ("OD", "OS")),
                               column_paths=(("日期",), ("Va", "OD"), ("Va", "OS")),
                               rows=(("2026-01-01", "1.0", "0.8"),),
                               parsing_issues=("NUMERIC_HEADER_SPAN_MISMATCH",))
        data = assess_data(aligned)
        self.assertTrue(data.complete)
        self.assertFalse(data.issues)
        self.assertEqual(data.warnings[0].code, "NUMERIC_HEADER_SPAN_MISMATCH")
        self.assertIsNone(assess_data({}).complete)
        self.assertEqual(assess_data({}).availability, "UNKNOWN")


class ConnectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = BotApplication(Settings(), Path(self.temp.name), BotSyntheticSDK)
        self.key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.key)
        self.gateway = self.work.gateway

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def runtime(self, *, login_error=None):
        auth = SimpleNamespace(generation=1, password_status=PasswordStatus(), ensure=Mock())
        def login(*, force):
            if login_error:
                raise login_error
            auth.generation += 1
        auth.login = Mock(side_effect=login)
        runtime = SDKRuntime(settings=SDKSettings(), transport=SimpleNamespace(), auth=auth)
        self.gateway.connection._runtime = runtime
        self.gateway.observe_auth()
        return runtime

    def test_real_runtime_restores_expired_read_once_without_rebuilding_account(self):
        runtime = self.runtime()
        operation = Mock(side_effect=[AuthExpiredError("SECRET"), []])
        self.gateway.connection.opd.get_probe = lambda: runtime.execute(
            operation_spec("webmaas.demographics"), operation, operation_name="get_probe")
        original_session = self.gateway.session_id
        with patch.object(self.gateway, "factory", wraps=self.gateway.factory) as factory:
            self.assertEqual(self.gateway.invoke("opd", "get_probe"), [])
            factory.assert_not_called()
        self.assertEqual(operation.call_count, 2)
        runtime.auth.login.assert_called_once_with(force=True)
        self.assertEqual(self.gateway.session_id, original_session)
        self.assertEqual(self.gateway.recovery_count, 1)
        self.assertTrue(self.gateway.online)

    def test_failed_runtime_recovery_retains_network_cause_and_stops_login(self):
        runtime = self.runtime(login_error=RequestError("SECRET", code="NETWORK_DNS_FAILED", retry_safe=True))
        operation = Mock(side_effect=AuthExpiredError("SECRET"))
        self.gateway.connection.opd.get_probe = lambda: runtime.execute(
            operation_spec("webmaas.demographics"), operation, operation_name="get_probe")
        with self.assertRaises(AuthenticationError) as caught:
            self.gateway.invoke("opd", "get_probe")
        self.assertEqual(error_info(caught.exception).code, "AUTH_RELOGIN_FAILED")
        self.assertEqual(self.gateway.connection_issue["root_cause"]["code"], "NETWORK_DNS_FAILED")
        self.assertEqual(self.gateway.connection_issue["action"], "network")
        self.assertFalse(self.gateway.connection_issue["retry_safe"])
        runtime.auth.login.assert_called_once_with(force=True)
        self.assertEqual(operation.call_count, 1)
        with self.assertRaises(Exception) as repeated:
            self.gateway.invoke("opd", "get_probe")
        self.assertEqual(error_info(repeated.exception).root_cause.code, "NETWORK_DNS_FAILED")
        runtime.auth.login.assert_called_once()

    def test_password_change_and_login_rejection_during_recovery_stop_queries(self):
        for error in (PasswordChangeRequiredError("SECRET"), LoginRejectedError("SECRET")):
            with self.subTest(code=error_info(error).code):
                self.gateway.online = True
                runtime = self.runtime(login_error=error)
                operation = Mock(side_effect=AuthExpiredError("SECRET"))
                self.gateway.connection.opd.get_probe = lambda runtime=runtime, operation=operation: runtime.execute(
                    operation_spec("webmaas.demographics"), operation, operation_name="get_probe")
                with self.assertRaises(type(error)):
                    self.gateway.invoke("opd", "get_probe")
                runtime.auth.login.assert_called_once()
                self.assertEqual(operation.call_count, 1)
                self.assertEqual(self.gateway.last_error_code, error_info(error).code)
                self.assertFalse(self.gateway.online)

    def test_clinical_write_inside_read_never_replays_even_with_safe_network_cause(self):
        runtime = self.runtime()
        def operation():
            runtime._operation_write_attempts[-1] = True
            raise RequestError("SECRET", code="NETWORK_READ_TIMEOUT", retry_safe=True, retry_recommended=True)
        query = Mock(side_effect=operation)
        self.gateway.connection.orders.get_probe = lambda: runtime.execute(
            operation_spec("webmaas.demographics"), query, operation_name="get_probe")
        with self.assertRaises(RequestError) as caught:
            self.gateway.invoke("orders", "get_probe")
        self.assertFalse(error_info(caught.exception).retry_safe)
        self.assertFalse(error_info(caught.exception).retry_recommended)
        self.assertEqual(query.call_count, 1)
        runtime.auth.login.assert_not_called()

    def test_all_services_pause_network_errors_without_an_outer_relogin(self):
        for service in ("opd", "records", "orders", "patients", "surgery", "reviews", "earnings"):
            with self.subTest(service=service):
                self.gateway.online = True
                read = Mock(side_effect=RequestError("SECRET", code="NETWORK_READ_TIMEOUT", retry_safe=True))
                getattr(self.gateway.connection, service).get_probe = read
                with patch.object(self.gateway, "_reconnect_locked") as reconnect:
                    with self.assertRaises(RequestError):
                        self.gateway.invoke(service, "get_probe")
                    reconnect.assert_not_called()
                read.assert_called_once()
                self.assertFalse(self.gateway.online)

    def test_empty_unknown_and_partial_results_are_saved_separately_in_events(self):
        partial = NumericTable(title="Va", headers=("日期", "OD"), rows=(("2026-01-01", "1.0"),),
                               parsing_issues=("NUMERIC_HEADER_SPAN_MISMATCH",))
        for value, status, availability in [([], "empty", "EMPTY"), (None, "empty", "EMPTY"),
                                             ({}, "ok", "UNKNOWN"), (partial, "partial", "AVAILABLE")]:
            with self.subTest(status=status, availability=availability):
                self.gateway.connection.orders.get_probe = Mock(return_value=value)
                with self.gateway.task_context("synthetic-task", "history"):
                    self.assertIs(self.gateway.invoke("orders", "get_probe"), value)
                event = self.work.review.db.sdk_events(task_id="synthetic-task")[0]
                self.assertEqual(event["status"], status)
                self.assertEqual(event["assessment"]["availability"], availability)
                self.assertTrue(self.gateway.online)
        self.assertNotIn("2026-01-01", json.dumps(event["assessment"]))

    def test_password_countdown_is_in_status_without_disabling_queries(self):
        self.gateway.connection.auth.password_status = PasswordStatus("EXPIRING", 3, "VISIBLE_TEXT")
        self.gateway.connection.opd.get_probe = Mock(return_value=[])
        self.gateway.invoke("opd", "get_probe")
        from vghks_bot.workbench import status as workbench_status
        status = workbench_status(self.work)
        self.assertEqual(status["password_status"]["remaining_days"], 3)
        self.assertTrue(status["online"])
        self.assertFalse(status["connection_issue"])

    def test_password_change_during_activate_has_explicit_result_and_no_secret(self):
        self.gateway.online = False
        with patch.object(self.gateway, "login", side_effect=PasswordChangeRequiredError("SECRET")) as login:
            result = self.app.activate(self.key)
        login.assert_called_once()
        self.assertEqual(result["status"], "password_change_required")
        self.assertEqual(result["connection_issue"]["action"], "password_change")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_http_503_pauses_patient_task_without_claiming_absence_or_invalidating_login(self):
        resolved = self.work.review.start({"kind": "resolve", "identifiers": "TEST001"})["task_id"]
        wait_task(self.work, resolved)
        group = self.work.review.save_set({"name": "合成", "mrns": "TEST001"})
        with patch.object(self.gateway.connection.records, "get_visit_cases", side_effect=RequestError(
                "SECRET", code="HTTP_503", status_code=503, retry_safe=True, retry_recommended=True)) as read:
            key = self.work.review.start({"kind": "review", "set_id": group["id"]})["task_id"]
            task = wait_task(self.work, key)
        self.assertEqual(task["status"], "paused")
        self.assertEqual(task["error_code"], "HTTP_503")
        self.assertEqual(read.call_count, 1)
        self.assertTrue(self.gateway.online)
        self.assertEqual(self.gateway.connection_issue["action"], "service")

    def test_legacy_sdk_event_table_migrates_without_losing_rows(self):
        db = self.work.review.db
        db.sdk_event(session_id="old", task_id="old-task", service="orders", method="get_order_history", status="ok")
        with db.library.connect() as connection:
            connection.execute("ALTER TABLE bot_sdk_events DROP COLUMN assessment")
        from vghks_bot.bot_store import WorkbenchStore
        migrated = WorkbenchStore(db.library)
        self.assertEqual(migrated.sdk_events(task_id="old-task")[0]["assessment"], {})

    def test_partial_cached_analysis_remains_partial_without_another_query(self):
        from vghks_bot.analysis_fetch import PatientCollector
        from vghks_bot.scanner import ScanState
        from vghks_bot.settings import today

        partial = NumericTable("Va", ("日期", "OD"), (("2026-01-01", "1.0"),),
                               parsing_issues=("NUMERIC_HEADER_SPAN_MISMATCH",))
        def collector():
            state = ScanState(today(), today())
            state.data["counts"].update(analysis_cached=0, analysis_fetched=0)
            return PatientCollector(self.work.analysis.store, state, {"mrn": "TEST001"},
                                    self.work.settings, None, {"force": False})
        first = collector()
        query = Mock(return_value=partial)
        self.assertIsNotNone(first.query("synthetic-numeric", "numeric", query))
        self.assertEqual(first.state.data["counts"]["errors"], 1)
        assessment = self.work.analysis.store.step("TEST001", "synthetic-numeric")["assessment"]
        self.assertEqual(assessment["issues"][0]["code"], "NUMERIC_HEADER_SPAN_MISMATCH")
        self.assertFalse(assessment["warnings"])
        self.assertFalse(assessment["complete"])
        second = collector()
        forbidden = Mock(side_effect=AssertionError("cached partial data must be preserved"))
        self.assertEqual(second.query("synthetic-numeric", "numeric", forbidden), first.seen["synthetic-numeric"])
        forbidden.assert_not_called()
        self.assertEqual(second.state.data["counts"]["errors"], 1)

    def test_empty_result_and_network_failure_have_different_cache_effects(self):
        from vghks_bot.analysis_fetch import PatientCollector
        from vghks_bot.scanner import ScanState
        from vghks_bot.settings import today

        state = ScanState(today(), today())
        state.data["counts"].update(analysis_cached=0, analysis_fetched=0)
        collector = PatientCollector(self.work.analysis.store, state, {"mrn": "TEST001"},
                                     self.work.settings, None, {"force": False})
        self.assertEqual(collector.query("synthetic-empty", "order_index", lambda: []), [])
        self.assertEqual(self.work.analysis.store.step("TEST001", "synthetic-empty")["assessment"]["availability"], "EMPTY")
        with self.assertRaises(RequestError):
            collector.query("synthetic-error", "order_index", Mock(side_effect=RequestError("SECRET", code="NETWORK_DNS_FAILED")))
        self.assertIsNone(self.work.analysis.store.step("TEST001", "synthetic-error"))


if __name__ == "__main__":
    unittest.main()
