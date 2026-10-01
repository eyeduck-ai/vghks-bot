import http.client
import json
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from vghks_sdk import LoginRejectedError, RequestError

from vghks_bot.bot import BotApplication
from vghks_bot.bot_gateway import AccountGateway, NetworkGate
from vghks_bot.bot_server import BotServer
from vghks_bot.bounded_search import search
from vghks_bot.selftest_bot import BotSyntheticSDK, wait_task
from vghks_bot.settings import Settings, today


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        BotSyntheticSDK.calls = []
        BotSyntheticSDK.fail_case = ""
        BotSyntheticSDK.on_soap = None
        BotSyntheticSDK.revision = "original"
        self.app = BotApplication(Settings(), self.path, BotSyntheticSDK)
        self.key = self.app.login({"username": "TEST", "password": "synthetic-private"})["account"]["id"]
        self.work = self.app.workspace(self.key)

    def tearDown(self):
        BotSyntheticSDK.on_soap = None
        self.app.close()
        self.temp.cleanup()

    def group(self, mrns="TEST001"):
        run = self.work.review.start({"kind": "resolve", "identifiers": mrns})["task_id"]
        wait_task(self.work, run)
        return self.work.review.save_set({"mrns": mrns})

    def review(self, group=None, **options):
        group = group or self.group()
        run = self.work.review.start({"set_id": group["id"], **options})["task_id"]
        return wait_task(self.work, run)

    def test_accounts_isolate_every_clinical_store_and_encrypt_only_after_success(self):
        first = self.review()
        other_id = self.app.login({"username": "SECOND", "password": "other"})["account"]["id"]
        other = self.app.workspace(other_id)
        self.assertNotEqual(self.work.store.directory, other.store.directory)
        self.assertEqual(other.library_search({})["total"], 0)
        self.assertEqual(other.review.db.all("set"), [])
        self.assertEqual(other.analysis.store.documents("analysis_cohorts"), [])
        with self.assertRaises(ValueError):
            other.review.task(first["id"])
        self.work.patient_tags_update({"mrns": "TEST001", "new_tag": "測試手動"})
        self.assertEqual(other.tag_patients({})["total"], 0)
        self.assertNotEqual(self.work.analysis.google.path, other.analysis.google.path)
        self.assertIs(self.work.analysis.sheet_lock, other.analysis.sheet_lock)
        public = json.dumps([self.app.bootstrap(), self.work.bootstrap()])
        self.assertNotIn("synthetic-private", public)
        self.assertNotIn(b"synthetic-private", (self.path / "accounts.sqlite3").read_bytes())
        with self.assertRaises(ValueError):
            self.app.login({"username": "BADLOGIN", "password": "invalid"})
        self.assertEqual(len(self.app.registry.accounts()), 2)

    def test_saved_credentials_update_forget_offline_restart_and_no_legacy_import(self):
        task = self.review()
        self.app.login({"id": self.key, "password": "updated"})
        self.assertEqual(self.app.registry.password(self.key), "updated")
        self.app.close()
        self.app = BotApplication(Settings(), self.path, BotSyntheticSDK)
        self.app.offline(self.key)
        self.work = self.app.workspace(self.key)
        before = list(BotSyntheticSDK.calls)
        self.assertEqual(self.work.review.results({"id": task["id"]})["total"], 1)
        self.assertEqual(before, BotSyntheticSDK.calls)
        self.app.login({"id": self.key})
        self.app.forget(self.key)
        self.assertEqual(self.app.registry.password(self.key), "")
        self.app.logout(self.key)
        with self.assertRaises(ValueError):
            self.app.workspace(self.key)
        with self.assertRaises(ValueError):
            self.app.login({"id": self.key})

    def test_switching_accounts_keeps_independent_sessions_and_uses_memory_password(self):
        first_session = self.work.gateway.session_id
        other_id = self.app.login({"username": "SECOND", "password": "other", "remember": False})["account"]["id"]
        other = self.app.workspace(other_id)
        second_session = other.gateway.session_id
        self.assertNotEqual(first_session, second_session)
        self.assertEqual(self.app.registry.password(other_id), "")
        before = sum(call[0] == "TEST" and call[1] == "login" for call in BotSyntheticSDK.calls)
        self.assertTrue(self.app.activate(self.key)["online"])
        self.assertEqual(self.work.gateway.session_id, first_session)
        self.assertEqual(before, sum(call[0] == "TEST" and call[1] == "login" for call in BotSyntheticSDK.calls))
        other.gateway.close()
        self.assertTrue(self.app.activate(other_id)["online"])
        self.assertNotEqual(other.gateway.session_id, second_session)
        self.assertEqual(self.work.gateway.session_id, first_session)
        self.assertTrue(self.work.gateway.online)
        self.assertEqual(self.app.registry.password(other_id), "")
        self.app.logout(other_id)
        self.assertEqual(self.app.activate(other_id)["status"], "needs_password")
        self.assertTrue(self.work.gateway.online)

    def test_activate_reports_connection_failure_without_affecting_other_account(self):
        other_id = self.app.login({"username": "SECOND", "password": "other", "remember": False})["account"]["id"]
        other = self.app.workspace(other_id)
        other.gateway.close()
        first_session = self.work.gateway.session_id
        with patch.object(other.gateway, "login", side_effect=RequestError("temporary outage", code="NETWORK_TIMEOUT")):
            result = self.app.activate(other_id)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error_code"], "NETWORK_TIMEOUT")
        self.assertNotIn("temporary outage", json.dumps(result))
        self.assertEqual(self.work.gateway.session_id, first_session)
        self.assertTrue(self.app.activate(other_id)["online"])

    def test_activate_requests_password_only_after_explicit_rejection(self):
        other_id = self.app.login({"username": "SECOND", "password": "other"})["account"]["id"]
        other = self.app.workspace(other_id)
        other.gateway.close()
        with patch.object(other.gateway, "login", side_effect=LoginRejectedError("rejected", code="AUTH_LOGIN_REJECTED")):
            result = self.app.activate(other_id)
        self.assertEqual(result["status"], "needs_password")
        self.assertTrue(self.work.gateway.online)

    def test_latest_same_day_aliases_cutoff_and_first_visit_vs_error(self):
        result = self.review(self.group("TEST001,NEW000,FAIL"))
        patients = {p["mrn"]: p for p in result["items"]}
        self.assertEqual(patients["TEST001"]["status"], "ready")
        self.assertTrue(patients["TEST001"]["fallback"])
        self.assertEqual({r["case_no"] for r in patients["TEST001"]["records"]}, {"ONE", "TWO"})
        self.assertEqual(patients["NEW000"]["status"], "no_visit")
        self.assertEqual(patients["FAIL"]["status"], "error")
        older = self.review(cutoff=(today()-timedelta(days=2)).isoformat())
        self.assertEqual([r["case_no"] for r in older["items"][0]["records"]], ["OLD"])
        with self.assertRaises(ValueError):
            self.review(cutoff=(today()+timedelta(days=1)).isoformat())

    def test_verified_national_id_without_encounters_and_mismatched_profile(self):
        run = self.work.review.start({"kind": "resolve", "identifier_kind": "national_id", "identifiers": "A123456789"})["task_id"]
        self.assertEqual(wait_task(self.work, run)["items"][0]["mrn"], "NEW000")
        self.assertFalse(any(c[1] == "index" for c in BotSyntheticSDK.calls))
        self.assertEqual(len(self.work.review.save_set({"mrns": "NEW000"})["members"]), 1)
        run = self.work.review.start({"kind": "resolve", "identifiers": "BAD"})["task_id"]
        self.assertEqual(wait_task(self.work, run)["status"], "partial")
        with self.assertRaises(ValueError):
            self.work.review.save_set({"mrns": "BAD"})

    def test_future_lists_shared_selection_and_registration_mode_without_source(self):
        day = (today()+timedelta(days=2)).isoformat()
        self.work.browse({"account_ids": [self.key], "ranges": {self.key: {"start": day}}})
        self.assertTrue(self.work.idle.wait(10))
        rows = self.work.patient_list({"account_id": self.key, "start": day})["days"][0]["rows"]
        self.assertEqual(len(rows), 4)
        self.assertEqual([row["sequence_no"] for row in rows], ["001", "002", "003", "004"])
        refs = [{"day": day, "id": r["id"]} for r in rows]
        group = self.work.review.save_set({"registrations": refs + refs[:1]})
        self.assertEqual(len(group["members"]), 4)
        self.assertEqual(len(group["members"][0]["registrations"]), 1)
        self.assertTrue(all(p["status"] == "future" for p in self.review(group, mode="registration")["items"]))
        self.assertEqual(self.review(mode="registration")["items"][0]["status"], "no_source")
        self.assertTrue(self.review(group)["items"][0]["records"])

    def test_task_search_snapshot_reclassification_and_manual_tags_are_local(self):
        task = self.review()
        source = task["items"][0]["records"][0]
        self.work.store.library.save_record({**source, "id": "unrelated", "soap": "UNRELATED"}, "TEST", "other")
        self.work.store.library.save_record({**source, "soap": "NEW-VERSION"}, "TEST", "new")
        self.assertEqual(self.work.review.results({"id": task["id"], "q": "UNRELATED"})["total"], 0)
        self.assertEqual(self.work.review.results({"id": task["id"], "q": "NEW-VERSION"})["total"], 0)
        self.assertEqual(self.work.review.results({"id": task["id"], "q": "original", "search_mode": "regex"})["total"], 1)
        before = list(BotSyntheticSDK.calls)
        self.work.save_settings({"categories": [{"id": "custom", "name": "自訂", "keywords": ["original"]}]})
        self.work.review.reclassify(task["id"])
        self.assertEqual(self.work.review.results({"id": task["id"], "tag": "custom"})["total"], 1)
        self.work.patient_tags_update({"mrns": "TEST001", "new_tag": "手動群組"})
        self.assertTrue(self.work.review.results({"id": task["id"]})["patients"][0]["manual_tags"])
        self.assertEqual(before, BotSyntheticSDK.calls)

    def test_cache_refresh_force_and_lazy_extensions(self):
        group = self.group()
        task = self.review(group)
        self.assertFalse(any(c[1] in {"numeric", "registrations"} for c in BotSyntheticSDK.calls))
        BotSyntheticSDK.calls.clear()
        self.review(group)
        self.assertFalse(any(c[1] == "soap" and c[-1] in {"ONE", "TWO"} for c in BotSyntheticSDK.calls))
        self.assertFalse(any(c[1] == "index" for c in BotSyntheticSDK.calls))
        self.review(group, refresh=True)
        self.assertTrue(any(c[1] == "index" for c in BotSyntheticSDK.calls))
        BotSyntheticSDK.calls.clear()
        self.review(group, force=True)
        self.assertEqual(sum(c[1] == "soap" for c in BotSyntheticSDK.calls), 3)
        key = task["items"][0]["records"][0]["id"]
        for kind in ("numeric", "registrations"):
            run = self.work.review.start({"kind": kind, "mrn": "TEST001", "record_id": key})["task_id"]
            self.assertEqual(wait_task(self.work, run)["status"], "completed")
            before = list(BotSyntheticSDK.calls)
            run = self.work.review.start({"kind": kind, "mrn": "TEST001", "record_id": key})["task_id"]
            self.assertEqual(wait_task(self.work, run)["status"], "completed")
            self.assertEqual(BotSyntheticSDK.calls, before)

    def test_partial_resume_freezes_successful_versions_and_deletion_tombstones(self):
        BotSyntheticSDK.fail_case = "TWO"
        task = self.review()
        self.assertEqual(task["status"], "partial")
        record = task["items"][0]["records"][0]
        self.work.store.library.save_record({**record, "soap": "changed later"}, "TEST", "other")
        BotSyntheticSDK.fail_case = ""
        run = self.work.review.start({"resume": task["id"]})["task_id"]
        completed = wait_task(self.work, run)
        self.assertEqual(completed["items"][0]["records"][0]["soap"], record["soap"])
        self.assertTrue(self.work.idle.wait(10))
        self.work.delete_records({"ids": [r["id"] for r in completed["items"][0]["records"]]})
        self.work.review.start({"resume": task["id"]})
        completed = wait_task(self.work, run)
        self.assertEqual(completed["items"][0]["status"], "deleted")
        self.assertEqual(self.work.library_search({})["total"], 0)

    def test_cancel_checkpoint_and_restart_is_explicit_resume(self):
        group = self.group()
        state_seen = threading.Event()
        def pause(case):
            if case.case_no == "ONE":
                for state in self.work.states.values():
                    state.cancel.set()
                state_seen.set()
        BotSyntheticSDK.on_soap = pause
        task = self.review(group)
        self.assertTrue(state_seen.is_set())
        self.assertEqual(task["status"], "paused")
        self.assertEqual(len(task["items"][0]["records"]), 1)
        self.app.close()
        BotSyntheticSDK.on_soap = None
        before = len(BotSyntheticSDK.calls)
        self.app = BotApplication(Settings(), self.path, BotSyntheticSDK)
        self.app.login({"id": self.key})
        self.work = self.app.workspace(self.key)
        self.assertEqual(self.work.review.task(task["id"])["status"], "paused")
        self.assertEqual(len(BotSyntheticSDK.calls), before+1)
        self.work.review.start({"resume": task["id"]})
        self.assertEqual(wait_task(self.work, task["id"])["status"], "completed")

    def test_unknown_department_does_not_become_first_visit(self):
        group = self.group()
        with self.assertRaises(ValueError):
            self.work.review.preferences({"review_options": {"department_keyword": ""}})
        original = BotSyntheticSDK.visits
        from dataclasses import replace
        with patch.object(BotSyntheticSDK, "visits", lambda sdk, mrn: [replace(c, section_name="", section_code="") for c in original(sdk, mrn)]):
            # Construct a new session so its bound method uses the patched method.
            self.app.login({"id": self.key})
            self.assertEqual(self.review(group, refresh=True)["items"][0]["status"], "unknown")

    def test_regex_invalid_and_timeout_keep_existing_results(self):
        self.assertEqual(search("原文", ["中文原文"])[0], [[2, 4]])
        with self.assertRaises(ValueError):
            search("[", ["anything"])
        start = time.monotonic()
        with self.assertRaises(ValueError):
            search("(a+)+$", ["a"*60000+"!"], timeout=.5)
        self.assertLess(time.monotonic()-start, 3)

    def test_deletion_recovers_after_interruption_without_task_copy_resurrection(self):
        task = self.review()
        ids = [r["id"] for r in task["items"][0]["records"]]
        self.work.review.db.purge_records(ids)  # Interrupt before journal compaction.
        self.app.close()
        self.app = BotApplication(Settings(), self.path, BotSyntheticSDK)
        self.app.offline(self.key)
        self.work = self.app.workspace(self.key)
        self.assertEqual(self.work.library_search({})["total"], 0)
        self.assertFalse(self.work.review.task(task["id"])["items"][0]["records"])

    def test_storage_failure_stops_review_before_reading_more_soap(self):
        from vghks_bot.storage import StorageError
        group = self.group()
        BotSyntheticSDK.calls.clear()
        with patch.object(self.work.store.library, "save_record", side_effect=StorageError("full")):
            task = self.review(group)
            self.assertEqual(task["status"], "failed")
        self.assertEqual([c[-1] for c in BotSyntheticSDK.calls if c[1] == "soap"], ["EMPTY", "ONE"])

    def test_permission_error_does_not_label_patient_first_visit(self):
        from vghks_sdk.core.errors import SDKError
        group = self.group()
        with patch.object(self.work.gateway.connection.records, "get_visit_cases", side_effect=SDKError("denied", http_status=403)):
            task = self.review(group)
        self.assertEqual(task["status"], "partial")
        self.assertEqual(task["items"][0]["status"], "forbidden")

    def test_resolve_individual_retry_only_rechecks_requested_input(self):
        run = self.work.review.start({"kind": "resolve", "identifiers": "TEST001,BAD"})["task_id"]
        self.assertEqual(wait_task(self.work, run)["status"], "partial")
        BotSyntheticSDK.calls.clear()
        self.work.review.start({"resume": run, "retry_only": ["BAD"]})
        wait_task(self.work, run)
        self.assertEqual([c[2] for c in BotSyntheticSDK.calls if c[1] == "profile"], ["BAD"])

    def test_inline_plan_label_arrange_builds_one_surgery_with_both_sources(self):
        group = self.group()
        self.review(group)
        cohort = self.work.review.cohort({"set_id": group["id"]})
        result = self.work.analysis.surgery_candidates({"cohort_id": cohort["id"]})["candidates"]
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]["sources"]), 2)
        self.assertEqual(result[0]["fields"]["procedure"], "Phaco-IOL")
        self.assertTrue(result[0]["sources"][0]["excerpt"].startswith("# Arrange"))

    def test_same_mrn_remains_independent_after_both_accounts_fetch(self):
        first = self.review()
        first_workspace = self.work
        other = self.app.login({"username": "SECOND", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(other)
        second = self.review()
        self.assertIn("TEST original", first["items"][0]["records"][0]["soap"])
        self.assertIn("SECOND original", second["items"][0]["records"][0]["soap"])
        self.assertTrue(first_workspace.idle.wait(10))
        first_workspace.delete_records({"ids": [r["id"] for r in first["items"][0]["records"]]})
        self.assertEqual(self.work.library_search({})["total"], 2)

    def test_http_scope_csrf_and_cross_account_assets(self):
        other = self.app.login({"username": "OTHER", "password": "synthetic"})["account"]["id"]
        with BotServer(0, self.app) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            def request(path, values=None, csrf=True, cookie=True):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
                headers = {"Content-Type": "application/json"}
                if cookie:
                    headers["Cookie"] = "opd_session="+self.app.session_token
                if csrf:
                    headers["X-CSRF-Token"] = self.app.csrf_token
                connection.request("GET" if values is None else "POST", path, None if values is None else json.dumps(values), headers)
                response = connection.getresponse()
                value = response.status, response.read()
                connection.close()
                return value
            try:
                self.assertEqual(request("/api/history")[0], 404)
                self.assertEqual(request(f"/api/accounts/{self.key}/workbench", cookie=False)[0], 401)
                self.assertEqual(request(f"/api/accounts/{self.key}/draft/save", {}, csrf=False)[0], 403)
                self.assertEqual(request("/api/accounts/activate", {"id": other}, cookie=False)[0], 401)
                self.assertEqual(request("/api/accounts/activate", {"id": other}, csrf=False)[0], 403)
                self.assertEqual(json.loads(request("/api/accounts/activate", {"id": other})[1])["status"], "ready")
                self.assertEqual(request(f"/api/accounts/{self.key}/draft/save", {"test": "first"})[0], 200)
                self.assertNotIn(b"first", request(f"/api/accounts/{other}/workbench")[1])
                self.assertEqual(request(f"/api/accounts/{self.key}/accounts/save", {"accounts": [{"id": other, "username": "OTHER"}]})[0], 400)
                self.assertEqual(request(f"/api/accounts/{other}/analysis/asset?id="+"a"*64)[0], 404)
                for asset in ("/tool-workspace.js", "/monitor-ui.js", "/approvals.js", "/choices.js", "/choices.css"):
                    self.assertEqual(request(asset, cookie=False)[0], 200)
                own = f"/api/accounts/{self.key}"
                foreign = f"/api/accounts/{other}"
                group = self.group()
                self.assertEqual(request(own+"/sets/cohort", {"set_id": group["id"]})[0], 200)
                self.assertEqual(request(foreign+"/sets/cohort", {"set_id": group["id"]})[0], 400)
                state = {"module": "retina", "set_id": group["id"]}
                self.assertEqual(request(own+"/tools/state/save", state, csrf=False)[0], 403)
                self.assertEqual(request(own+"/tools/state/save", state)[0], 200)
                self.assertEqual(request(foreign+"/tools/state/save", state)[0], 400)
                self.assertNotIn(group["id"].encode(), request(foreign+"/tools/state", {})[1])
                for path in ("cases", "sync-history"):
                    self.assertEqual(request(own+"/approvals/"+path, {})[0], 200)
                self.assertEqual(request(own+"/reviews/diagnostics", {"mrn": "TEST001"})[0], 200)
                self.app.logout(self.key)
                self.assertEqual(request(f"/api/accounts/{self.key}/workbench")[0], 400)
            finally:
                server.shutdown()
                thread.join(2)


class GatewayTests(unittest.TestCase):
    def test_safe_read_reconnects_once_after_network_failure(self):
        logins_before = sum(call[1] == "login" for call in BotSyntheticSDK.calls)
        gateway = AccountGateway(BotSyntheticSDK, NetworkGate())
        gateway.login(Settings(username="TEST", password="fake"))
        first_session = gateway.session_id
        try:
            with patch.object(gateway.connection.opd, "get_doctor_patients",
                              side_effect=RequestError("temporary outage", code="NETWORK_TIMEOUT")):
                rows = gateway.invoke("opd", "get_doctor_patients", "TEST", today())
            self.assertEqual(len(rows), 4)
            self.assertEqual(gateway.recovery_count, 1)
            self.assertNotEqual(gateway.session_id, first_session)
            self.assertTrue(gateway.online)
            self.assertEqual(sum(call[1] == "login" for call in BotSyntheticSDK.calls) - logins_before, 2)
        finally:
            gateway.close()

    def test_network_failure_does_not_repeat_non_read_operation(self):
        gateway = AccountGateway(BotSyntheticSDK, NetworkGate())
        gateway.login(Settings(username="TEST", password="fake"))
        try:
            with patch.object(gateway.connection.earnings, "open_performance",
                              side_effect=RequestError("temporary outage", code="NETWORK_TIMEOUT")) as operation:
                with self.assertRaises(RequestError):
                    gateway.invoke("earnings", "open_performance", object())
            self.assertEqual(operation.call_count, 1)
            self.assertEqual(gateway.recovery_count, 0)
            self.assertFalse(gateway.online)
        finally:
            gateway.close()

    def test_http_error_does_not_disconnect_account(self):
        gateway = AccountGateway(BotSyntheticSDK, NetworkGate())
        gateway.login(Settings(username="TEST", password="fake"))
        try:
            with patch.object(gateway.connection.opd, "get_doctor_patients",
                              side_effect=RequestError("hospital HTTP error", status_code=404)) as operation:
                with self.assertRaises(RequestError):
                    gateway.invoke("opd", "get_doctor_patients", "TEST", today())
            self.assertEqual(operation.call_count, 1)
            self.assertEqual(gateway.recovery_count, 0)
            self.assertTrue(gateway.online)
        finally:
            gateway.close()

    def test_foreground_runs_between_background_steps_not_after_whole_batch(self):
        gateway = AccountGateway(BotSyntheticSDK, NetworkGate())
        gateway.login(Settings(username="TEST", password="fake"))
        entered, release = threading.Event(), threading.Event()
        order = []
        def step(name):
            order.append(name)
            if name == "one":
                entered.set()
                release.wait(3)
        gateway.connection.opd.step = step
        def background():
            gateway.invoke("opd", "step", "one")
            gateway.invoke("opd", "step", "two")
        def foreground():
            with gateway.foreground():
                gateway.invoke("opd", "step", "foreground")
        a, b = threading.Thread(target=background), threading.Thread(target=foreground)
        try:
            a.start()
            self.assertTrue(entered.wait(2))
            b.start()
            with gateway.condition:
                self.assertTrue(gateway.condition.wait_for(lambda: gateway.foreground_waiters > 0, timeout=2))
            release.set()
            a.join(3)
            b.join(3)
            self.assertEqual(order, ["one", "foreground", "two"])
        finally:
            release.set()
            a.join(3)
            if b.ident:
                b.join(3)
            gateway.close()

    def test_foreground_patient_context_is_restored_at_next_safe_step(self):
        gateway = AccountGateway(BotSyntheticSDK, NetworkGate())
        gateway.login(Settings(username="TEST", password="fake"))
        try:
            cases = gateway.invoke("records", "get_visit_cases", "TEST001")
            with gateway.foreground():
                gateway.invoke("patients", "get_registration_history", "TEST002")
            result = gateway.invoke("records", "get_soap", cases[1])
            self.assertEqual(result.case.mrn, "TEST001")
        finally:
            gateway.close()

    def test_global_account_concurrency_is_bounded(self):
        gate = NetworkGate(2)
        active = []
        ready, release = threading.Event(), threading.Event()
        def worker():
            with gate.slot():
                active.append(gate.active)
                if len(active) == 2:
                    ready.set()
                release.wait(2)
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        self.assertTrue(ready.wait(2))
        self.assertEqual(len(active), 2)
        release.set()
        for thread in threads:
            thread.join(2)
        self.assertEqual(len(active), 4)
        self.assertLessEqual(max(active), 2)
        with self.assertRaises(ValueError):
            gate.set_limit(5)
