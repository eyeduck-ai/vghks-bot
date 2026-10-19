"""The legacy entry points execute the same parser as the standalone service."""
import json
import unittest
from pathlib import Path

from vghks_sheet_core import identity, schema, state
from vghks_sheet_core.contract import CONTRACT_VERSION

from vghks_bot import sheet_identity, sheet_schema, sheet_sync_state


class SharedCoreTests(unittest.TestCase):
    def test_main_uses_shared_implementations(self):
        self.assertIs(sheet_schema, schema)
        self.assertIs(sheet_identity, identity)
        self.assertIs(sheet_sync_state, state)
        manifest = json.loads((Path(__file__).resolve().parent / "fixtures/gas_contract.json").read_text())
        self.assertEqual(CONTRACT_VERSION, manifest["contract_version"])
