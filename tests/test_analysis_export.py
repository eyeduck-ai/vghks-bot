"""Report and attachment exports remain offline, portable and account/patient scoped."""
from __future__ import annotations

import http.client
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode
from zipfile import ZipFile

from vghks_sdk.models import BinaryAsset

from vghks_bot.analysis_export import archive, listing
from vghks_bot.bot import BotApplication
from vghks_bot.bot_server import BotServer
from vghks_bot.downloads import disposition, filename
from vghks_bot.selftest_bot import BotSyntheticSDK
from vghks_bot.selftest_exports import check_saved_export, seed_report
from vghks_bot.settings import Settings


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.app = BotApplication(Settings(), Path(self.folder.name), BotSyntheticSDK)
        self.key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.key)
        self.cohort, self.assets = seed_report(self.work)
        self.values = {"cohort_id": self.cohort["id"], "mrns": ["EXPORT01"]}
        BotSyntheticSDK.calls.clear()

    def tearDown(self):
        self.app.close()
        self.folder.cleanup()

    def test_offline_portable_report_original_bytes_dedup_and_grouped_numeric(self):
        other_id = self.app.login({"username": "OTHER", "password": "synthetic"})["account"]["id"]
        self.app.offline(self.key)
        BotSyntheticSDK.calls.clear()
        check_saved_export(self.work, self.app.workspace(other_id))
        self.assertEqual(BotSyntheticSDK.calls, [])

    def test_multiple_patients_and_unfetched_or_forged_member_rejected(self):
        seed_report(self.work, "EXPORT02")
        group = self.work.analysis.save_cohort({"source": "manual", "account_id": self.key,
            "name": "multiple", "mrns": "EXPORT01 EXPORT02 EMPTY"})
        rows = listing(self.work.analysis, {"cohort_id": group["id"]})["members"]
        self.assertEqual([row["available"] for row in rows], [True, True, False])
        with archive(self.work.analysis, {"cohort_id": group["id"], "mrns": ["EXPORT02", "EXPORT01", "EXPORT02"]}) as (path, _), ZipFile(path) as saved:
            manifest = json.loads(saved.read("manifest.json"))
            self.assertEqual([p["mrn"] for p in manifest["patients"]], ["EXPORT01", "EXPORT02"])
            self.assertEqual(len(saved.namelist()), len(set(saved.namelist())))
            self.assertTrue(all(not name.startswith("/") and ".." not in name.split("/") for name in saved.namelist()))
        for values in ({"cohort_id": group["id"], "mrns": ["EMPTY"]},
                       {**self.values, "mrns": ["NOT-MEMBER"]},
                       {**self.values, "mrns": []}, {**self.values, "mrns": "EXPORT01"},
                       {**self.values, "mrns": ["EXPORT01"] * 501}, {"cohort_id": group["id"]}):
            with self.subTest(values=values), self.assertRaises(ValueError), archive(self.work.analysis, values):
                pass
        self.assertEqual(BotSyntheticSDK.calls, [])

    def test_missing_blob_and_foreign_patient_blob_are_reported_without_fetch(self):
        missing = self.work.analysis.store.asset(self.assets[0]["digest"])[0]
        missing.unlink()
        foreign = self.work.analysis.store.save_asset("OTHER-PATIENT", "history-asset:other",
            BinaryAsset(b"\x89PNG\r\n\x1a\nother-patient", "image/png"), self.work.username)
        order = self.work.analysis.cataract_orders("EXPORT01")[0]
        step = self.work.analysis.store.step("EXPORT01", "history-order:" + order["id"])
        step["payload"]["assets"].append(foreign)
        self.work.analysis.store.save_step("EXPORT01", step["key"], step["kind"], step["payload"], self.work.username)
        with archive(self.work.analysis, self.values) as (path, _), ZipFile(path) as saved:
            patient = json.loads(saved.read("manifest.json"))["patients"][0]
            self.assertTrue(patient["missing"])
            self.assertEqual({file["sha256"] for file in patient["files"]}, {self.assets[1]["digest"]})
            self.assertFalse(any("other-patient" in saved.read(name).decode("utf-8", errors="ignore")
                                 for name in saved.namelist() if "/attachments/" in name))
        self.assertEqual(BotSyntheticSDK.calls, [])

    def test_account_and_assigned_fetch_account_enforced(self):
        other_id = self.app.login({"username": "OTHER", "password": "synthetic"})["account"]["id"]
        other = self.app.workspace(other_id)
        with self.assertRaises(ValueError), archive(other.analysis, self.values):
            pass
        cohort = self.work.analysis.store.document("analysis_cohorts", self.cohort["id"])
        cohort["members"][0]["account_id"] = other_id
        self.work.analysis.store.save_document("analysis_cohorts", cohort)
        with self.assertRaises(ValueError), archive(self.work.analysis, self.values):
            pass

    def test_http_download_headers_binary_original_zip_and_readonly_export(self):
        self.app.read_only = True
        with BotServer(0, self.app) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
            thread.start()
            try:
                headers = {"Cookie": f"opd_session={self.app.session_token}", "X-CSRF-Token": self.app.csrf_token,
                           "Content-Type": "application/json"}
                def request(method, path, body=None, extra=None):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                    connection.request(method, path, json.dumps(body) if body is not None else None, {**headers, **(extra or {})})
                    response = connection.getresponse()
                    value = response.status, dict(response.getheaders()), response.read()
                    connection.close()
                    return value
                prefix = f"/api/accounts/{self.key}"
                asset = self.assets[1]
                query = urlencode({"id": asset["digest"], "download": "1", "name": "病人\r\nX-Injection: ../眼科.jpg"})
                status, fields, body = request("GET", prefix + "/analysis/asset?" + query)
                self.assertEqual(status, 200)
                self.assertEqual(fields["Content-Type"], "image/jpeg")
                self.assertTrue(fields["Content-Disposition"].startswith("attachment;"))
                self.assertIn("filename*=UTF-8''", fields["Content-Disposition"])
                self.assertNotIn("X-Injection", fields)
                self.assertNotIn("\r", fields["Content-Disposition"])
                self.assertEqual(body, self.work.analysis.store.asset(asset["digest"])[0].read_bytes())
                status, fields, body = request("GET", prefix + "/analysis/asset?id=" + asset["digest"], extra={"Range": "bytes=0-2"})
                self.assertEqual((status, body), (206, b"\xff\xd8\xff"))
                self.assertTrue(fields["Content-Disposition"].startswith("inline;"))
                status, _, body = request("POST", prefix + "/analysis/export/read", self.values)
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(body)["members"][0]["available"])
                status, fields, body = request("POST", prefix + "/analysis/export", self.values)
                self.assertEqual(status, 200, body[:100])
                self.assertEqual(fields["Content-Type"], "application/zip")
                with ZipFile(io.BytesIO(body)) as saved:
                    self.assertEqual(len(json.loads(saved.read("manifest.json"))["patients"]), 1)
                status, _, _ = request("POST", prefix + "/analysis/export", self.values, {"X-CSRF-Token": "wrong"})
                self.assertEqual(status, 403)
                status, _, _ = request("GET", prefix + "/analysis/asset?id=" + asset["digest"], extra={"Cookie": ""})
                self.assertEqual(status, 401)
            finally:
                server.shutdown()
                thread.join(2)
        self.assertEqual(BotSyntheticSDK.calls, [])

    def test_download_names_are_portable_and_headers_cannot_be_injected(self):
        for bad in ("..\\report\r\nHeader: x", "CON", "nul.txt", "", "../", "a" * 200):
            name = filename(bad, "image/jpeg")
            self.assertTrue(name.endswith(".jpg"))
            self.assertNotIn("/", name)
            self.assertNotIn("\\", name)
            self.assertLessEqual(len(name), 125)
            self.assertNotIn("\r", disposition(name, download=True))


if __name__ == "__main__":
    unittest.main()
