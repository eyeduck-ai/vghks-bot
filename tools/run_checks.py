"""The same synthetic validation pipeline for local builds and Windows CI."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / ".build" / "ci"


def run(arguments, log=None):
    print("Running: " + " ".join(map(str, arguments)), flush=True)
    if log:
        with (REPORTS / log).open("w", encoding="utf-8") as stream:
            result = subprocess.run(arguments, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            print((REPORTS / log).read_text(encoding="utf-8", errors="replace")[-16000:])
            result.check_returncode()
    else:
        subprocess.run(arguments, cwd=ROOT, check=True)


def report_ok(path):
    if json.loads(path.read_text(encoding="utf-8")).get("ok") is not True:
        raise RuntimeError("Self-test failed: " + path.name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true", help="Also build, test and package the Windows EXE")
    args = parser.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    os.environ["PYTHONUTF8"] = "1"
    python = sys.executable
    run([python, "tools/verify_repository.py"])
    run([python, "-m", "pip", "check"])
    run([python, "-m", "ruff", "check", "."])
    for script in sorted((ROOT / "opd_monitor/static").glob("*.js")):
        run(["node", "--check", str(script)])
    run([python, "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-v"], "tests.log")
    source = REPORTS / "source-selftest.json"
    run([python, "run.py", "--self-test-report", str(source)])
    report_ok(source)
    if args.build:
        run([python, "tools/build_exe.py"], "build.log")
        frozen = REPORTS / "exe-selftest.json"
        run([str(ROOT / "dist/VGHKS-bot.exe"), "--self-test-report", str(frozen)])
        report_ok(frozen)
        run([python, "tools/verify_bundle.py", "--report", str(REPORTS / "bundle-verification.json")])
        run([python, "tools/package_release.py"])
    print("All requested checks passed.")


if __name__ == "__main__":
    main()
