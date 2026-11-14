"""Patient-name display uses an SDK profile without rewriting clinical source data."""
import shutil
import subprocess
import unittest
from pathlib import Path


class PatientNameUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for UI regression checks")
    def test_profile_display_copy_and_source_preservation(self):
        result = subprocess.run(
            ["node", "tests/patient_name_ui.js"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertIn(
            "SDK-configured patient names, source isolation, safe rendering and raw preservation checks passed",
            result.stdout,
        )


if __name__ == "__main__":
    unittest.main()
