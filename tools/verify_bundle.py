"""Read-only verification of the final executable's embedded files."""
import argparse
import hashlib
import json
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader
from vghks_sdk import __version__ as sdk_version

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--report", type=Path, default=ROOT / ".build" / "bundle-verification.json")
args = parser.parse_args()
exe = ROOT / "dist" / "VGHKS-bot.exe"
bundle = CArchiveReader(str(exe))
names = {name.replace("\\", "/"): name for name in bundle.toc}
assets = []
for path in sorted((ROOT / "vghks_bot" / "static").iterdir()):
    key = "vghks_bot/static/" + path.name
    assert bundle.extract(names[key]) == path.read_bytes(), path.name
    assets.append(path.name)
defaults = json.loads(bundle.extract(names["vghks_bot/defaults.json"]))
assert not defaults["username"] and not defaults["password"]
expected = json.loads((ROOT / "vghks_bot" / "defaults.json").read_text(encoding="utf-8"))
assert defaults["categories"] == expected["categories"]
licenses = bundle.extract(names["vghks_bot/licenses/THIRD_PARTY_NOTICES.txt"])
for dependency in (b"google-auth", b"cryptography", b"vghks-sdk"):
    assert dependency in licenses
assert f"vghks-sdk {sdk_version}".encode() in licenses
report = {"ok": True, "size_bytes": exe.stat().st_size, "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
          "assets_identical": assets, "credentials_blank": True, "third_party_notices": True,
          "sdk_version": sdk_version, "tag_scopes": {tag["id"]: tag["scope"] for tag in defaults["categories"]}}
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False))
