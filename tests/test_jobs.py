import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
        first, second = self.start([a,b])
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

        def factory(settings):
            entered.set()
            gate.wait(4)
            return original_factory(settings)

        self.app.sdk_factory = factory
        a, b = self.account("DOC1"), self.account("DOC2")
        ids = self.app.start({"account_ids":[a,b]})["run_ids"]
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.app.snapshot(ids[1])["status"],"queued")
            with self.assertRaises(BusyError):
                self.app.start({"account_ids":[a]})
            self.app.stop(ids[1])
        finally:
            gate.set()
        self.assertTrue(self.app.idle.wait(6))
        self.assertEqual(self.app.snapshot(ids[1])["status"],"cancelled")
        self.assertEqual([s.card for s in self.instances],["DOC1"])

    def test_account_failure_does_not_block_next_account(self):
        original_factory = self.app.sdk_factory

        def factory(settings):
            sdk = original_factory(settings)
            if settings.username == "DOC1":
                sdk.auth.check = lambda **_: SimpleNamespace(ok=False,targets=[])
            return sdk

        self.app.sdk_factory = factory
        first, second = self.start([self.account("DOC1"),self.account("DOC2")])
        self.assertEqual(first["status"],"failed")
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
        key = self.account("DOC1","2026-09-01","2026-09-18")
        stop = threading.Event()

        def read_history():
            while not stop.is_set():
                self.app.history()
                stop.wait(.001)

        reader = threading.Thread(target=read_history,daemon=True)
        reader.start()
        try:
            result = self.start([key])[0]
            self.assertEqual(result["status"],"completed")
            self.assertEqual(result["counts"]["soap_read"],54)
        finally:
            stop.set()
            reader.join(2)

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
