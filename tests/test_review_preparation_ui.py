"""Finite background preparation controls must keep explicit intent and scope."""
import subprocess
import unittest
from pathlib import Path


class ReviewPreparationUITests(unittest.TestCase):
    def test_explicit_scopes_permissions_progress_and_stale_contexts(self):
        subprocess.run(["node", "tests/review_preparation_ui.js"],
                       cwd=Path(__file__).resolve().parents[1], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20)
