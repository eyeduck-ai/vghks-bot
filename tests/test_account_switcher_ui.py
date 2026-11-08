"""Synthetic account presentation and asynchronous switching regression checks."""
import shutil
import subprocess
import unittest
from pathlib import Path


class AccountSwitcherUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for UI regression checks")
    def test_account_cards_and_switching_lifecycle(self):
        result = subprocess.run(
            ["node", "tests/account_switcher_ui.js"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Account cards, switching isolation, navigation memory and cancellation checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
