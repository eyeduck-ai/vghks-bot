import copy
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools import ci_artifacts as ci
from tools.test_suite import select_shard, shard_for


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def reports(self):
        ids = sorted(f"synthetic.Case{index}.test_{method}" for index in range(15) for method in range(3))
        for index in range(4):
            selected = [name for name in ids if shard_for(name, 4) == index]
            ci.write_json(self.directory / f"shard-{index}/test-timings.json",
                          {"ok": True, "shard_index": index, "shard_count": 4, "all_test_ids": ids,
                           "completed_test_ids": selected, "tests": len(selected), "duration_seconds": 1})
        for name in ("source-selftest.json", "monitor-source-selftest.json"):
            ci.write_json(self.directory / name, {"ok": True})
        return ids

    def test_shards_cover_every_test_and_keep_class_lifecycle_together(self):
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(ArtifactTests)
        all_ids, groups = None, []
        for index in range(4):
            selected, ids = select_shard(suite, index, 4)
            all_ids = ids
            groups.append([case.id() for case in selected])
        self.assertEqual(sorted(name for group in groups for name in group), all_ids)
        self.assertEqual(sum(bool(group) for group in groups), 1)
        with self.assertRaises(ValueError):
            select_shard(suite, 4, 4)

    def test_collect_requires_the_complete_disjoint_successful_suite(self):
        ids = self.reports()
        summary = ci.collect_tests(self.directory, 4)
        self.assertEqual(summary["tests"], len(ids))
        self.assertTrue(ci.read_json(self.directory / "test-summary.json")["ok"])
        for mutation in ("missing", "duplicate", "failed", "different_discovery"):
            self.reports()
            path = self.directory / "shard-0/test-timings.json"
            value = ci.read_json(path)
            if mutation == "missing":
                value["completed_test_ids"].pop()
            elif mutation == "duplicate":
                value["completed_test_ids"].append(value["completed_test_ids"][0])
            elif mutation == "failed":
                value["ok"] = False
            else:
                value["all_test_ids"].pop()
            ci.write_json(path, value)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                ci.collect_tests(self.directory, 4)
        self.reports()
        ci.write_json(self.directory / "monitor-source-selftest.json", {"ok": False})
        with self.assertRaisesRegex(ValueError, "source self-test"):
            ci.collect_tests(self.directory, 4)

    def run_metadata(self):
        return {"id": 123, "run_attempt": 2, "event": "push", "head_branch": "main", "head_sha": "a" * 40,
                "conclusion": "success", "status": "completed", "workflow_id": 42, "path": ci.WORKFLOW,
                "repository": {"full_name": "owner/repo"}, "head_repository": {"full_name": "owner/repo"}}

    def test_release_accepts_only_successful_same_repository_main_commit(self):
        valid = self.run_metadata()
        self.assertEqual(ci.successful_main_run([valid], "owner/repo", "a" * 40, 42), valid)
        for field, value in (("event", "pull_request"), ("head_branch", "feature"), ("head_sha", "b" * 40),
                             ("conclusion", "failure"), ("status", "in_progress"), ("workflow_id", 43),
                             ("path", "another.yml"), ("head_repository", {"full_name": "fork/repo"}),
                             ("repository", {"full_name": "other/repo"})):
            changed = {**valid, field: value}
            with self.subTest(field=field):
                self.assertIsNone(ci.successful_main_run([changed], "owner/repo", "a" * 40, 42))

    def public_files(self):
        for name in ("VGHKS-bot.exe", "VGHKS-calendar-monitor.exe"):
            (self.directory / name).write_bytes(name.encode())
        with zipfile.ZipFile(self.directory / "VGHKS-bot-v1.2.3-windows-x64.zip", "w") as bundle:
            for exe in ("VGHKS-bot.exe", "VGHKS-calendar-monitor.exe"):
                bundle.write(self.directory / exe, exe)
            for name in ci.REPORT_NAMES:
                exe = "VGHKS-calendar-monitor.exe" if name.startswith("monitor-") else "VGHKS-bot.exe"
                bundle.writestr("validation/" + name, json.dumps(
                    {"ok": True, "credentials_blank": True, "sha256": ci.digest(self.directory / exe)}))
        names = ci.release_files("1.2.3") - {"SHA256SUMS.txt", "RELEASE_NOTES.md"}
        (self.directory / "SHA256SUMS.txt").write_text(
            "".join(f"{ci.digest(self.directory / name)}  {name}\n" for name in sorted(names)), encoding="utf-8")
        (self.directory / "RELEASE_NOTES.md").write_text("Synthetic release", encoding="utf-8")

    def test_release_checksums_zip_executables_and_frozen_reports(self):
        self.public_files()
        files = ci.verify_files(self.directory, "1.2.3")
        self.assertEqual(set(files), ci.release_files("1.2.3"))
        (self.directory / "VGHKS-bot.exe").write_bytes(b"different binary")
        with self.assertRaisesRegex(ValueError, "checksums"):
            ci.verify_files(self.directory, "1.2.3")

    def test_release_manifest_binds_run_attempt_tree_version_and_file_hashes(self):
        run = self.run_metadata()
        info = {"schema": 1, "version": "1.2.3", "commit": "a" * 40, "tree": "c" * 40,
                "repository": "owner/repo", "workflow": ci.WORKFLOW, "run_id": 123, "run_attempt": 2,
                "gas_revision": ci.read_json(ci.ROOT / "tests/fixtures/gas_contract.json")["revision"],
                "test_summary": {"ok": True, "tests": 45}, "files": {"synthetic": hashlib.sha256(b"data").hexdigest()}}
        ci.validate_build_info(info, run, "owner/repo", "a" * 40, "c" * 40, "1.2.3", info["files"])
        for field in ("commit", "tree", "version", "run_id", "run_attempt", "files", "gas_revision"):
            changed = copy.deepcopy(info)
            changed[field] = "incorrect"
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "provenance"):
                ci.validate_build_info(changed, run, "owner/repo", "a" * 40, "c" * 40, "1.2.3", info["files"])

    def test_artifact_extra_paths_are_rejected_before_any_extraction(self):
        archive = self.directory / "unexpected.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("../outside.txt", "untrusted")
        with self.assertRaisesRegex(ValueError, "contents"):
            ci.extract_release(archive, self.directory, "1.2.3")
        self.assertFalse((self.directory.parent / "outside.txt").exists())


if __name__ == "__main__":
    unittest.main()
