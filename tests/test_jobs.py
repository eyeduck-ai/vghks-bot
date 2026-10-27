import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vghks_bot import storage
from vghks_bot.jobs import Application, BusyError
from vghks_bot.scanner import ScanState
from vghks_bot.selftest import SyntheticSDK
from vghks_bot.settings import Settings, parse_range
from vghks_bot.storage import Journal, StorageError, Store


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.instances = []

        def factory(settings):
            sdk = SyntheticSDK(settings)
            self.instances.append(sdk)
            return sdk

        self.app = Application(Settings(), self.path, factory)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def account(self, name, start="2026-09-18", end="2026-09-18"):
        key = uuid.uuid4().hex
        self.app.save_accounts({"accounts":[{"id":key,"username":name,"password":"secret-"+name,"mode":"range","start":start,"end":end}]})
        return key

    def start(self, keys):
        ids = self.app.start({"account_ids":keys})["run_ids"]
        self.assertTrue(self.app.idle.wait(8))
        return [self.app.snapshot(key) for key in ids]

    def test_accounts_have_independent_date_ranges_and_sessions(self):
        a = self.account("DOC1","2026-09-17","2026-09-18")
        b = self.account("DOC2","2026-09-19","2026-09-19")
        second_started = threading.Event()
        original_factory = self.app.sdk_factory

        def factory(settings):
            if settings.username == "DOC2":
                second_started.set()
            return original_factory(settings)

        self.app.sdk_factory = factory
        ids = self.app.start({"account_ids": [a, b]})["run_ids"]
        # Keep one real batch while giving each account its own eight-second
        # bound. DOC2 enters its factory only after DOC1's queued job finishes.
        self.assertTrue(second_started.wait(8))
        self.assertTrue(self.app.idle.wait(8))
        first, second = [self.app.snapshot(key) for key in ids]
        self.assertEqual([first["status"],second["status"]],["completed","completed"])
        self.assertEqual(first["counts"]["soap_read"],6)
        self.assertEqual(second["counts"]["soap_read"],3)
        self.assertEqual({r["date"] for r in first["records"]},{"2026-09-17","2026-09-18"})
        self.assertEqual({r["date"] for r in second["records"]},{"2026-09-19"})
        self.assertEqual([s.card for s in self.instances],["DOC1","DOC2"])
        self.assertTrue(all(s.closed for s in self.instances))

    def test_history_survives_new_queries_restart_and_passwords_are_excluded(self):
        key = self.account("DOC1")
        first = self.start([key])[0]
        second = self.start([key])[0]
        self.assertNotEqual(first["id"],second["id"])
        # SOAP without a match must also be durable.
        self.assertEqual(len(first["records"]),3)
        self.assertEqual(first["counts"]["matched_visits"],2)
        self.assertEqual(first["records"][2]["matches"],[])
        self.app.close()
        self.app = Application(Settings(), self.path, SyntheticSDK)
        self.assertEqual(self.app.snapshot(first["id"])["records"],first["records"])
        self.assertEqual(len(self.app.history()["runs"]),2)
        self.assertEqual(self.app.snapshot(first["id"],str(first["revision"])),{"unchanged":True})
        self.assertFalse(self.app.accounts[key].password)
        for path in self.path.rglob("*.json*"):
            self.assertNotIn("secret-DOC1",path.read_text(encoding="utf-8"))

    def test_reclassify_saved_unmatched_soap_without_network_and_preserve_original(self):
        key = self.account("DOC1")
        original = self.start([key])[0]
        self.app.save_settings({"categories":[{"id":"follow","name":"追蹤","keywords":["追蹤視力"]}]})
        new_id = self.app.reclassify(original["id"])["run_ids"][0]
        self.assertTrue(self.app.idle.wait(8))
        result = self.app.snapshot(new_id)
        self.assertEqual(len(self.instances),1)
        self.assertEqual(result["counts"]["matched_visits"],1)
        self.assertEqual(result["records"][2]["matches"][0]["category"],"follow")
        self.assertEqual(result["source_id"],original["id"])
        self.assertEqual(self.app.snapshot(original["id"])["counts"]["matched_visits"],2)
        self.assertEqual(result["counts"]["soap_read"],3)

    def test_cancel_queued_account_without_starting_its_sdk(self):
        gate, entered = threading.Event(), threading.Event()
        original_factory = self.app.sdk_factory
        factory_entries = []

        def factory(settings):
            factory_entries.append(settings.username)
            entered.set()
            # Only the test's finally block releases the SDK. This watchdog
            # covers real queued-state journal I/O, not a product time limit.
            if not gate.wait(60):
                raise AssertionError("synthetic SDK factory was not released")
            return original_factory(settings)

        self.app.sdk_factory = factory
        a, b = self.account("DOC1"), self.account("DOC2")
        ids = self.app.start({"account_ids":[a,b]})["run_ids"]
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(factory_entries, ["DOC1"])
            self.assertEqual(self.app.snapshot(ids[1])["status"],"queued")
            with self.assertRaises(BusyError):
                self.app.start({"account_ids":[a]})
            self.app.stop(ids[1])
            self.assertEqual(self.instances, [], "the held SDK factory advanced before cancellation")
        finally:
            gate.set()
        self.assertTrue(self.app.idle.wait(6))
        self.assertEqual(self.app.snapshot(ids[1])["status"],"cancelled")
        self.assertEqual([s.card for s in self.instances],["DOC1"])
        self.assertEqual(factory_entries, ["DOC1"])

    def test_account_failure_does_not_block_next_account(self):
        original_factory = self.app.sdk_factory

        def factory(settings):
            sdk = original_factory(settings)
            if settings.username == "DOC1":
                sdk.auth.check = lambda **_: SimpleNamespace(ok=False,targets=[])
            return sdk

        self.app.sdk_factory = factory
        first, second = self.start([self.account("DOC1"),self.account("DOC2")])
        self.assertEqual(first["status"],"paused")
        self.assertEqual(second["status"],"completed")

    def test_storage_failure_stops_further_reads_and_preserves_current_soap_in_memory(self):
        key = self.account("DOC1")
        append = Journal.append

        def fail_record(journal, name, value):
            if name == "records":
                raise StorageError("disk full")
            return append(journal,name,value)

        with patch.object(Journal,"append",fail_record):
            result = self.start([key])[0]
        self.assertEqual(result["status"],"failed")
        self.assertEqual(result["counts"]["soap_read"],1)
        self.assertEqual(len(result["records"]),1)
        self.assertEqual(result["issues"][-1]["code"],"STORAGE_FAILED")

    def test_batch_validates_all_accounts_before_start(self):
        good = self.account("DOC1")
        with self.assertRaises(ValueError):
            self.app.start({"account_ids":[good,"missing"]})
        self.assertEqual(self.app.history()["runs"],[])
        self.assertEqual(self.instances,[])

    def test_history_polling_during_checkpoint_does_not_break_windows_replace(self):
        start, end = parse_range({"start":"2026-09-18"})
        state = ScanState(start, end, id=uuid.uuid4().hex)
        journal = self.app.store.create(state.snapshot(detail=False))
        metadata_path = journal.directory / "run.json"
        updated = {**state.snapshot(detail=False), "status":"completed", "revision":1}
        reader_open, reader_closed, release_read = (threading.Event() for _ in range(3))
        writer_attempted, writer_blocked = threading.Event(), threading.Event()
        results, errors, replacements = {}, [], []
        io_lock, read_text, replace = storage._IO_LOCK, Path.read_text, storage.os.replace

        class ObservedLock:
            def __enter__(self):
                if threading.current_thread() is writer:
                    acquired = io_lock.acquire(blocking=False)
                    if not acquired:
                        writer_blocked.set()
                    writer_attempted.set()
                    if not acquired:
                        io_lock.acquire()
                else:
                    io_lock.acquire()
                return self

            def __exit__(self, *_):
                io_lock.release()

        def held_read(path, *args, **kwargs):
            if path != metadata_path or threading.current_thread() is not reader:
                return read_text(path, *args, **kwargs)
            try:
                with path.open("r", encoding="utf-8") as handle:
                    reader_open.set()
                    if not release_read.wait(10):
                        raise AssertionError("metadata reader was not released")
                    return handle.read()
            finally:
                reader_closed.set()

        def windows_replace(source, destination):
            if destination == metadata_path:
                # Reproduce Windows sharing rules even on non-Windows hosts.
                if reader_open.is_set() and not reader_closed.is_set():
                    raise PermissionError("metadata is still open for reading")
                replacements.append(destination)
            return replace(source, destination)

        def run(name, operation):
            try:
                results[name] = operation()
            except Exception as exc:
                errors.append(exc)

        reader = threading.Thread(target=run, args=("history", self.app.history), daemon=True)
        writer = threading.Thread(target=run, args=("checkpoint", lambda: journal.checkpoint(updated)), daemon=True)
        with patch.object(storage, "_IO_LOCK", ObservedLock()), \
                patch.object(Path, "read_text", held_read), \
                patch.object(storage.os, "replace", windows_replace):
            reader.start()
            try:
                self.assertTrue(reader_open.wait(10), "history did not open the metadata file")
                writer.start()
                self.assertTrue(writer_attempted.wait(10), "checkpoint did not acquire the metadata lock")
                # The read handle must force actual contention, without sleeps
                # or a long scan whose many fsync calls depend on runner speed.
                self.assertTrue(writer_blocked.is_set(), "checkpoint overlapped an open metadata reader")
            finally:
                release_read.set()
                reader.join(10)
                if writer.ident is not None:
                    writer.join(10)
        self.assertFalse(reader.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results["history"]["runs"][0]["status"], "running")
        self.assertEqual(replacements, [metadata_path])
        saved = self.app.store.metadata(state.data["id"])
        self.assertEqual((saved["status"], saved["revision"]), ("completed", 1))

    def test_deleting_profile_keeps_history(self):
        key = self.account("DOC1")
        result = self.start([key])[0]
        self.app.delete_account(key)
        self.assertEqual(len(self.app.snapshot(result["id"])["records"]),3)




