import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.verify_repository import (
    check_contents,
    check_core_installation,
    check_metadata,
    check_paths,
    check_sdk_installation,
    source_paths,
)


class ReleaseBoundaryTests(unittest.TestCase):
    def test_installed_sheet_core_has_the_exact_pinned_provenance(self):
        revision = check_core_installation()
        self.assertEqual(len(revision), 40)
        invalid = Mock()
        invalid.read_text.return_value = json.dumps({"url": "https://github.com/eyeduck-ai/op_spreadsheet.git",
            "subdirectory": "packages/python", "vcs_info": {"vcs": "git", "commit_id": "0" * 40}})
        with patch("tools.verify_repository.importlib.metadata.distribution", return_value=invalid):
            with self.assertRaisesRegex(ValueError, "sheet-core.*pinned commit"):
                check_core_installation()

    def test_runtime_files_cannot_be_committed_under_different_directories(self):
        for name in ("archive/accounts.sqlite3", "archive/clinical.sqlite3-wal", "copy/VGHKS-bot-data/database.json",
                     ".local/legacy-docs/report.md", "dist/VGHKS-bot.exe", ".env.production", "keys/service-account-real.json",
                     "captures/session.har", "captures/diagnostics.jsonl", "release/old-build.zip", "defaults.private.json",
                     "custom-data/.instance.json",
                     ".playwright-cli/page.yml", ".ruff_cache/check", "output/playwright/patient.png"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                check_paths([name])
        check_paths(["docs/USER_GUIDE.md", "vghks_bot/defaults.json", ".github/workflows/windows.yml"])

    def test_release_tag_must_match_both_project_versions(self):
        version = check_metadata()
        self.assertEqual(check_metadata(tag="v"+version), version)
        with self.assertRaises(ValueError):
            check_metadata(tag="v0.0.0")

    def test_installed_sdk_matches_the_pinned_git_commit(self):
        revision = check_sdk_installation()
        self.assertEqual(len(revision), 40)

    def test_same_sdk_version_cannot_hide_a_stale_commit_or_missing_provenance(self):
        revision = check_sdk_installation()
        valid = {"url": "https://github.com/eyeduck-ai/vghks-sdk.git",
                 "vcs_info": {"vcs": "git", "commit_id": revision}}
        values = [None, "invalid JSON", "[]", json.dumps({**valid, "vcs_info": []}),
                  json.dumps({**valid, "url": "https://example.invalid/another-sdk.git"}),
                  json.dumps({**valid, "vcs_info": {"vcs": "git", "commit_id": "0" * 40}})]
        for value in values:
            installed = Mock(version="0.23.5")
            installed.read_text.return_value = value
            with self.subTest(provenance=value), patch(
                    "tools.verify_repository.importlib.metadata.distribution", return_value=installed):
                with self.assertRaisesRegex(ValueError, "SDK|vghks-sdk"):
                    check_sdk_installation()

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
