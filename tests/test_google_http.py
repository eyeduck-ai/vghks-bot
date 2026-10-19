"""Synthetic Google transport and patch behavior without live patient data."""
import unittest
from unittest.mock import Mock

import requests
from google.auth.exceptions import RefreshError

from vghks_bot.google_http import GoogleHTTP
from vghks_bot.google_sheets import SheetError, SheetsClient, SheetUncertain


def response(status, reason=""):
    return Mock(status_code=status, ok=status < 400,
                json=Mock(return_value={"error": {"errors": [{"reason": reason}]}} if reason else {}))


class GoogleHTTPTests(unittest.TestCase):
    def client(self):
        session, sleep = Mock(), Mock()
        client = SheetsClient("synthetic", session)
        client.http = GoogleHTTP(session, sleep=sleep, jitter=lambda: 0)
        return client, session, sleep

    def test_sheets_403_rate_limit_retries_without_permission_error(self):
        client, session, sleep = self.client()
        session.request.side_effect = [response(403, "rateLimitExceeded"), response(429), response(200)]
        self.assertEqual(client.request("GET", ""), {})
        self.assertEqual(client.http.requests, 3)
        self.assertEqual(client.http.retries, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.3, 0.6])

    def test_quota_exhaustion_and_real_permission_failure_stay_distinct(self):
        client, session, _ = self.client()
        session.request.return_value = response(403, "userRateLimitExceeded")
        with self.assertRaises(SheetError) as failed:
            client.request("GET", "")
        self.assertEqual(failed.exception.code, "rate_limit")
        self.assertTrue(failed.exception.retryable)
        self.assertNotIn("授權失敗", str(failed.exception))
        session.request.reset_mock()
        session.request.return_value = response(403, "forbidden")
        with self.assertRaises(SheetError) as failed:
            client.request("GET", "")
        self.assertEqual(failed.exception.code, "permission")
        session.request.assert_called_once()

    def test_mutation_retries_only_a_definite_quota_rejection(self):
        client, session, _ = self.client()
        session.request.side_effect = [response(403, "rateLimitExceeded"), response(200)]
        client.write([])
        self.assertEqual(session.request.call_count, 2)
        session.request.reset_mock()
        session.request.side_effect = [response(503)]
        with self.assertRaises(SheetUncertain):
            client.write([])
        session.request.assert_called_once()

    def test_timeout_on_mutation_is_not_replayed_and_reads_are_bounded(self):
        client, session, _ = self.client()
        session.request.side_effect = requests.Timeout()
        with self.assertRaises(SheetUncertain):
            client.write([])
        session.request.assert_called_once()
        session.request.reset_mock()
        with self.assertRaises(SheetError):
            client.request("GET", "")
        self.assertEqual(session.request.call_count, 3)

    def test_refresh_permission_failure_is_not_reported_as_retryable_transport(self):
        client, session, _ = self.client()
        session.request.side_effect = RefreshError("synthetic invalid credential")
        with self.assertRaises(SheetError) as failed:
            client.request("GET", "")
        self.assertEqual(failed.exception.code, "permission")
        self.assertFalse(failed.exception.retryable)
        session.request.assert_called_once()

    def test_pause_checkpoint_stops_a_pending_read_retry(self):
        client, session, _ = self.client()
        session.request.return_value = response(503)
        client.http.checkpoint = Mock(side_effect=[None, SheetError("paused", code="paused")])
        with self.assertRaises(SheetError) as failed:
            client.request("GET", "")
        self.assertEqual(failed.exception.code, "paused")
        session.request.assert_called_once()
