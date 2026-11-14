"""Synthetic UI regression for repeatable, separately downloadable earnings exports."""
import shutil
import subprocess
import unittest
from pathlib import Path


class EarningsExportUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for UI regression checks")
    def test_export_split_reselection_and_request_lifecycle(self):
        result = subprocess.run(
            ["node", "tests/earnings_export_ui.js"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertIn(
            "Earnings export splitting, reselection, cancellation and retry checks passed",
            result.stdout,
        )


if __name__ == "__main__":
    unittest.main()