class RecoveryTests(unittest.TestCase):
    def test_partial_journal_and_interrupted_job_recover_complete_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            start,end = parse_range({"start":"2026-09-18"})
            state = ScanState(start,end,id=uuid.uuid4().hex)
            state.journal = store.create(state.snapshot(detail=False))
            state.add_record({"id":"a"*24,"mrn":"TEST","date":"2026-09-18","matches":[],"soap":"saved text"})
            with (store.path(state.data["id"])/"records.jsonl").open("ab") as handle:
                handle.write(b'{"incomplete":')
            store.close()
            recovered = Store(Path(directory))
            try:
                self.assertEqual(recovered.summaries()[0]["status"],"interrupted")
                result = recovered.load(state.data["id"])
                self.assertEqual(len(result["records"]),1)
                self.assertEqual(result["counts"]["soap_read"],1)
                self.assertEqual(result["issues"][-1]["code"],"JOURNAL_DAMAGED")
            finally:
                recovered.close()

    def test_path_traversal_rejected_and_atomic_settings_keep_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            try:
                with self.assertRaises(ValueError):
                    store.load("../settings")
                store.save_config(Settings(),[])
                before = json.loads((store.directory/"settings.json").read_text(encoding="utf-8"))
                with patch("vghks_bot.storage.os.replace",side_effect=OSError("disk error")), self.assertRaises(StorageError):
                    store.save_config(Settings(min_delay_seconds=2),[])
                self.assertEqual(json.loads((store.directory/"settings.json").read_text(encoding="utf-8")),before)
            finally:
                store.close()
