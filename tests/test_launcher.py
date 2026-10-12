import http.client
import io
import json
import secrets
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from vghks_bot import app
from vghks_bot.bot_server import BotServer
from vghks_bot.databases import DatabaseManager, ManagerInUseError
from vghks_bot.launcher import (
    CHALLENGE_HEADER,
    INSTANCE_FILE,
    REOPEN_HEADER,
    REOPEN_PATH,
    publish_instance,
    running_url,
    signature,
)
from vghks_bot.selftest_launcher import check_launcher
from vghks_bot.settings import Settings
from vghks_bot.storage import StorageError, atomic_json


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.manager = DatabaseManager(self.home, Settings())
        self.addCleanup(self.manager.close)
        self.server = BotServer(0, self.manager.current, self.manager)
        self.addCleanup(self.server.server_close)
        publication = publish_instance(self.home, self.server)
        publication.__enter__()
        self.addCleanup(publication.__exit__, None, None, None)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = f"{self.server.origin}/#{self.manager.current.launch_token}"

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(3)

    def test_repeated_main_opens_current_url_without_starting_or_closing_server(self):
        before = self.manager.catalog_path.read_bytes()
        with (patch("sys.argv", ["VGHKS-bot.exe", "--data-dir", str(self.home), "--port", "1"]),
              patch("sys.stdout", new_callable=io.StringIO),
              patch.object(app, "BotServer") as second_server,
              patch.object(app.webbrowser, "open", return_value=True) as browser):
            self.assertEqual(app.main(), 0)
            self.assertEqual(app.main(), 0)
            self.assertEqual(browser.call_count, 2)
            browser.assert_called_with(self.url)
            second_server.assert_not_called()
        self.assertTrue(self.thread.is_alive())
        self.assertEqual(running_url(self.home), self.url)
        self.assertEqual(self.manager.catalog_path.read_bytes(), before)

    def test_no_browser_reuses_service_and_reports_url(self):
        with (patch("sys.argv", ["VGHKS-bot.exe", "--data-dir", str(self.home), "--no-browser"]),
              patch("sys.stdout", new_callable=io.StringIO) as output,
              patch.object(app.webbrowser, "open") as browser):
            self.assertEqual(app.main(), 0)
            browser.assert_not_called()
            self.assertIn(self.url, output.getvalue())

    def test_waits_for_first_process_to_publish_its_endpoint(self):
        path = self.home / INSTANCE_FILE
        record = path.read_bytes()
        path.unlink()
        with patch.object(app.time, "sleep", side_effect=lambda _: path.write_bytes(record)) as sleep:
            manager, url = app.connect_or_start(self.home, Settings())
        self.assertIsNone(manager)
        self.assertEqual(url, self.url)
        sleep.assert_called_once()

    def test_database_switch_in_progress_is_retried(self):
        lease = self.server.request_gate.lease(exclusive=True)
        lease.__enter__()
        released = False

        def finish_switch(_):
            nonlocal released
            lease.__exit__(None, None, None)
            released = True

        try:
            with patch.object(app.time, "sleep", side_effect=finish_switch) as sleep:
                manager, url = app.connect_or_start(self.home, Settings())
            self.assertIsNone(manager)
            self.assertEqual(url, self.url)
            sleep.assert_called_once()
        finally:
            if not released:
                lease.__exit__(None, None, None)

    def test_discovery_rejects_missing_wrong_and_cross_site_credentials(self):
        challenge = secrets.token_urlsafe(32)
        valid = {CHALLENGE_HEADER: challenge,
                 REOPEN_HEADER: signature(self.server.launcher_token, challenge)}
        for headers, expected in (({}, 401), ({**valid, REOPEN_HEADER: "a" * 64}, 401),
                                  ({**valid, CHALLENGE_HEADER: "invalid"}, 401),
                                  ({**valid, "Host": "evil.example"}, 403),
                                  ({**valid, "Origin": "http://evil.example"}, 403),
                                  ({**valid, "Sec-Fetch-Site": "cross-site"}, 403)):
            with self.subTest(headers=headers), closing(http.client.HTTPConnection(
                    "127.0.0.1", self.server.server_port, timeout=3)) as connection:
                connection.request("GET", REOPEN_PATH, headers=headers)
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                self.assertNotIn(self.manager.current.launch_token.encode(), response.read())

    def test_invalid_or_relocated_discovery_never_connects(self):
        record = json.loads((self.home / INSTANCE_FILE).read_text(encoding="utf-8"))
        cases = [[], None, {**record, "home": str(self.home / "other")},
                 {**record, "port": True}, {**record, "port": 0}, {**record, "port": 65536},
                 {**record, "port": "http://example.com"}, {**record, "secret": "invalid"}]
        for data in cases:
            with self.subTest(data=data):
                atomic_json(self.home / INSTANCE_FILE, data)
                with patch("vghks_bot.launcher.http.client.HTTPConnection") as connection:
                    self.assertIsNone(running_url(self.home))
                    connection.assert_not_called()
        (self.home / INSTANCE_FILE).write_text("{invalid", encoding="utf-8")
        self.assertIsNone(running_url(self.home))

    def test_stale_secret_and_wrong_server_proof_are_rejected(self):
        record = json.loads((self.home / INSTANCE_FILE).read_text(encoding="utf-8"))
        atomic_json(self.home / INSTANCE_FILE, {**record, "secret": secrets.token_urlsafe(32)})
        self.assertIsNone(running_url(self.home))
        atomic_json(self.home / INSTANCE_FILE, record)
        for status, body in ((302, {}), (200, {"application": "Other"}),
                             (200, {"application": "VGHKS-bot", "launch_token": "http://example.com"}),
                             (200, {"application": "VGHKS-bot", "launch_token": secrets.token_urlsafe(32),
                                    "proof": "0" * 64})):
            with self.subTest(status=status, body=body), patch(
                    "vghks_bot.launcher.http.client.HTTPConnection") as connection:
                connection.return_value.getresponse.return_value = Mock(
                    status=status, read=Mock(return_value=json.dumps(body).encode()))
                self.assertIsNone(running_url(self.home))
                connection.return_value.close.assert_called_once()


