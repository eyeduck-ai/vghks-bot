"""New SDK attendance evidence, cache migration and source-preserving UI."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from vghks_bot.bot import BotApplication
from vghks_bot.review import REGISTRATION_SCHEMA
from vghks_bot.selftest_bot import wait_task
from vghks_bot.selftest_registrations import MRN, RegistrationScenario, check_sdk_registrations
from vghks_bot.settings import Settings


class RegistrationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.scenario = RegistrationScenario()
        self.app = BotApplication(Settings(), self.path, self.scenario.factory)
        self.account = self.app.login({"username": "SYNTHETIC", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.account)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def query(self, **options):
        key = self.work.review.start({"kind": "registrations", "mrn": MRN, **options})["task_id"]
        return wait_task(self.work, key)

    def legacy(self, payload=None):
        return self.work.review.db.save("registrations", {"id": MRN, "mrn": MRN,
            "payload": payload if payload is not None else [{"mrn": MRN, "status": "原始狀態"}],
            "status": "ready", "cached": False})

    def test_real_sdk_through_source_and_frozen_selftest_path(self):
        check_sdk_registrations(self.path / "selftest")

    def test_fresh_legacy_cache_is_refreshed_once_without_losing_source_columns(self):
        self.legacy()
        result = self.query()
        self.assertEqual(result["status"], "completed")
        saved, = result["items"]
        self.assertEqual(saved["registration_schema"], REGISTRATION_SCHEMA)
        self.assertEqual(saved["sdk_version"], "0.23.5")
        self.assertEqual([r["no_show"] for r in saved["payload"]], [None, True, None, None, None])
        self.assertEqual(saved["payload"][0]["registered_by"], "MWEB")
        self.assertEqual(saved["payload"][0]["columns"]["院方新增欄位"], "新欄內容 <保留>")
        reused, = self.query()["items"]
        self.assertTrue(reused["cached"])
        self.assertEqual(len(self.scenario.queries), 1)
        self.query(force=True)
        self.assertEqual(len(self.scenario.queries), 2)
        encoded = json.dumps(saved)
        self.assertIn('"no_show": true', encoded)
        self.assertIn('"no_show": null', encoded)

    def test_empty_legacy_cache_also_refreshes_and_new_empty_result_is_cacheable(self):
        self.legacy([])
        self.scenario.empty = True
        result = self.query()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["items"][0]["payload"], [])
        self.assertEqual(self.query()["items"][0]["payload"], [])
        self.assertEqual(len(self.scenario.queries), 1)

    def test_failed_refresh_preserves_legacy_data_for_offline_reading(self):
        old = self.legacy()
        self.scenario.broken = True
        result = self.query()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.work.review.db.get("registrations", MRN), old)
        self.app.logout(self.account)
        self.assertEqual(self.work.review.db.get("registrations", MRN)["payload"], old["payload"])

    def test_foreign_patient_is_rejected_before_replacing_cache(self):
        old = self.legacy()
        self.scenario.response_mrn = "00099999"
        result = self.query()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "WEBMAAS_PATIENT_MISMATCH")
        self.assertEqual(self.work.review.db.get("registrations", MRN), old)

    def test_registration_cache_stays_with_its_account(self):
        self.query()
        other_id = self.app.login({"username": "OTHER", "password": "synthetic"})["account"]["id"]
        self.assertIsNone(self.app.workspace(other_id).review.db.get("registrations", MRN, required=False))

    def test_registration_display_does_not_infer_attendance_or_account_meaning(self):
        subprocess.run(["node", "tests/registration_history_ui.js"],
            cwd=Path(__file__).resolve().parents[1], check=True, timeout=20)
