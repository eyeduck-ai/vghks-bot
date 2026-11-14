"""The app consumes an SDK-owned display table without rewriting clinical data."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import vghks_sdk

from vghks_bot.bot import BotApplication
from vghks_bot.encoding import patient_name_display_profile
from vghks_bot.selftest_bot import BotSyntheticSDK
from vghks_bot.settings import Settings


class NameDisplayProfileTests(unittest.TestCase):
    def test_installed_sdk_provides_the_confirmed_source_character_profile(self):
        profile = patient_name_display_profile()
        self.assertEqual(profile["source"], "vghks")
        self.assertTrue(profile["version"])
        for original, displayed in (("\ue3a4", "凃"), ("\uea81", "眞"), ("\ue03e", "綉")):
            self.assertEqual(profile["mapping"].get(original), displayed)

    def test_older_sdk_has_no_guessed_application_character_table(self):
        with patch.object(vghks_sdk, "patient_name_profile", None, create=True):
            self.assertEqual(patient_name_display_profile(), {
                "source": "vghks", "version": "", "mapping": {},
            })

    def test_main_and_embedded_bootstrap_forward_the_sdk_profile(self):
        profile = {"source": "vghks", "version": "synthetic-test",
                   "mapping": {"\ue3a4": "凃", "\uea81": "眞", "\ue03e": "綉"}}
        with tempfile.TemporaryDirectory() as directory, patch.object(
                vghks_sdk, "patient_name_profile", return_value=profile, create=True) as provider:
            app = BotApplication(Settings(), Path(directory), BotSyntheticSDK)
            try:
                self.assertEqual(app.bootstrap()["patient_name_profile"], profile)
                account = app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
                work = app.workspace(account)
                self.assertEqual(work.bootstrap()["patient_name_profile"], profile)
                self.assertTrue(provider.called)
                for call in provider.call_args_list:
                    self.assertEqual(call.kwargs, {"source": "vghks"})
            finally:
                app.close()
