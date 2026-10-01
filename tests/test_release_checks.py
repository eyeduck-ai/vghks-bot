import unittest

from tools.verify_repository import check_metadata, check_paths


class ReleaseBoundaryTests(unittest.TestCase):
    def test_runtime_files_cannot_be_committed_under_different_directories(self):
        for name in ("archive/accounts.sqlite3", "archive/clinical.sqlite3-wal", "copy/VGHKS-bot-data/database.json",
                     ".local/legacy-docs/report.md", "dist/VGHKS-bot.exe", ".env.production", "keys/service-account-real.json",
                     "captures/session.har", "captures/diagnostics.jsonl", "release/old-build.zip"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                check_paths([name])
        check_paths(["docs/USER_GUIDE.md", "vghks_bot/defaults.json", ".github/workflows/windows.yml"])

    def test_release_tag_must_match_both_project_versions(self):
        version = check_metadata()
        self.assertEqual(check_metadata(tag="v"+version), version)
        with self.assertRaises(ValueError):
            check_metadata(tag="v0.0.0")
