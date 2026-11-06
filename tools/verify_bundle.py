"""Read-only verification of the final executable's embedded files."""
import argparse
import hashlib
import json
import sys
from importlib.resources import files
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader
from vghks_sdk import __version__ as sdk_version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.verify_repository import (  # noqa: E402
    check_core_installation,
    check_sdk_installation,
    tracked_directory,
)


def verify(exe, *, root=ROOT):
    sdk_commit = check_sdk_installation(root)
    core_commit = check_core_installation(root)
    bundle = CArchiveReader(str(exe))
    names = {name.replace("\\", "/"): name for name in bundle.toc}
    if len(names) != len(bundle.toc):
        raise ValueError("Duplicate embedded file names")
    if any("calendar-monitor" in name for name in names):
        raise ValueError("Monitor assets belong in the independent repository")

    def embedded(key):
        if key not in names:
            raise ValueError("Missing embedded file: " + key)
        return bundle.extract(names[key])

    static = tracked_directory("vghks_bot/static", root=root)
    paths = {path.relative_to(root).as_posix(): path for path in static}
    if {name for name in names if name.startswith("vghks_bot/static/")} != set(paths):
        raise ValueError("Embedded static file names differ from the tracked allowlist")
    for key, path in paths.items():
        if embedded(key) != path.read_bytes():
            raise ValueError("Embedded file differs: " + key)

    defaults = json.loads(embedded("vghks_bot/defaults.json"))
    if defaults["username"] or defaults["password"]:
        raise ValueError("Embedded login defaults must be blank")
    expected = json.loads((root / "vghks_bot" / "defaults.json").read_text(encoding="utf-8"))
    if defaults["categories"] != expected["categories"]:
        raise ValueError("Embedded tag defaults differ from source")

    core_root = files("vghks_sheet_core")
    for data in core_root.joinpath("data").rglob("*.json"):
        key = "vghks_sheet_core/" + data.relative_to(core_root).as_posix()
        if embedded(key) != data.read_bytes():
            raise ValueError("Embedded file differs: " + key)
    licenses = embedded("vghks_bot/licenses/THIRD_PARTY_NOTICES.txt")
    notices = ("vghks-sheet-core", "google-auth", "cryptography", f"vghks-sdk {sdk_version}",
               f"vghks-sdk commit {sdk_commit}", f"vghks-sheet-core commit {core_commit}")
    if any(notice.encode() not in licenses for notice in notices):
        raise ValueError("Embedded third-party notices are incomplete")
    return {"ok": True, "executable": exe.name, "size_bytes": exe.stat().st_size,
            "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
            "assets_identical": [path.relative_to(root / "vghks_bot/static").as_posix() for path in static],
            "credentials_blank": True, "third_party_notices": True,
            "sdk_version": sdk_version, "sdk_commit": sdk_commit, "sheet_core_commit": core_commit,
            "tag_scopes": {tag["id"]: tag["scope"] for tag in defaults["categories"]}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=ROOT / ".build" / "bundle-verification.json")
    args = parser.parse_args()
    report = verify(ROOT / "dist" / "VGHKS-bot.exe")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
