"""Run the synthetic suite with live progress, timing data and JUnit output."""
from __future__ import annotations

import argparse
import json
import sys
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


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


def reports(result, directory):
    duration = round(time.monotonic() - result.started, 3)
    timing = {"ok": result.wasSuccessful(), "tests": result.testsRun, "duration_seconds": duration,
              "slowest": sorted(result.timings.items(), key=lambda item: item[1], reverse=True)[:30]}
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
    args = parser.parse_args()
    args.report_dir.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=args.pattern)
    with (args.report_dir / "tests.log").open("w", encoding="utf-8") as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=Result).run(suite)
    reports(result, args.report_dir)
    if not result.wasSuccessful():
        print((args.report_dir / "tests.log").read_text(encoding="utf-8")[-16000:])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
