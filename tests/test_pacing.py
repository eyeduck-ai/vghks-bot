"""Exercise the SDK HTTP transport with fake replies and a recording clock."""
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from requests import Response

from vghks_bot.scanner import create_sdk
from vghks_bot.settings import load_defaults

URL = "https://example.invalid/offline-test"


def response(status=200, retry_after=None):
    value = Response()
    value.status_code = status
    value.url = URL
    value._content = b"offline test"
    value._content_consumed = True
    if retry_after is not None:
        value.headers["Retry-After"] = retry_after
    return value


class PacingTests(unittest.TestCase):
    def test_new_defaults_reduce_added_wait_in_real_transport(self):
        totals = []
        for delay in ((0.8, 1.8), None):
            settings = replace(load_defaults(), username="TEST", password="synthetic")
            if delay:
                settings = replace(settings, min_delay_seconds=delay[0], max_delay_seconds=delay[1])
            waits = []
            with create_sdk(settings) as sdk:
                transport = sdk._runtime.transport
                transport.sleeper = waits.append
                transport.rng = Mock(uniform=lambda low, high: (low + high) / 2)
                with patch.object(transport.session, "request", side_effect=lambda *a, **kw: response()) as http:
                    for _ in range(100):
                        self.assertEqual(transport.request("GET", URL).status_code, 200)
                    self.assertEqual(http.call_count, 100)
            self.assertEqual(len(waits), 100)
            totals.append(sum(waits))
        self.assertAlmostEqual(totals[0], 130)
        self.assertAlmostEqual(totals[1], 10)

    def test_zero_added_wait_still_honors_retry_after(self):
        settings = load_defaults().update({"username": "TEST", "password": "synthetic",
            "min_delay_seconds": 0, "max_delay_seconds": 0})
        waits = []
        with create_sdk(settings) as sdk:
            transport = sdk._runtime.transport
            transport.sleeper = waits.append
            with patch.object(transport.session, "request", side_effect=[response(429, "2"), response()]) as http:
                self.assertEqual(transport.request("GET", URL).status_code, 200)
                self.assertEqual(http.call_count, 2)
        self.assertEqual(waits, [2])

    def test_zero_added_wait_keeps_failure_backoff(self):
        settings = load_defaults().update({"username": "TEST", "password": "synthetic",
            "min_delay_seconds": 0, "max_delay_seconds": 0})
        waits = []
        with create_sdk(settings) as sdk:
            transport = sdk._runtime.transport
            transport.sleeper = waits.append
            transport.rng = Mock(uniform=lambda low, high: (low + high) / 2)
            with patch.object(transport.session, "request", side_effect=[response(503), response()]) as http:
                self.assertEqual(transport.request("GET", URL).status_code, 200)
                self.assertEqual(http.call_count, 2)
        self.assertEqual(waits, [1.25])
