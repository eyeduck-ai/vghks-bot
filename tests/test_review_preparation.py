"""Finite review preparation uses only isolated synthetic hospital sessions."""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.test_review_history import HistorySDK
from vghks_bot.bot import BotApplication
from vghks_bot.bot_server import BotServer
from vghks_bot.jobs import BusyError
from vghks_bot.settings import Settings, timestamp, today
from vghks_bot.workbench import status


class PreparationSDK(HistorySDK):
    entered = threading.Event()
    release = threading.Event()
    block = ""
    fail_numeric = ""
    many_orders = 0
    fail_pdf = False

    def history(self, mrn, options):
        value = super().history(mrn, options)
        if mrn == type(self).block:
            type(self).block = ""
            type(self).entered.set()
            if not type(self).release.wait(8):
                raise AssertionError("synthetic preparation read timed out")
        if mrn == type(self).fail_numeric:
            raise ValueError("synthetic numeric failure")
        return value

    def order_history(self, mrn, options, *, verified_cases=()):
        rows = super().order_history(mrn, options, verified_cases=verified_cases)
        if not type(self).many_orders or not rows:
            return rows
        row = rows[0]
        return [replace(row, name="OCT " + str(index), case_no="LIMIT" + str(index),
                        detail_ref=replace(row.detail_ref, case_no="LIMIT" + str(index)),
                        report_ref=replace(row.report_ref, case_no="LIMIT" + str(index)))
                for index in range(type(self).many_orders)]

    def download_pdf(self, reference):
        value = super().download_pdf(reference)
        if type(self).fail_pdf:
            raise ValueError("synthetic failed PDF download")
        return value


class ReviewPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        PreparationSDK.calls = []
        PreparationSDK.block = PreparationSDK.fail_numeric = ""
        PreparationSDK.many_orders = 0
        PreparationSDK.fail_pdf = False
        PreparationSDK.fail_case = ""
        PreparationSDK.on_soap = None
        PreparationSDK.expire_once = PreparationSDK.reject_new_sessions = False
        PreparationSDK.expire_pdf_once = False
        PreparationSDK.fail_visits = False
        PreparationSDK.cataract_order_name = "OCT"
        PreparationSDK.entered.clear()
        PreparationSDK.release.clear()
        self.app = BotApplication(Settings(), Path(self.temp.name), PreparationSDK)
        self.key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.key)
        self.queue = self.work.review.preparation
        self.task = self.source()

    def tearDown(self):
        PreparationSDK.release.set()
        self.app.close()
        self.temp.cleanup()

    def source(self, mrns=("TEST001", "TEST002"), state="completed"):
        return self.work.review.db.save("task", {"id": uuid.uuid4().hex, "kind": "review", "name": "合成準備清單",
            "account_id": self.key, "created_at": timestamp(), "status": state, "mode": "latest",
            "cutoff": today().isoformat(), "department_keyword": "眼科", "categories": self.work.settings.public()["categories"], "force": False,
            "refresh": False, "members": [{"mrn": mrn, "name": "", "registrations": []} for mrn in mrns]})

    def action(self, action="start", **values):
        return self.queue.handle({"review_task_id": self.task["id"], "action": action, **values})

    def wait(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail("synthetic preparation timeout: " + str(self.queue.snapshot()))

    def done(self):
        self.wait(lambda: self.queue.snapshot()["state"] in {"completed", "partial"} and not self.queue.snapshot()["active_mrn"])

    def calls(self, kind, mrn=None):
        return [row for row in PreparationSDK.calls if row[1] == kind and (mrn is None or row[2] == mrn)]

    def test_explicit_start_finite_order_and_idempotent_read_does_not_crawl(self):
        before = list(PreparationSDK.calls)
        self.assertEqual(self.queue.read({"review_task_id": self.task["id"]})["state"], "idle")
        self.assertEqual(PreparationSDK.calls, before)
        self.action(options={"soap": False, "numeric": True}, mrns=["TEST002", "TEST001"])
        self.done()
        self.assertEqual([row[2] for row in self.calls("numeric-history")], ["TEST001", "TEST002"])
        self.assertEqual(self.queue.snapshot()["done_count"], 2)
        before = list(PreparationSDK.calls)
        self.action(options={"soap": False, "numeric": True}, mrns=["TEST001", "TEST002"])
        self.action("resume")
        self.done()
        self.assertEqual(PreparationSDK.calls, before)
        self.assertFalse(self.calls("pdf"))
        self.assertFalse(self.calls("basic-info"))

    def test_source_paused_blocks_soap_and_extras_can_finish(self):
        self.task = self.source(state="paused")
        self.action(options={"numeric": True})
        self.done()
        value = self.queue.snapshot()
        self.assertEqual(value["state"], "partial")
        self.assertEqual(value["blocked_count"], 2)
        self.assertEqual(value["done_count"], 2)
        self.assertFalse(self.calls("soap"))
        self.assertEqual(self.work.review.db.get("task", self.task["id"])["status"], "paused")

    def test_pause_finishes_sent_step_and_resume_does_not_repeat_success(self):
        PreparationSDK.block = "TEST001"
        self.action(options={"soap": False, "numeric": True, "profile": True})
        self.assertTrue(PreparationSDK.entered.wait(4))
        with self.assertRaises(BusyError):
            self.action(options={"soap": False, "orders": True})
        self.action("pause")
        PreparationSDK.release.set()
        self.wait(lambda: not self.queue.snapshot()["active_mrn"])
        self.assertEqual(self.queue.snapshot()["state"], "paused")
        self.assertIsNotNone(self.work.analysis.store.step("TEST001", "numeric-history"))
        self.assertFalse(self.calls("basic-info"))
        self.action("resume")
        self.done()
        self.assertEqual(len(self.calls("numeric-history", "TEST001")), 1)
        self.assertEqual(len(self.calls("basic-info")), 2)

    def test_failed_step_needs_explicit_retry_and_other_members_continue(self):
        PreparationSDK.fail_numeric = "TEST001"
        options = {"soap": False, "numeric": True}
        self.action(options=options)
        self.done()
        self.assertEqual(self.queue.snapshot()["failed_count"], 1)
        self.action(options=options)
        self.action("resume")
        self.done()
        self.assertEqual(len(self.calls("numeric-history", "TEST001")), 1)
        PreparationSDK.fail_numeric = ""
        self.action("retry")
        self.done()
        self.assertEqual(len(self.calls("numeric-history", "TEST001")), 2)
        self.assertEqual(len(self.calls("numeric-history", "TEST002")), 1)

    def test_clear_cache_blocks_resume_until_explicit_retry(self):
        self.action(options={"soap": False, "numeric": True})
        self.done()
        request = {"mrns": ["TEST001"], "categories": ["numeric"]}
        preview = self.work.library_data.preview(request)
        self.work.library_data.delete({**request, "fingerprint": preview["fingerprint"]})
        self.action("resume")
        self.done()
        self.assertEqual(self.queue.snapshot()["blocked_count"], 1)
        self.assertIsNone(self.work.analysis.store.step("TEST001", "numeric-history"))
        self.action("retry")
        self.done()
        self.assertIsNotNone(self.work.analysis.store.step("TEST001", "numeric-history"))
        self.assertEqual(len(self.calls("numeric-history", "TEST001")), 2)
        # Individual SOAP deletion uses the same invalidation hook. A partially
        # retained ready patient and an entirely deleted patient both need repair.
        self.task = self.source(mrns=("TEST001",))
        self.action()
        self.done()
        for delete_all in (False, True):
            with self.subTest(delete_all=delete_all):
                patient = self.work.review.db.item(self.task["id"], "TEST001")
                ids = [record["id"] for record in patient["records"]]
                chosen = ids if delete_all else ids[:1]
                self.work.delete_records({"ids": chosen})
                remaining = self.work.review.db.item(self.task["id"], "TEST001")
                self.assertEqual(remaining["status"], "deleted" if delete_all else "ready")
                self.assertEqual(self.queue.snapshot()["blocked_count"], 1)
                before = len(self.calls("soap"))
                self.action()
                self.action("resume")
                self.done()
                self.assertEqual(len(self.calls("soap")), before)
                self.assertEqual(self.queue.snapshot()["blocked_count"], 1)
                self.action("retry")
                self.done()
                self.assertEqual(len(self.calls("soap")), before + len(chosen))
                restored = self.work.review.db.item(self.task["id"], "TEST001")
                self.assertEqual(restored["status"], "ready")
                self.assertEqual({record["id"] for record in restored["records"]}, set(ids))

    def test_foreground_work_has_priority_without_occupying_main_queue(self):
        with self.work.task_manager.lock:
            self.work.task_manager.active.add("synthetic-foreground")
        self.action(options={"soap": False, "numeric": True})
        self.wait(lambda: self.queue.snapshot()["state"] == "waiting")
        self.assertFalse(self.calls("numeric-history"))
        self.assertEqual(self.work.queue.unfinished_tasks, 0)
        with self.work.task_manager.lock:
            self.work.task_manager.active.clear()
        self.done()
        self.assertEqual(len(self.calls("numeric-history")), 2)

    def test_pause_while_waiting_gateway_starts_no_new_sdk_operation(self):
        with self.work.gateway.lock:
            self.action(options={"soap": False, "numeric": True})
            self.wait(lambda: bool(self.queue.snapshot()["active_mrn"]))
            self.assertFalse(self.calls("numeric-history"))
            self.action("pause")
        self.wait(lambda: not self.queue.snapshot()["active_mrn"])
        self.assertFalse(self.calls("numeric-history"))
        self.action("resume")
        self.done()
        self.assertEqual(len(self.calls("numeric-history")), 2)

    def test_shared_visit_admission_yields_to_foreground_before_any_sdk_io(self):
        self.task = self.source(mrns=("TEST001",))
        with self.work.gateway.lock:
            self.action()
            self.wait(lambda: any(flight.started for flight in self.work.resources.flights.values()))
            with self.work.task_manager.lock:
                self.work.task_manager.active.add("synthetic-foreground")
        self.wait(lambda: self.queue.snapshot()["state"] == "waiting" or
                  any(not flight.started for flight in self.work.resources.flights.values()))
        self.assertFalse(self.calls("index"))
        with self.work.task_manager.lock:
            self.work.task_manager.active.clear()
        self.done()
        self.assertTrue(self.calls("index"))

    def test_retired_preparation_subscriber_does_not_block_other_consumers(self):
        for foreground, mrn in ((True, "TEST001"), (False, "TEST002")):
            with self.subTest(foreground=foreground):
                self.task = self.source(mrns=(mrn,))
                results, errors = [], []
                def consume(foreground=foreground, mrn=mrn, results=results, errors=errors):
                    try:
                        with self.work.gateway.task_context("independent-consumer", "history"), \
                                self.work.gateway.foreground() if foreground else nullcontext():
                            results.append(self.work.resources.visits(mrn).value)
                    except Exception as exc:
                        errors.append(exc)
                with self.work.gateway.lock:
                    self.action()
                    self.wait(lambda: any(flight.started for flight in self.work.resources.flights.values()))
                    consumer = threading.Thread(target=consume)
                    consumer.start()
                    self.wait(lambda: any(len(flight.subscribers) == 2 for flight in self.work.resources.flights.values()))
                    self.action("pause")
                consumer.join(4)
                self.assertFalse(consumer.is_alive())
                self.assertFalse(errors)
                self.assertTrue(results)
                self.wait(lambda: not self.queue.snapshot()["active_mrn"])
                self.assertEqual(len(self.calls("index", mrn)), 1)
                self.assertFalse([row for row in self.calls("soap") if row[2] == mrn])

    def test_logout_fences_late_numeric_save_and_restart_does_not_autostart(self):
        PreparationSDK.block = "TEST001"
        self.action(options={"soap": False, "numeric": True})
        self.assertTrue(PreparationSDK.entered.wait(4))
        logout = threading.Thread(target=self.work.logout)
        logout.start()
        self.wait(lambda: self.queue.snapshot()["state"] == "paused")
        PreparationSDK.release.set()
        logout.join(4)
        self.assertFalse(logout.is_alive())
        self.wait(lambda: not self.queue.snapshot()["active_mrn"])
        self.assertIsNone(self.work.analysis.store.step("TEST001", "numeric-history"))
        self.app.close()
        before = list(PreparationSDK.calls)
        self.app = BotApplication(Settings(), Path(self.temp.name), PreparationSDK)
        self.work = self.app.workspace(self.key, require_entered=False)
        self.queue = self.work.review.preparation
        self.assertEqual(self.queue.read({"review_task_id": self.task["id"]})["state"], "paused")
        self.assertEqual(PreparationSDK.calls, before)
        self.app.login({"id": self.key, "password": "synthetic"})
        self.action("resume")
        self.done()

    def test_report_content_preparation_does_not_download_assets(self):
        self.task = self.source(mrns=("TEST001",))
        self.action(options={"soap": False, "reports": True, "attachments": False, "max_reports": 1})
        self.done()
        self.assertTrue(self.calls("order-report"))
        self.assertFalse(self.calls("pdf"))
        self.assertFalse(self.calls("image"))
        self.assertFalse(self.calls("pacs-study"))
        self.assertEqual(self.queue.snapshot()["state"], "completed")

    def test_report_manifest_caps_many_orders_and_attachment_budget(self):
        self.task = self.source(mrns=("TEST001",))
        PreparationSDK.many_orders = 8
        self.action(options={"soap": False, "reports": True, "max_reports": 2})
        self.done()
        member = self.queue.data["members"][0]
        self.assertEqual(sum(name.startswith("report:") for name in member["steps"]), 2)
        self.assertEqual(len(self.calls("order-detail")), 2)
        self.assertFalse(self.calls("pdf"))
        self.assertFalse(self.calls("image"))
        self.action(options={"soap": False, "reports": True, "attachments": True, "max_reports": 2, "max_attachments": 1})
        self.done()
        self.assertEqual(len(self.calls("pdf")) + len(self.calls("image")), 1)
        self.assertEqual(self.queue.snapshot()["state"], "completed")

    def test_failed_attachments_consume_durable_budget_across_reports(self):
        self.task = self.source(mrns=("TEST001",))
        PreparationSDK.many_orders, PreparationSDK.fail_pdf = 2, True
        self.action(options={"soap": False, "reports": True, "attachments": True,
                             "max_reports": 2, "max_attachments": 1})
        self.done()
        self.assertEqual(self.queue.snapshot()["state"], "partial")
        self.assertEqual(len(self.calls("pdf")) + len(self.calls("image")), 1)
        member = self.queue.data["members"][0]
        self.assertEqual(sum(step.get("attachment_count", 0) for step in member["steps"].values()), 1)
        self.action("resume")
        self.done()
        self.assertEqual(len(self.calls("pdf")) + len(self.calls("image")), 1)
        PreparationSDK.fail_pdf = False
        self.action("retry")
        self.done()
        self.assertEqual(len(self.calls("pdf")) + len(self.calls("image")), 2)

    def test_soap_completed_items_not_repeated_and_missing_items_use_source_definition(self):
        self.task = self.source(mrns=("TEST001",))
        self.action()
        self.done()
        self.assertTrue(self.calls("soap"), self.queue.snapshot())
        saved = self.work.review.db.item(self.task["id"], "TEST001")
        self.assertEqual(saved["status"], "ready")
        self.assertTrue(all("眼科" in record["section"] for record in saved["records"]))
        before = list(PreparationSDK.calls)
        self.action("retry")
        self.done()
        self.assertEqual(PreparationSDK.calls, before)

    def test_bounded_status_account_scope_invalid_options_and_readonly(self):
        idle = self.queue.snapshot(compact=True)
        self.assertEqual(idle["state"], "idle")
        self.assertNotIn("members", idle)
        self.assertNotIn("errors", idle)
        self.task = self.source(mrns=tuple(f"TEST{i:03}" for i in range(60)))
        with self.work.task_manager.lock:
            self.work.task_manager.active.add("synthetic-foreground")
        self.action(options={"soap": False, "numeric": True})
        value = self.queue.read({"review_task_id": self.task["id"]})
        self.assertEqual(value["member_total"], 60)
        self.assertEqual(len(value["members"]), 40)
        compact = status(self.work)["review_preparation"]
        self.assertNotIn("members", compact)
        self.assertNotIn("errors", compact)
        with patch("vghks_bot.review_preparation.copy.deepcopy", side_effect=AssertionError("compact copied members")):
            compact = self.queue.snapshot(compact=True)
        self.assertEqual(compact["member_total"], 60)
        self.action("pause")
        saved = self.queue.data
        self.queue.data = None
        try:
            with patch.object(self.queue, "_load", side_effect=AssertionError("compact loaded member rows")):
                compact = self.queue.snapshot(compact=True)
            self.assertEqual(compact["member_total"], 60)
            self.assertNotIn("members", compact)
        finally:
            self.queue.data = saved
        other = self.app.login({"username": "OTHER", "password": "synthetic"})["account"]["id"]
        with self.assertRaises(ValueError):
            self.app.workspace(other).review.preparation.read({"review_task_id": self.task["id"]})
        for values in ({"mrns": ["FOREIGN"]}, {"options": {"max_reports": 6}}, {"options": {"attachments": True}}):
            with self.assertRaises(ValueError):
                self.action(**values)
        self.app.read_only = True
        with self.assertRaises(ValueError):
            self.action("resume")
        self.queue.read({"review_task_id": self.task["id"]})

    def test_http_read_is_local_and_actions_require_existing_session_csrf(self):
        with BotServer(0, self.app) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            def request(route, csrf=True):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
                headers = {"Content-Type": "application/json", "Cookie": "opd_session=" + self.app.session_token}
                if csrf:
                    headers["X-CSRF-Token"] = self.app.csrf_token
                connection.request("POST", f"/api/accounts/{self.key}/reviews/preparation/{route}",
                    json.dumps({"review_task_id": self.task["id"], "action": "pause"}), headers)
                response = connection.getresponse()
                value = response.status, json.loads(response.read())
                connection.close()
                return value
            before = list(PreparationSDK.calls)
            self.assertEqual(request("read")[0], 200)
            self.assertEqual(PreparationSDK.calls, before)
            self.assertEqual(request("action", csrf=False)[0], 403)
            server.shutdown()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
