"""Cataract PDF scope, background progress and per-document outcomes."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from async_helpers import join_threads, wait_event, wait_until
from test_cataract_reads import seed_patients
from vghks_sdk.models import BinaryAsset, PdfAttachmentRef, ScannedRecord

from vghks_bot.analysis_fetch import clean
from vghks_bot.bot import BotApplication
from vghks_bot.jobs import BusyError
from vghks_bot.scanned_records import scan_id
from vghks_bot.selftest_biometry import synthetic_biometry_pdf
from vghks_bot.selftest_bot import BotSyntheticSDK
from vghks_bot.settings import Settings


class CataractBiometryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.app = BotApplication(Settings(), self.directory, BotSyntheticSDK)
        self.addCleanup(self.app.close)
        account = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(account)
        self.analysis = self.work.analysis
        cohort = seed_patients(self.work, count=2, order_count=2)
        self.scope = {"cohort_id": cohort["id"], "mrn": "TEST001", "source": "orders"}
        self.orders = self.analysis.cataract_orders(self.scope["mrn"])

    def request(self, action, **values):
        return self.analysis.handle("cataract/biometry", {**self.scope, "action": action, **values})

    def wait_for_result(self, job, **scope):
        def completed():
            value = self.request("status", job_id=job["job_id"], **scope)
            if value["state"] not in {"queued", "running"}:
                return value
            return None

        return wait_until(completed, label="biometry extraction finished")

    def save_scans(self, *refs):
        self.analysis.store.save_step(self.scope["mrn"], "scans-history", "scan_index",
                                      {"schema": 2, "records": clean([ScannedRecord(None, ref) for ref in refs])},
                                      self.work.username)

    def capture_order(self):
        order = self.orders[0]
        selection = {"id": order["id"], "digest": order["attachments"][0]["digest"]}
        data = {"eyes": {"OD": {"al": {"candidate_value": 23.62}}}, "metadata": {"selected_pages": [2]}}
        with patch("vghks_bot.biometry.extract_for_review", return_value=(data, {})):
            job = self.request("start", mode="selected", view="inline", documents=[{**selection, "pages": [2]}])
            result = self.wait_for_result(job)["result"]["documents"][0]
        return selection, result

    def test_reopened_application_loads_capture_and_confirmed_draft_without_extraction(self):
        selection, original = self.capture_order()
        self.assertEqual(original["saved_review"], {"revision": 0, "confirmed": False, "edits": []})
        confirmed = {"confirmed": True, "edits": [["eyes.OD.al", {"raw": "23.85", "value": 23.85}]]}
        response = self.request("save_review", documents=[selection], capture_id=original["capture_id"], revision=0,
                                review=confirmed)
        self.assertEqual(response, {"state": "saved", "saved_review": {"revision": 1, **confirmed}})
        self.assertEqual(self.request("status")["result"]["documents"][0]["saved_review"], response["saved_review"])
        self.app.close()
        reopened = BotApplication(Settings(), self.directory, BotSyntheticSDK)
        self.addCleanup(reopened.close)
        account = reopened.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.analysis = reopened.workspace(account).analysis
        self.assertEqual(self.request("status"), {"state": "idle"})
        with patch("vghks_bot.biometry.extract_for_review") as extract:
            cached = self.request("load", documents=[selection])
            extract.assert_not_called()
        document = cached["document"]
        self.assertEqual((cached["state"], cached["category"], document["pages"]), ("saved", "all", [2]))
        self.assertEqual(document["capture_id"], original["capture_id"])
        self.assertEqual(document["data"], original["data"])
        self.assertEqual(document["saved_review"], response["saved_review"])
        with self.assertRaises(ValueError):
            self.request("load", documents=[selection], mrn="TEST002")
        reopened.read_only = True
        self.assertEqual(self.request("load", documents=[selection]), cached)
        with self.assertRaisesRegex(ValueError, "唯讀"):
            self.request("save_review", documents=[selection], capture_id=original["capture_id"], revision=1,
                         review=confirmed)

    def test_draft_version_conflicts_stale_capture_and_clear_are_transactional(self):
        selection, captured = self.capture_order()
        capture_id = captured["capture_id"]
        unconfirmed = {"confirmed": False, "edits": [["eyes.OD.al", {"raw": "?"}]]}

        def save(draft, revision=0, **options):
            return self.request("save_review", documents=[selection], capture_id=capture_id, revision=revision,
                                review=draft, **options)

        saved = save(unconfirmed)
        self.assertEqual(save(unconfirmed), saved, "network retry must not create a new revision")
        self.assertEqual(saved["saved_review"], {"revision": 1, **unconfirmed})
        self.assertEqual(save({"confirmed": True, "edits": []}), {**saved, "state": "conflict"})
        with self.assertRaisesRegex(ValueError, "格式有誤"):
            save({**unconfirmed, "confirmed": True}, revision=1)
        with self.assertRaisesRegex(ValueError, "欄位格式"):
            save({"confirmed": False, "edits": [["__proto__", {"raw": "unsafe"}]]}, revision=1)
        _, rerun = self.capture_order()
        self.assertEqual(save(unconfirmed, revision=1), {"state": "changed"})
        clear = {"mrns": [self.scope["mrn"]], "categories": ["analysis"]}
        preview = self.work.library_data.preview(clear)
        self.request("save_review", documents=[selection], capture_id=rerun["capture_id"], revision=0,
                     review={"confirmed": True, "edits": []})
        with self.assertRaisesRegex(ValueError, "資料已變更"):
            self.work.library_data.delete({**clear, "fingerprint": preview["fingerprint"]})
        self.work.library_data.delete(clear, compatibility=True)
        self.assertEqual(self.request("load", documents=[selection]), {"state": "missing"})
        self.assertEqual(self.request("status"), {"state": "idle"})

    def test_changed_sources_do_not_reuse_previous_pdf_captures(self):
        selection, capture = self.capture_order()
        mrn, store = self.scope["mrn"], self.analysis.store
        key = "collected-order:" + selection["id"]
        original = store.step(mrn, key)["payload"]
        changed = store.save_asset(mrn, "synthetic-changed-attachment", BinaryAsset(b"%PDF-new", "application/pdf"),
                                   self.work.username)
        new = {**selection, "digest": changed["digest"]}
        store.save_step(mrn, key, "order_report", {**original, "assets": [changed, *original["assets"]]}, self.work.username)
        self.assertEqual(self.request("load", documents=[new]), {"state": "missing"}, "added file must not look replaced")
        store.save_step(mrn, key, "order_report", {**original, "assets": [changed]}, self.work.username)
        self.assertEqual(self.request("load", documents=[new]), {"state": "changed"})
        self.assertEqual(self.request("load", documents=[selection]), {"state": "changed"})
        self.assertEqual(self.request("save_review", documents=[selection], capture_id=capture["capture_id"], revision=0,
                                      review={"confirmed": True, "edits": []}), {"state": "changed"})

        ref = PdfAttachmentRef(mrn, f"//HFS01_1A0/EMRU/{mrn}/biometry-change.pdf")
        self.save_scans(ref)
        scan = scan_id(ref)
        scan_key = "scan-asset:" + scan
        old = store.save_asset(mrn, scan_key, BinaryAsset(b"%PDF-previous", "application/pdf"), self.work.username)
        pick = {"source": "scans", "documents": [{"id": scan, "digest": old["digest"]}]}
        with patch("vghks_bot.biometry.extract_for_review", return_value=({"pages": []}, {})):
            self.wait_for_result(self.request("start", mode="selected", **pick), source="scans")
        self.assertEqual(self.request("load", **pick)["state"], "saved")
        replacement = store.save_asset(mrn, scan_key, BinaryAsset(b"%PDF-replacement", "application/pdf"), self.work.username)
        self.assertEqual(self.request("load", **pick), {"state": "changed"})
        self.assertEqual(self.request("load", source="scans", documents=[{"id": scan, "digest": replacement["digest"]}]),
                         {"state": "changed"})

    def test_delete_during_parse_does_not_restore_a_saved_capture(self):
        order = self.orders[0]
        selection = {"id": order["id"], "digest": order["attachments"][0]["digest"]}
        entered, release = threading.Event(), threading.Event()

        def parse(*_):
            entered.set()
            wait_event(release, label="release parsing during deletion")
            return {"eyes": {}}, {}

        with patch("vghks_bot.biometry.extract_for_review", side_effect=parse):
            self.request("start", mode="selected", documents=[selection])
            try:
                wait_event(entered, label="parser started")
                self.work.library_data.delete({"mrns": [self.scope["mrn"]], "categories": ["analysis"]}, compatibility=True)
            finally:
                release.set()
            wait_until(lambda: self.analysis.biometry.worker is None, label="deleted parser stopped")
        self.assertEqual(self.request("load", documents=[selection]), {"state": "missing"})

    def test_basic_edition_rejects_extraction_before_queueing(self):
        with patch("vghks_bot.biometry.biometry_variant", return_value="basic"):
            for action in ("start", "status", "pages"):
                with self.subTest(action=action), self.assertRaisesRegex(ValueError, "基本版"):
                    self.request(action)
        self.assertEqual(self.request("status"), {"state": "idle"})

    def test_pages_are_selected_and_background_status_is_patient_scoped(self):
        order = self.orders[0]
        selected = {"id": order["id"], "digest": order["attachments"][0]["digest"]}
        with patch("vghks_bot.biometry.pdf_page_count", return_value=4) as count:
            self.assertEqual(self.request("pages", documents=[selected]), {"page_count": 4})
            count.assert_called_once_with(self.analysis.store.assets / selected["digest"])
        entered, release = threading.Event(), threading.Event()

        def parse(path, pages, category):
            self.assertEqual(path.read_bytes(), b"%PDF-1.4\nTEST001")
            self.assertEqual(category, "all")
            entered.set()
            wait_event(release, label="release biometry parse")
            return {"pages": pages, "measurements": [{"eye": "OD"}]}, {}

        with patch("vghks_bot.biometry.extract_for_review", side_effect=parse) as extract:
            job = self.request("start", mode="selected", documents=[{**selected, "pages": [4, 2, 2]}])
            try:
                wait_event(entered, label="biometry started")
                with patch.object(self.analysis, "member_demographics", side_effect=AssertionError):
                    self.assertEqual(self.request("status")["state"], "running")
                self.assertEqual(self.request("status", mrn="TEST002"), {"state": "idle"})
                with self.assertRaises(ValueError):
                    self.request("status", mrn="TEST002", job_id=job["job_id"])
                with self.assertRaises(ValueError):
                    self.request("pages", mrn="TEST002", documents=[selected])
                replay = self.request("start", mode="selected", documents=[{**selected, "pages": [2, 4]}])
                self.assertEqual(replay["job_id"], job["job_id"])
                with self.assertRaisesRegex(BusyError, "正在擷取"):
                    self.request("start", mode="selected", documents=[selected], category="iol", view="inline")
            finally:
                release.set()
            value = self.wait_for_result(job)
            extract.assert_called_once_with(self.analysis.store.assets / selected["digest"], [2, 4], "all")
        self.assertEqual(value["state"], "completed")
        self.assertEqual((value["category"], value["view"], value["result"]["category"]), ("all", "batch", "all"))
        self.assertEqual(value["result"]["documents"][0]["pages"], [2, 4])
        self.assertEqual(value["result"]["documents"][0]["data"]["measurements"], [{"eye": "OD"}])
        self.assertEqual(self.request("status"), value)

    def test_all_is_filtered_and_one_bad_selection_does_not_hide_other_files(self):
        with patch("vghks_bot.biometry.extract_for_review", return_value=({"pages": []}, {})):
            job = self.request("start", mode="all", start="2026-09-02", end="2026-09-02")
            value = self.wait_for_result(job)
        self.assertEqual([doc["date"] for doc in value["result"]["documents"]], ["2026-09-02"])
        selection = [{"id": row["id"], "digest": row["attachments"][0]["digest"], "pages": [page]}
                     for row, page in zip(self.orders, (1, 15), strict=True)]
        with patch("vghks_bot.biometry.extract_for_review", side_effect=(
                ({"pages": [1]}, {}), ValueError("頁碼超出 PDF 範圍。"))):
            value = self.wait_for_result(self.request("start", mode="selected", documents=selection))
        self.assertEqual(value["state"], "partial")
        self.assertEqual(value["processed"], 2)
        self.assertEqual(value["result"]["documents"][1]["error"], "頁碼超出 PDF 範圍。")
        self.work.library_data.delete({"mrns": [self.scope["mrn"]], "categories": ["orders"]}, compatibility=True)
        self.assertEqual(self.request("status"), {"state": "idle"})

    def test_scans_support_cached_pages_and_background_retrieval(self):
        mrn = self.scope["mrn"]
        refs = [PdfAttachmentRef(mrn, f"//HFS01_1A0/EMRU/{mrn}/{index}.pdf") for index in (1, 2)]
        self.save_scans(*refs)
        cached = self.analysis.store.save_asset(mrn, f"scan-asset:{scan_id(refs[0])}",
                                                BinaryAsset(b"%PDF-cached", "application/pdf"), self.work.username)
        with patch("vghks_bot.biometry.pdf_page_count", return_value=3):
            self.assertEqual(self.request("pages", source="scans", documents=[{"id": scan_id(refs[0])}]),
                             {"page_count": 3, "digest": cached["digest"]})
        with (patch.object(self.work.scans, "_download_ref", return_value=BinaryAsset(b"%PDF-network", "application/pdf")) as fetch,
              patch.object(self.work.scans, "_history_refs", wraps=self.work.scans._history_refs) as read_index,
              patch("vghks_bot.biometry.extract_for_review", return_value=({"pages": []}, {})) as extract):
            value = self.wait_for_result(self.request("start", source="scans", mode="all"), source="scans")
            read_index.assert_called_once_with(mrn)
            fetch.assert_called_once_with(refs[1])
            self.assertEqual({call.args[0].read_bytes() for call in extract.call_args_list}, {b"%PDF-cached", b"%PDF-network"})
            self.assertTrue(self.work.scans.download(mrn, scan_id(refs[1]))["cached"])
            fetch.assert_called_once_with(refs[1])
        self.assertEqual(value["state"], "completed")
        self.assertEqual({row["id"] for row in value["result"]["documents"]}, {scan_id(ref) for ref in refs})
        self.assertNotIn("//HFS01_1A0", str(value))

    def test_scan_api_returns_real_parser_output_for_the_selected_page(self):
        mrn = self.scope["mrn"]
        ref = PdfAttachmentRef(mrn, f"//HFS01_1A0/EMRU/{mrn}/biometry-report.pdf")
        scan = scan_id(ref)
        self.save_scans(ref)
        pdf = synthetic_biometry_pdf("25.58", "23.62", include_iol=True)
        asset = self.analysis.store.save_asset(mrn, f"scan-asset:{scan}", BinaryAsset(pdf, "application/pdf"),
                                               self.work.username)
        pick = {"source": "scans", "mode": "selected", "view": "inline", "documents": [{"id": scan, "pages": [2]}]}
        job = self.request("start", category="biometry", **pick)
        value = self.wait_for_result(job, source="scans")
        self.assertEqual(value["state"], "completed")
        self.assertEqual((value["category"], value["view"]), ("biometry", "inline"))
        doc = value["result"]["documents"][0]
        self.assertEqual(doc["digest"], asset["digest"])
        self.assertEqual(doc["review"]["eyes"]["OD"]["al"], {"pages": [2]})
        result = doc["data"]
        self.assertEqual(result["metadata"]["selected_pages"], [2])
        self.assertEqual(result["eyes"]["OD"]["al"]["candidate_value"], 23.62)
        self.assertNotIn("iol_calculations", result)
        iol_job = self.request("start", category="iol", **pick)
        iol_value = self.wait_for_result(iol_job, source="scans")
        self.assertEqual(iol_value["result"]["category"], "iol")
        iol_doc = iol_value["result"]["documents"][0]
        self.assertNotIn("eyes", iol_doc["data"])
        self.assertNotIn("review", iol_doc)
        iol = iol_doc["data"]["iol_calculations"][0]
        self.assertEqual((iol["page_number"], iol["lens_model"], iol["rows"][0]["iol_power"]), (2, "SYNTHETIC-LENS", 20.5))

    def test_invalid_category_and_inline_batch_do_not_queue_a_job(self):
        for category in ("unknown", []):
            with self.subTest(category=category), self.assertRaisesRegex(ValueError, "擷取類型"):
                self.request("start", mode="all", category=category)
        with self.assertRaisesRegex(ValueError, "擷取檢視方式"):
            self.request("start", mode="all", view="unknown")
        with self.assertRaisesRegex(ValueError, "一份 PDF"):
            self.request("start", mode="all", view="inline")
        self.assertEqual(self.request("status"), {"state": "idle"})

    def test_scan_page_selection_rejects_replacement_content(self):
        mrn = self.scope["mrn"]
        ref = PdfAttachmentRef(mrn, f"//HFS01_1A0/EMRU/{mrn}/replaced.pdf")
        scan, store = scan_id(ref), self.analysis.store
        self.save_scans(ref)
        store.save_asset(mrn, f"scan-asset:{scan}", BinaryAsset(synthetic_biometry_pdf("23.62"), "application/pdf"),
                         self.work.username)
        pages = self.request("pages", source="scans", documents=[{"id": scan}])
        store.save_asset(mrn, f"scan-asset:{scan}", BinaryAsset(synthetic_biometry_pdf("24.62"), "application/pdf"),
                         self.work.username)
        picked = {"id": scan, "digest": pages["digest"], "pages": [1]}
        with patch("vghks_bot.biometry.extract_for_review") as parse:
            result = self.wait_for_result(self.request("start", source="scans", mode="selected", documents=[picked]),
                                          source="scans")
            parse.assert_not_called()
        self.assertEqual(result["state"], "failed")
        self.assertIn("PDF 內容已變更", result["result"]["documents"][0]["error"])

    def test_preview_shares_active_download_with_extraction(self):
        mrn = self.scope["mrn"]
        ref = PdfAttachmentRef(mrn, f"//HFS01_1A0/EMRU/{mrn}/shared.pdf")
        self.save_scans(ref)
        entered, release = threading.Event(), threading.Event()
        result = []

        def fetch(_ref):
            entered.set()
            wait_event(release, label="release shared scan download")
            return BinaryAsset(b"%PDF-shared", "application/pdf")

        def shared():
            with self.work.resources.condition:
                return any(len(flight.subscribers) == 2 for flight in self.work.resources.flights.values())

        with (patch.object(self.work.scans, "_download_ref", side_effect=fetch) as remote,
              patch("vghks_bot.biometry.extract_for_review", return_value=({"pages": []}, {}))):
            started = self.request("start", source="scans", mode="all")
            preview = threading.Thread(target=lambda: result.append(self.work.scans.download(mrn, scan_id(ref))))
            try:
                wait_event(entered, label="extraction download started")
                preview.start()
                wait_until(shared, label="preview joined extraction download")
            finally:
                release.set()
                join_threads([preview], label="preview download complete", preserve_error=True)
            extracted = self.wait_for_result(started, source="scans")
            remote.assert_called_once_with(ref)
        self.assertEqual(result[0]["asset"]["digest"], extracted["result"]["documents"][0]["digest"])

    def test_delete_during_scan_download_does_not_restore_attachment(self):
        mrn = self.scope["mrn"]
        ref = PdfAttachmentRef(mrn, f"//HFS01_1A0/EMRU/{mrn}/deleted.pdf")
        self.save_scans(ref)
        entered, release = threading.Event(), threading.Event()

        def fetch(_ref):
            entered.set()
            wait_event(release, label="release deleted scan download")
            return BinaryAsset(b"%PDF-deleted", "application/pdf")

        with (patch.object(self.work.scans, "_download_ref", side_effect=fetch),
              patch("vghks_bot.biometry.extract_for_review") as parse):
            self.request("start", source="scans", mode="all")
            try:
                wait_event(entered, label="scan download started")
                self.work.library_data.delete({"mrns": [mrn], "categories": ["scans"]}, compatibility=True)
            finally:
                release.set()
            wait_until(lambda: self.analysis.biometry.worker is None, label="deleted extraction stopped")
            parse.assert_not_called()
        self.assertIsNone(self.analysis.store.step(mrn, f"scan-asset:{scan_id(ref)}"))
        self.assertEqual(self.request("status", source="scans"), {"state": "idle"})

    def test_revoked_cohort_assignment_drops_old_result(self):
        mrn = self.scope["mrn"]
        with patch("vghks_bot.biometry.extract_for_review", return_value=({"pages": []}, {})):
            self.wait_for_result(self.request("start", mode="all"))
        cohort = {"id": self.scope["cohort_id"], "name": "Test Cataract Cohort"}
        self.analysis.save_cohort({**cohort, "assignments": {mrn: ""}})
        with self.assertRaises(ValueError):
            self.request("status")
        self.analysis.save_cohort({**cohort, "assignments": {mrn: self.work.account_id}})
        self.assertEqual(self.request("status"), {"state": "idle"})
