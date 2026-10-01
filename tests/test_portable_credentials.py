import json
import tempfile
import unittest
from pathlib import Path

from vghks_bot.bot_store import Registry
from vghks_bot.google_sheets import GoogleSettings
from vghks_bot.portable_credentials import is_portable, seal
from vghks_bot.selftest_portable import check_portable_credentials, synthetic_google_key
from vghks_bot.storage import StorageError


class PortableCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_database_only_relocation_and_forgetting_without_windows(self):
        check_portable_credentials(self.path)

    def test_corrupt_or_obsolete_envelope_is_rejected_without_conversion(self):
        registry = Registry(self.path / "accounts.sqlite3")
        key = registry.save("TEST", "TEST", "高榮", "original", True)["id"]
        with registry.connect() as db:
            corrupt = seal(db, b"original")[:-5] + b"xxxxx"
        for value in (corrupt, b"obsolete-dpapi"):
            with registry.connect() as db:
                db.execute("UPDATE accounts SET secret=? WHERE id=?", (value, key))
            with self.assertRaises(ValueError):
                registry.password(key)
            with registry.connect() as db:
                self.assertEqual(bytes(db.execute("SELECT secret FROM accounts").fetchone()[0]), value)

    def test_google_accounts_are_portable_isolated_and_ignore_old_files(self):
        (self.path / "google-service-account.dpapi").write_bytes(b"obsolete")
        (self.path / "google-sheets.json").write_text(json.dumps({
            "spreadsheet_id": "old-sheet-00000000000"}), encoding="utf-8")
        second = self.path / "second"
        second.mkdir()
        one, two = GoogleSettings(self.path), GoogleSettings(second)
        self.assertFalse(one.public()["configured"])
        self.assertNotEqual(one.public()["spreadsheet_id"], "old-sheet-00000000000")
        one.save({"key": synthetic_google_key(), "spreadsheet_id": "synthetic-first-sheet-0000000000"})
        self.assertFalse(two.public()["configured"])
        self.assertNotIn("private_key", json.dumps(one.public()))
        self.assertNotIn(b"BEGIN PRIVATE KEY", one.path.read_bytes())
        with one.connect() as db:
            self.assertTrue(is_portable(db.execute("SELECT secret FROM bot_google_config").fetchone()[0]))
        one.remove_key()
        self.assertFalse(GoogleSettings(self.path).public()["configured"])

    def test_unsupported_sqlite_format_is_not_modified(self):
        path = self.path / "accounts.sqlite3"
        registry = Registry(path)
        with registry.connect() as db:
            db.execute("PRAGMA user_version=999")
        before = path.read_bytes()
        with self.assertRaises(StorageError):
            Registry(path)
        self.assertEqual(before, path.read_bytes())
