import csv
import io
import json
import shutil
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from async_helpers import HOLD_TIMEOUT, join_threads, wait_event
from async_helpers import wait_workspace_task as wait_task
from vghks_sdk import ConfigurationError

from vghks_bot.analysis_fetch import clean
from vghks_bot.approval_tracker import later
from vghks_bot.bot import BotApplication
from vghks_bot.earnings_parser import month_value, normalize_document, numeric_cell
from vghks_bot.selftest_bot import BotSyntheticSDK
from vghks_bot.settings import Settings, timestamp


class FollowUpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "database"
        self.app = BotApplication(Settings(), self.path, BotSyntheticSDK)
        self.key = self.app.login({"username": "TEST", "password": "fake"})["account"]["id"]
        self.work = self.app.workspace(self.key)
        self.db = self.work.review.db
        self.tracker = self.work.approvals.tracker
        BotSyntheticSDK.calls = []

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def task(self, **values):
        return wait_task(self.work, self.work.review.start(values)["task_id"])

    def query(self):
        return self.task(kind="approval_search")

    def overdue(self):
        for row in self.db.all("approval_tracking"):
            self.db.save("approval_tracking", {**row, "next_check": "2000-01-01T00:00:00+08:00"})

    def test_terminal_codes_only_and_status_change_history(self):
        query = self.query()
        self.assertEqual(self.tracker.overview()["counts"], {"pending": 2, "due": 0, "unread": 0, "closed": 1})
        original = BotSyntheticSDK.review_case
        def changed(sdk, ref):
            row = original(sdk, ref)
            return replace(row, verify_code="2" if ref.apply_seq == "1001" else "1")
        with patch.object(self.work.gateway.connection.reviews, "get_case", lambda ref: changed(self.work.gateway.connection, ref)):
            task = self.task(kind="approval_refresh")
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["progress"]["total"], 2)
        self.assertEqual(self.tracker.overview()["counts"], {"pending": 0, "due": 0, "unread": 2, "closed": 3})
        self.assertEqual(len(self.work.approvals.detail({"apply_seq": "1001"})["observations"]), 2)
        self.assertEqual(self.work.approvals.results({"id": query["id"]})["cases"][0]["verify_code"], "4")
        self.assertFalse(any(c[1] == "review-detail" and c[2] == "1002" for c in BotSyntheticSDK.calls))
        self.tracker.change({"ids": ["1001", "1003"], "action": "seen"})
        self.assertEqual(self.tracker.overview()["counts"]["unread"], 0)
        with self.assertRaises(ValueError):
            self.task(kind="approval_refresh")

    def test_unknown_partial_and_returned_keep_tracking(self):
        self.query()
        base = clean(self.work.gateway.connection._review_rows()[0])
        for code in ("0", "3", "4", "5", "7", "9"):
            self.tracker.observe({**base, "verify_code": code})
            self.assertFalse(self.db.get("approval_tracking", "1001")["closed"])
        prior = self.db.get("approval_tracking", "1001")["checked_at"]
        self.tracker.observe({**base, "verify_code": "1"}, checked_at="2000-01-01T00:00:00+08:00")
        self.assertEqual(self.db.get("approval_tracking", "1001")["checked_at"], prior)
        self.assertEqual(self.db.get("approval_case", "1001")["case"]["verify_code"], "9")
        with self.assertRaises(ValueError):
            self.tracker.observe({**base, "mrn": "OTHER"})

    def test_failures_preserve_previous_case_and_back_off(self):
        self.query()
        old = self.db.get("approval_case", "1001")
        with patch.object(self.work.gateway.connection.reviews, "get_case", side_effect=RuntimeError("not public")):
            task = self.task(kind="approval_refresh", ids=["1001"])
        self.assertEqual(task["status"], "partial")
        self.assertEqual(task["progress"]["failed"], 1)
        self.assertEqual(old, self.db.get("approval_case", "1001"))
        row = self.db.get("approval_tracking", "1001")
        self.assertEqual(row["failures"], 1)
        self.assertGreater(row["next_check"], row["last_attempt"])
        self.assertNotIn("not public", row["error"])
        retry = self.task(resume=task["id"])
        self.assertEqual(retry["status"], "completed")
        self.assertEqual(self.db.get("approval_tracking", "1001")["failures"], 0)

    def test_scheduler_respects_offline_readonly_preferences_and_pause(self):
        self.query()
        self.overdue()
        self.tracker.monitor_started = True
        with patch.object(self.work.review, "start") as start:
            self.work.gateway.online = False
            self.app.follow_up_tick()
            start.assert_not_called()
            self.work.gateway.online = True
            self.app.read_only = True
            self.app.follow_up_tick()
            start.assert_not_called()
            self.app.read_only = False
            self.tracker.preferences({"automatic": False, "hours": 6})
            self.overdue()
            self.app.follow_up_tick()
            start.assert_not_called()
            self.tracker.preferences({"automatic": True, "hours": 24})
            self.overdue()
            self.app.follow_up_tick()
            start.assert_called_once_with({"kind": "approval_sync", "automatic": True})
        self.db.save("task", {"id": "paused-refresh", "kind": "approval_refresh", "status": "paused"})
        with patch.object(self.work.review, "start") as start:
            self.app.follow_up_tick()
            start.assert_not_called()
        self.tracker.change({"ids": ["1001"], "action": "pause"})
        self.assertEqual(self.tracker.overview()["counts"]["pending"], 1)
        self.tracker.change({"ids": ["1001"], "action": "resume"})
        self.assertTrue(next(r for r in self.tracker.overview()["rows"] if r["id"] == "1001")["due"])

    def test_account_isolation_and_restart_pending_task(self):
        self.query()
        other = self.app.workspace(self.app.login({"username": "OTHER", "password": "fake"})["account"]["id"])
        self.assertEqual(other.approvals.tracker.overview()["counts"]["pending"], 0)
        self.db.save("task", {"id": "unfinished", "kind": "approval_refresh", "references": ["1001"], "status": "running"})
        self.app.close()
        self.app = BotApplication(Settings(), self.path, BotSyntheticSDK)
        self.app.offline(self.key)
        self.work = self.app.workspace(self.key)
        self.assertEqual(self.work.review.task("unfinished")["status"], "paused")
        self.assertEqual(self.work.approvals.tracker.overview()["counts"]["pending"], 2)

    def test_live_review_progress_and_cancel_resume(self):
        self.task(kind="resolve", identifiers="TEST001,TEST002")
        group = self.work.review.save_set({"mrns": "TEST001,TEST002"})
        entered, release = threading.Event(), threading.Event()
        def block(case):
            if case.mrn == "TEST002":
                entered.set()
                wait_event(release, label=self.id() + ": release SOAP response", timeout=HOLD_TIMEOUT)
        with patch.object(BotSyntheticSDK, "on_soap", block):
            key = self.work.review.start({"set_id": group["id"]})["task_id"]
            try:
                wait_event(entered, label=self.id() + ": SOAP response entered")
                live = self.work.review.task(key)
                self.assertEqual(live["progress"]["percent"], 50)
                self.assertEqual(live["done"], 1)
                self.work.review.stop(key)
            finally:
                release.set()
            paused = wait_task(self.work, key)
        self.assertEqual(paused["status"], "paused")
        finished = self.task(resume=key)
        self.assertEqual(finished["progress"]["percent"], 100)

    def test_disabled_monitor_and_active_refresh_never_queue_duplicates(self):
        self.query()
        self.overdue()
        self.tracker.monitor_started = True
        self.tracker.preferences({"enabled": False, "automatic": True, "hours": 24})
        with patch.object(self.work.review, "start") as start:
            self.app.follow_up_tick()
            start.assert_not_called()
        self.tracker.preferences({"enabled": True, "automatic": True, "hours": 24})
        self.overdue()
        entered, release = threading.Event(), threading.Event()
        original = self.work.gateway.connection.reviews.get_cases
        def slow(ref):
            entered.set()
            wait_event(release, label=self.id() + ": release review response", timeout=HOLD_TIMEOUT)
            return original(ref)
        with patch.object(self.work.gateway.connection.reviews, "get_cases", slow):
            self.app.follow_up_tick()
            try:
                wait_event(entered, label=self.id() + ": review response entered")
                self.app.follow_up_tick()
                tasks = [t for t in self.db.all("task") if t["kind"] == "approval_sync"]
                self.assertEqual(len(tasks), 1)
                self.assertTrue(tasks[0]["automatic"])
                with self.assertRaises(ValueError):
                    self.work.review.start({"kind": "approval_refresh"})
            finally:
                release.set()
            self.assertEqual(wait_task(self.work, tasks[0]["id"])["status"], "completed")

    def test_scheduler_skips_while_database_management_holds_root_lock(self):
        entered, release = threading.Event(), threading.Event()
        def snapshot():
            with self.app.lock:
                entered.set()
                wait_event(release, label=self.id() + ": release database lock", timeout=HOLD_TIMEOUT)
        thread = threading.Thread(target=snapshot)
        thread.start()
        try:
            wait_event(entered, label=self.id() + ": database lock acquired", threads=(thread,))
            with patch.object(self.tracker, "schedule") as schedule:
                self.app.follow_up_tick()
                schedule.assert_not_called()
        finally:
            release.set()
            join_threads((thread,), label="database lock holder cleanup", preserve_error=True)


