"""Synthetic patient selection and batch handoff UI regression."""
import shutil
import subprocess
import unittest
from pathlib import Path


class HandoffSelectionUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for UI regression checks")
    def test_selection_and_batch_handoff(self):
        result = subprocess.run(
            ["node", "tests/handoff_selection_ui.js"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertIn("Handoff selection UI checks passed", result.stdout)