class StartupTests(unittest.TestCase):
    def test_frozen_selftest_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            check_launcher(directory)

    def test_owner_exits_during_startup_wait_then_this_process_starts(self):
        manager = Mock()
        with (patch.object(app, "DatabaseManager", side_effect=[ManagerInUseError(), manager]),
              patch.object(app, "running_url", return_value=None),
              patch.object(app.time, "sleep") as sleep):
            self.assertEqual(app.connect_or_start(Path("unused"), Settings()), (manager, None))
            sleep.assert_called_once()

    def test_unresponsive_owner_preserves_lock_and_gives_actionable_error(self):
        with (patch.object(app, "DatabaseManager", side_effect=ManagerInUseError()) as manager,
              patch.object(app, "running_url", return_value=None),
              patch.object(app.time, "sleep") as sleep):
            with self.assertRaisesRegex(StorageError, "稍候再雙擊 EXE"):
                app.connect_or_start(Path("unused"), Settings(), timeout=0)
            manager.assert_called_once()
            sleep.assert_not_called()

    def test_other_storage_failures_do_not_reconnect(self):
        with (patch.object(app, "DatabaseManager", side_effect=StorageError("synthetic disk failure")),
              patch.object(app, "running_url") as discover):
            with self.assertRaisesRegex(StorageError, "synthetic disk failure"):
                app.connect_or_start(Path("unused"), Settings())
            discover.assert_not_called()

    def test_stale_record_is_replaced_and_cleaned_on_exit_or_server_error(self):
        for error in (None, OSError("synthetic server failure")):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                (home / INSTANCE_FILE).write_text("stale record", encoding="utf-8")

                def opened(url, *, home=home, **_):
                    record = json.loads((home / INSTANCE_FILE).read_text(encoding="utf-8"))
                    self.assertIn(f":{record['port']}/#", url)

                with (patch("sys.argv", ["VGHKS-bot.exe", "--data-dir", str(home)]),
                      patch("sys.stderr", new_callable=io.StringIO),
                      patch.object(app, "open_interface", side_effect=opened) as browser,
                      patch.object(BotServer, "serve_forever", side_effect=error)):
                    self.assertEqual(app.main(), 1 if error else 0)
                    browser.assert_called_once()
                self.assertFalse((home / INSTANCE_FILE).exists())
                with closing(DatabaseManager(home, Settings())):
                    pass

    def test_browser_failure_shows_copyable_url(self):
        for error in (False, OSError("synthetic browser failure")):
            with (self.subTest(error=error), patch.object(app.sys, "platform", "win32"),
                  patch("sys.stdout", new_callable=io.StringIO),
                  patch.object(app.webbrowser, "open", **({"side_effect": error} if error else {"return_value": False})),
                  patch.object(app.ctypes, "windll", create=True) as windll):
                app.open_interface("http://127.0.0.1:1234/#synthetic")
                self.assertIn("http://127.0.0.1:1234/#synthetic", windll.user32.MessageBoxW.call_args.args[1])
