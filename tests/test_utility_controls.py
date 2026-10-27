"""Focused, synthetic utility control lifecycle and confirmation regression."""
import shutil
import subprocess
import unittest
from pathlib import Path


class UtilityControlsUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for UI regression checks")
    def test_clocks_dock_and_comparison_confirmation(self):
        result = subprocess.run(
            ["node", "tests/utility_controls_ui.js"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertIn("Utility control clocks, dock placement, confirmations and selection isolation checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
