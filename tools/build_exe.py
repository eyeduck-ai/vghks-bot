"""Build the Windows executables with embedded assets."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory

from biometry_parser.model_store import file_sha256, load_manifest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.exe_profiles import EXCLUDED_MODULES  # noqa: E402
from tools.verify_repository import (  # noqa: E402
    bundled_static_assets,
    check_biometry_installation,
    check_core_installation,
    check_sdk_installation,
)
from vghks_bot.biometry import (  # noqa: E402
    BIOMETRY_EXECUTABLES,
    BIOMETRY_VARIANT_FILE,
    BUNDLED_OCR_MODELS,
    OCR_MODEL_NAME,
)
from vghks_bot.settings import Settings  # noqa: E402


def notices() -> Path:
    sdk_commit = check_sdk_installation()
    core_commit = check_core_installation()
    biometry_commit = check_biometry_installation()
    directory = ROOT / ".build" / "licenses"
    directory.mkdir(parents=True, exist_ok=True)
    parts = ["Third-party notices for VGHKS-bot\n", f"vghks-sdk commit {sdk_commit}\n",
             f"vghks-sheet-core commit {core_commit}\n", f"biometry-parser commit {biometry_commit}\n"]
    for name in ("vghks-sdk", "requests", "beautifulsoup4", "soupsieve", "truststore", "urllib3", "certifi", "charset-normalizer", "idna", "typing-extensions", "pyinstaller",
                 "vghks-sheet-core", "google-auth", "cryptography", "cffi", "pycparser", "pyasn1", "pyasn1-modules",
                 "biometry-parser", "pypdfium2", "pydantic", "pydantic-core", "annotated-types", "typing-inspection",
                 "Pillow", "psutil", "rapidocr", "onnxruntime", "opencv-python-headless", "numpy", "pyclipper",
                 "PyYAML", "Shapely", "six", "omegaconf", "tqdm", "colorlog", "colorama", "flatbuffers", "protobuf"):
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


def ocr_model_flags():
    """Embed only the pinned weights already distributed in the RapidOCR wheel."""
    registry = load_manifest()["models"][OCR_MODEL_NAME]["artifacts"]
    wheel_models = files("rapidocr").joinpath("models")
    destination = f"vghks_bot/{BUNDLED_OCR_MODELS}/{OCR_MODEL_NAME}"
    flags = []
    for artifact in registry.values():
        model = wheel_models.joinpath(artifact["filename"])
        if not model.is_file() or file_sha256(model) != artifact["sha256"]:
            raise ValueError(f"RapidOCR wheel model is missing or differs from the pinned manifest: {model.name}")
        flags.extend(("--add-data", f"{model};{destination}"))
    return flags


def pdf_flags(variant):
    flags = [flag for module in EXCLUDED_MODULES[variant] for flag in ("--exclude-module", module)]
    if variant != "basic":
        flags.extend(("--collect-data", "biometry_parser", "--copy-metadata", "pypdfium2"))
    if variant == "full":
        flags.extend(("--copy-metadata", "rapidocr", "--hidden-import", "rapidocr.main",
                      "--hidden-import", "rapidocr.inference_engine.onnxruntime.main"))
        for name in ("config.yaml", "default_models.yaml"):
            flags.extend(("--add-data", f"{files('rapidocr').joinpath(name)};rapidocr"))
        flags.extend(ocr_model_flags())
    return flags


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--defaults", type=Path, help="Optional private JSON defaults to embed")
    parser.add_argument("--variant", choices=("all", *BIOMETRY_EXECUTABLES), default="all",
                        help="Build all three variants by default, or select full, lite or basic")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("請在 Windows 上建置 Windows EXE。")
    source = args.defaults or ROOT / "vghks_bot" / "defaults.json"
    raw = json.loads(source.read_text(encoding="utf-8-sig"))
    settings = Settings().update(raw)
    staging = ROOT / ".build" / "embedded"
    staging.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=staging) as directory:
        defaults = Path(directory) / "defaults.json"
        defaults.write_text(json.dumps(asdict(settings), ensure_ascii=False), encoding="utf-8")
        marker = Path(directory) / BIOMETRY_VARIANT_FILE
        environment = os.environ.copy()
        environment["PYINSTALLER_CONFIG_DIR"] = str(ROOT / ".build" / "pyinstaller-cache")
        license_directory = notices()
        static = bundled_static_assets()
        static_flags = [flag for path in static for flag in (
            "--add-data", f"{path};{path.parent.relative_to(ROOT).as_posix()}")]
        command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--noupx", "--onefile", "--windowed",
            "--distpath", str(ROOT / "dist"),
            "--workpath", str(ROOT / ".build" / "pyinstaller"), "--specpath", str(ROOT / ".build"),
            "--additional-hooks-dir", str(ROOT / "tools" / "pyinstaller_hooks"),
            "--paths", str(ROOT), *static_flags,
            "--add-data", f"{defaults};vghks_bot", "--add-data", f"{marker};vghks_bot",
            "--add-data", f"{license_directory / 'THIRD_PARTY_NOTICES.txt'};vghks_bot/licenses",
            "--collect-data", "vghks_sheet_core", "--hidden-import", "truststore", "--exclude-module", "tkinter", "--exclude-module", "pytest"]
        variants = BIOMETRY_EXECUTABLES if args.variant == "all" else (args.variant,)
        for variant in variants:
            filename = BIOMETRY_EXECUTABLES[variant]
            marker.write_bytes(variant.encode("ascii") + b"\n")
            subprocess.run([*command, "--name", Path(filename).stem, *pdf_flags(variant), str(ROOT / "run.py")],
                           cwd=ROOT, env=environment, check=True)
            executable = ROOT / "dist" / filename
            print(f"Built {executable.name}: {executable.stat().st_size / (1024 * 1024):.1f} MiB")


if __name__ == "__main__":
    main()