class EarningsArchiveTests(unittest.TestCase):
    setUp = FollowUpTests.setUp
    tearDown = FollowUpTests.tearDown
    task = FollowUpTests.task

    def configure(self, remember=True, password="synthetic-salary-secret"):
        self.tracker.preferences({"enabled": False})
        return self.work.earnings.save_credentials({"national_id": "A123456789", "password": password, "remember": remember})

    def capture(self, **values):
        values.setdefault("force", False)
        return self.task(kind="earnings_capture", all_available=True, **values)

    def test_identity_switch_separates_month_cache_and_rejects_old_task_and_report_ids(self):
        self.configure()
        first = self.capture()
        earnings = self.work.earnings
        original = earnings.overview()["reports"][0]
        earnings.save_credentials({"national_id": "B123456789", "password": "synthetic-other", "remember": False})
        self.assertEqual(earnings.overview()["reports"], [])
        self.assertEqual(earnings.overview()["catalogs"], [])
        self.assertEqual(earnings.overview()["tasks"], [])
        before = len(BotSyntheticSDK.calls)
        with self.assertRaisesRegex(ValueError, "身分"):
            self.work.review.start({"resume": first["id"]})
        self.assertEqual(len(BotSyntheticSDK.calls), before)
        with self.assertRaisesRegex(ValueError, "身分"):
            self.work.review.task(first["id"])
        for action, values in ((earnings.detail, {"id": original["id"]}),
                               (earnings.export, {"ids": [original["id"]]}),
                               (earnings.delete, {"ids": [original["id"]]})):
            with self.assertRaisesRegex(ValueError, "身分"):
                action(values)
        self.assertEqual(self.capture()["status"], "completed")
        self.assertEqual(len(earnings.overview()["reports"]), 4)
        self.assertNotIn(original["id"], [row["id"] for row in earnings.overview()["reports"]])
        self.configure(password="new-password-same-identity")
        self.assertIn(original["id"], [row["id"] for row in earnings.overview()["reports"]])
        earnings.forget()
        self.assertEqual(earnings.detail({"id": original["id"]})["report"]["id"], original["id"])

    def test_legacy_reports_are_retained_as_unattributed_archive(self):
        from vghks_sdk.models.documents import HtmlDocument, HtmlTable

        earnings = self.work.earnings
        document = HtmlDocument((HtmlTable("Synthetic legacy", (("A", "1"),)),), "raw", "")
        legacy = earnings.store_report("performance", "202610", "2026/10", document, scope="")
        self.assertEqual(earnings.overview()["view"], "archive")
        self.configure()
        self.assertEqual(earnings.overview()["view"], "active")
        self.assertTrue(earnings.overview()["has_archive"])
        self.assertEqual(earnings.overview()["reports"], [])
        with self.assertRaisesRegex(ValueError, "檢視範圍"):
            earnings.detail({"id": legacy["id"]})
        archive = earnings.overview({"view": "archive"})
        self.assertEqual([r["id"] for r in archive["reports"]], [legacy["id"]])
        self.assertEqual(archive["catalogs"], [])
        self.assertEqual(earnings.detail({"id": legacy["id"], "view": "archive"})["report"]["id"], legacy["id"])
        self.assertIn("Synthetic legacy", earnings.export({"ids": [legacy["id"]], "view": "archive"})["files"][0]["content"])

    def test_queued_query_prevents_replacing_credentials(self):
        self.configure()
        self.db.save("task", {"id": "queued-synthetic", "kind": "earnings_options", "status": "queued",
                              "earnings_scope": self.work.earnings.scope()})
        with self.assertRaisesRegex(ValueError, "處理中"):
            self.work.earnings.save_credentials({"national_id": "B123456789", "password": "synthetic-other"})
        self.assertEqual(self.work.earnings.credentials().national_id, "A123456789")

    def test_month_parser_and_literal_numbers(self):
        for raw, expected in [("11509", "2026-09"), ("202609", "2026-09"), ("115/09", "2026-09"), ("2026-09", "2026-09"), ("11513", ""), ("foo", "")]:
            self.assertEqual(month_value(raw), expected)
        for raw, number, unit in [("NT$ 1,234.50", "1234.50", "NT$"), ("(200)", "-200", ""), ("12.5%", "12.5", "%"), ("00123", None, ""), ("2026-09", None, ""), ("HM", None, "")]:
            self.assertEqual(numeric_cell(raw), {"number": number, "unit": unit})

    def test_capture_normalized_tables_immutable_versions_and_cache(self):
        self.configure()
        first = self.capture()
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["progress"]["done"], 4)
        self.assertEqual(first["progress"]["percent"], 100)
        reports = self.work.earnings.overview()["reports"]
        self.assertEqual(len(reports), 4)
        before = sum(c[1] == "earnings-report" for c in BotSyntheticSDK.calls)
        self.capture()
        self.assertEqual(sum(c[1] == "earnings-report" for c in BotSyntheticSDK.calls), before)
        self.capture(force=True)
        self.assertTrue(all(r["version_count"] == 1 for r in self.work.earnings.overview()["reports"]))
        with patch.object(BotSyntheticSDK, "revision", "changed"):
            self.capture(force=True)
        detail = self.work.earnings.detail({"id": reports[0]["id"]})
        self.assertEqual(detail["report"]["version_count"], 2)
        old = self.work.earnings.detail({"id": reports[0]["id"], "version": reports[0]["latest"]})
        self.assertIn("original", old["version"]["payload"]["text"])
        table = detail["version"]["payload"]["tables"][0]
        self.assertEqual(table["grid"][1], ["項目", "數值", "備註"])
        self.assertEqual(table["cells"][0]["rowspan"], 2)
        self.assertTrue(any(c["raw"] == "00123" and c["number"] is None for c in table["cells"]))

    def test_month_withdrawal_preserves_archive_and_offline_exports(self):
        self.configure()
        self.capture()
        original = BotSyntheticSDK.earnings_open
        def newer(sdk, credentials, kind):
            context = original(sdk, credentials, kind)
            return replace(context, form=replace(context.form, choices={"BEGYM": (("11510", "115 年 10 月"),)}))
        with patch.object(BotSyntheticSDK, "earnings_open", newer):
            self.capture()
        self.assertEqual(len(self.work.earnings.overview()["reports"]), 6)
        self.app.logout(self.key)
        self.app.offline(self.key)
        before = list(BotSyntheticSDK.calls)
        rows = self.work.earnings.overview()["reports"]
        ids = [r["id"] for r in rows]
        exported = self.work.earnings.export({"ids": ids, "format": "json"})
        self.assertEqual(sum(len(json.loads(file["content"])["reports"]) for file in exported["files"]), 6)
        files = self.work.earnings.export({"ids": ids, "format": "csv"})["files"]
        self.assertTrue(all(file["content"].startswith("\ufeff") for file in files))
        cells = [row for file in files for row in list(csv.reader(io.StringIO(file["content"])))[1:]]
        self.assertTrue(any(r[11] == "'=SUM(A1:A2)" for r in cells))
        self.assertTrue(any(r[12] == "-200" for r in cells))
        self.assertEqual(BotSyntheticSDK.calls, before)

    def test_credentials_portable_and_context_secrets_never_saved_or_exported(self):
        self.configure()
        self.capture()
        data = self.work.earnings.overview()
        public = json.dumps(data)+json.dumps(self.db.all("task"))+json.dumps(self.work.earnings.export({"ids": [r["id"] for r in data["reports"]]}))
        for secret in ("A123456789", "synthetic-salary-secret", "never-persist-this-token", "sensitive-context"):
            self.assertNotIn(secret, public)
        self.assertEqual(self.work.earnings.public_credentials(), {"configured": True, "remembered": True})
        other = self.app.workspace(self.app.login({"username": "OTHER", "password": "fake"})["account"]["id"])
        self.assertFalse(other.earnings.public_credentials()["configured"])
        self.assertEqual(other.earnings.overview()["reports"], [])
        self.app.close()
        copied = Path(self.temp.name) / "copied"
        shutil.copytree(self.path, copied)
        self.app = BotApplication(Settings(), copied, BotSyntheticSDK)
        self.app.offline(self.key)
        self.work = self.app.workspace(self.key)
        self.assertEqual(self.work.earnings.credentials().password, "synthetic-salary-secret")
        self.assertEqual(len(self.work.earnings.overview()["reports"]), 4)

    def test_exports_split_report_types_for_json_and_csv(self):
        self.configure()
        self.capture()
        earnings = self.work.earnings
        reports = earnings.overview()["reports"]
        ids = [row["id"] for row in reports]
        names = {"performance": "醫療績點明細", "payroll": "專勤工作獎金明細"}
        for format in ("json", "csv"):
            with self.subTest(format=format):
                files = earnings.export({"ids": ids + ids[:1], "format": format})["files"]
                self.assertEqual([file["kind"] for file in files], list(names))
                for file in files:
                    report_kind = file["kind"]
                    self.assertEqual(file["filename"], f"VGHKS-{names[report_kind]}_202608-202609.{format}")
                    expected = [row["latest"] for row in reports if row["kind"] == report_kind]
                    if format == "json":
                        self.assertEqual(file["mime"], "application/json")
                        payload = json.loads(file["content"])
                        self.assertTrue(payload["exported_at"])
                        self.assertEqual([row["id"] for row in payload["reports"]], expected)
                        self.assertTrue(all(row["kind"] == report_kind for row in payload["reports"]))
                    else:
                        self.assertEqual(file["mime"], "text/csv;charset=utf-8")
                        self.assertTrue(file["content"].startswith("\ufeff"))
                        rows = list(csv.reader(io.StringIO(file["content"])))[1:]
                        self.assertEqual({row[0] for row in rows}, {report_kind})
                        self.assertEqual({row[4] for row in rows}, set(expected))

    def test_export_filenames_use_each_report_types_selected_periods(self):
        self.configure()
        self.capture()
        earnings = self.work.earnings
        reports = {(row["kind"], row["period"]): row for row in earnings.overview()["reports"]}
        selected = [reports[("performance", "11508")], reports[("payroll", "11509")]]
        for format in ("json", "csv"):
            files = earnings.export({"ids": [row["id"] for row in selected], "format": format})["files"]
            self.assertEqual([file["filename"] for file in files], [
                f"VGHKS-醫療績點明細_202608.{format}",
                f"VGHKS-專勤工作獎金明細_202609.{format}",
            ])

    def test_reselected_exports_only_contain_current_selection(self):
        self.configure()
        self.capture()
        earnings = self.work.earnings
        reports = earnings.overview()["reports"]
        before = list(BotSyntheticSDK.calls)
        for selected in (reports, reports[:1], reports[-1:]):
            files = earnings.export({"ids": [row["id"] for row in selected]})["files"]
            exported = [row for file in files for row in json.loads(file["content"])["reports"]]
            self.assertEqual({row["report_id"] for row in exported}, {row["id"] for row in selected})
            self.assertEqual({file["kind"] for file in files}, {row["kind"] for row in selected})
        self.assertEqual(BotSyntheticSDK.calls, before)

    def test_export_reads_saved_reports_without_waiting_for_crawler_or_writer_lock(self):
        self.configure()
        self.capture()
        earnings = self.work.earnings
        reports = earnings.overview()["reports"]
        entered, release, exported = threading.Event(), threading.Event(), threading.Event()
        original = self.work.gateway.connection.earnings.get_report
        results, errors = [], []

        def slow(context, period):
            entered.set()
            wait_event(release, label=self.id() + ": release report refresh", timeout=HOLD_TIMEOUT)
            return original(context, period)

        def export():
            try:
                results.append(earnings.export({"ids": [row["id"] for row in reports]}))
            except Exception as exc:
                errors.append(exc)
            finally:
                exported.set()

        with patch.object(self.work.gateway.connection.earnings, "get_report", slow):
            key = self.work.review.start({"kind": "earnings_capture", "all_available": True, "force": True})["task_id"]
            thread = threading.Thread(target=export, name="earnings-export-during-refresh")
            try:
                wait_event(entered, label=self.id() + ": report refresh entered")
                with earnings.lock, self.work.store.library.lock:
                    thread.start()
                    wait_event(exported, label=self.id() + ": local export completed", timeout=5, threads=[thread])
                self.assertFalse(errors)
                self.assertEqual(len(results[0]["files"]), 2)
                self.assertFalse(release.is_set())
            finally:
                release.set()
                if thread.ident is not None:
                    join_threads([thread], label=self.id() + ": export thread joined")
            self.assertEqual(wait_task(self.work, key)["status"], "completed")

    def test_bad_secondary_password_leaves_hospital_session_usable(self):
        self.configure(password="invalid")
        task = self.capture()
        self.assertEqual(task["status"], "failed")
        self.assertIn("薪資", task["message"])
        self.assertTrue(self.work.gateway.online)
        self.assertEqual(len([c for c in BotSyntheticSDK.calls if c[1] == "earnings-open"]), 1)
        self.assertEqual(self.task(kind="resolve", identifiers="TEST001")["status"], "completed")
        self.configure()
        self.assertEqual(self.task(resume=task["id"])["status"], "completed")

    def test_failed_report_resume_and_expired_context_retry(self):
        self.configure()
        original = BotSyntheticSDK.earnings_report
        def fail(sdk, context, period):
            if context.kind == "performance" and period == "11509":
                raise RuntimeError("not public")
            return original(sdk, context, period)
        with patch.object(self.work.gateway.connection.earnings, "get_report", lambda c, p: fail(self.work.gateway.connection, c, p)):
            task = self.capture()
        self.assertEqual(task["status"], "partial")
        self.assertEqual(task["progress"]["failed"], 1)
        before = len([c for c in BotSyntheticSDK.calls if c[1] == "earnings-report"])
        self.task(resume=task["id"])
        self.assertEqual(len([c for c in BotSyntheticSDK.calls if c[1] == "earnings-report"]), before+1)
        attempts = []
        def expired(sdk, context, period):
            if not attempts:
                attempts.append(1)
                raise ConfigurationError("expired", code="EARNINGS_CONTEXT_EXPIRED")
            return original(sdk, context, period)
        with patch.object(self.work.gateway.connection.earnings, "get_report", lambda c, p: expired(self.work.gateway.connection, c, p)):
            self.assertEqual(self.capture(force=True)["status"], "completed")

    def test_catalog_failure_retry_clears_error_and_empty_periods_complete(self):
        self.configure()
        original = BotSyntheticSDK.earnings_open
        def fail(sdk, credentials, kind):
            if kind == "payroll":
                raise RuntimeError("no catalog")
            return original(sdk, credentials, kind)
        with patch.object(BotSyntheticSDK, "earnings_open", fail):
            task = self.task(kind="earnings_options")
        self.assertEqual(task["status"], "partial")
        self.assertEqual(self.task(resume=task["id"])["status"], "completed")
        def empty(sdk, credentials, kind):
            ctx = original(sdk, credentials, kind)
            return replace(ctx, form=replace(ctx.form, choices={"BEGYM": ()}))
        with patch.object(BotSyntheticSDK, "earnings_open", empty):
            task = self.capture()
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["progress"]["total"], 0)

    def test_delete_tombstone_stops_old_task_restoring_and_allows_new_capture(self):
        self.configure()
        task = self.capture()
        report = self.work.earnings.overview()["reports"][0]
        self.work.earnings.delete({"ids": [report["id"]]})
        self.db.item(task["id"], report["id"], {"status": "error", "kind": report["kind"], "period": report["period"]})
        resumed = self.task(resume=task["id"])
        self.assertTrue(any(i["status"] == "deleted" for i in resumed["items"]))
        self.assertEqual(len(self.work.earnings.overview()["reports"]), 3)
        self.assertFalse(any(v["report_id"] == report["id"] for v in self.db.all("earnings_version")))
        self.capture()
        self.assertEqual(len(self.work.earnings.overview()["reports"]), 4)

    def test_no_secret_when_not_remembered_and_forget(self):
        self.configure(remember=False)
        self.assertEqual(self.work.earnings.public_credentials(), {"configured": True, "remembered": False})
        self.work.earnings.forget()
        self.assertFalse(self.work.earnings.public_credentials()["configured"])
        with self.assertRaises(ValueError):
            self.capture()

    def test_live_unknown_total_until_both_month_catalogs_discovered(self):
        self.configure()
        entered, release = threading.Event(), threading.Event()
        original = BotSyntheticSDK.earnings_open
        def slow(sdk, credentials, kind):
            if kind == "payroll":
                entered.set()
                wait_event(release, label=self.id() + ": release month catalog", timeout=HOLD_TIMEOUT)
            return original(sdk, credentials, kind)
        with patch.object(BotSyntheticSDK, "earnings_open", slow):
            key = self.work.review.start({"kind": "earnings_capture", "all_available": True})["task_id"]
            try:
                wait_event(entered, label=self.id() + ": month catalog entered")
                task = self.work.review.task(key)
                self.assertEqual(task["progress"]["done"], 2)
                self.assertIsNone(task["progress"]["percent"])
            finally:
                release.set()
            self.assertEqual(wait_task(self.work, key)["progress"]["percent"], 100)

    def test_parser_rejects_missing_tables_and_handles_sdk_tables(self):
        from vghks_sdk.models.documents import HtmlDocument, HtmlTable
        with self.assertRaises(ValueError):
            normalize_document(HtmlDocument((), "empty", ""))
        value = normalize_document(HtmlDocument((HtmlTable("name", (("A", "1"),)),), "raw", ""))
        self.assertEqual(value["tables"][0]["cells"][1]["number"], "1")

    def test_script_written_cells_use_sdk_static_parser(self):
        from vghks_sdk.parsing.documents import parse_document
        document = parse_document('<table><tr><th>績點</th><script>document.write("<td>1,200</td>");</script></tr></table>')
        result = normalize_document(document)
        self.assertEqual(result["tables"][0]["grid"], [["績點", "1,200"]])
        self.assertEqual(result["tables"][0]["cells"][1]["number"], "1200")
        self.assertIn("document.write", result["html"])

    def test_cancel_saves_completed_months_and_resumes_without_redownload(self):
        self.configure()
        entered, release = threading.Event(), threading.Event()
        original = self.work.gateway.connection.earnings.get_report
        def slow(context, period):
            if period == "11509":
                entered.set()
                wait_event(release, label=self.id() + ": release salary report", timeout=HOLD_TIMEOUT)
            return original(context, period)
        with patch.object(self.work.gateway.connection.earnings, "get_report", slow):
            key = self.work.review.start({"kind": "earnings_capture", "all_available": True})["task_id"]
            try:
                wait_event(entered, label=self.id() + ": salary report entered")
                self.work.review.stop(key)
            finally:
                release.set()
            self.assertEqual(wait_task(self.work, key)["status"], "paused")
        before = [c for c in BotSyntheticSDK.calls if c[1] == "earnings-report"]
        self.assertEqual(len(before), 2)
        self.assertEqual(self.task(resume=key)["status"], "completed")
        calls = [c for c in BotSyntheticSDK.calls if c[1] == "earnings-report"]
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(set(calls)), 4)

    def test_daily_monitor_refreshes_once_per_interval_and_versions_changes(self):
        self.configure()
        self.app.follow_up_tick()
        tasks = [t for t in self.db.all("task") if t["kind"] == "earnings_capture"]
        self.assertEqual(len(tasks), 1)
        first = wait_task(self.work, tasks[0]["id"])
        self.assertTrue(first["automatic"] and first["force"])
        monitor = self.work.earnings.monitor.overview()
        self.assertEqual((datetime.fromisoformat(monitor["next_check"])-datetime.fromisoformat(monitor["last_attempt"])).total_seconds(), 86400)
        self.app.follow_up_tick()
        self.assertEqual(len(self.db.all("task")), 1)
        self.db.save("preferences", {**self.work.earnings.monitor._preferences(), "next_check": "2000-01-01T00:00:00+08:00"})
        with patch.object(BotSyntheticSDK, "revision", "monthly-revision"):
            self.app.follow_up_tick()
            task = self.db.all("task")[0]
            self.assertEqual(wait_task(self.work, task["id"])["status"], "completed")
        self.assertTrue(all(r["version_count"] == 2 for r in self.work.earnings.overview()["reports"]))
        self.assertEqual(len([c for c in BotSyntheticSDK.calls if c[1] == "earnings-report"]), 8)

    def test_monitor_disabled_offline_readonly_or_paused_does_not_start(self):
        monitor = self.work.earnings.monitor
        with patch.object(self.work.review, "start") as start:
            monitor.schedule()
            start.assert_not_called()
            self.configure()
            monitor.preferences({"enabled": False, "hours": 24})
            monitor.schedule()
            start.assert_not_called()
            monitor.preferences({"enabled": True, "hours": 6})
            self.work.gateway.online = False
            monitor.schedule()
            start.assert_not_called()
            self.work.gateway.online = True
            self.app.read_only = True
            monitor.schedule()
            start.assert_not_called()
            self.app.read_only = False
            self.db.save("task", {"id": "paused", "kind": "earnings_capture", "status": "paused",
                                  "earnings_scope": self.work.earnings.scope()})
            monitor.schedule()
            start.assert_not_called()
            self.db.delete("task", "paused")
            monitor.schedule()
            start.assert_called_once_with({"kind": "earnings_capture", "all_available": True, "force": True, "automatic": True})

    def test_monitor_does_not_resurrect_deleted_report(self):
        self.configure()
        self.capture()
        report = self.work.earnings.overview()["reports"][0]
        self.work.earnings.delete({"ids": [report["id"]]})
        task = self.capture(force=True, automatic=True)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(len(self.work.earnings.overview()["reports"]), 3)
        self.assertEqual(sum(i["status"] == "deleted" for i in task["items"]), 1)
        self.capture(force=True)
        self.assertEqual(len(self.work.earnings.overview()["reports"]), 4)

    def test_failed_scheduled_login_waits_daily_and_manual_refresh_is_available(self):
        self.configure(password="invalid")
        self.app.follow_up_tick()
        task = self.db.all("task")[0]
        self.assertEqual(wait_task(self.work, task["id"])["status"], "failed")
        self.app.follow_up_tick()
        self.assertEqual(len(self.db.all("task")), 1)
        self.configure()
        result = self.task(kind="earnings_capture", all_available=True)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["force"])

    def test_monitor_preferences_and_due_time_survive_restart(self):
        self.configure()
        self.task(kind="earnings_capture", all_available=True)
        before = self.work.earnings.monitor.preferences({"enabled": False, "hours": 6})
        self.app.close()
        self.app = BotApplication(Settings(), self.path, BotSyntheticSDK)
        self.app.offline(self.key)
        self.work = self.app.workspace(self.key)
        after = self.work.earnings.monitor.overview()
        self.assertFalse(after["enabled"])
        self.assertEqual(after["hours"], 6)
        self.assertEqual(after["next_check"], before["next_check"])


class TimeTests(unittest.TestCase):
    def test_follow_up_interval_keeps_timezone(self):
        now = timestamp()
        self.assertEqual((datetime.fromisoformat(later(now, 24))-datetime.fromisoformat(now)).total_seconds(), 86400)
