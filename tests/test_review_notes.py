"""Task-scoped clinical notes and cached registration sequence matching."""
import tempfile
import unittest
from pathlib import Path

from vghks_bot.bot import BotApplication
from vghks_bot.selftest_bot import BotSyntheticSDK
from vghks_bot.settings import Settings, today


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

    def test_registration_sequence_comes_from_selected_outpatient_list_without_history_query(self):
        self.task["members"][0]["registrations"] = [{"mrn": "TEST001", "visit_date": today().isoformat(),
            "section_code": "70", "room": "12", "sequence_no": "003"}]
        self.work.review.db.save("task", self.task)
        before = list(BotSyntheticSDK.calls)
        self.assertEqual(self.work.review.read_notes({"task_id": self.task["id"]})["patients"][0]["sequence_no"], "003")
        self.assertEqual(BotSyntheticSDK.calls, before)


if __name__ == "__main__":
    unittest.main()
