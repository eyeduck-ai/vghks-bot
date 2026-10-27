"""Bounded snapshot pages must not decode other patients' SOAP bodies."""
import copy
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from vghks_bot.bot import BotApplication
from vghks_bot.bot_server import BotServer
from vghks_bot.review_pages import literal_span
from vghks_bot.selftest_bot import BotSyntheticSDK
from vghks_bot.settings import Settings
from vghks_bot.workbench import snapshot


class ReviewPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = BotApplication(Settings(), Path(self.temp.name), BotSyntheticSDK)
        self.account = self.app.login({"username": "PAGE", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.account)
        self.review, self.store = self.work.review, self.work.review.db
        self.pages = self.review.pages

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def seed(self, count=85, key="pages"):
        members = [{"mrn": f"PAGE{i:04}", "name": f"合成{i}", "registrations": []} for i in range(count)]
        task = self.store.save("task", {"id": key, "kind": "review", "status": "completed", "mode": "latest",
            "cutoff": "2026-10-27", "account_id": self.account, "members": members})
        with self.work.store.library.batch():
            for index, member in enumerate(members):
                record = {"id": f"record-{index}", "mrn": member["mrn"], "date": "2026-10-20", "section": "眼科", "case_no": str(index),
                          "soap": ("Straße 100%_ match" if index % 2 else "unmatched") + " synthetic body " * 500,
                          "matches": [{"category": "followup", "category_name": "追蹤"}] if index % 3 else [],
                          "tag_scope_issues": ["synthetic"] if index == 2 else []}
                self.store.item(key, member["mrn"], {**member, "records": [record], "status": "ready"})
        return task

    def test_page_is_metadata_only_and_sql_is_bounded(self):
        self.seed(1000)
        decoded, sql, original_loads, original_connect = [], [], json.loads, sqlite3.connect

        def loads(value, *args, **kwargs):
            if isinstance(value, str) and '"soap"' in value:
                decoded.append(value)
            return original_loads(value, *args, **kwargs)

        def connect(*args, **kwargs):
            db = original_connect(*args, **kwargs)
            db.set_trace_callback(sql.append)
            return db

        with patch("json.loads", side_effect=loads), patch("vghks_bot.library.sqlite3.connect", side_effect=connect):
            result = self.pages.page({"id": "pages", "offset": 40, "selected_mrn": "PAGE0999"})
        self.assertEqual((result["total"], len(result["patients"]), result["page"]), (1000, 40, 1))
        self.assertEqual(result["patients"][0]["mrn"], "PAGE0040")
        self.assertNotIn("members", result["task"])
        self.assertEqual(result["task"]["member_count"], 1000)
        self.assertTrue(result["has_results"])
        self.assertFalse(decoded)
        self.assertLessEqual(len(sql), 22)
        self.assertLess(len(json.dumps(result)), 80000)
        detail = self.pages.patient({"id": "pages", "mrn": "PAGE0999"})
        self.assertEqual(result["selected_revision"], detail["revision"])
        self.assertEqual(result["selected_navigation"]["position"], 999)

    def test_pending_and_empty_legacy_members_need_no_optional_fields(self):
        self.store.save("task", {"id": "minimal", "kind": "review", "members": [{"mrn": "PAGE0000"}], "status": "running"})
        page = self.pages.page({"id": "minimal"})
        self.assertEqual(page["patients"][0]["status"], "pending")
        self.assertFalse(page["has_results"])
        self.assertEqual(self.pages.notes({"task_id": "minimal", "mrns": ["PAGE0000"]})["patients"][0]["sequence_no"], "")
        self.store.save("task", {"id": "empty", "kind": "review", "members": [], "status": "completed"})
        self.assertEqual(self.pages.page({"id": "empty"})["patients"], [])

    def test_badge_projection_omits_clinical_excerpts_and_progress_keeps_member_rows(self):
        task = self.seed(85)
        patient = self.store.item("pages", "PAGE0001")
        patient["records"][0]["matches"][0].update(excerpt="private clinical body " * 20000, source_start=1, source_end=400000)
        before = self.store.task_summaries(key="pages")[0]["revision"]
        self.store.item("pages", "PAGE0001", patient)
        self.assertGreater(self.store.task_summaries(key="pages")[0]["revision"], before)
        page = self.pages.page({"id": "pages"})
        self.assertEqual(page["patients"][1]["auto_tags"], [{"category": "followup", "category_name": "追蹤"}])
        self.assertLess(len(json.dumps(page)), 80000)
        with self.work.store.library.connect() as db:
            before_ids = [tuple(row) for row in db.execute("SELECT rowid,mrn FROM review_page_members WHERE task_id='pages' ORDER BY ordinal")]
        task["message"] = "new progress"
        with self.work.store.library.batch() as db:
            before_changes = db.total_changes
            self.store.save("task", task)
            self.assertLess(db.total_changes - before_changes, 20, "progress rewrote all member projections")
        with self.work.store.library.connect() as db:
            self.assertEqual(before_ids, [tuple(row) for row in db.execute("SELECT rowid,mrn FROM review_page_members WHERE task_id='pages' ORDER BY ordinal")])
        before = self.store.revisions().get("patient_tags", 0)
        self.work.store.library.set_patient_tags(["PAGE0001"], ["surgery"])
        self.assertGreater(self.store.revisions()["patient_tags"], before)
        task["members"].append({"mrn": "PAGE9999", "name": "new member"})
        task["members"][0]["handoff_sources"] = [{"record": {"soap": "private evidence " * 10000}}]
        self.store.save("task", task)
        self.assertEqual(self.pages.page({"id": "pages"})["all_total"], 86)
        with self.work.store.library.connect() as db:
            source = db.execute("SELECT source_json FROM review_page_member_source WHERE task_id='pages'").fetchone()[0]
        self.assertNotIn("private evidence", source)

    def test_full_scope_search_facets_and_snapshot_version(self):
        self.seed()
        self.work.store.library.set_patient_tags(["PAGE0000", "PAGE0001"], ["surgery"])
        for values in ({}, {"q": "STRASSE"}, {"q": "100%_"}, {"tag": "followup"}, {"q": "match", "tag": "surgery"}):
            old = self.review.results({"id": "pages", **values})
            page = self.pages.page({"id": "pages", "limit": 100, **values})
            self.assertEqual([p["mrn"] for p in old["patients"]], [p["mrn"] for p in page["patients"]])
            for field in ("total", "all_total", "tag_counts", "scope_issue_count", "incremental"):
                self.assertEqual(old[field], page[field], field)
        before = self.pages.patient({"id": "pages", "mrn": "PAGE0001", "q": "STRASSE"})
        self.work.store.library.save_record({**before["patient"]["records"][0], "soap": "current library changed"}, "PAGE", "synthetic")
        after = self.pages.patient({"id": "pages", "mrn": "PAGE0001", "q": "STRASSE"})
        self.assertEqual(before, after)
        self.assertEqual(after["previous_mrn"], None)
        self.assertEqual(after["next_mrn"], "PAGE0003")

    def test_patient_revision_changes_only_for_target_and_ignores_notes(self):
        self.seed(3)
        before = self.pages.page({"id": "pages", "selected_mrn": "PAGE0000"})
        self.review.save_note({"task_id": "pages", "mrn": "PAGE0000", "text": "keep typing"})
        noted = self.pages.page({"id": "pages", "selected_mrn": "PAGE0000"})
        self.assertEqual(before["selected_revision"], noted["selected_revision"])
        other = self.store.item("pages", "PAGE0001")
        other["records"][0]["soap"] += " new"
        self.store.item("pages", "PAGE0001", other)
        changed = self.pages.page({"id": "pages", "selected_mrn": "PAGE0000"})
        self.assertEqual(before["selected_revision"], changed["selected_revision"])
        self.assertNotEqual(before["patient_revisions"]["PAGE0001"], changed["patient_revisions"]["PAGE0001"])
        self.store.save("profile", {"id": "PAGE0000", "mrn": "PAGE0000", "status": "resolved", "name": "新合成姓名", "mobile_phone": "0900000000"})
        profile = self.pages.page({"id": "pages", "selected_mrn": "PAGE0000"})
        self.assertNotEqual(before["selected_revision"], profile["selected_revision"])
        self.assertNotIn("mobile_phone", profile["patients"][0])
        self.assertEqual(self.pages.patient({"id": "pages", "mrn": "PAGE0000"})["patient"]["mobile_phone"], "0900000000")

    def test_focus_cross_page_notes_and_scope(self):
        self.seed()
        page = self.pages.page({"id": "pages", "focus_mrn": "PAGE0084"})
        self.assertEqual((page["offset"], page["focus_found"]), (80, True))
        missing = self.pages.page({"id": "pages", "focus_mrn": "PAGE0084", "q": "STRASSE"})
        self.assertFalse(missing["focus_found"])
        self.review.save_note({"task_id": "pages", "mrn": "PAGE0084", "text": "page note"})
        notes = self.pages.notes({"task_id": "pages", "mrns": ["PAGE0084", "PAGE0000"]})
        self.assertEqual([row["text"] for row in notes["patients"]], ["page note", ""])
        self.assertEqual(self.pages.notes({"task_id": "pages"}, summary=True)["note_count"], 1)
        for values in ({"mrn": "FOREIGN"}, {"limit": 101}, {"offset": -1}, {"q": "x" * 501}):
            with self.assertRaises(ValueError):
                (self.pages.patient if "mrn" in values else self.pages.page)({"id": "pages", **values})
        with self.assertRaises(ValueError):
            self.pages.notes({"task_id": "pages", "mrns": ["FOREIGN"]})
        other = self.app.login({"username": "SECOND", "password": "synthetic"})["account"]["id"]
        with self.assertRaises(ValueError):
            self.app.workspace(other).review.pages.page({"id": "pages"})

    def test_regex_uses_one_whole_task_deadline_and_reuses_highlights(self):
        self.seed(85)
        with patch("vghks_bot.review_pages.regex_search", return_value={1: [[0, 6]], 81: [[0, 6]]}) as search:
            page = self.pages.page({"id": "pages", "q": "Straße", "search_mode": "regex", "limit": 1})
            detail = self.pages.patient({"id": "pages", "mrn": "PAGE0081", "q": "Straße", "search_mode": "regex"})
        self.assertEqual((page["total"], len(page["patients"])), (2, 1))
        self.assertEqual(search.call_count, 1)
        self.assertEqual(len(search.call_args.args[1]), 85)
        self.assertEqual(detail["patient"]["records"][0]["highlights"], [[0, 6]])
        with patch("vghks_bot.review_pages.regex_search", side_effect=ValueError("regex 執行逾時")):
            with self.assertRaisesRegex(ValueError, "逾時"):
                self.pages.page({"id": "pages", "q": "slow", "search_mode": "regex"})

    def test_literal_scans_once_then_pages_and_detail_use_versioned_id_cache(self):
        self.seed(85)
        sql, connect = [], sqlite3.connect

        def traced(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(sql.append)
            return db

        with patch("vghks_bot.library.sqlite3.connect", side_effect=traced):
            self.pages.page({"id": "pages", "q": "STRASSE", "selected_mrn": "PAGE0001", "limit": 1})
            self.pages.page({"id": "pages", "q": "STRASSE", "offset": 1, "limit": 1})
            self.pages.patient({"id": "pages", "q": "STRASSE", "mrn": "PAGE0001"})
        self.assertEqual(sum(statement.startswith("SELECT mrn,ordinal,soap FROM review_page_search") for statement in sql), 1)
        patient = self.store.item("pages", "PAGE0001")
        patient["records"][0]["soap"] = "replaced snapshot"
        self.store.item("pages", "PAGE0001", patient)
        self.assertEqual(self.pages.page({"id": "pages", "q": "STRASSE"})["total"], 41)

    def test_unicode_casefold_highlights_use_original_codepoint_offsets(self):
        for text, query, expected in (("Straße", "SS", "ß"), ("Straße", "STRASSE", "Straße"),
                                      ("ß 🙂 後段 needle", "NEEDLE", "needle"), ("🙂 ﬃ END", "FI", "ﬃ"),
                                      ("İstanbul", "i", "İ"), ("🙂 中文 ABC", "abc", "ABC")):
            start, end = literal_span(text, query.casefold())
            self.assertEqual(text[start:end], expected)
        self.seed(1)
        patient = self.store.item("pages", "PAGE0000")
        patient["records"][0]["soap"] = "🙂 Straße 後續"
        self.store.item("pages", "PAGE0000", patient)
        record = self.pages.patient({"id": "pages", "mrn": "PAGE0000", "q": "SS"})["patient"]["records"][0]
        start, end = record["highlights"][0]
        self.assertEqual(record["soap"][start:end], "ß")

    def test_oversized_highlights_are_not_retained_or_searched_twice_in_one_detail(self):
        self.seed(3)
        with patch("vghks_bot.review_pages.SEARCH_CACHE_BYTES", 1), \
                patch("vghks_bot.review_pages.regex_search", return_value={1: [[0, 6]]}) as search:
            detail = self.pages.patient({"id": "pages", "mrn": "PAGE0001", "q": "Straße", "search_mode": "regex"})
        self.assertEqual(search.call_count, 1)
        self.assertEqual(detail["patient"]["records"][0]["highlights"], [[0, 6]])
        self.assertFalse(self.pages.search_cache)

    def test_legacy_projection_rebuild_and_deletion(self):
        self.seed(405)
        expected = self.pages.page({"id": "pages", "offset": 400})
        with self.work.store.library.connect() as db:
            for name in ("review_page_task", "review_page_members", "review_page_member_source", "review_page_patients", "review_page_search", "review_page_ready"):
                db.execute("DELETE FROM " + name)
        self.assertEqual(expected["patients"], self.pages.page({"id": "pages", "offset": 400})["patients"])
        patient = self.store.item("pages", "PAGE0404")
        patient.update(records=[], status="deleted")
        self.store.item("pages", "PAGE0404", patient)
        self.assertEqual(self.pages.patient({"id": "pages", "mrn": "PAGE0404"})["patient"]["status"], "deleted")
        self.assertEqual(self.pages.page({"id": "pages", "q": "STRASSE"})["total"], 202)

    def test_compact_sets_can_search_hidden_member_and_expand_without_private_soap(self):
        group = self.store.save("set", {"id": "group", "name": "合成集合", "members": [{"mrn": f"PAGE{i:04}", "name": "合成"} for i in range(100)]})
        compact = snapshot(self.work, compact=True)["sets"][0]
        self.assertEqual(compact["member_count"], 100)
        self.assertNotIn("members", compact)
        self.assertEqual(len(compact["members_preview"]), 3)
        self.assertEqual(self.store.set_summaries("PAGE0099")[0]["id"], group["id"])
        self.assertEqual(len(snapshot(self.work)["sets"][0]["members"]), 100)

    def test_handoff_uses_only_selected_snapshot(self):
        self.seed(50)
        selected = copy.deepcopy(self.store.item("pages", "PAGE0049")["records"][0])
        self.work.store.library.save_record(selected, self.work.username, "synthetic")
        with patch.object(self.review, "results", side_effect=AssertionError("full results loaded")):
            result = self.review.handoff({"module": "retina", "review_task_id": "pages", "mrns": ["PAGE0049"]})
        self.assertEqual(result["cohort"]["members"][0]["mrn"], "PAGE0049")

    def test_http_local_reads_keep_cookie_csrf_account_offline_readonly_guards(self):
        self.seed(45)
        self.store.save("set", {"id": "private-group", "name": "synthetic", "members": [{"mrn": "PAGE0000"}]})
        other = self.app.login({"username": "SECOND", "password": "synthetic"})["account"]["id"]
        self.app.offline(self.account)
        server = BotServer(0, self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(path, values, *, account=None, cookie=True, csrf=True):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            headers = {"Content-Type": "application/json", "Origin": server.origin}
            if cookie:
                headers["Cookie"] = "opd_session=" + self.app.session_token
            if csrf:
                headers["X-CSRF-Token"] = self.app.csrf_token
            connection.request("POST", "/api/accounts/" + (account or self.account) + path, json.dumps(values), headers)
            response = connection.getresponse()
            value = json.loads(response.read())
            connection.close()
            return response.status, value

        try:
            self.assertEqual(request("/reviews/patients", {"id": "pages"}, cookie=False)[0], 401)
            self.assertEqual(request("/reviews/patients", {"id": "pages"}, csrf=False)[0], 403)
            self.assertEqual(request("/reviews/patients", {"id": "pages"}, account=other)[0], 400)
            self.assertEqual(request("/sets/read", {"id": "private-group"}, account=other)[0], 400)
            self.app.read_only = True
            for path, values in (("/reviews/patients", {"id": "pages"}), ("/reviews/patient", {"id": "pages", "mrn": "PAGE0044"}),
                                 ("/reviews/notes/page", {"task_id": "pages", "mrns": ["PAGE0044"]}),
                                 ("/reviews/notes/summary", {"task_id": "pages"}), ("/sets/read", {"id": "private-group"}), ("/sets/search", {"q": "PAGE0000"})):
                self.assertEqual(request(path, values)[0], 200, path)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            self.app.read_only = False
