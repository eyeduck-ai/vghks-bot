"""The legacy entry points execute the same parser as the standalone service."""
import unittest

from vghks_sheet_core import calendar, identity, schema, state

from vghks_bot import calendar_sync, sheet_identity, sheet_schema, sheet_sync_state


class SharedCoreTests(unittest.TestCase):
    def test_main_uses_shared_implementations(self):
        self.assertIs(sheet_schema, schema)
        self.assertIs(sheet_identity, identity)
        self.assertIs(sheet_sync_state, state)
        self.assertIs(calendar_sync, calendar)
