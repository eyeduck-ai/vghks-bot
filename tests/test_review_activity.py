"""Safe notes clearing, explicit task ancestry and forward-only default tags."""
import http.client
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from vghks_bot.bot import BotApplication
from vghks_bot.bot_server import BotServer
from vghks_bot.selftest_bot import BotSyntheticSDK, wait_task
from vghks_bot.settings import Settings
from vghks_bot.tags import classification


class ReviewActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.app = BotApplication(Settings(), self.folder, BotSyntheticSDK)
        self.key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.key)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def reopen(self, readonly=False):
        self.app.close()
        self.app = BotApplication(Settings(), self.folder, BotSyntheticSDK, read_only=readonly)
        self.work = self.app.workspace(self.key, require_entered=False)

    def group(self):
        wait_task(self.work, self.work.review.start({"kind": "resolve", "identifiers": "TEST001"})["task_id"])
        return self.work.review.save_set({"mrns": "TEST001"})

    def test_existing_account_migrates_once_preserving_custom_rules_and_old_task_snapshot(self):
        categories = [{"id": "custom", "name": "自訂", "keywords": ["提醒"], "scope": "s"}]
        self.work.save_settings({"categories": categories})
        task = self.work.review.db.save("task", {"id": "legacy", "kind": "review", "status": "completed",
            "categories": categories, "members": [{"mrn": "TEST001"}]})
        self.work.review.db.delete("preferences", "default-tags-v1")
        self.reopen()
        migrated = self.work.settings.public()["categories"]
        self.assertEqual(migrated[0], {**categories[0], "keywords": tuple(categories[0]["keywords"]), "parser": "none"})
        self.assertEqual(migrated[1]["id"], "followup")
        self.assertEqual(self.work.review.db.get("task", task["id"])["categories"], categories)
        self.work.save_settings({"categories": categories})
        self.reopen()
        self.assertEqual([tag.id for tag in self.work.settings.categories], ["custom"])

    def test_migration_respects_same_named_custom_rule_and_readonly_account(self):
        categories = [{"id": "my-followup", "name": "追蹤", "keywords": ["回診"], "scope": "ap"}]
        self.work.save_settings({"categories": categories})
        self.work.review.db.delete("preferences", "default-tags-v1")
        self.reopen()
        self.assertEqual([tag.id for tag in self.work.settings.categories], ["my-followup"])
        self.work.save_settings({"categories": [{"id": "custom", "name": "自訂", "keywords": []}]})
        self.work.review.db.delete("preferences", "default-tags-v1")
        self.reopen(readonly=True)
        self.assertEqual([tag.id for tag in self.work.settings.categories], ["custom"])
        self.assertIsNone(self.work.review.db.get("preferences", "default-tags-v1", required=False))

    def test_new_review_reclassifies_reused_soap_without_changing_previous_review(self):
        old_rules = [rule for rule in Settings().public()["categories"] if rule["id"] != "followup"]
        self.work.save_settings({"categories": old_rules})
        group = self.group()
        old = wait_task(self.work, self.work.review.start({"set_id": group["id"]})["task_id"])
        self.assertEqual(old["status"], "completed")
        patient = old["items"][0]
        for record in patient["records"]:
            record["soap"] = "S: # fu followup\n" + record["soap"]
            record.update(classification(record, self.work.settings))
            self.work.store.library.save_record(record, self.work.username, old["id"])
        self.work.review.db.item(old["id"], patient["mrn"], patient)
        before = list(BotSyntheticSDK.calls)
        self.work.save_settings({"categories": Settings().public()["categories"]})
        new = wait_task(self.work, self.work.review.start({"set_id": group["id"]})["task_id"])
        self.assertEqual(new["status"], "completed")
        self.assertTrue(all(record["cached"] for record in new["items"][0]["records"]))
        self.assertTrue(all(any(hit["category"] == "followup" for hit in record["matches"])
                            for record in new["items"][0]["records"]))
        previous = self.work.review.results({"id": old["id"]})
        self.assertTrue(all(not any(hit["category"] == "followup" for hit in record["matches"])
                            for record in previous["patients"][0]["records"]))
        self.assertNotIn("followup", [tag["id"] for tag in previous["task"]["categories"]])
        saved_cases = {record["case_no"] for record in patient["records"]}
        self.assertEqual([call for call in before if call[1] == "soap" and call[3] in saved_cases],
                         [call for call in BotSyntheticSDK.calls if call[1] == "soap" and call[3] in saved_cases])

    def test_full_custom_tag_list_is_preserved_and_migration_retries_after_room_is_available(self):
        categories = [{"id": f"custom{i}", "name": f"自訂{i}", "keywords": []} for i in range(50)]
        self.work.save_settings({"categories": categories})
        self.work.review.db.delete("preferences", "default-tags-v1")
        self.reopen()
        self.assertEqual(len(self.work.settings.categories), 50)
        self.assertIsNone(self.work.review.db.get("preferences", "default-tags-v1", required=False))
        self.assertTrue(any("50 個" in warning for warning in self.work.store.warnings))
        self.work.save_settings({"categories": categories[:-1]})
        self.reopen()
        self.assertEqual([tag.id for tag in self.work.settings.categories],
                         [tag["id"] for tag in categories[:-1]] + ["followup"])

    def test_extension_parent_requires_same_workspace_and_review_membership(self):
        group = self.group()
        parent = wait_task(self.work, self.work.review.start({"set_id": group["id"]})["task_id"])
        record_id = parent["items"][0]["records"][0]["id"]
        for kind in ("numeric", "registrations"):
            values = {"kind": kind, "mrn": "TEST001", "record_id": record_id, "review_task_id": parent["id"]}
            child = wait_task(self.work, self.work.review.start(values)["task_id"])
            self.assertEqual(child["review_task_id"], parent["id"])
            self.assertEqual(child["status"], "completed")
            with self.assertRaisesRegex(ValueError, "不在此檢閱任務"):
                self.work.review.start({**values, "mrn": "TEST002"})
        other_key = self.app.login({"username": "OTHER", "password": "synthetic"})["account"]["id"]
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.app.workspace(other_key).review.start({"kind": "registrations", "mrn": "TEST001", "review_task_id": parent["id"]})
        standalone = wait_task(self.work, self.work.review.start({"kind": "registrations", "mrn": "TEST001"})["task_id"])
        self.assertNotIn("review_task_id", standalone)

    def test_clear_api_auth_scope_and_note_counts_without_note_text(self):
        task = self.work.review.db.save("task", {"id": "api-notes", "kind": "review", "status": "completed",
            "members": [{"mrn": "TEST001"}]})
        self.work.review.save_note({"task_id": task["id"], "mrn": "TEST001", "text": "秘密備註"})
        with BotServer(0, self.app) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            def request(path, values=None, csrf=True):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
                headers = {"Content-Type": "application/json", "Cookie": "opd_session="+self.app.session_token}
                if csrf:
                    headers["X-CSRF-Token"] = self.app.csrf_token
                connection.request("GET" if values is None else "POST", "/api/accounts/"+self.key+path,
                                   None if values is None else json.dumps(values), headers)
                response = connection.getresponse()
                result = response.status, json.loads(response.read())
                connection.close()
                return result
            try:
                status, data = request("/workbench")
                self.assertEqual(status, 200)
                self.assertEqual(data["tasks"][0]["note_count"], 1)
                self.assertNotIn("秘密備註", json.dumps(data, ensure_ascii=False))
                self.assertEqual(request("/reviews/notes/clear", {"task_id": task["id"]}, csrf=False)[0], 403)
                self.app.read_only = True
                self.assertEqual(request("/reviews/notes/clear", {"task_id": task["id"]})[0], 400)
                self.app.read_only = False
                self.assertEqual(request("/reviews/notes/clear", {"task_id": task["id"]}),
                                 (200, {"task_id": task["id"], "cleared_count": 1}))
                self.assertEqual(request("/workbench")[1]["tasks"][0]["note_count"], 0)
            finally:
                server.shutdown()
                thread.join(4)

    def test_javascript_task_hierarchy_and_pending_note_saves(self):
        subprocess.run(["node", "tests/review_ui.js"], cwd=Path(__file__).resolve().parents[1], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20)
