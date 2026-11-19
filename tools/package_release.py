"""Package an explicit public allowlist, never the local data directory."""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.release_variants import VARIANTS, digest, verify_report  # noqa: E402
from tools.verify_repository import tracked_directory  # noqa: E402
from vghks_bot import __version__  # noqa: E402


def main(root=ROOT, version=__version__):
    output, reports = root / "dist", root / ".build/ci"
    sums = {}
    # Validate every build before writing any public archive.
    for variant in VARIANTS:
        checksum = digest(output / variant.executable)
        for name in variant.reports:
            report = json.loads((reports / name).read_text(encoding="utf-8"))
            verify_report(report, name, variant, checksum)
        sums[variant.executable] = checksum

    changes = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = f"## {version}\n"
    if heading not in changes:
        raise ValueError("Add release notes to CHANGELOG.md before publishing")
    notes = changes.split(heading, 1)[1].split("\n## ", 1)[0].strip()
    files = [root / "README.md", root / "CHANGELOG.md", *tracked_directory("docs", root=root, suffixes={".md"})]
    notices = root / ".build/licenses/THIRD_PARTY_NOTICES.txt"
    for variant in VARIANTS:
        archive = output / variant.archive(version)
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(output / variant.executable, variant.executable)
            for path in files:
                bundle.write(path, path.relative_to(root).as_posix())
            bundle.write(notices, "THIRD_PARTY_NOTICES.txt")
            for filename in variant.reports:
                bundle.write(reports / filename, "validation/" + filename)
        sums[archive.name] = digest(archive)
        print(f"Packaged {archive.name}")
    (output / "SHA256SUMS.txt").write_text(
        "".join(f"{checksum}  {name}\n" for name, checksum in sums.items()), encoding="utf-8")
    (output / "RELEASE_NOTES.md").write_text(notes + "\n\nWindows x64 主程式。ZIP 附操作文件、第三方授權與合成測試結果。\n"
        "\n`VGHKS-bot.exe` 提供 PDF 預覽與比較。`VGHKS-bot-pdf.exe` 另可擷取文字型 PDF 的結構化數據。"
        "`VGHKS-bot-ocr.exe` 內建 OCR 模型，亦可離線擷取掃描 PDF 的數據。\n"
        "\n更新時先關閉舊程式，再更換 EXE；保留完整 VGHKS-bot-data。日曆監控由獨立 vghks-sheet2cal 提供。院內功能需連線院內網路。\n", encoding="utf-8")
    print("SHA256SUMS.txt is ready")


if __name__ == "__main__":
    main()
