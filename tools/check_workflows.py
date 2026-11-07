"""Check workflows with a checksum-pinned actionlint, before installing app dependencies.

The first run requires --install. Later checks are offline and verify the cached
executable on every invocation; a modified cache is never executed.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.7.12"
RELEASE = f"https://github.com/rhysd/actionlint/releases/download/v{VERSION}"
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
ASSETS = {
    "windows_amd64": {
        "archive": f"actionlint_{VERSION}_windows_amd64.zip", "executable": "actionlint.exe",
        "archive_sha256": "6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9",
        "executable_sha256": "54ca21be3de4c7cfa26914aa8b61bd76bf573ef3caac5f80d110558cdf241718",
    },
    "linux_amd64": {
        "archive": f"actionlint_{VERSION}_linux_amd64.tar.gz", "executable": "actionlint",
        "archive_sha256": "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
        "executable_sha256": "c872d6db8c6bf83a8eaa704fc93999f027d55dffbc63b8a6abdccb47df5f4cd4",
    },
}
PUBLIC_MEMBERS = {"LICENSE.txt", "README.md", "docs/README.md", "docs/api.md", "docs/checks.md",
                  "docs/config.md", "docs/install.md", "docs/reference.md", "docs/usage.md", "man/actionlint.1"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def target_platform():
    operating_system, architecture = platform.system().lower(), platform.machine().lower()
    if operating_system not in {"windows", "linux"} or architecture not in {"amd64", "x86_64"}:
        raise ValueError("Pinned actionlint supports Windows/Linux x64; add reviewed official hashes for other targets")
    return operating_system + "_amd64"


def confined(path, root):
    root, path = root.resolve(), path.absolute()
    if not path.is_relative_to(root) or path.resolve() != path:
        raise ValueError("Actionlint cache/report path must stay within the workspace without links")
    return path


def download_asset(url):
    request = urllib.request.Request(url, headers={"User-Agent": "vghks-bot-actionlint/" + VERSION})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(MAX_ARCHIVE_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError("Cannot download the pinned actionlint archive; check GitHub access and retry --install") from exc
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("Actionlint archive exceeds the download limit")
    return data


def executable_bytes(data, asset):
    """Read the one allowed regular executable in memory; never extract archive paths."""
    allowed, output = PUBLIC_MEMBERS | {asset["executable"]}, None
    if asset["archive"].endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > 32 or sum(member.file_size for member in members) > MAX_EXPANDED_BYTES:
                raise ValueError("Actionlint archive exceeds extraction limits")
            names = [member.filename for member in members]
            if len(names) != len(set(names)):
                raise ValueError("Duplicate actionlint archive members")
            for member in members:
                mode = member.external_attr >> 16
                if (member.filename not in allowed or member.is_dir()
                        or mode & 0o170000 not in {0, 0o100000} or member.flag_bits & 1):
                    raise ValueError("Unexpected actionlint archive member")
                if member.filename == asset["executable"]:
                    output = archive.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r|gz") as archive:
            names, expanded = set(), 0
            for member in archive:
                expanded += member.size
                if len(names) >= 32 or expanded > MAX_EXPANDED_BYTES:
                    raise ValueError("Actionlint archive exceeds extraction limits")
                if member.name in names:
                    raise ValueError("Duplicate actionlint archive members")
                names.add(member.name)
                if member.name not in allowed or not member.isfile():
                    raise ValueError("Unexpected actionlint archive member")
                if member.name == asset["executable"]:
                    stream = archive.extractfile(member)
                    with stream:
                        output = stream.read(MAX_EXPANDED_BYTES + 1)
    if output is None or digest(output) != asset["executable_sha256"]:
        raise ValueError("Actionlint executable checksum mismatch")
    return output


def ensure_executable(root=ROOT, *, install=False, target=None):
    target = target or target_platform()
    if target not in ASSETS:
        raise ValueError("Unpinned actionlint platform")
    asset = ASSETS[target]
    directory = confined(root / ".build" / "toolcache" / "actionlint" / VERSION / target, root)
    executable = confined(directory / asset["executable"], root)
    if (executable.is_file() and executable.stat().st_size <= MAX_EXPANDED_BYTES
            and digest(executable.read_bytes()) == asset["executable_sha256"]):
        return executable, asset
    if not install:
        raise RuntimeError("Pinned actionlint is missing or its checksum changed. Run: python tools/check_workflows.py --install")
    archive = download_asset(RELEASE + "/" + asset["archive"])
    if digest(archive) != asset["archive_sha256"]:
        raise ValueError("Actionlint archive checksum mismatch")
    payload = executable_bytes(archive, asset)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".actionlint-", dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            temporary.chmod(0o755)
        temporary.replace(executable)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return executable, asset


def invoke(executable, arguments, root=ROOT):
    return subprocess.run([str(executable), *map(str, arguments)], cwd=root, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=30)


def smoke_tests(executable, root=ROOT):
    valid = "name: synthetic\non: push\njobs:\n  check:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo synthetic\n"
    invalid_expression = valid.replace("    steps:", "    env:\n      TEMP: ${{ runner.temp }}\n    steps:")
    cases = (("valid-workflow", valid, True), ("invalid-yaml", "jobs: [\n", False),
             ("invalid-context", invalid_expression, False))
    directory = confined(root / ".build" / "actionlint-smoke", root)
    directory.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(dir=directory, prefix="synthetic-") as temporary:
        for name, text, should_pass in cases:
            path = Path(temporary) / (name + ".yml")
            path.write_text(text, encoding="utf-8")
            result = invoke(executable, ["-shellcheck=", "-pyflakes=", path], root)
            if result.returncode not in {0, 1} or (result.returncode == 0) != should_pass:
                raise RuntimeError("Pinned actionlint failed its parser regression: " + name)
            results.append({"name": name, "expected_valid": should_pass, "returncode": result.returncode})
    return results


def check(root=ROOT, *, install=False, self_test=False, target=None):
    executable, asset = ensure_executable(root, install=install, target=target)
    version = invoke(executable, ["-version"], root)
    if version.returncode or version.stdout.splitlines()[0:1] != [VERSION]:
        raise ValueError("Actionlint executable version mismatch")
    workflow_directory = root / ".github" / "workflows"
    workflows = sorted([*workflow_directory.glob("*.yml"), *workflow_directory.glob("*.yaml")])
    if not workflows:
        raise ValueError("No GitHub workflow files found")
    for workflow in workflows:
        confined(workflow, root)
    regressions = smoke_tests(executable, root) if self_test else []
    result = invoke(executable, ["-shellcheck=", "-pyflakes=", *workflows], root)
    diagnostics = (result.stdout + result.stderr).strip()
    report = {"schema": 1, "ok": result.returncode == 0, "version": VERSION,
              "archive": asset["archive"], "archive_sha256": asset["archive_sha256"],
              "executable_sha256": asset["executable_sha256"], "source": RELEASE,
              "workflows": [path.relative_to(root).as_posix() for path in workflows],
              "self_tests": regressions, "diagnostics": diagnostics}
    return result.returncode, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Download only the pinned checksum-verified tool when needed")
    parser.add_argument("--self-test", action="store_true", help="Check valid YAML, invalid YAML and invalid expressions with the real tool")
    args = parser.parse_args()
    report = {"schema": 1, "ok": False, "version": VERSION}
    try:
        status, report = check(install=args.install, self_test=args.self_test)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile) as exc:
        status, report["diagnostics"] = 1, str(exc)
    report_path = confined(ROOT / ".build" / "ci" / "actionlint.json", ROOT)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if report.get("diagnostics"):
        print(report["diagnostics"], file=sys.stderr)
    print(f"actionlint {VERSION}: {'passed' if status == 0 else 'FAILED'}; report: {report_path}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
