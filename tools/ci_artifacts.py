"""Verify complete test shards and promote an exact successful main build.

Release promotion reads GitHub Actions metadata and downloads data only. It never
executes artifact content and never accepts a PR or a different commit's build.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.test_suite import shard_for  # noqa: E402

WORKFLOW = ".github/workflows/windows.yml"
REPORT_NAMES = (
    "source-selftest.json", "exe-selftest.json", "bundle-verification.json",
    "monitor-source-selftest.json", "monitor-exe-selftest.json", "monitor-bundle-verification.json",
)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_value(*arguments):
    return subprocess.check_output(["git", *arguments], cwd=ROOT, text=True).strip()


def project_version():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def collect_tests(directory, count):
    """Fail closed on a missing, duplicated, failed or partial shard."""
    if count < 1:
        raise ValueError("Invalid shard count")
    expected, completed, seconds = None, set(), []
    for index in range(count):
        report = read_json(directory / f"shard-{index}" / "test-timings.json")
        all_ids, actual = report["all_test_ids"], report["completed_test_ids"]
        if (report.get("ok") is not True or report.get("shard_index") != index
                or report.get("shard_count") != count or not all_ids
                or sorted(set(all_ids)) != all_ids):
            raise ValueError(f"Invalid or failed shard {index}")
        if expected is None:
            expected = all_ids
        if all_ids != expected:
            raise ValueError("Shards discovered different test suites")
        selected = [name for name in expected if shard_for(name, count) == index]
        if actual != selected or report.get("tests") != len(selected) or completed.intersection(actual):
            raise ValueError(f"Incomplete or duplicated coverage in shard {index}")
        completed.update(actual)
        seconds.append(report["duration_seconds"])
    if completed != set(expected):
        raise ValueError("Test shards do not cover the complete suite")
    for name in ("source-selftest.json", "monitor-source-selftest.json"):
        if read_json(directory / name).get("ok") is not True:
            raise ValueError("Failed source self-test: " + name)
    summary = {"ok": True, "tests": len(completed), "shards": count, "shard_seconds": seconds,
               "test_ids_sha256": hashlib.sha256("\n".join(expected).encode()).hexdigest()}
    write_json(directory / "test-summary.json", summary)
    print(f"Complete suite verified: {len(completed)} tests across {count} disjoint shards; both source self-tests passed")
    return summary


def release_files(version):
    return {"VGHKS-bot.exe", "VGHKS-calendar-monitor.exe",
            f"VGHKS-bot-v{version}-windows-x64.zip", "SHA256SUMS.txt", "RELEASE_NOTES.md"}


def verify_files(directory, version):
    expected = release_files(version) - {"SHA256SUMS.txt", "RELEASE_NOTES.md"}
    sums = {}
    for line in (directory / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  ([A-Za-z0-9_.-]+)", line)
        if not match or match[2] in sums:
            raise ValueError("Invalid checksum manifest")
        sums[match[2]] = match[1]
    if set(sums) != expected or any(digest(directory / name) != value for name, value in sums.items()):
        raise ValueError("Release file names or checksums differ")
    with zipfile.ZipFile(directory / f"VGHKS-bot-v{version}-windows-x64.zip") as bundle:
        if len(bundle.namelist()) != len(set(bundle.namelist())):
            raise ValueError("Duplicate ZIP entries")
        for exe in ("VGHKS-bot.exe", "VGHKS-calendar-monitor.exe"):
            if hashlib.sha256(bundle.read(exe)).hexdigest() != sums[exe]:
                raise ValueError("ZIP executable differs from the verified release executable")
        for name in REPORT_NAMES:
            report = json.loads(bundle.read("validation/" + name))
            if report.get("ok") is not True:
                raise ValueError("Failed packaged validation: " + name)
            if "bundle-verification" in name:
                exe = "VGHKS-calendar-monitor.exe" if name.startswith("monitor-") else "VGHKS-bot.exe"
                if report.get("credentials_blank") is not True or report.get("sha256") != sums[exe]:
                    raise ValueError("Packaged executable validation differs: " + name)
    return {name: digest(directory / name) for name in sorted(release_files(version))}


def record_build(directory):
    commit = git_value("rev-parse", "HEAD")
    if (os.environ["GITHUB_EVENT_NAME"] != "push" or os.environ["GITHUB_REF"] != "refs/heads/main"
            or os.environ["GITHUB_SHA"] != commit):
        raise ValueError("Only the exact main push can produce promotable release files")
    version = project_version()
    summary = read_json(ROOT / ".build/ci/test-summary.json")
    if summary.get("ok") is not True or summary.get("tests", 0) < 1:
        raise ValueError("Missing complete test summary")
    manifest = {"schema": 1, "version": version, "commit": commit,
                "tree": git_value("rev-parse", "HEAD^{tree}"),
                "repository": os.environ["GITHUB_REPOSITORY"], "workflow": WORKFLOW,
                "run_id": int(os.environ["GITHUB_RUN_ID"]), "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
                "gas_revision": read_json(ROOT / "tests/fixtures/gas_contract.json")["revision"],
                "test_summary": summary, "files": verify_files(directory, version)}
    write_json(directory / "BUILD_INFO.json", manifest)
    print("Recorded main build provenance for " + commit)


def api(endpoint):
    return json.loads(subprocess.check_output(["gh", "api", endpoint], text=True, encoding="utf-8"))


def successful_main_run(runs, repository, commit, workflow_id):
    for run in runs:
        if (run.get("event") == "push" and run.get("head_branch") == "main"
                and run.get("head_sha") == commit and run.get("conclusion") == "success"
                and run.get("status") == "completed" and run.get("workflow_id") == workflow_id
                and run.get("path") == WORKFLOW
                and run.get("repository", {}).get("full_name") == repository
                and run.get("head_repository", {}).get("full_name") == repository):
            return run
    return None


def validate_build_info(info, run, repository, commit, tree, version, files):
    expected = {"schema": 1, "version": version, "commit": commit, "tree": tree,
                "repository": repository, "workflow": WORKFLOW, "run_id": run["id"],
                "run_attempt": run["run_attempt"], "files": files,
                "gas_revision": read_json(ROOT / "tests/fixtures/gas_contract.json")["revision"]}
    if any(info.get(key) != value for key, value in expected.items()):
        raise ValueError("Artifact provenance does not match the exact successful main run")
    if info.get("test_summary", {}).get("ok") is not True or info["test_summary"].get("tests", 0) < 1:
        raise ValueError("Artifact has no complete successful test summary")


def extract_release(archive_path, directory, version):
    allowed = release_files(version) | {"BUILD_INFO.json"}
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if set(names) != allowed or len(names) != len(allowed):
            raise ValueError("Unexpected release artifact contents")
        for name in allowed:
            (directory / name).write_bytes(archive.read(name))


def download_release(directory, tag, wait_seconds=0):
    commit, tree, version = git_value("rev-parse", "HEAD"), git_value("rev-parse", "HEAD^{tree}"), project_version()
    if tag != "v" + version or git_value("rev-parse", f"refs/tags/{tag}^{{commit}}") != commit:
        raise ValueError("Tag must match both the checked-out commit and the project version")
    repository = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    workflow = api(f"repos/{repository}/actions/workflows/windows.yml")
    if workflow["path"] != WORKFLOW:
        raise ValueError("Unexpected build workflow")
    deadline = time.monotonic() + wait_seconds
    while True:
        runs = api(f"repos/{repository}/actions/workflows/{workflow['id']}/runs?event=push&branch=main&head_sha={commit}&per_page=50")
        run = successful_main_run(runs["workflow_runs"], repository, commit, workflow["id"])
        if run:
            break
        if time.monotonic() >= deadline:
            raise ValueError("No successful main build for this exact commit. Complete or rerun its Windows workflow, then rerun this release job.")
        print("Waiting for this commit's main verification/build...", flush=True)
        time.sleep(min(30, max(0, deadline - time.monotonic())))
    artifacts = api(f"repos/{repository}/actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
    matches = [item for item in artifacts if item["name"] == "windows-" + commit and not item["expired"]]
    if len(matches) != 1:
        raise ValueError("Main build artifact missing, expired or ambiguous. Rerun that main workflow before retrying the release.")
    directory.mkdir(parents=True, exist_ok=True)
    download = directory / "download.zip"
    try:
        with download.open("wb") as stream:
            subprocess.run(["gh", "api", f"repos/{repository}/actions/artifacts/{matches[0]['id']}/zip"], stdout=stream, check=True)
        extract_release(download, directory, version)
    finally:
        download.unlink(missing_ok=True)
    files = verify_files(directory, version)
    info = read_json(directory / "BUILD_INFO.json")
    validate_build_info(info, run, repository, commit, tree, version, files)
    print(f"Verified {tag}: exact main commit {commit}, run {run['id']}, {info['test_summary']['tests']} tests and both frozen EXEs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect-tests")
    collect.add_argument("--directory", type=Path, default=ROOT / ".build/ci")
    collect.add_argument("--count", type=int, default=4)
    record = commands.add_parser("record-build")
    record.add_argument("--directory", type=Path, default=ROOT / "dist")
    download = commands.add_parser("download-release")
    download.add_argument("--directory", type=Path, default=ROOT / "release")
    download.add_argument("--tag", required=True)
    download.add_argument("--wait-seconds", type=int, default=0)
    args = parser.parse_args()
    if args.command == "collect-tests":
        collect_tests(args.directory, args.count)
    elif args.command == "record-build":
        record_build(args.directory)
    else:
        download_release(args.directory, args.tag, args.wait_seconds)


if __name__ == "__main__":
    main()
