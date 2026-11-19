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
sys.path.insert(0, str(ROOT))

from tools.release_variants import SOURCE_REPORT, VARIANTS  # noqa: E402


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
    parser.add_argument("--build", action="store_true", help="Also build, test and package all Windows EXEs")
    phase = parser.add_mutually_exclusive_group()
    phase.add_argument("--checks-only", action="store_true", help="Run fast source/lint/contract checks")
    phase.add_argument("--tests-only", action="store_true", help="Run synthetic tests and source EXE self-tests")
    phase.add_argument("--source-only", action="store_true", help="Run the main-program source self-test")
    phase.add_argument("--build-only", action="store_true", help="Build after separately required CI checks and tests")
    parser.add_argument("--gas-repo", type=Path, help="Verify the pinned GAS source, not just saved fixtures")
    args = parser.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    os.environ["PYTHONUTF8"] = "1"
    python = sys.executable
    if not (args.tests_only or args.source_only or args.build_only):
        run([python, "tools/check_workflows.py", "--self-test"])
    if args.build_only:
        run([python, "tools/verify_repository.py"])
        run([python, "-m", "pip", "check"])
        path = REPORTS / SOURCE_REPORT
        if not path.is_file():
            raise RuntimeError("Run --tests-only or download this run's test artifact before --build-only: " + SOURCE_REPORT)
        report_ok(path)
    if not (args.tests_only or args.source_only or args.build_only):
        run([python, "tools/verify_repository.py"])
        run([python, "-m", "pip", "check"])
        run([python, "-m", "ruff", "check", "."])
        for script in sorted((ROOT / "vghks_bot/static").glob("*.js")):
            run(["node", "--check", str(script)])
        contract = [python, "tools/verify_sheet_contract.py"]
        if args.gas_repo:
            contract += ["--gas-repo", str(args.gas_repo)]
        run(contract)
    if not (args.checks_only or args.source_only or args.build_only):
        run([python, "tools/test_suite.py"])
    if not (args.checks_only or args.build_only):
        source = REPORTS / SOURCE_REPORT
        run([python, "run.py", "--self-test-report", str(source)])
        report_ok(source)
    if args.build or args.build_only:
        run([python, "tools/build_exe.py"], "build.log")
        for variant in VARIANTS:
            frozen = REPORTS / variant.selftest
            run([str(ROOT / "dist" / variant.executable), "--self-test-report", str(frozen)])
            report_ok(frozen)
            run([python, "tools/verify_bundle.py", "--variant", variant.kind,
                 "--report", str(REPORTS / variant.verification)])
        run([python, "tools/package_release.py"])
    print("All requested checks passed.")


if __name__ == "__main__":
    main()
