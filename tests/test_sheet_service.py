import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from vghks_bot.sheet_service import SheetService


class SheetServiceTests(unittest.TestCase):
    def test_read_only_service_does_not_reclaim_cloud_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            app = SimpleNamespace(store=SimpleNamespace(directory=Path(directory)), lock=threading.RLock(), read_only=True)
            service = SheetService(app)
            source, client = Mock(), Mock(key="synthetic-spreadsheet")
            service.receipt_sources.append(source)
            service.client_factory = lambda: client
            self.assertEqual(service.sheet_operation(lambda _: "read"), "read")
            source.assert_not_called()
            client.write.assert_not_called()

    def test_client_creation_and_close_failures_release_account_and_sheet_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            app = SimpleNamespace(store=SimpleNamespace(directory=Path(directory)), lock=threading.RLock())
            service = SheetService(app)
            service.client_factory = Mock(side_effect=ValueError("synthetic configuration error"))
            with self.assertRaises(ValueError):
                service.sheet_operation(lambda _: None)
            self.assertFalse(service.busy)
            self.assertFalse(service.lock.locked())
            client = Mock(key="synthetic-spreadsheet")
            client.close.side_effect = RuntimeError("synthetic close failure")
            service.client_factory = lambda: client
            with self.assertRaises(RuntimeError):
                service.sheet_operation(lambda _: "done")
            self.assertFalse(service.busy)
            self.assertFalse(service.lock.locked())
