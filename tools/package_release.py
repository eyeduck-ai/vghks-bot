"""Package an explicit public allowlist, never the local data directory."""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vghks_bot import __version__  # noqa: E402


def main():
    output, reports = ROOT / "dist", ROOT / ".build/ci"
    exe = output / "VGHKS-bot.exe"
    check = json.loads((reports / "bundle-verification.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    if not check["ok"] or not check["credentials_blank"] or check["sha256"] != digest:
        raise ValueError("Verify this exact executable before packaging")
    archive = output / f"VGHKS-bot-v{__version__}-windows-x64.zip"
    files = [exe, ROOT / "README.md", ROOT / "CHANGELOG.md", *sorted((ROOT / "docs").glob("*.md"))]
    notices = ROOT / ".build/licenses/THIRD_PARTY_NOTICES.txt"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            bundle.write(path, path.name if path == exe else path.relative_to(ROOT).as_posix())
        bundle.write(notices, "THIRD_PARTY_NOTICES.txt")
        for filename in ("source-selftest.json", "exe-selftest.json", "bundle-verification.json"):
            bundle.write(reports / filename, "validation/" + filename)
    checksums = output / "SHA256SUMS.txt"
    checksums.write_text("".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in (exe, archive)), encoding="utf-8")
    changes = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = f"## {__version__}\n"
    if heading not in changes:
        raise ValueError("Add release notes to CHANGELOG.md before publishing")
    notes = changes.split(heading, 1)[1].split("\n## ", 1)[0].strip()
    (output / "RELEASE_NOTES.md").write_text(notes + "\n\nWindows x64 單檔 EXE。ZIP 另附操作文件、第三方授權與合成測試結果。\n"
        "\n更新時先關閉舊程式，再更換 EXE；保留原有 VGHKS-bot-data。院內功能需連線院內網路。\n", encoding="utf-8")
    print(f"Packaged {archive.name}; SHA256SUMS.txt is ready")


if __name__ == "__main__":
    main()
