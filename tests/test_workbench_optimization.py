"""Performance bounds, concurrent API isolation and scoped discovery workflows."""
import http.client
import json
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic
from unittest.mock import patch

from vghks_bot.activity import search
from vghks_bot.bot import BotApplication
from vghks_bot.bot_gateway import AccountGateway, NetworkGate
from vghks_bot.bot_server import BotServer
from vghks_bot.jobs import BusyError
from vghks_bot.request_gate import RequestGate
from vghks_bot.scanner import Cancelled
from vghks_bot.selftest_bot import BotSyntheticSDK
from vghks_bot.settings import Settings
from vghks_bot.storage import StorageError
from vghks_bot.workbench import snapshot, status


class WorkbenchOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = BotApplication(Settings(), Path(self.temp.name), BotSyntheticSDK)
        self.key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.key)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_concurrent_tool_sources_preserve_other_tools_and_accounts(self):
        review = self.work.review
        group = review.db.save("set", {"id": "SYNTHETIC-SET", "members": [{"mrn": "TEST001"}]})
        review.save_tool_state({"module": "cataract", "set_id": group["id"]})
        first_read, release, second_done = threading.Event(), threading.Event(), threading.Event()
        original_get = review.db.get

        def read(kind, key, **options):
            value = original_get(kind, key, **options)
            if kind == "draft" and key == "tools" and not first_read.is_set():
                first_read.set()
                if not release.wait(3):
                    raise AssertionError("tool source test did not release the first save")
            return value

        def second_save():
            try:
                return review.save_tool_state({"module": "retina", "set_id": group["id"]})
            finally:
                second_done.set()

        with patch.object(review.db, "get", side_effect=read), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(review.save_tool_state, {"module": "review", "set_id": group["id"]})
            try:
                self.assertTrue(first_read.wait(2))
                second = pool.submit(second_save)
                second_done.wait(.2)
            finally:
                release.set()
            first.result(timeout=2)
            second.result(timeout=2)
        self.assertEqual(review.tool_state()["modules"], {
            kind: {"set_id": group["id"]} for kind in ("review", "retina", "cataract")})
        review.save_tool_state({"module": "review", "set_id": ""})
        self.assertEqual(review.tool_state()["modules"]["retina"]["set_id"], group["id"])
        self.assertEqual(review.tool_state()["modules"]["review"]["set_id"], "")
        other = self.app.login({"username": "SECOND", "password": "synthetic"})["account"]["id"]
        self.assertEqual(self.app.workspace(other).review.tool_state(), {"modules": {}})
        self.assertEqual(review.db.get("set", group["id"])["members"], group["members"])

    def test_failed_tool_source_write_rolls_back_the_entire_update(self):
        review = self.work.review
        before = review.save_tool_state({"module": "cataract", "set_id": ""})
        original_save = review.db.save

        def fail(kind, value):
            original_save(kind, value)
            raise StorageError("synthetic write interruption")

        with patch.object(review.db, "save", side_effect=fail), self.assertRaises(StorageError):
            review.save_tool_state({"module": "review", "set_id": ""})
        self.assertEqual(review.tool_state(), before)

    def test_invalid_tool_source_does_not_modify_saved_selection(self):
        before = self.work.review.tool_state()
        for values in ({"module": []}, {"module": "review", "set_id": []},
                       {"module": "retina", "set_id": "missing"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.work.review.save_tool_state(values)
        self.assertEqual(self.work.review.tool_state(), before)

    def test_stop_does_not_invert_the_task_start_lock_order(self):
        entered = threading.Event()
        original_stop = self.work.stop

        def stop(key):
            entered.set()
            return original_stop(key)

        with patch.object(self.work, "stop", side_effect=stop), ThreadPoolExecutor(max_workers=1) as pool:
            with self.work.lock:
                stopping = pool.submit(self.work.review.stop, "SYNTHETIC-QUEUE")
                self.assertTrue(entered.wait(2))
                # Task launch owns the application lock before acquiring this
                # lock. Stop must release it before waiting for the application.
                can_launch = self.work.review.lock.acquire(timeout=.3)
                if can_launch:
                    self.work.review.lock.release()
            self.assertEqual(stopping.result(timeout=2), {"ok": True})
        self.assertTrue(can_launch, "stop and task launch can deadlock in opposite lock order")

    def task(self, key, **values):
        return self.work.review.db.save("task", {"id": key, "kind": "review", "status": "completed",
            "created_at": "2026-10-04T08:00:00", "members": [{"mrn": "TEST001", "name": "合成病人"}], **values})

    def test_65_notes_share_one_connection_and_keep_task_scope(self):
        self.task("old")
        self.work.review.save_note({"task_id": "old", "mrn": "TEST001", "text": "earlier"})
        members = [{"mrn": f"{i:08}", "name": "synthetic"} for i in range(65)]
        self.task("current", members=members)
        self.work.review.save_note({"task_id": "current", "mrn": "00000000", "text": "current"})
        with patch("vghks_bot.library.sqlite3.connect", wraps=sqlite3.connect) as connect:
            result = self.work.review.read_notes({"task_id": "current"})
            self.assertEqual(connect.call_count, 1)
        self.assertEqual(len(result["patients"]), 65)
        self.assertEqual(result["patients"][0]["text"], "current")
        self.assertEqual(self.work.review.read_notes({"task_id": "old"})["patients"][0]["text"], "earlier")

    def test_browser_refresh_and_floating_progress_use_lightweight_status(self):
        subprocess.run(["node", "tests/polling_ui.js"], cwd=Path(__file__).resolve().parents[1],
                       check=True, capture_output=True, text=True, encoding="utf-8")

    def test_status_never_reads_full_history_or_clinical_cases_and_is_bounded(self):
        members = [{"mrn": f"{i:08}", "name": "synthetic"} for i in range(65)]
        for i in range(250):
            self.task(f"task{i:04}", members=members, clinical_secret="DO_NOT_POLL" * 100)
        self.task("watch", members=members, status="paused")
        self.task("active", members=members, status="running")
        with patch.object(self.work, "history", side_effect=AssertionError("full history")), \
                patch.object(self.work.approvals.tracker, "overview", side_effect=AssertionError("clinical case overview")), \
                patch("vghks_bot.library.sqlite3.connect", wraps=sqlite3.connect) as connect:
            result = status(self.work, ["watch"])
            self.assertEqual(connect.call_count, 1)
        self.assertLessEqual(len(result["tasks"]), 22)
        self.assertEqual(result["active_count"], 1)
        self.assertTrue({"watch", "active"}.issubset({r["id"] for r in result["tasks"]}))
        self.assertNotIn("DO_NOT_POLL", json.dumps(result))
        self.assertNotIn('"members"', json.dumps(result))
        self.assertLess(len(json.dumps(result).encode()), 20000)
        # Synthetic running documents are not live queue jobs.
        self.work.review.db.save("task", {**self.work.review.db.get("task", "active"), "status": "completed"})

    def test_revision_observes_same_timestamp_updates_deletion_and_note_clear(self):
        task = self.task("revision")
        before = self.work.review.db.revisions()
        self.task("revision", name="updated")
        self.assertGreater(self.work.review.db.revisions()["task"], before["task"])
        self.work.review.save_note({"task_id": task["id"], "mrn": "TEST001", "text": "saved"})
        revision = self.work.review.db.revisions()["review_note"]
        self.work.review.clear_notes({"task_id": task["id"]})
        self.assertGreater(self.work.review.db.revisions()["review_note"], revision)

    def test_activity_pages_whole_families_and_filters_child_status_notes_and_patient(self):
        for i in range(45):
            self.task(f"parent{i:02}", name=f"合成檢閱{i}")
        self.task("child", kind="numeric", members=[], mrn="TEST001", review_task_id="parent00", status="running")
        self.work.review.save_note({"task_id": "parent00", "mrn": "TEST001", "text": "saved"})
        result = search(self.work, {"system": "門診系統", "limit": 40})
        self.assertEqual(result["total"], 45)
        self.assertEqual(sum(t["kind"] == "review" for t in result["tasks"]), 40)
        self.assertEqual(sum(t["kind"] == "review" for t in search(self.work, {"system": "門診系統", "offset": 40})["tasks"]), 5)
        for values in ({"status": "active"}, {"notes": "1"}, {"q": "child"}):
            result = search(self.work, {"system": "門診系統", **values})
            self.assertEqual(result["total"], 1)
            self.assertEqual({r["id"] for r in result["tasks"]}, {"parent00", "child"})
        self.assertEqual(search(self.work, {"q": "合成病人", "system": "門診系統"})["total"], 45)
        self.assertEqual(search(self.work, {"start": "2026-10-05", "system": "門診系統"})["total"], 0)
        with self.assertRaises(ValueError):
            search(self.work, {"start": "2026-10-05", "end": "2026-10-04"})

    def test_notes_search_keeps_every_review_and_excludes_other_accounts(self):
        for key in ("first", "second"):
            self.task(key, name=key)
            self.work.review.save_note({"task_id": key, "mrn": "TEST001", "text": key})
        other = self.app.login({"username": "OTHER", "password": "synthetic"})["account"]["id"]
        db = self.app.workspace(other).review.db
        db.save("task", {"id": "other", "kind": "review", "status": "completed", "members": [{"mrn": "TEST001"}]})
        db.save("review_note", {"id": "other:TEST001", "text": "other-account-secret"})
        result = self.work.review.search_notes({"mrn": "TEST001", "limit": 1})
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["notes"]), 1)
        self.assertNotIn("other-account-secret", json.dumps(result))
        self.assertEqual(self.work.review.search_notes({"mrn": "TEST001", "offset": 1, "limit": 1})["total"], 2)
        self.assertEqual(len(snapshot(self.work, compact=True)["tasks"]), 2)

    def test_slow_login_does_not_block_status_notes_or_static_assets(self):
        self.task("note")
        entered, release = threading.Event(), threading.Event()
        server = BotServer(0, self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, body=None):
            headers = {"Cookie": "opd_session=" + self.app.session_token}
            if body is not None:
                headers.update({"Content-Type": "application/json", "X-CSRF-Token": self.app.csrf_token,
                                "X-Database-Context": self.app.context_token, "Origin": server.origin})
            conn = http.client.HTTPConnection(*server.server_address, timeout=3)
            try:
                conn.request("POST" if body is not None else "GET", path,
                             body=json.dumps(body) if body is not None else None, headers=headers)
                response = conn.getresponse()
                return response.status, response.read()
            finally:
                conn.close()
        original = self.work.gateway._connect_locked
        def slow(settings):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test release missing")
            return original(settings)
        try:
            with patch.object(self.work.gateway, "_connect_locked", side_effect=slow), ThreadPoolExecutor(max_workers=4) as pool:
                pending = pool.submit(request, "/api/accounts/login", {"id": self.key, "password": "synthetic"})
                self.assertTrue(entered.wait(2))
                try:
                    self.assertEqual(request(f"/api/accounts/{self.key}/status")[0], 200)
                    self.assertEqual(request(f"/api/accounts/{self.key}/reviews/notes/save", {"task_id": "note", "mrn": "TEST001", "text": "during login"})[0], 200)
                    self.assertEqual(request("/bot.js")[0], 200)
                    self.assertEqual(request("/api/databases/open", {})[0], 409)
                finally:
                    release.set()
                self.assertEqual(pending.result(timeout=3)[0], 200)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(3)


