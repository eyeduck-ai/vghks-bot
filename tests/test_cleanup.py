"""Exercise workspace cleanup only against disposable synthetic directories."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "clean.ps1"


@unittest.skipUnless(POWERSHELL, "PowerShell is required for the Windows cleanup script")
class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace"
        (self.root / "tools").mkdir(parents=True)
        shutil.copyfile(SCRIPT, self.root / "tools" / "clean.ps1")

    def tearDown(self):
        self.temp.cleanup()

    def file(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic fixture")
        return path

    def clean(self, *options):
        return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File",
                               str(self.root / "tools" / "clean.ps1"), *options],
                              cwd=self.root, capture_output=True, text=True, timeout=20)

    def test_preview_and_cleanup_preserve_runtime_evidence_and_two_newest_releases(self):
        removed = [self.file(name) for name in (
            ".build/pyinstaller/cache", "build/cache", ".ruff_cache/cache",
            "vghks_bot/__pycache__/cached.pyc", ".playwright-cli/page.yml",
            "output/playwright/synthetic.png", "dist/VGHKS-bot-v6.6.33-windows-x64.zip")]
        kept = [self.file(name) for name in (
            ".build/ci/report.json", "dist/VGHKS-bot-v6.6.48-windows-x64.zip",
            "dist/VGHKS-bot-v6.6.49-windows-x64.zip", "dist/VGHKS-bot.exe",
            "dist/SHA256SUMS.txt", "dist/incident.har", "dist/VGHKS-debug.zip",
            "output/keep.txt", ".venv/keep.txt", ".local/keep.txt",
            "VGHKS-bot-data/accounts/clinical.sqlite3", "vghks_bot/source.py")]
        options = ("-KeepValidationReports", "-OldReleases", "-BrowserArtifacts")
        preview = self.clean(*options, "-WhatIf")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertTrue(all(path.exists() for path in removed + kept), "preview deleted files")
        result = self.clean(*options)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(json.loads(result.stdout)["BytesFreed"], 0)
        self.assertTrue(all(not path.exists() for path in removed))
        self.assertTrue(all(path.read_bytes() == b"synthetic fixture" for path in kept))

    def test_linked_cache_is_rejected_before_any_deletion(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        evidence = outside / "keep.txt"
        evidence.write_bytes(b"synthetic evidence")
        cache = self.root / ".ruff_cache"
        if os.name == "nt":
            script = Path(self.temp.name) / "junction.ps1"
            script.write_text("param($LinkPath, $TargetPath)\n"
                              "New-Item -ItemType Junction -Path $LinkPath -Target $TargetPath | Out-Null\n",
                              encoding="utf-8")
            subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script),
                            "-LinkPath", str(cache), "-TargetPath", str(outside)],
                           check=True, capture_output=True, timeout=20)
        else:
            cache.symlink_to(outside, target_is_directory=True)
        build = self.file("build/keep.txt")
        result = self.clean()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("linked cleanup target", result.stderr)
        self.assertEqual(evidence.read_bytes(), b"synthetic evidence")
        self.assertTrue(build.exists(), "another target was deleted before link validation failed")
