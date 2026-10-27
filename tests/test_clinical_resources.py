"""Shared acquisitions retain identity, pacing, cancellation and cache fences."""
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import requests
from vghks_sdk import ParseError, RequestError, VisitCase
from vghks_sdk.core.config import RequestPolicy
from vghks_sdk.models import BinaryAsset, OrderReport, OrderReportRef, PacsImageRef

from vghks_bot.analysis_fetch import PatientCollector, clean, model
from vghks_bot.analysis_store import digest
from vghks_bot.bot import BotApplication
from vghks_bot.encoding import ClinicalTransport
from vghks_bot.jobs import BusyError
from vghks_bot.review import case_key
from vghks_bot.scanner import Cancelled, ScanState
from vghks_bot.selftest_bot import BotSyntheticSDK
from vghks_bot.settings import Settings, today


class ClinicalResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        BotSyntheticSDK.calls, BotSyntheticSDK.on_soap, BotSyntheticSDK.fail_case = [], None, ""
        self.app = BotApplication(Settings(), Path(self.temp.name), BotSyntheticSDK)
        key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(key)
        self.results, self.errors, self.threads = {}, {}, []

    def tearDown(self):
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), "shared consumer did not finish")
        self.app.close()
        self.temp.cleanup()

    def launch(self, name, operation, *, cancel=None, foreground=False):
        cancel = cancel or threading.Event()
        def run():
            try:
                with self.work.gateway.task_context(name, "synthetic", cancel):
                    if foreground:
                        with self.work.gateway.foreground():
                            self.results[name] = operation()
                    else:
                        self.results[name] = operation()
            except BaseException as exc:
                self.errors[name] = exc
        thread = threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()
        return thread

    def wait_subscribers(self, count):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.work.resources.condition:
                if any(len(flight.subscribers) >= count for flight in self.work.resources.flights.values()):
                    return
            time.sleep(.01)
        self.fail("consumers did not join the same flight")

    def unit(self, *, key="unit", fetch, cache=None, force=False, pacing="normal"):
        cache = cache if cache is not None else {}
        def save(value, saved, _assessment):
            cache["value"] = value
            return value, saved
        return self.work.resources.acquire((key,), "TEST001", "visits",
            load=lambda: (cache["value"], "saved") if "value" in cache else None,
            fetch=fetch, save=save, force=force, pacing=pacing)

    def test_visits_share_across_tasks_and_copies_cannot_mutate_each_other(self):
        entered, release = threading.Event(), threading.Event()
        original = self.work.gateway.connection.records.get_visit_cases
        def delayed(mrn):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(mrn)
        with patch.object(self.work.gateway.connection.records, "get_visit_cases", side_effect=delayed) as query:
            self.launch("review", lambda: self.work.review.cases("TEST001"))
            self.assertTrue(entered.wait(2))
            self.launch("analysis", lambda: self.work.resources.visits("TEST001"), foreground=True)
            self.wait_subscribers(2)
            release.set()
            for thread in self.threads:
                thread.join(3)
            self.assertEqual(query.call_count, 1)
        self.assertEqual(self.errors, {})
        self.assertEqual(self.results["analysis"].source, "shared")
        receipts = self.work.review.db.sdk_events(task_id="analysis")
        logical = next(value for value in receipts if value["service"] == "clinical_resources")
        self.assertEqual(logical["assessment"]["source"], "shared")
        self.assertEqual(logical["assessment"]["producer_task_id"], "review")
        self.assertNotIn("TEST001", str(logical["assessment"]))
        producer = self.work.review.db.sdk_events(task_id="review")
        self.assertTrue(any(value["assessment"].get("acquisition_id") == logical["assessment"]["acquisition_id"]
                            for value in producer if value["service"] == "records"))
        self.results["analysis"].value[0]["mrn"] = "WRONG"
        self.assertEqual(self.results["review"][0][0].patient_mrn, "TEST001")
        self.assertIsNotNone(self.work.review.db.get("visits", "TEST001"))
        self.assertIsNotNone(self.work.analysis.store.step("TEST001", "visits"))

    def test_soap_owner_cancel_keeps_other_consumer_and_both_sources_without_new_version(self):
        case = self.work.review.cases("TEST001")[0][1]
        entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
        original = self.work.gateway.connection.records.get_soap
        def delayed(value):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(value)
        with patch.object(self.work.gateway.connection.records, "get_soap", side_effect=delayed) as query:
            self.launch("review", lambda: self.work.resources.soap("TEST001", case), cancel=cancel)
            self.assertTrue(entered.wait(2))
            self.launch("analysis", lambda: self.work.resources.soap("TEST001", case), foreground=True)
            self.wait_subscribers(2)
            cancel.set()
            release.set()
            for thread in self.threads:
                thread.join(3)
            self.assertEqual(query.call_count, 1)
        self.assertEqual(self.errors, {})
        self.assertFalse(self.results["analysis"].value["empty"])
        with self.work.store.library.connect() as db:
            sources = {row[0] for row in db.execute("SELECT run_id FROM sources WHERE record_id=?", (case_key(case),))}
            versions = db.execute("SELECT count(*) FROM versions WHERE record_id=?", (case_key(case),)).fetchone()[0]
        self.assertEqual(sources, {"review", "analysis"})
        self.assertEqual(versions, 1)

    def test_all_subscribers_cancel_before_sdk_lock_sends_no_request(self):
        first, second = threading.Event(), threading.Event()
        self.work.gateway.lock.acquire()
        try:
            before = len(BotSyntheticSDK.calls)
            self.launch("one", lambda: self.work.resources.visits("TEST001"), cancel=first)
            self.launch("two", lambda: self.work.resources.visits("TEST001"), cancel=second)
            self.wait_subscribers(2)
            first.set()
            second.set()
            for thread in self.threads:
                thread.join(2)
            self.assertEqual(len(BotSyntheticSDK.calls), before)
            self.assertTrue(all(isinstance(value, Cancelled) for value in self.errors.values()))
            self.assertEqual(len(self.errors), 2)
        finally:
            self.work.gateway.lock.release()

    def test_force_is_separate_and_latest_success_wins(self):
        entered, release, cache = threading.Event(), threading.Event(), {}
        def ordinary():
            entered.set()
            self.assertTrue(release.wait(3))
            return {"version": "old"}
        self.launch("ordinary", lambda: self.unit(fetch=ordinary, cache=cache))
        self.assertTrue(entered.wait(2))
        self.launch("force", lambda: self.unit(fetch=lambda: {"version": "new"}, cache=cache, force=True))
        release.set()
        for thread in self.threads:
            thread.join(3)
        self.assertEqual(self.errors, {})
        self.assertEqual(self.results["ordinary"].value["version"], "old")
        self.assertEqual(self.results["force"].value["version"], "new")
        self.assertEqual(cache["value"]["version"], "new")
        self.assertEqual(self.work.resources.stats["network"], 2)

    def test_login_reset_during_io_cannot_promote_or_serve_old_result(self):
        entered, release, cache = threading.Event(), threading.Event(), {}
        def fetch():
            entered.set()
            self.assertTrue(release.wait(3))
            return "old-session"
        self.launch("old", lambda: self.unit(fetch=fetch, cache=cache))
        self.assertTrue(entered.wait(2))
        self.work.resources.reset()
        release.set()
        self.threads[0].join(3)
        self.assertIsInstance(self.errors["old"], Cancelled)
        self.assertEqual(cache, {})
        self.assertEqual(self.unit(fetch=lambda: "new-session", cache=cache).value, "new-session")

    def test_clear_cache_during_io_prevents_resurrection(self):
        self.work.review.cases("TEST001")
        entered, release = threading.Event(), threading.Event()
        original = self.work.gateway.connection.records.get_visit_cases
        def delayed(mrn):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(mrn)
        with patch.object(self.work.gateway.connection.records, "get_visit_cases", side_effect=delayed):
            self.launch("refresh", lambda: self.work.resources.visits("TEST001", force=True))
            self.assertTrue(entered.wait(2))
            preview = self.work.library_data.preview({"mrns": ["TEST001"], "categories": ["visits"]})
            self.work.library_data.delete({"mrns": ["TEST001"], "categories": ["visits"], "fingerprint": preview["fingerprint"]})
            release.set()
            self.threads[0].join(3)
        self.assertIsInstance(self.errors["refresh"], Cancelled)
        self.assertIsNone(self.work.review.db.get("visits", "TEST001", required=False))
        self.assertIsNone(self.work.analysis.store.step("TEST001", "visits"))

    def test_numeric_full_selectors_and_partial_assessment_do_not_hit_cache(self):
        case = self.work.review.cases("TEST001")[0][1]
        first = replace(case, index=1)
        second = replace(case, index=2)
        self.work.resources.case_numeric("TEST001", first)
        before = sum(call[1] == "numeric" for call in BotSyntheticSDK.calls)
        self.work.resources.case_numeric("TEST001", second)
        self.assertEqual(sum(call[1] == "numeric" for call in BotSyntheticSDK.calls), before + 1)
        step = self.work.analysis.store.step("TEST001", "numeric-case:" + digest(case.identity))
        self.work.analysis.store.save_step("TEST001", step["key"], "numeric", step["payload"], self.work.username,
                                           assessment={"complete": False})
        self.work.resources.case_numeric("TEST001", second)
        self.assertEqual(sum(call[1] == "numeric" for call in BotSyntheticSDK.calls), before + 2)

    def test_local_numeric_read_checks_full_case_patient_and_partial_without_network(self):
        case = replace(self.work.review.cases("TEST001")[0][1], index=1)
        self.work.resources.soap("TEST001", case)
        self.work.resources.case_numeric("TEST001", case)
        request = {"mrn": "TEST001", "record_id": case_key(case)}
        before = len(BotSyntheticSDK.calls)
        self.assertIsNotNone(self.work.review.read_numeric(request))
        self.assertEqual(len(BotSyntheticSDK.calls), before)
        self.work.resources.case_numeric("TEST001", replace(case, index=2, doctor_card="DIFFERENT"))
        before = len(BotSyntheticSDK.calls)
        self.assertIsNone(self.work.review.read_numeric(request))
        self.assertEqual(len(BotSyntheticSDK.calls), before)
        self.work.resources.case_numeric("TEST001", case)
        cached = self.work.review.db.get("numeric", case_key(case))
        self.work.review.db.save("numeric", {**cached, "mrn": "TEST002"})
        self.assertIsNone(self.work.review.read_numeric(request))
        self.work.review.db.save("numeric", {**cached, "assessment": {"complete": False}})
        before = len(BotSyntheticSDK.calls)
        self.assertIsNone(self.work.review.read_numeric(request))
        self.assertEqual(len(BotSyntheticSDK.calls), before)

    def test_sdk_same_identity_wrong_full_case_is_rejected_before_fingerprinting(self):
        case = replace(self.work.review.cases("TEST001")[0][1], index=1)
        wrong = replace(case, index=2, doctor_card="WRONG")
        numeric = self.work.gateway.connection.numeric(wrong)
        with patch.object(self.work.gateway.connection.records, "get_numeric_report", return_value=numeric):
            with self.assertRaises(ValueError):
                self.work.resources.case_numeric("TEST001", case)
        soap = self.work.gateway.connection.soap(wrong)
        with patch.object(self.work.gateway.connection.records, "get_soap", return_value=soap):
            with self.assertRaises(ParseError):
                self.work.resources.soap("TEST001", case)
        self.assertIsNone(self.work.analysis.store.step("TEST001", "numeric-case:" + digest(case.identity)))
        self.assertIsNone(self.work.store.library.get_record(case_key(case), include_versions=False))

    def test_history_partial_numeric_is_readable_but_not_complete_and_execute_repairs(self):
        case = self.work.review.cases("TEST001")[0][1]
        self.work.resources.case_numeric("TEST001", case)
        key = "numeric-case:" + digest(case.identity)
        step = self.work.analysis.store.step("TEST001", key)
        self.work.analysis.store.save_step("TEST001", key, "numeric", step["payload"], self.work.username,
                                           assessment={"complete": False, "issues": [{"code": "SYNTHETIC_PARTIAL"}]})
        history = self.work.review.history
        scope = (None, "TEST001", "case_numeric", case_key(case))
        with patch.object(history, "scope", return_value=scope):
            value = history.read({})
        self.assertIsNotNone(value["data"])
        self.assertFalse(value["complete"])
        before = sum(call[1] == "numeric" for call in BotSyntheticSDK.calls)
        history.execute({"mrn": "TEST001", "resource": "case_numeric", "reference": case_key(case)})
        self.assertEqual(sum(call[1] == "numeric" for call in BotSyntheticSDK.calls), before + 1)

    def test_analysis_consumer_uses_shared_repository_for_stale_or_partial_visits(self):
        self.work.review.cases("TEST001")
        with self.work.store.library.connect() as db:
            db.execute("UPDATE bot_documents SET updated_at='2000-01-01',payload=json_set(payload,'$.updated_at','2000-01-01') WHERE kind='visits'")
            db.execute("UPDATE analysis_steps SET saved_at='2000-01-01' WHERE key='visits'")
        before = sum(call[1] == "index" for call in BotSyntheticSDK.calls)
        state = ScanState(today(), today(), kind="analysis", account_id=self.work.account_id)
        state.data["counts"].update(analysis_cached=0, analysis_fetched=0)
        collector = PatientCollector(self.work.analysis.store, state, {"mrn": "TEST001", "name": "Synthetic"},
            replace(self.work.settings, username="TEST", password="synthetic"), self.work.sdk_factory,
            {"modules": ["retina"], "refresh": False, "force": False}, review=self.work.review)
        collector.examinations([])
        self.assertFalse(collector.issues)
        self.assertGreater(sum(call[1] == "index" for call in BotSyntheticSDK.calls), before)
        self.assertGreater(self.work.resources.stats["network"], 1)

    def test_warm_analysis_adds_no_auth_or_clinical_request_and_soap_uses_selector_guard(self):
        settings = replace(self.work.settings, username="TEST", password="synthetic")
        options = {"modules": ["cataract"], "mrn": "TEST001", "refresh": False, "force": False}
        def collector():
            state = ScanState(today(), today(), kind="analysis", account_id=self.work.account_id)
            state.data["counts"].update(analysis_cached=0, analysis_fetched=0)
            return PatientCollector(self.work.analysis.store,
                state,
                {"mrn": "TEST001", "name": "Synthetic"}, settings, self.work.sdk_factory,
                options, review=self.work.review)
        first = collector()
        first.examinations([], prioritize_soap=True)
        self.assertFalse(first.issues)
        first.stack.close()
        before = len(BotSyntheticSDK.calls)
        with patch.object(self.work.gateway.connection.auth, "check", wraps=self.work.gateway.connection.auth.check) as check:
            warm = collector()
            warm.examinations([], prioritize_soap=True)
            self.assertFalse(warm.issues)
            warm.stack.close()
            self.assertEqual(check.call_count, 0)
        self.assertEqual(len(BotSyntheticSDK.calls), before)
        case = model(VisitCase, self.work.analysis.store.step("TEST001", "visits")["payload"][1])
        changed = replace(case, index=99)
        before = sum(call[1] == "soap" for call in BotSyntheticSDK.calls)
        fresh = collector()
        fresh.latest_eye_soap([clean(changed)])
        fresh.stack.close()
        self.assertEqual(sum(call[1] == "soap" for call in BotSyntheticSDK.calls), before + 1)
        self.assertEqual(self.work.store.library.get_record(case_key(case), include_versions=False)["case_index"], 99)

    def test_wrong_patient_and_error_fanout_do_not_save_or_retry(self):
        entered, release = threading.Event(), threading.Event()
        def fail(_mrn):
            entered.set()
            self.assertTrue(release.wait(3))
            raise RequestError("synthetic final retry failure", code="NETWORK_TIMEOUT")
        with patch.object(self.work.gateway.connection.records, "get_visit_cases", side_effect=fail) as query:
            self.launch("one", lambda: self.work.resources.visits("TEST001"))
            self.assertTrue(entered.wait(2))
            self.launch("two", lambda: self.work.resources.visits("TEST001"))
            self.wait_subscribers(2)
            release.set()
            for thread in self.threads:
                thread.join(3)
            self.assertEqual(query.call_count, 1)
        self.assertEqual(len(self.errors), 2)
        self.assertIsNone(self.work.analysis.store.step("TEST001", "visits"))

    def test_transport_429_backoff_occurs_once_for_shared_acquisition(self):
        entered, release, sleeps = threading.Event(), threading.Event(), []
        class Session(requests.Session):
            calls = 0
            def request(self, method, url, **kwargs):
                self.calls += 1
                response = requests.Response()
                response.status_code = 429 if self.calls == 1 else 200
                response.url, response._content, response._content_consumed = url, b"synthetic", True
                response.headers["Retry-After"] = "2"
                return response
        session = Session()
        transport = ClinicalTransport(policy=RequestPolicy(min_delay_seconds=.1, max_delay_seconds=.1, max_attempts=2),
                                      session=session, sleeper=sleeps.append)
        def fetch():
            entered.set()
            self.assertTrue(release.wait(3))
            return transport.request("GET", "https://synthetic.invalid/read").content
        self.launch("one", lambda: self.unit(fetch=fetch))
        self.assertTrue(entered.wait(2))
        self.launch("two", lambda: self.unit(fetch=fetch))
        self.wait_subscribers(2)
        release.set()
        for thread in self.threads:
            thread.join(3)
        transport.close()
        self.assertEqual(self.errors, {})
        self.assertEqual(session.calls, 2)
        self.assertIn(2, sleeps)
        self.assertEqual(sleeps.count(.1), 2)

    def test_legacy_case_payload_lookup_alias_and_account_do_not_cross(self):
        cases = self.work.review.cases("TEST001")[0]
        foreign = replace(cases[1], lookup_mrn="TEST002")
        with self.assertRaises(ValueError):
            self.work.resources.case_numeric("TEST001", foreign)
        key = self.app.login({"username": "SECOND", "password": "synthetic"})["account"]["id"]
        other = self.app.workspace(key)
        other.resources.visits("TEST001")
        self.assertIsNot(self.work.resources, other.resources)
        self.assertEqual(other.resources.stats["network"], 1)
        self.assertTrue(all(model(VisitCase, value).patient_mrn == "TEST001" for value in clean(cases)))

    def test_wrong_patient_never_enters_either_cache(self):
        with patch.object(self.work.gateway.connection.records, "get_visit_cases", return_value=[
                VisitCase("TEST002", today(), "O", "ONE", "70", "眼科")]):
            with self.assertRaises(ParseError):
                self.work.resources.visits("TEST001")
        self.assertIsNone(self.work.analysis.store.step("TEST001", "visits"))
        self.assertIsNone(self.work.review.db.get("visits", "TEST001", required=False))

    def test_successful_auth_generation_change_is_delivered_without_outer_retry(self):
        cache, calls = {}, []
        def fetch():
            calls.append("read")
            self.work.gateway.auth_generation += 1
            return "recovered"
        self.assertEqual(self.unit(fetch=fetch, cache=cache).value, "recovered")
        self.assertEqual(self.unit(fetch=fetch, cache=cache).source, "cache")
        self.assertEqual(calls, ["read"])

    def test_queued_foreground_and_pacing_scope_are_kept_distinct(self):
        entered, release, order = threading.Event(), threading.Event(), []
        def block():
            entered.set()
            self.assertTrue(release.wait(3))
            return "block"
        self.launch("active", lambda: self.unit(key="active", fetch=block))
        self.assertTrue(entered.wait(2))
        self.launch("background", lambda: self.unit(key="pending", fetch=lambda: order.append("promoted") or "value"))
        self.launch("other", lambda: self.unit(key="other", fetch=lambda: order.append("other") or "other"))
        self.launch("foreground", lambda: self.unit(key="pending", fetch=lambda: self.fail("shared callback executed")), foreground=True)
        self.wait_subscribers(2)
        release.set()
        for thread in self.threads:
            thread.join(3)
        self.assertEqual(self.errors, {})
        self.assertEqual(order[0], "promoted")
        self.assertEqual(self.results["foreground"].source, "shared")
        self.unit(key="pace", fetch=lambda: order.append("normal") or "normal", pacing="normal")
        self.unit(key="pace", fetch=lambda: order.append("fast") or "fast", pacing="report_attachment")
        self.assertEqual(order[-2:], ["normal", "fast"])

    def test_pending_bound_and_newer_force_promotion_fence(self):
        entered, release, cache = threading.Event(), threading.Event(), {}
        def block():
            entered.set()
            self.assertTrue(release.wait(3))
            return "active"
        self.launch("active", lambda: self.unit(key="block", fetch=block))
        self.assertTrue(entered.wait(2))
        self.work.resources.pending_limit = 1
        self.launch("old-force", lambda: self.unit(fetch=lambda: "old", cache=cache, force=True))
        deadline = time.monotonic() + 2
        while not self.work.resources.pending and time.monotonic() < deadline:
            time.sleep(.01)
        with self.assertRaises(BusyError):
            self.unit(key="overflow", fetch=lambda: self.fail("overflow request executed"))
        self.work.resources.pending_limit = 64
        self.launch("new-force", lambda: self.unit(fetch=lambda: "new", cache=cache, force=True), foreground=True)
        release.set()
        for thread in self.threads:
            thread.join(3)
        self.assertEqual(self.errors, {})
        self.assertEqual(cache["value"], "new", "an older queued refresh replaced the newer foreground result")

    def test_attachment_worker_restores_scoped_pacing_and_mirrors_legacy_keys(self):
        from vghks_bot.encoding import _report_attachment_scope

        cases = self.work.review.cases("TEST001")[0]
        ref = {"mrn": "TEST001", "request_no": "REQ", "uid": "IMAGE"}
        entered, release, scopes = threading.Event(), threading.Event(), []
        def download(_reference):
            scopes.append(_report_attachment_scope.get())
            entered.set()
            self.assertTrue(release.wait(3))
            return BinaryAsset(b"%PDF-1.4 synthetic", "application/pdf")
        self.work.gateway.connection.orders.download_pacs_image = download
        self.launch("report", lambda: self.work.resources.branch("TEST001", PacsImageRef, "download_pacs_image",
                    ref, verified_cases=cases, fast_attachment=True))
        self.assertTrue(entered.wait(2))
        self.launch("report-two", lambda: self.work.resources.branch("TEST001", PacsImageRef, "download_pacs_image",
                    ref, verified_cases=cases, fast_attachment=True))
        self.wait_subscribers(2)
        release.set()
        for thread in self.threads:
            thread.join(3)
        self.assertEqual(self.errors, {})
        self.assertEqual(scopes, [True])
        self.work.resources.branch("TEST001", PacsImageRef, "download_pacs_image", ref,
                                   verified_cases=cases, force=True)
        self.assertEqual(scopes, [True, False])
        key = "download_pacs_image:" + digest(ref)
        self.assertEqual(self.work.analysis.store.step("TEST001", key)["payload"],
                         self.work.analysis.store.step("TEST001", "history-asset:" + key)["payload"])
        report_ref = OrderReportRef("TEST001", "ONE", "O", "1", department="70")
        packed = clean(report_ref)
        report_key, reads = "get_order_report:" + digest(packed), []
        def report(reference):
            reads.append(reference)
            return OrderReport(reference, {}, report_text="verified", report_data_status="TEXT_AVAILABLE")
        self.work.gateway.connection.orders.get_order_report = report
        for bad in (clean(OrderReport(replace(report_ref, sequence_no="2"), {},
                                      report_text="wrong same-patient report", report_data_status="TEXT_AVAILABLE")),
                    {"fields": {}, "report_text": "unproven legacy", "report_data_status": "TEXT_AVAILABLE"}):
            for saved_key in (report_key, "history-asset:" + report_key):
                self.work.analysis.store.save_step("TEST001", saved_key, "report", bad,
                                                   self.work.username, assessment={"complete": True})
            acquired = self.work.resources.branch("TEST001", OrderReportRef, "get_order_report", packed,
                                                   verified_cases=cases)
            self.assertEqual(acquired.source, "network")
            self.assertEqual(acquired.value["report_text"], "verified")
        self.assertEqual(len(reads), 2)
        self.assertEqual(self.work.resources.branch("TEST001", OrderReportRef, "get_order_report", packed,
                                                    verified_cases=cases).source, "cache")
        self.assertEqual(len(reads), 2)

    def test_unsent_background_yields_to_different_foreground_resource(self):
        order = []
        original = self.work.gateway.connection.records.get_visit_cases
        def query(mrn):
            order.append(mrn)
            return original(mrn)
        self.work.gateway.lock.acquire()
        try:
            with patch.object(self.work.gateway.connection.records, "get_visit_cases", side_effect=query):
                self.launch("background", lambda: self.work.resources.visits("TEST001"))
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if any(value.started for value in self.work.resources.flights.values()):
                        break
                    time.sleep(.01)
                self.launch("foreground", lambda: self.work.resources.visits("TEST002"), foreground=True)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    with self.work.gateway.condition:
                        if self.work.gateway.foreground_waiters:
                            break
                    time.sleep(.01)
                self.assertGreater(self.work.gateway.foreground_waiters, 0)
                self.work.gateway.lock.release()
                for thread in self.threads:
                    thread.join(3)
            self.assertEqual(self.errors, {})
            self.assertEqual(order, ["TEST002", "TEST001"])
        finally:
            # The lock is already released on the successful path.
            if self.work.gateway.lock.acquire(blocking=False):
                self.work.gateway.lock.release()
            else:
                self.work.gateway.lock.release()

    def test_network_slot_admission_rechecks_pending_foreground_before_sending(self):
        gate = self.work.gateway.gate
        gate.maximum = 1
        entered, order = threading.Event(), []
        original_wait = gate.condition.wait
        original_query = self.work.gateway.connection.records.get_visit_cases
        def waiting(timeout=None):
            entered.set()
            return original_wait(timeout)
        def query(mrn):
            order.append(mrn)
            return original_query(mrn)
        holder = gate.slot()
        holder.__enter__()
        released = False
        try:
            with patch.object(gate.condition, "wait", side_effect=waiting), \
                    patch.object(self.work.gateway.connection.records, "get_visit_cases", side_effect=query):
                self.launch("background", lambda: self.work.resources.visits("TEST001"))
                self.assertTrue(entered.wait(2))
                # Keep the gate condition locked until capacity becomes free,
                # forcing the waiter to leave its while loop directly on wake.
                with gate.condition:
                    self.launch("foreground", lambda: self.work.resources.visits("TEST002"), foreground=True)
                    with self.work.resources.condition:
                        self.assertTrue(self.work.resources.condition.wait_for(
                            lambda: any(value.priority for value in self.work.resources.pending), timeout=2))
                    holder.__exit__(None, None, None)
                    released = True
                for thread in self.threads:
                    thread.join(3)
            self.assertEqual(self.errors, {})
            self.assertEqual(order, ["TEST002", "TEST001"])
        finally:
            if not released:
                holder.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
