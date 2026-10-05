"""Task-scoped clinical notes and cached registration sequence matching."""
import tempfile
import unittest
from pathlib import Path

from vghks_bot.bot import BotApplication
from vghks_bot.selftest_bot import BotSyntheticSDK
from vghks_bot.settings import Settings, today
from vghks_bot.storage import StorageError


class ReviewNoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.app = BotApplication(Settings(), self.folder, BotSyntheticSDK)
        self.account = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(self.account)
        self.task = self.work.review.db.save("task", {"id": "note-task", "kind": "review", "status": "completed",
            "members": [{"mrn": "TEST001", "name": "合成病人", "registrations": []}]})

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_note_is_task_scoped_persistent_and_requires_membership(self):
        before = list(BotSyntheticSDK.calls)
        saved = self.work.review.save_note({"task_id": self.task["id"], "mrn": "TEST001", "text": "需追蹤\n報告"})
        self.assertEqual(saved["text"], "需追蹤\n報告")
        self.assertEqual(self.work.review.read_notes({"task_id": self.task["id"]})["patients"][0]["text"], "需追蹤\n報告")
        self.assertEqual(before, BotSyntheticSDK.calls)
        with self.assertRaisesRegex(ValueError, "不在此檢閱任務"):
            self.work.review.save_note({"task_id": self.task["id"], "mrn": "TEST002", "text": "錯誤病人"})
        with self.assertRaisesRegex(ValueError, "2,000"):
            self.work.review.save_note({"task_id": self.task["id"], "mrn": "TEST001", "text": "字" * 2001})
        self.app.close()
        self.app = BotApplication(Settings(), self.folder, BotSyntheticSDK)
        account = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(account)
        self.assertEqual(self.work.review.read_notes({"task_id": self.task["id"]})["patients"][0]["text"], "需追蹤\n報告")
        other = self.app.login({"username": "OTHER", "password": "synthetic"})["account"]["id"]
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.app.workspace(other).review.read_notes({"task_id": self.task["id"]})
        self.work.review.save_note({"task_id": self.task["id"], "mrn": "TEST001", "text": "  "})
        self.assertEqual(self.work.review.read_notes({"task_id": self.task["id"]})["patients"][0]["text"], "")

    def test_registration_sequence_uses_only_uniquely_matching_cache(self):
        day = today().isoformat()
        self.task["members"][0]["registrations"] = [{"visit_date": day, "section_code": "70",
            "section_name": "眼科", "room": "12"}]
        self.work.review.db.save("task", self.task)
        def read():
            return self.work.review.read_notes({"task_id": self.task["id"]})["patients"][0]["sequence_no"]
        self.assertEqual(read(), "")
        rows = [{"mrn": "TEST001", "visit_date": day, "section_code": "70", "section_name": "眼科",
            "room": "12", "sequence_no": "37"},
            {"mrn": "TEST001", "visit_date": day, "section_code": "10", "section_name": "內科",
             "room": "12", "sequence_no": "8"}]
        self.work.review.db.save("registrations", {"id": "TEST001", "payload": rows})
        self.assertEqual(read(), "37")
        self.work.review.db.save("registrations", {"id": "TEST001", "payload": [*rows,
            {**rows[0], "sequence_no": "38"}]})
        self.assertEqual(read(), "")
        self.work.review.db.save("registrations", {"id": "TEST001", "payload": {"legacy": rows}})
        self.assertEqual(read(), "")
        self.work.review.db.save("registrations", {"id": "TEST001", "payload": [None, rows[0]]})
        self.assertEqual(read(), "37")

    def test_clear_all_members_is_atomic_and_isolated_from_other_tasks_and_documents(self):
        self.task["members"].append({"mrn": "TEST002", "name": "合成病人乙", "registrations": []})
        self.work.review.db.save("task", self.task)
        other = self.work.review.db.save("task", {**self.task, "id": "note-task-other"})
        for mrn in ("TEST001", "TEST002"):
            self.work.review.save_note({"task_id": self.task["id"], "mrn": mrn, "text": mrn})
        self.work.review.save_note({"task_id": other["id"], "mrn": "TEST001", "text": "另一任務"})
        self.work.review.db.save("numeric", {"id": self.task["id"] + ":TEST001", "payload": "保留"})
        self.assertEqual(self.work.review.db.review_note_counts(), {self.task["id"]: 2, other["id"]: 1})
        before = list(BotSyntheticSDK.calls)
        self.assertEqual(self.work.review.clear_notes({"task_id": self.task["id"], "q": "no match", "tag": "review"}),
                         {"task_id": self.task["id"], "cleared_count": 2})
        self.assertTrue(all(not row["text"] for row in self.work.review.read_notes({"task_id": self.task["id"]})["patients"]))
        self.assertEqual(self.work.review.read_notes({"task_id": other["id"]})["patients"][0]["text"], "另一任務")
        self.assertEqual(self.work.review.db.get("numeric", self.task["id"] + ":TEST001")["payload"], "保留")
        self.assertEqual(self.work.review.db.review_note_counts(), {other["id"]: 1})
        self.assertEqual(self.work.review.clear_notes({"task_id": self.task["id"]})["cleared_count"], 0)
        self.assertEqual(BotSyntheticSDK.calls, before)
        self.app.close()
        self.app = BotApplication(Settings(), self.folder, BotSyntheticSDK)
        key = self.app.login({"username": "TEST", "password": "synthetic"})["account"]["id"]
        self.work = self.app.workspace(key)
        self.assertTrue(all(not row["text"] for row in self.work.review.read_notes({"task_id": self.task["id"]})["patients"]))
        self.assertEqual(self.work.review.read_notes({"task_id": other["id"]})["patients"][0]["text"], "另一任務")

    def test_clear_failure_rolls_back_every_note(self):
        self.task["members"].append({"mrn": "TEST002", "registrations": []})
        self.work.review.db.save("task", self.task)
        for mrn in ("TEST001", "TEST002"):
            self.work.review.save_note({"task_id": self.task["id"], "mrn": mrn, "text": "保留備註"})
        with self.work.store.library.connect() as db:
            db.execute("CREATE TRIGGER fail_note_delete BEFORE DELETE ON bot_documents "
                "WHEN OLD.kind='review_note' AND OLD.id='note-task:TEST002' "
                "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(StorageError):
            self.work.review.clear_notes({"task_id": self.task["id"]})
        self.assertEqual([row["text"] for row in self.work.review.read_notes({"task_id": self.task["id"]})["patients"]],
                         ["保留備註", "保留備註"])

    def test_clear_rejects_wrong_account_kind_and_readonly(self):
        self.work.review.save_note({"task_id": self.task["id"], "mrn": "TEST001", "text": "保留備註"})
        other = self.app.login({"username": "OTHER", "password": "synthetic"})["account"]["id"]
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.app.workspace(other).review.clear_notes({"task_id": self.task["id"]})
        self.work.review.db.save("task", {"id": "not-review", "kind": "numeric", "status": "completed"})
        with self.assertRaisesRegex(ValueError, "不是病歷檢閱"):
            self.work.review.clear_notes({"task_id": "not-review"})
        self.app.read_only = True
        with self.assertRaisesRegex(ValueError, "唯讀"):
            self.work.review.clear_notes({"task_id": self.task["id"]})
        self.assertEqual(self.work.review.read_notes({"task_id": self.task["id"]})["patients"][0]["text"], "保留備註")

    def test_registration_sequence_comes_from_selected_outpatient_list_without_history_query(self):
        self.task["members"][0]["registrations"] = [{"mrn": "TEST001", "visit_date": today().isoformat(),
            "section_code": "70", "room": "12", "sequence_no": "003"}]
        self.work.review.db.save("task", self.task)
        before = list(BotSyntheticSDK.calls)
        self.assertEqual(self.work.review.read_notes({"task_id": self.task["id"]})["patients"][0]["sequence_no"], "003")
        self.assertEqual(BotSyntheticSDK.calls, before)


if __name__ == "__main__":
    unittest.main()
