"""Crash-safe local receipts keep cloud state small without weakening identity."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from analysis_fixtures import MemorySheets

from vghks_bot.bot_store import WorkbenchStore
from vghks_bot.library import Library
from vghks_bot.sheet_journal import MANAGEMENT_KEY, OPD_KEY, reclaim
from vghks_bot.sheet_management import PLAN_KIND, SheetManagement
from vghks_bot.sheet_plan import fingerprint, verify


def marker(token):
    return {"metadataKey": OPD_KEY, "metadataValue": token, "visibility": "DOCUMENT", "metadataId": 1}


class SheetJournalTests(unittest.TestCase):
    def test_failed_copy_cleanup_is_found_after_delete_phase_already_retired(self):
        with tempfile.TemporaryDirectory() as directory:
            db = WorkbenchStore(Library(Path(directory) / "clinical.sqlite3"))
            service = SimpleNamespace(google=SimpleNamespace(public=lambda: {"spreadsheet_id": "test", "configured": True}))
            manager = SheetManagement(SimpleNamespace(sheets=service, review=SimpleNamespace(db=db)))
            plan = {"id": "a" * 32, "spreadsheet_id": "test", "action": "archive", "phase": "delete", "status": "applied",
                    "marker": "a" * 32 + ":delete", "confirmed_markers": ["a" * 32 + ":copy", "a" * 32 + ":delete"],
                    "retired_markers": ["a" * 32 + ":delete"]}
            db.save(PLAN_KIND, plan)
            sheets = MemorySheets({"sheets": [], "developerMetadata": [{**marker("a" * 32 + ":copy"), "metadataKey": MANAGEMENT_KEY}]})
            receipts = manager._marker_receipts("test")
            self.assertEqual(len(receipts), 1)
            self.assertTrue(reclaim(sheets, sheets.read(), receipts))
            self.assertEqual(sheets.book["developerMetadata"], [])
            self.assertEqual(manager._marker_receipts("test"), [])

    def test_receipt_is_durable_before_cloud_marker_retirement_and_survives_restart(self):
        sheets = MemorySheets({"sheets": [], "developerMetadata": [marker("confirmed"), marker("unresolved")]})
        record = {"id": "confirmed", "status": "applied", "expectations": []}
        saved = []

        def save(value):
            saved.append(copy.deepcopy(value))
            if len(saved) == 2:
                raise RuntimeError("synthetic crash after marker deletion")

        with self.assertRaises(RuntimeError):
            reclaim(sheets, sheets.read(), [(record, OPD_KEY, ["confirmed"], save)])
        self.assertEqual([m["metadataValue"] for m in sheets.book["developerMetadata"]], ["unresolved"])
        self.assertEqual(verify(sheets.read(), saved[0]), "verified")
        self.assertNotIn("retired_markers", saved[0])
        self.assertTrue(reclaim(sheets, sheets.read(), [(saved[0], OPD_KEY, ["confirmed"], lambda _: None)]))
        self.assertEqual(sheets.marker_writes, 1)

    def test_uncertain_operations_and_unknown_markers_are_never_reclaimed(self):
        sheets = MemorySheets({"sheets": [], "developerMetadata": [marker("pending"), marker("other-machine")]})
        pending = {"id": "pending", "status": "uncertain"}
        self.assertTrue(reclaim(sheets, sheets.read(), [(pending, OPD_KEY, ["pending"], lambda _: None)]))
        self.assertEqual(len(sheets.book["developerMetadata"]), 2)
        self.assertEqual(sheets.marker_writes, 0)
        self.assertNotIn("confirmed_markers", pending)

    def test_more_than_old_metadata_capacity_does_not_accumulate_markers(self):
        sheets = MemorySheets({"sheets": [], "developerMetadata": []})
        for index in range(650):
            token = f"{index:032x}"
            sheets.book["developerMetadata"] = [marker(token)]
            record = {"id": token, "status": "applied", "expectations": []}
            self.assertTrue(reclaim(sheets, sheets.read(), [(record, OPD_KEY, [token], lambda _: None)]))
            self.assertEqual(sheets.book["developerMetadata"], [])
            self.assertEqual(verify(sheets.read(), record), "verified")
        self.assertEqual(sheets.book["sheets"], [])  # No additional system tab.

    def test_failed_cleanup_does_not_lose_confirmed_commit(self):
        sheets = MemorySheets({"sheets": [], "developerMetadata": [marker("confirmed")]})
        sheets.timeout_after_commit = True
        record = {"id": "confirmed", "status": "applied", "expectations": []}
        self.assertFalse(reclaim(sheets, sheets.read(), [(record, OPD_KEY, ["confirmed"], lambda _: None)]))
        self.assertEqual(verify(sheets.read(), record), "verified")
        self.assertNotIn("retired_markers", record)

    def test_marker_cleanup_does_not_stale_a_preview_but_clinical_change_does(self):
        book = {"sheets": [{"rows": [[{"userEnteredValue": {"stringValue": "synthetic"}}]]}], "developerMetadata": [marker("old")]}
        before = fingerprint(book)
        book["developerMetadata"] = []
        self.assertEqual(fingerprint(book), before)
        book["sheets"][0]["rows"][0][0]["userEnteredValue"]["stringValue"] = "edited"
        self.assertNotEqual(fingerprint(book), before)

    def test_management_lists_and_local_backup_are_separate_and_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            db = WorkbenchStore(Library(Path(directory) / "clinical.sqlite3"))
            service = SimpleNamespace(google=SimpleNamespace(public=lambda: {"spreadsheet_id": "test", "configured": True}))
            manager = SheetManagement(SimpleNamespace(sheets=service, review=SimpleNamespace(db=db)))
            for index in range(35):
                manager._save({"id": f"{index:032x}", "spreadsheet_id": "test", "status": "preview", "action": "archive",
                    "created_at": f"2026-10-13T00:00:{index:02d}", "params": {"year": 2025},
                    "requests": [{"synthetic": "backup" * 10000}], "expected": [], "archive_plan": {"source": index}})
            summaries = db.summaries(PLAN_KIND, fields=("id", "params"), spreadsheet_id="test")
            self.assertEqual(len(summaries), 30)
            self.assertEqual(summaries[0]["params"], {"year": 2025})
            packed = db.get(PLAN_KIND, summaries[0]["id"])
            self.assertNotIn("requests", packed)
            self.assertLess(len(json.dumps(packed)), 2000)
            restored = manager._plan({"id": packed["id"]})
            self.assertEqual(restored["archive_plan"], {"source": 34})
            self.assertEqual(len(restored["requests"][0]["synthetic"]), 60000)
            with db.library.connect() as connection:
                connection.execute("UPDATE bot_sheet_backups SET payload=? WHERE digest=?", (b"corrupt", packed["details_ref"]))
            with self.assertRaisesRegex(ValueError, "備份核對失敗"):
                manager._plan({"id": packed["id"]})
