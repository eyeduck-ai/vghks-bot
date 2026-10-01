import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from vghks_bot.bot import BotApplication
from vghks_bot.database_format import MANIFEST
from vghks_bot.databases import DatabaseManager, copy_database, inspect_database
from vghks_bot.selftest_bot import BotSyntheticSDK
from vghks_bot.selftest_databases import check_databases, contents
from vghks_bot.settings import Settings
from vghks_bot.storage import StorageError


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.manager = DatabaseManager(self.path / "home", Settings(), BotSyntheticSDK)

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def test_full_copy_readonly_backup_http_and_credentials(self):
        check_databases(self.path / "integration")

    def test_second_manager_cannot_overwrite_catalog(self):
        before = self.manager.catalog_path.read_bytes()
        with self.assertRaises(StorageError):
            DatabaseManager(self.manager.home, Settings(), BotSyntheticSDK)
        self.assertEqual(before, self.manager.catalog_path.read_bytes())

    def test_create_rename_switch_and_bookmarks_survive_home_relocation(self):
        manager = self.manager
        original = manager.active_entry
        created = manager.handle("create", {"name": "第二份"})
        manager.open_entry(created["entry"]["id"])
        manager.handle("rename", {"id": manager.active_entry, "name": "門診研究"})
        self.assertEqual(manager.current.database["name"], "門診研究")
        manager.handle("forget", {"id": original})
        self.assertEqual(len(manager.listing()["databases"]), 1)
        with self.assertRaises(ValueError):
            manager.handle("forget", {"id": manager.active_entry})
        manager.close()
        # Windows TEMP can use an 8.3 alias; the manager canonicalizes its paths.
        relocated = (self.path / "relocated").resolve()
        shutil.copytree(manager.home, relocated)
        copied = DatabaseManager(relocated, Settings(), BotSyntheticSDK)
        try:
            self.assertEqual(copied.current.database["name"], "門診研究")
            self.assertTrue(copied.current.directory.is_relative_to(relocated))
        finally:
            copied.close()

    def test_import_is_independent_and_changed_source_needs_new_preview(self):
        manager = self.manager
        original = manager.current.directory
        preview = manager.handle("import-preview", {"path": str(original)})
        manager.handle("rename", {"id": manager.active_entry, "name": "已更名"})
        with self.assertRaisesRegex(ValueError, "重新預覽"):
            manager.handle("import", {"preview_id": preview["preview_id"]})
        preview = manager.handle("import-preview", {"path": str(original)})
        before = contents(original)
        result = manager.handle("import", {"preview_id": preview["preview_id"]})
        self.assertNotEqual(result["database"]["id"], preview["database"]["id"])
        self.assertEqual(contents(original), before)
        with self.assertRaises(ValueError):
            manager.handle("import", {"preview_id": preview["preview_id"]})

    def test_busy_account_blocks_switch_backup_and_copy(self):
        manager = self.manager
        key = manager.current.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        work = manager.current.workspace(key)
        created = manager.handle("create", {})
        for kind in ("queue", "foreground", "sheets"):
            if kind == "queue":
                work.idle.clear()
            elif kind == "foreground":
                work.review.foreground["busy"] = None
            else:
                work.analysis.sheet_busy = True
            try:
                for action, values in (("open", {"id": created["entry"]["id"]}), ("backup", {}), ("copy", {})):
                    with self.assertRaisesRegex(ValueError, "暫停"):
                        manager.handle(action, values)
            finally:
                work.idle.set()
                work.review.foreground.clear()
                work.analysis.sheet_busy = False

    def test_unsupported_or_incomplete_data_is_never_converted(self):
        fresh = self.path / "invalid"
        fresh.mkdir()
        (fresh / "accounts.sqlite3").write_bytes(b"old format")
        before = contents(fresh)
        with self.assertRaises(StorageError):
            BotApplication(Settings(), fresh, BotSyntheticSDK)
        self.assertEqual(before, contents(fresh))
        original = self.manager.current.directory
        copy = copy_database(original, self.path / "future", name="未來版本", owned=True)
        dest = Path(copy["path"])
        meta = json.loads((dest / MANIFEST).read_text(encoding="utf-8"))
        meta["format"] = 999
        (dest / MANIFEST).write_text(json.dumps(meta), encoding="utf-8")
        before = contents(dest)
        with self.assertRaises(StorageError):
            inspect_database(dest)
        self.assertEqual(before, contents(dest))
        meta["format"] = 1
        (dest / MANIFEST).write_text(json.dumps(meta), encoding="utf-8")
        with closing(sqlite3.connect(dest / "accounts.sqlite3")) as db:
            db.execute("PRAGMA user_version=999")
        before = contents(dest)
        with self.assertRaises(StorageError):
            inspect_database(dest)
        self.assertEqual(before, contents(dest))

    def test_copy_failure_no_overwrite_no_nested_paths_and_no_partial_publication(self):
        source = self.manager.current.directory
        for destination in (source, source / "nested", source.parent):
            with self.assertRaises(ValueError):
                copy_database(source, destination, name="invalid", owned=True)
        dest = self.path / "failed"
        with patch("vghks_bot.databases.shutil.copy2", side_effect=OSError("synthetic disk full")):
            (source / "attachment.bin").write_bytes(b"test")
            with self.assertRaises(OSError):
                copy_database(source, dest, name="failed", owned=True)
        self.assertFalse(dest.exists())
        self.assertEqual(list(self.path.glob(".vghks-copy-*")), [])
        with self.assertRaises(StorageError):
            copy_database(source, dest, name="locked")

    def test_unavailable_last_database_leaves_management_available(self):
        manager = self.manager
        source = manager.current.directory
        manager.close()
        meta = json.loads((source / MANIFEST).read_text(encoding="utf-8"))
        meta["format"] = 999
        (source / MANIFEST).write_text(json.dumps(meta), encoding="utf-8")
        before = contents(source)
        reopened = DatabaseManager(manager.home, Settings(), BotSyntheticSDK)
        try:
            self.assertTrue(reopened.current.read_only)
            self.assertTrue(reopened.listing()["notice"])
            self.assertTrue(reopened.listing()["databases"][0]["error"])
            self.assertEqual(before, contents(source))
            new = reopened.handle("create", {"name": "新研究"})
            reopened.open_entry(new["entry"]["id"])
            self.assertFalse(reopened.current.read_only)
            self.assertEqual(reopened.current.database["name"], "新研究")
            self.assertEqual(before, contents(source))
        finally:
            reopened.close()

    def test_committed_wal_data_is_included_without_altering_source(self):
        source = self.manager.current.directory
        key = self.manager.current.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        path = source / "accounts" / key / "clinical.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("CREATE TABLE snapshot_probe(value TEXT)")
            db.execute("INSERT INTO snapshot_probe VALUES('committed in WAL')")
            db.commit()
            self.assertTrue(path.with_name(path.name+"-wal").exists())
            before = contents(source)
            copied = copy_database(source, self.path / "wal-copy", name="WAL", owned=True)
            self.assertEqual(contents(source), before)
            with closing(sqlite3.connect(Path(copied["path"]) / "accounts" / key / "clinical.sqlite3")) as check:
                self.assertEqual(check.execute("SELECT value FROM snapshot_probe").fetchone()[0], "committed in WAL")
