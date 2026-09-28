"""Check the source-only publication boundary before building a public release."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from opd_monitor import __version__  # noqa: E402


def check_metadata(root=ROOT, tag=""):
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    if project["version"] != __version__:
        raise ValueError("pyproject.toml and opd_monitor.__version__ must match")
    if tag and tag != "v" + __version__:
        raise ValueError(f"Release tag must equal v{__version__}")
    sdk = next(v for v in project["dependencies"] if v.startswith("vghks-sdk @ "))
    if not re.search(r"@[a-f0-9]{40}$", sdk):
        raise ValueError("Pin vghks-sdk to a full commit")
    if sdk not in (root / "requirements-build.lock.txt").read_text(encoding="utf-8").splitlines():
        raise ValueError("Build lock and project SDK commits differ")
    defaults = json.loads((root / "opd_monitor/defaults.json").read_text(encoding="utf-8"))
    if defaults.get("username") or defaults.get("password"):
        raise ValueError("Public builds must have blank login defaults")
    return project["version"]


def check_paths(paths):
    forbidden = {".build", ".local", ".venv", "dist", "build", "private", "__pycache__",
                 "vghks-bot-data", "vghks-opd-data"}
    for name in paths:
        path = PurePosixPath(name.lower())
        if (any(part in forbidden for part in path.parts) or path.suffix in
                {".sqlite", ".sqlite3", ".db", ".exe", ".pem", ".key", ".p12", ".pfx", ".log", ".har", ".jsonl", ".zip"}
                or re.search(r"\.(?:db|sqlite3?)-", path.name)
                or path.name == ".env" or path.name.startswith(".env.") and path.name != ".env.example"
                or re.match(r"(?:credentials|service[-_]account|client_secret).*\.json$", path.name)):
            raise ValueError("Local-only file is tracked: " + name)


def main():
    version = check_metadata(tag=os.environ.get("RELEASE_TAG", ""))
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True)
    paths = [p for p in result.stdout.decode("utf-8").split("\0") if p]
    if not paths:
        raise ValueError("Stage the intended source files before checking the repository")
    check_paths(paths)
    for name in paths:
        content = (ROOT / name).read_bytes()
        if len(content) > 5 * 1024 * 1024:
            raise ValueError("Large artifact should not be committed: " + name)
        if re.search(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\r?\n[A-Za-z0-9+/=]{20}", content):
            raise ValueError("Private key material found in: " + name)
    print(f"Source boundary OK: {len(paths)} files, version {version}")


if __name__ == "__main__":
    main()
