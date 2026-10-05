"""Scope correctness, bounded task execution and rebuildable metadata."""
import json
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from time import monotonic, sleep
from unittest.mock import patch

from test_cataract_reads import seed_patients

from vghks_bot.bot import BotApplication
from vghks_bot.jobs import BusyError
from vghks_bot.library import Library
from vghks_bot.library_search import ensure
from vghks_bot.migrations import migrate
from vghks_bot.scanner import ScanState
from vghks_bot.selftest_bot import BotSyntheticSDK
from vghks_bot.settings import Settings, today


class EfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = BotApplication(Settings(), Path(self.temp.name), BotSyntheticSDK)
        key = self.app.login({"username": "SYNTHETIC", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(key)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def record(self, number, *, mrn=None, soap="A+P：# FU (Straße) 100%_", day="2026-09-11"):
        row = {"id": str(number), "mrn": mrn or f"TEST{number:03}", "name": "合成病人", "date": day,
               "section": "眼科", "case_no": str(number), "soap": soap}
        self.work.store.library.save_record(row, self.work.username, "synthetic")
        return row

    def task(self, key="review", members=None):
        return self.work.review.db.save("task", {"id": key, "kind": "review", "status": "running",
            "created_at": "2026-10-04T08:00:00", "members": members or [{"mrn": "TEST001"}]})

    def test_scoped_facets_pagination_literal_unicode_and_current_task_references(self):
        a, b = self.record(1, soap="A+P：未分類 (Straße) 100%_"), self.record(2)
        self.task()
        self.work.review.db.item("review", a["mrn"], {"status": "reviewed", "records": [a]})
        library = self.work.store.library
        ensure(library, self.work.settings)
        result = self.work.library_search({"task_id": "review", "limit": 1, "q": "STRASSE"})
        self.assertEqual((result["total"], result["page_total"]), (1, 1))
        self.assertEqual(result["tag_counts"].get("followup", 0), 0)
        self.assertEqual(library.search({"q": "100%_"}, self.work.settings)["total"], 2)
        self.work.review.db.item("review", a["mrn"], {"status": "reviewed", "records": [b]})
        self.assertEqual([r["id"] for r in self.work.library_search({"task_id": "review"})["records"]], [b["id"]])
        with library.connect() as db:
            db.execute("DELETE FROM records WHERE id=?", (b["id"],))
        self.assertEqual(self.work.library_search({"task_id": "review"})["page_total"], 0)

    def test_warm_page_decodes_only_selected_soap_and_rules_rebuild(self):
        for number in range(85):
            self.record(number)
        library = self.work.store.library
        ensure(library, self.work.settings)
        decoded = []
        original = json.loads

        def loads(value, *args, **kwargs):
            if isinstance(value, str) and '"soap"' in value:
                decoded.append(value)
            return original(value, *args, **kwargs)

        with patch("json.loads", side_effect=loads):
            result = library.search({"limit": 40, "offset": 40}, self.work.settings)
        self.assertEqual(len(result["records"]), 40)
        self.assertEqual(len(decoded), 40)
        settings = self.work.settings.update({"categories": [{"id": "new", "name": "新規則", "keywords": ["Straße"]}]})
        self.assertEqual(library.search({"tag": "new"}, settings)["total"], 85)
        self.record(0, soap="A+P：沒有關鍵字")
        self.assertEqual(library.search({"tag": "new"}, settings)["total"], 84)

    def test_progress_summary_does_not_read_items_or_rewrite_members(self):
        task = self.task(members=[{"mrn": f"TEST{i:04}"} for i in range(1000)])
        self.work.review.db.item(task["id"], "one", {"status": "reviewed", "records": [{"soap": "large" * 10000}]})
        self.work.review.db.item(task["id"], "two", {"status": "partial", "processing": True, "records": []})
        self.work.review.db.item(task["id"], "three", {"status": "error"})
        with self.work.store.library.connect() as db:
            original = db.execute("SELECT payload FROM bot_documents WHERE kind='task' AND id='review'").fetchone()[0]
        state = ScanState(today(), today(), id=task["id"], status="running")
        with patch.object(self.work.review.db, "items", side_effect=AssertionError("SOAP read")):
            self.work.review.report(state, task, stage="updated")
            result = self.work.review.task(task["id"], summary=True)
        self.assertEqual((result["done"], result["total"], result["progress"]["failed"]), (2, 1000, 1))
        self.assertNotIn("members", result)
        self.assertNotIn("items", result)
        with self.work.store.library.connect() as db:
            self.assertEqual(db.execute("SELECT payload FROM bot_documents WHERE kind='task' AND id='review'").fetchone()[0], original)

    def test_cataract_65_by_10_uses_under_100_sql_and_checks_file_loss(self):
        cohort = seed_patients(self.work, count=65, order_count=10)
        statements, original = [], sqlite3.connect

        def connect(*args, **kwargs):
            db = original(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db

        values = {"cohort_id": cohort["id"]}
        with patch("vghks_bot.library.sqlite3.connect", side_effect=connect), \
                patch.object(self.work.store.library, "get_record", side_effect=AssertionError("SOAP read")):
            first = self.work.analysis.cataract_status(values)
        self.assertLessEqual(len(statements), 100)
        mrn = cohort["members"][0]["mrn"]
        before = next(row for row in first["members"] if row["mrn"] == mrn)
        file = next(self.work.analysis.store.assets.iterdir())
        file.unlink()
        second = self.work.analysis.cataract_status(values)
        after = next(row for row in second["members"] if row["mrn"] == mrn)
        if before["data_revision"] == after["data_revision"]:
            # Every seeded patient has its own attachment; find the lost owner.
            self.assertNotEqual([r["data_revision"] for r in first["members"]], [r["data_revision"] for r in second["members"]])
        self.assertEqual(first["queue"], second["queue"])

    def test_inventory_paginates_without_decoding_any_soap_or_numeric_body(self):
        for i in range(45):
            self.record(i)
            self.work.analysis.store.save_step(f"TEST{i:03}", "numeric-history", "numeric", {"body": "x" * 10000}, self.work.username)
        ensure(self.work.store.library, self.work.settings)
        with patch("json.loads", side_effect=AssertionError("inventory decoded a document")):
            page = self.work.library_data.read({"limit": 40})
            tail = self.work.library_data.read({"limit": 40, "offset": 40})
        self.assertEqual((page["total"], len(page["patients"]), len(tail["patients"])), (45, 40, 5))

    def test_foreground_limit_dedup_full_queue_cancel_and_account_isolation(self):
        release, entered = threading.Event(), threading.Event()
        running, peak = [], [0]
        mutex = threading.Lock()

        def execute(state, task):
            with mutex:
                running.append(task["id"])
                peak[0] = max(peak[0], len(running))
                if len(running) == 4:
                    entered.set()
            state.update(status="running")
            release.wait(10)
            state.update(status="completed")
            with mutex:
                running.remove(task["id"])

        with patch.object(self.work.review, "execute", side_effect=execute):
            try:
                ids = [self.work.review.start({"kind": "resolve", "identifiers": f"TEST{i:03}"})["task_id"] for i in range(68)]
                self.assertTrue(entered.wait(2))
                self.assertEqual(peak[0], 4)
                self.assertEqual(len(self.work.task_manager.pending), 64)
                self.assertEqual(self.work.review.start({"kind": "resolve", "identifiers": "TEST067"})["task_id"], ids[-1])
                with self.assertRaises(BusyError):
                    self.work.review.start({"kind": "resolve", "identifiers": "TEST068"})
                self.assertEqual(len(self.work.review.db.task_summaries()), 68)
                self.work.review.stop(ids[4])
                self.assertEqual(self.work.review.task(ids[4], summary=True)["status"], "paused")
                self.work.review.start({"kind": "resolve", "identifiers": "TEST068"})
                other = self.app.login({"username": "SECOND", "password": "synthetic"})["account"]["id"]
                self.assertEqual(len(self.app.workspace(other).task_manager.pending), 0)
            finally:
                release.set()
                deadline = monotonic() + 8
                while self.work.review.foreground and monotonic() < deadline:
                    sleep(.02)
        self.assertFalse(self.work.review.foreground)
        self.assertEqual(peak[0], 4)

    def test_read_snapshot_allows_writer_and_remains_consistent(self):
        library = self.work.store.library
        self.record(1, soap="old")
        complete = threading.Event()
        with library.read_snapshot():
            self.assertEqual(library.get_record("1")["soap"], "old")
            thread = threading.Thread(target=lambda: (self.record(1, soap="new"), complete.set()))
            thread.start()
            self.assertTrue(complete.wait(2), "read snapshot blocked the writer")
            self.assertEqual(library.get_record("1")["soap"], "old")
        thread.join(2)
        self.assertEqual(library.get_record("1")["soap"], "new")

    def test_browser_shared_status_visibility_and_progress_memory(self):
        result = subprocess.run(["node", "tests/efficiency_ui.js"], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class MigrationTests(unittest.TestCase):
    def test_interrupted_addition_rolls_back_and_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Library(Path(directory) / "test.sqlite")

            def interrupt(db):
                db.execute("CREATE TABLE resumable(value)")
                db.execute("INSERT INTO resumable VALUES(1)")
                raise RuntimeError("synthetic interruption")

            with self.assertRaises(RuntimeError), library.connect() as db:
                migrate(db, "interrupted", interrupt)
            with library.connect() as db:
                self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='resumable'").fetchone())
                migrate(db, "interrupted", lambda db: db.execute("CREATE TABLE resumable(value)"))
            with library.connect() as db:
                migrate(db, "interrupted", lambda db: self.fail("migration ran twice"))
                self.assertEqual(db.execute("SELECT count(*) FROM resumable").fetchone()[0], 0)
