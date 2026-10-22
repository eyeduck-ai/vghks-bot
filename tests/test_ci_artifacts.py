import copy
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools import ci_artifacts as ci
from tools.test_suite import class_timings, plan_shards, select_shard


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def reports(self, ids=None):
        if ids is None:
            ids = sorted(f"synthetic.Case{index}.test_{method}" for index in range(15) for method in range(3))
        plan = plan_shards(ids, 4)
        for index in range(4):
            selected = [name for name in ids if plan["class_shards"][name.rpartition(".")[0]] == index]
            ci.write_json(self.directory / f"shard-{index}/test-timings.json",
                          {"ok": True, "shard_index": index, "shard_count": 4, "all_test_ids": ids,
                           "plan_sha256": plan["sha256"],
                           "class_timings": class_timings(dict.fromkeys(selected, 1.0)),
                           "completed_test_ids": selected, "tests": len(selected), "duration_seconds": 1})
        for name in ("source-selftest.json",):
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

    def test_lpt_balances_measured_classes_with_deterministic_ties(self):
        costs = {"A": 90000, "B": 60000, "C": 40000, "D": 10000}
        baseline = {"synthetic." + name: {"tests": 1, "milliseconds": milliseconds}
                    for name, milliseconds in costs.items()}
        ids = ["synthetic." + name + ".test_one" for name in costs]
        plan = plan_shards(ids, 2, baseline)
        self.assertEqual(plan["estimated_shard_milliseconds"], [100000, 100000])
        self.assertEqual(plan, plan_shards(reversed(ids), 2, dict(reversed(list(baseline.items())))))
        tied = {name: {"tests": 1, "milliseconds": 1000} for name in baseline}
        self.assertEqual(plan_shards(ids, 2, tied)["class_shards"],
                         {"synthetic.A": 0, "synthetic.B": 1, "synthetic.C": 0, "synthetic.D": 1})

    def test_plan_scales_new_methods_and_keeps_unknown_classes_together(self):
        ids = [f"synthetic.Known.test_{index}" for index in range(4)]
        ids += [f"synthetic.New.test_{index}" for index in range(2)]
        baseline = {"synthetic.Known": {"tests": 2, "milliseconds": 8000}}
        plan = plan_shards(ids, 2, baseline)
        self.assertEqual(plan["estimated_shard_milliseconds"], [16000, 2000])
        self.assertEqual(plan["class_shards"], {"synthetic.Known": 0, "synthetic.New": 1})
        obsolete = {**baseline, "removed.Expensive": {"tests": 1, "milliseconds": 99999999}}
        self.assertEqual(plan, plan_shards(ids, 2, obsolete))

    def test_plan_rejects_duplicate_discovery_invalid_counts_and_invalid_measurements(self):
        name = "synthetic.Case.test_one"
        for count in (0, -1, True):
            with self.subTest(count=count), self.assertRaises(ValueError):
                plan_shards([name], count, {})
        with self.assertRaisesRegex(ValueError, "duplicate"):
            plan_shards([name, name], 2, {})
        for row in ({"tests": 0, "milliseconds": 1000}, {"tests": 1, "milliseconds": -1},
                    {"tests": True, "milliseconds": 1000}, {"tests": 1, "milliseconds": 1.5}):
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, "baseline"):
                plan_shards([name], 2, {"synthetic.Case": row})

    def test_collect_requires_the_complete_disjoint_successful_suite(self):
        ids = self.reports()
        summary = ci.collect_tests(self.directory, 4)
        self.assertEqual(summary["tests"], len(ids))
        self.assertEqual(summary["plan_sha256"], plan_shards(ids, 4)["sha256"])
        classes = ci.read_json(self.directory / "test-class-timings.json")["classes"]
        self.assertEqual(sum(row["tests"] for row in classes.values()), len(ids))
        self.assertEqual(sum(row["milliseconds"] for row in classes.values()), 45000)
        self.assertTrue(ci.read_json(self.directory / "test-summary.json")["ok"])
        for mutation in ("missing", "duplicate", "failed", "different_discovery", "plan", "wrong_shard",
                         "class_timings", "timing_count", "timing_duration", "foreign_class"):
            self.reports()
            path = self.directory / "shard-0/test-timings.json"
            value = ci.read_json(path)
            if mutation == "missing":
                value["completed_test_ids"].pop()
            elif mutation == "duplicate":
                value["completed_test_ids"].append(value["completed_test_ids"][0])
            elif mutation == "failed":
                value["ok"] = False
            elif mutation == "different_discovery":
                value["all_test_ids"].pop()
            elif mutation == "plan":
                value["plan_sha256"] = "0" * 64
            elif mutation == "wrong_shard":
                foreign = next(name for name in ids if name not in value["completed_test_ids"])
                value["completed_test_ids"] = sorted([*value["completed_test_ids"][1:], foreign])
                value["class_shards"] = {name.rpartition(".")[0]: 0 for name in value["completed_test_ids"]}
            elif mutation == "class_timings":
                value.pop("class_timings")
            elif mutation == "timing_count":
                next(iter(value["class_timings"].values()))["tests"] += 1
            elif mutation == "timing_duration":
                next(iter(value["class_timings"].values()))["milliseconds"] = -1
            elif mutation == "foreign_class":
                value["class_timings"]["foreign.Class"] = {"tests": 1, "milliseconds": 0}
            ci.write_json(path, value)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                ci.collect_tests(self.directory, 4)
        self.reports()
        ci.write_json(self.directory / "source-selftest.json", {"ok": False})
        with self.assertRaisesRegex(ValueError, "source self-test"):
            ci.collect_tests(self.directory, 4)

    def test_collect_accepts_empty_shards_and_rejects_invalid_duration_and_boolean_timings(self):
        ids = self.reports(["synthetic.Single.test_one", "synthetic.Single.test_two"])
        self.assertEqual(ci.collect_tests(self.directory, 4)["tests"], len(ids))
        for duration in (-1, float("nan"), float("inf"), True, "1"):
            self.reports()
            path = self.directory / "shard-0/test-timings.json"
            value = ci.read_json(path)
            value["duration_seconds"] = duration
            ci.write_json(path, value)
            with self.subTest(duration=duration), self.assertRaisesRegex(ValueError, "duration"):
                ci.collect_tests(self.directory, 4)
        for field in ("tests", "milliseconds"):
            self.reports()
            path = self.directory / "shard-0/test-timings.json"
            value = ci.read_json(path)
            next(iter(value["class_timings"].values()))[field] = True
            ci.write_json(path, value)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "class timing"):
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
        for name in ("VGHKS-bot.exe",):
            (self.directory / name).write_bytes(name.encode())
        with zipfile.ZipFile(self.directory / "VGHKS-bot-v1.2.3-windows-x64.zip", "w") as bundle:
            for exe in ("VGHKS-bot.exe",):
                bundle.write(self.directory / exe, exe)
            for name in ci.REPORT_NAMES:
                exe = "VGHKS-bot.exe"
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

    def test_main_release_rejects_a_bundled_monitor_even_with_matching_checksums(self):
        self.public_files()
        archive = self.directory / "VGHKS-bot-v1.2.3-windows-x64.zip"
        with zipfile.ZipFile(archive, "a") as bundle:
            bundle.writestr("VGHKS-calendar-monitor.exe", b"obsolete synthetic monitor")
        names = ci.release_files("1.2.3") - {"SHA256SUMS.txt", "RELEASE_NOTES.md"}
        (self.directory / "SHA256SUMS.txt").write_text(
            "".join(f"{ci.digest(self.directory / name)}  {name}\n" for name in sorted(names)), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Monitor artifacts"):
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
