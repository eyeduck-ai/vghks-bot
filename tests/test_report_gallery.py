"""Synthetic browser DOM checks for incremental report attachments."""
import io
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from vghks_bot.server import Handler


class ReportGalleryTests(unittest.TestCase):
    def test_incremental_original_view_and_local_thumbnail_lifecycle(self):
        subprocess.run(["node", "tests/report_gallery_ui.js"],
                       cwd=Path(__file__).resolve().parents[1], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20)

    def test_local_thumbnail_blobs_do_not_broaden_other_content_security_directives(self):
        headers = {}
        response = SimpleNamespace(command="GET", wfile=io.BytesIO(), send_response=Mock(),
                                   send_header=lambda name, value: headers.update({name: value}),
                                   end_headers=Mock())
        Handler.reply(response, 200, b"synthetic HTML", mime="text/html; charset=utf-8")
        directives = dict(part.strip().split(" ", 1)
                          for part in headers["Content-Security-Policy"].split(";"))
        self.assertEqual(directives.pop("img-src"), "'self' data: blob:")
        self.assertEqual(directives, {
            "default-src": "'self'", "script-src": "'self'", "style-src": "'self'",
            "connect-src": "'self'", "frame-ancestors": "'none'", "base-uri": "'none'",
            "form-action": "'self'",
        })
