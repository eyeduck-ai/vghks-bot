"""A foreign archive can never acquire the fixed slot or write through it."""
import unittest
from unittest.mock import Mock

from analysis_fixtures import MemorySheets

from vghks_bot.google_sheets import SheetError, SheetsClient
from vghks_bot.sheet_archive_lock import (
    LOCK_ID,
    acquire_request,
    archive_lock,
    assert_archive_available,
    release_request,
)


class ArchiveLockTests(unittest.TestCase):
    def test_atomic_acquisition_rejects_second_owner_and_wrong_owner_cannot_release(self):
        client = MemorySheets({"sheets": [], "developerMetadata": []})
        client.write([acquire_request("MAIN")])
        with self.assertRaises(SheetError):
            client.write([acquire_request("GAS")])
        client.write([release_request("GAS")])
        self.assertEqual(archive_lock(client.book), "MAIN")
        client.write([release_request("MAIN")])
        self.assertEqual(archive_lock(client.book), "")

    def test_occupied_reserved_id_is_not_repurposed(self):
        with self.assertRaises(SheetError):
            assert_archive_available({"developerMetadata": [{"metadataId": LOCK_ID, "metadataKey": "another-owner"}]})

    def test_real_client_blocks_post_before_mutation_and_allows_only_archive_owner(self):
        client = SheetsClient("synthetic", Mock())
        client.read_metadata = Mock(return_value={"developerMetadata": [acquire_request("GAS")["createDeveloperMetadata"]["developerMetadata"]]})
        client.request = Mock(return_value={})
        with self.assertRaises(SheetError):
            client.write([{"deleteSheet": {"sheetId": 1}}])
        client.request.assert_not_called()
        client.archive_owner = "GAS"
        client.write([{"deleteSheet": {"sheetId": 1}}])
        self.assertEqual(client.request.call_args.args[:2], ("POST", ":batchUpdate"))
