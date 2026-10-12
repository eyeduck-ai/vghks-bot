"""Synthetic DOM checks for independent online surgery-sheet management."""
import shutil
import subprocess
import unittest
from pathlib import Path


class SheetManagementUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for UI regression checks")
    def test_management_flows(self):
        result = subprocess.run(
            ["node", "tests/sheet_management_ui.js"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertIn("Sheet management UI checks passed", result.stdout)
