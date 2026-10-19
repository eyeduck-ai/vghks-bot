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

from tools.verify_repository import check_core_installation, check_sdk_installation  # noqa: E402

sdk_commit = check_sdk_installation()
core_commit = check_core_installation()
parser = argparse.ArgumentParser()
parser.add_argument("--report", type=Path, default=ROOT / ".build" / "bundle-verification.json")
args = parser.parse_args()
exe = ROOT / "dist" / "VGHKS-bot.exe"
bundle = CArchiveReader(str(exe))
names = {name.replace("\\", "/"): name for name in bundle.toc}
assert not any("calendar-monitor" in name for name in names), "Monitor assets belong in the independent repository"
assets = []
for path in sorted((ROOT / "vghks_bot" / "static").iterdir()):
    key = "vghks_bot/static/" + path.name
    assert bundle.extract(names[key]) == path.read_bytes(), path.name
    assets.append(path.name)
defaults = json.loads(bundle.extract(names["vghks_bot/defaults.json"]))
assert not defaults["username"] and not defaults["password"]
expected = json.loads((ROOT / "vghks_bot" / "defaults.json").read_text(encoding="utf-8"))
assert defaults["categories"] == expected["categories"]
core_root = files("vghks_sheet_core")
for data in core_root.joinpath("data").rglob("*.json"):
    key = "vghks_sheet_core/" + data.relative_to(core_root).as_posix()
    assert bundle.extract(names[key]) == data.read_bytes(), key
licenses = bundle.extract(names["vghks_bot/licenses/THIRD_PARTY_NOTICES.txt"])
for dependency in (b"vghks-sheet-core", b"google-auth", b"cryptography", b"vghks-sdk"):
    assert dependency in licenses
assert f"vghks-sdk {sdk_version}".encode() in licenses
assert f"vghks-sdk commit {sdk_commit}".encode() in licenses
assert f"vghks-sheet-core commit {core_commit}".encode() in licenses
report = {"ok": True, "executable": exe.name, "size_bytes": exe.stat().st_size, "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
          "assets_identical": assets, "credentials_blank": True, "third_party_notices": True,
          "sdk_version": sdk_version, "sdk_commit": sdk_commit,
          "sheet_core_commit": core_commit,
          "tag_scopes": {tag["id"]: tag["scope"] for tag in defaults["categories"]}}
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False))
