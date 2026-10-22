"""Run the synthetic suite with live progress, timing data and JUnit output."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import unittest
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TIMING_BASELINE = ROOT / "tools/test-timings-baseline.json"


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def load_baseline():
    value = json.loads(TIMING_BASELINE.read_text(encoding="utf-8"))
    if value.get("schema") != 1 or not isinstance(value.get("classes"), dict):
        raise ValueError("Invalid class timing baseline")
    return value["classes"]


def plan_shards(all_ids, count, baseline=None):
    """Reproduce one LPT plan from discovery and checked-in class timings.

    Integer milliseconds and explicit ties keep Windows runners and the Linux
    collector in agreement. Unknown classes use one second per test; known
    classes scale with their current test count. Obsolete baseline classes do
    not participate, and each TestCase stays together for class fixtures.
    """
    if type(count) is not int or count < 1:
        raise ValueError("Invalid shard count")
    ids = sorted(all_ids)
    if len(ids) != len(set(ids)) or any(not name.rpartition(".")[0] for name in ids):
        raise ValueError("Invalid or duplicate test IDs in discovery")
    baseline = load_baseline() if baseline is None else baseline
    if not isinstance(baseline, dict):
        raise ValueError("Invalid class timing baseline")
    classes = Counter(name.rpartition(".")[0] for name in ids)
    estimates = {}
    for name, tests in classes.items():
        saved = baseline.get(name)
        if saved is None:
            estimates[name] = tests * 1000
        else:
            if not isinstance(saved, dict):
                raise ValueError("Invalid timing baseline for " + name)
            saved_tests, milliseconds = saved.get("tests"), saved.get("milliseconds")
            if (type(saved_tests) is not int or saved_tests < 1
                    or type(milliseconds) is not int or milliseconds < 0):
                raise ValueError("Invalid timing baseline for " + name)
            estimates[name] = (milliseconds * tests + saved_tests // 2) // saved_tests
    loads, assignments = [0] * count, {}
    for name in sorted(classes, key=lambda name: (-estimates[name], name)):
        index = min(range(count), key=lambda index: (loads[index], index))
        assignments[name] = index
        loads[index] += estimates[name]
    identity = {"algorithm": "class-lpt-v1", "shard_count": count, "all_test_ids": ids,
                "class_shards": assignments, "class_milliseconds": estimates}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"class_shards": assignments, "estimated_shard_milliseconds": loads, "sha256": fingerprint}


def select_shard(suite, index, count):
    if type(index) is not int or type(count) is not int or count < 1 or not 0 <= index < count:
        raise ValueError("Shard index must be between zero and shard count minus one")
    tests = sorted(flatten(suite), key=lambda test: test.id())
    all_ids = [test.id() for test in tests]
    plan = plan_shards(all_ids, count)
    selected = [test for test in tests if plan["class_shards"][test.id().rpartition(".")[0]] == index]
    return unittest.TestSuite(selected), all_ids


def class_timings(timings):
    result = {}
    for name, seconds in timings.items():
        row = result.setdefault(name.rpartition(".")[0], {"tests": 0, "milliseconds": 0})
        row["tests"] += 1
        row["milliseconds"] += round(seconds * 1000)
    return dict(sorted(result.items()))


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started = self.last_progress = time.monotonic()
        self.timings = {}

    def startTest(self, test):
        self.test_started = time.monotonic()
        super().startTest(test)

    def stopTest(self, test):
        self.timings[test.id()] = round(time.monotonic() - self.test_started, 3)
        super().stopTest(test)
        now = time.monotonic()
        if self.testsRun % 25 == 0 or now - self.last_progress >= 15:
            print(f"Tests completed: {self.testsRun}; failures: {len(self.failures)}; errors: {len(self.errors)}; elapsed: {now - self.started:.0f}s", flush=True)
            self.last_progress = now


def reports(result, directory, all_ids, index=0, count=1):
    duration = round(time.monotonic() - result.started, 3)
    timing = {"ok": result.wasSuccessful(), "tests": result.testsRun, "duration_seconds": duration,
              "slowest": sorted(result.timings.items(), key=lambda item: item[1], reverse=True)[:30],
              "shard_index": index, "shard_count": count,
              "plan_sha256": plan_shards(all_ids, count)["sha256"],
              "class_timings": class_timings(result.timings),
              "all_test_ids": all_ids, "completed_test_ids": sorted(result.timings)}
    (directory / "test-timings.json").write_text(json.dumps(timing, indent=2), encoding="utf-8")
    suite = ET.Element("testsuite", name="synthetic", tests=str(result.testsRun), time=str(duration),
                       failures=str(len(result.failures) + len(result.unexpectedSuccesses)), errors=str(len(result.errors)),
                       skipped=str(len(result.skipped)))
    cases = {}
    for name, seconds in result.timings.items():
        module, _, method = name.rpartition(".")
        cases[name] = ET.SubElement(suite, "testcase", classname=module, name=method, time=str(seconds))
    for kind, records in (("failure", result.failures), ("error", result.errors), ("skipped", result.skipped)):
        for test, message in records:
            case = cases.get(test.id())
            if case is None:  # unittest subtests have a separate ID.
                case = ET.SubElement(suite, "testcase", name=test.id())
            ET.SubElement(case, kind).text = str(message)
    for test in result.unexpectedSuccesses:
        ET.SubElement(cases[test.id()], "failure").text = "Unexpected success"
    ET.ElementTree(suite).write(directory / "tests.xml", encoding="utf-8", xml_declaration=True)
    print(f"Synthetic suite: {result.testsRun} tests, {duration:.1f}s, {'passed' if result.wasSuccessful() else 'FAILED'}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--report-dir", type=Path, default=ROOT / ".build" / "ci")
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    args = parser.parse_args()
    args.report_dir.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=args.pattern)
    suite, all_ids = select_shard(suite, args.shard_index, args.shard_count)
    print(f"Shard {args.shard_index + 1}/{args.shard_count}: {suite.countTestCases()}/{len(all_ids)} tests", flush=True)
    with (args.report_dir / "tests.log").open("w", encoding="utf-8") as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=Result).run(suite)
    reports(result, args.report_dir, all_ids, args.shard_index, args.shard_count)
    if not result.wasSuccessful():
        print((args.report_dir / "tests.log").read_text(encoding="utf-8")[-16000:])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