class GateWaitTests(unittest.TestCase):
    def test_shared_leases_parallel_and_exclusive_rejection_is_recoverable(self):
        gate = RequestGate()
        with gate.lease(), gate.lease():
            with self.assertRaises(BusyError), gate.lease(exclusive=True):
                pass
        with gate.lease(exclusive=True):
            with self.assertRaises(BusyError), gate.lease():
                pass
        with gate.lease():
            self.assertEqual(gate.active, 1)

    def test_waiting_account_query_can_cancel_without_leaking_foreground_priority(self):
        gateway = AccountGateway(None, NetworkGate())
        cancel = threading.Event()
        gateway.lock.acquire()
        started = threading.Event()
        def wait():
            with gateway.foreground(), gateway.task_context("task", "review", cancel):
                started.set()
                with gateway.serial():
                    return "unexpected"
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(wait)
                self.assertTrue(started.wait(1))
                cancel.set()
                with self.assertRaises(Cancelled):
                    result.result(timeout=1)
        finally:
            gateway.lock.release()
        self.assertEqual(gateway.foreground_waiters, 0)
        self.assertEqual(gateway.gate.active, 0)

    def test_account_and_network_slot_waits_have_deadlines(self):
        gateway = AccountGateway(None, NetworkGate())
        gateway.wait_timeout = .02
        gateway.lock.acquire()
        def wait():
            with gateway.serial():
                return "unexpected"
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                with self.assertRaises(BusyError):
                    pool.submit(wait).result(timeout=1)
        finally:
            gateway.lock.release()
        with gateway.serial():
            self.assertEqual(gateway.gate.active, 1)
        gate = NetworkGate(1)
        with gate.slot():
            with self.assertRaises(BusyError), gate.slot(deadline=monotonic() + .02):
                pass
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(Cancelled), gate.slot(cancel=cancel):
                pass
        self.assertEqual(gate.active, 0)
