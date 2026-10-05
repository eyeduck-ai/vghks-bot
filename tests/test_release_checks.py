import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.verify_repository import check_contents, check_metadata, check_paths, source_paths


class ReleaseBoundaryTests(unittest.TestCase):
    def test_runtime_files_cannot_be_committed_under_different_directories(self):
        for name in ("archive/accounts.sqlite3", "archive/clinical.sqlite3-wal", "copy/VGHKS-bot-data/database.json",
                     ".local/legacy-docs/report.md", "dist/VGHKS-bot.exe", ".env.production", "keys/service-account-real.json",
                     "captures/session.har", "captures/diagnostics.jsonl", "release/old-build.zip", "defaults.private.json",
                     ".playwright-cli/page.yml", ".ruff_cache/check", "output/playwright/patient.png"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                check_paths([name])
        check_paths(["docs/USER_GUIDE.md", "vghks_bot/defaults.json", ".github/workflows/windows.yml"])

    def test_release_tag_must_match_both_project_versions(self):
        version = check_metadata()
        self.assertEqual(check_metadata(tag="v"+version), version)
        with self.assertRaises(ValueError):
            check_metadata(tag="v0.0.0")

    def test_unstaged_sources_are_checked_without_including_ignored_private_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet"], cwd=root, check=True, capture_output=True)
            (root / ".gitignore").write_text(".local/\n", encoding="utf-8")
            (root / "README.md").write_text("Synthetic repository", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True)
            (root / ".local").mkdir()
            (root / ".local" / "private.txt").write_text("synthetic local data", encoding="utf-8")
            (root / "new_module.py").write_text("value = 1\n", encoding="utf-8")
            paths = source_paths(root)
            self.assertIn("new_module.py", paths)
            self.assertNotIn(".local/private.txt", paths)
            check_contents(root, paths)
            # Deliberately invalid synthetic key material in an untracked source.
            (root / "new_module.py").write_text(
                "-----BEGIN PRIVATE KEY-----\n" + "A" * 32, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Private key material.*new_module.py"):
                check_contents(root, source_paths(root))
