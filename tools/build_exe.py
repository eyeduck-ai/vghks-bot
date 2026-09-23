"""Build a single Windows EXE; defaults and assets live inside the binary."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from opd_monitor.settings import Settings  # noqa: E402


def notices() -> Path:
    directory = ROOT / ".build" / "licenses"
    directory.mkdir(parents=True, exist_ok=True)
    parts = ["Third-party notices for VGHKS-bot\n"]
    for name in ("vghks-sdk", "requests", "beautifulsoup4", "soupsieve", "truststore", "urllib3", "certifi", "charset-normalizer", "idna", "typing-extensions", "pyinstaller",
                 "google-auth", "cryptography", "cffi", "pycparser", "pyasn1", "pyasn1-modules"):
        package = importlib.metadata.distribution(name)
        parts.append(f"\n{'=' * 70}\n{name} {package.version}\n{'=' * 70}\n")
        for entry in package.files or []:
            if any(word in entry.name.upper() for word in ("LICENSE", "COPYING", "NOTICE")):
                path = Path(package.locate_file(entry))
                if path.is_file():
                    parts.append(path.read_text(encoding="utf-8", errors="replace"))
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        parts.extend(["\nPython runtime license\n", python_license.read_text(encoding="utf-8")])
    (directory / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(parts), encoding="utf-8")
    return directory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--defaults", type=Path, help="Optional private JSON defaults to embed")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("請在 Windows 上建置 Windows EXE。")
    source = args.defaults or ROOT / "opd_monitor" / "defaults.json"
    raw = json.loads(source.read_text(encoding="utf-8-sig"))
    settings = Settings().update(raw)
    staging = ROOT / ".build" / "embedded"
    staging.mkdir(parents=True, exist_ok=True)
    defaults = staging / "defaults.json"
    from dataclasses import asdict

    defaults.write_text(json.dumps(asdict(settings), ensure_ascii=False), encoding="utf-8")
    try:
        command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--noupx", "--onefile", "--windowed",
            "--name", "VGHKS-bot", "--distpath", str(ROOT / "dist"),
            "--workpath", str(ROOT / ".build" / "pyinstaller"), "--specpath", str(ROOT / ".build"),
            "--paths", str(ROOT), "--add-data", f"{ROOT / 'opd_monitor' / 'static'};opd_monitor/static",
            "--add-data", f"{defaults};opd_monitor", "--add-data", f"{notices()};opd_monitor/licenses",
            "--hidden-import", "truststore",
            "--exclude-module", "tkinter", "--exclude-module", "pytest",
            str(ROOT / "run.py")]
        environment = os.environ.copy()
        environment["PYINSTALLER_CONFIG_DIR"] = str(ROOT / ".build" / "pyinstaller-cache")
        subprocess.run(command, cwd=ROOT, env=environment, check=True)
    finally:
        defaults.unlink(missing_ok=True)
    executable = ROOT / "dist" / "VGHKS-bot.exe"
    print(f"Built {executable.name}: {executable.stat().st_size / (1024 * 1024):.1f} MiB")


if __name__ == "__main__":
    main()
