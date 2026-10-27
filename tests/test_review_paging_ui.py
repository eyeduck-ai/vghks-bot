"""Browser-state regressions for bounded patient pages and lazy SOAP details."""
import subprocess
import unittest
from pathlib import Path


class ReviewPagingUITests(unittest.TestCase):
    def test_pages_details_and_late_responses_keep_their_account_task_scope(self):
        subprocess.run(["node", "tests/review_paging_ui.js"],
                       cwd=Path(__file__).resolve().parents[1], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20)
