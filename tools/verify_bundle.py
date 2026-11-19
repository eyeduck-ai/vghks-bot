"""Read-only verification of the final executable's embedded files."""
import argparse
import hashlib
import json
import sys
from importlib.resources import files
from pathlib import Path

from biometry_parser.model_store import load_manifest
from PyInstaller.archive.readers import CArchiveReader
from vghks_sdk import __version__ as sdk_version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.exe_profiles import EXCLUDED_MODULES  # noqa: E402
from tools.release_variants import VARIANTS  # noqa: E402
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


def verify_ocr_models(names, embedded, *, variant="full"):
    base = f"vghks_bot/{BUNDLED_OCR_MODELS}/"
    expected = {}
    if variant == "full":
        artifacts = load_manifest()["models"][OCR_MODEL_NAME]["artifacts"].values()
        expected = {f"{base}{OCR_MODEL_NAME}/{row['filename']}": row["sha256"] for row in artifacts}
    model_files = {name for name in names if name.startswith(base) or name.lower().endswith(".onnx")}
    if model_files != expected.keys():
        raise ValueError("Embedded OCR model file names differ from the expected variant")
    for name, checksum in expected.items():
        if hashlib.sha256(embedded(name)).hexdigest() != checksum:
            raise ValueError("Embedded OCR model differs from the pinned manifest: " + name)
    return {Path(name).name: checksum for name, checksum in expected.items()}


def verify_pdf_packages(names, modules, variant):
    entries = {name.lower().replace("/", ".") for name in names} | {module.lower() for module in modules}

    def present(package):
        key = package.lower()
        return any(entry == key or entry.startswith((key + ".", key + "-")) for entry in entries)

    excluded = EXCLUDED_MODULES[variant]
    if variant == "basic":
        excluded = (*excluded, "pillow")  # Distribution metadata uses the PyPI name, unlike imports.
    leaked = [package for package in excluded if present(package)]
    if leaked:
        raise ValueError(f"Unexpected PDF/OCR packages in {variant} executable: " + ", ".join(leaked))
    required = {
        "basic": (),
        "lite": ("biometry_parser", "pypdfium2", "PIL", "pydantic", "psutil"),
        "full": ("biometry_parser", "pypdfium2", "PIL", "pydantic", "psutil", "rapidocr", "onnxruntime", "cv2", "numpy"),
    }
    missing = [package for package in required[variant] if not present(package)]
    if missing:
        raise ValueError(f"Missing PDF/OCR packages in {variant} executable: " + ", ".join(missing))


def verify(exe, *, root=ROOT, variant="full"):
    if variant not in BIOMETRY_EXECUTABLES:
        raise ValueError("Unknown executable variant: " + variant)
    sdk_commit = check_sdk_installation(root)
    core_commit = check_core_installation(root)
    biometry_commit = check_biometry_installation(root)
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

    static = bundled_static_assets(root)
    paths = {path.relative_to(root).as_posix(): path for path in static}
    if {name for name in names if name.startswith("vghks_bot/static/")} != set(paths):
        raise ValueError("Embedded static file names differ from the public allowlist")
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
    if embedded(f"vghks_bot/{BIOMETRY_VARIANT_FILE}") != f"{variant}\n".encode():
        raise ValueError("Executable variant marker does not match the requested variant")
    verify_pdf_packages(names, bundle.open_embedded_archive("PYZ.pyz").toc, variant)
    required_data = [] if variant == "basic" else [("biometry_parser", "model_manifest.json")]
    if variant == "full":
        required_data.extend((("rapidocr", "config.yaml"), ("rapidocr", "default_models.yaml")))
    for package, name in required_data:
        key = f"{package}/{name}"
        if embedded(key) != files(package).joinpath(name).read_bytes():
            raise ValueError("Embedded file differs: " + key)
    ocr_models = verify_ocr_models(names, embedded, variant=variant)
    if any(Path(name).name.lower().startswith("opencv_videoio_ffmpeg") and name.lower().endswith(".dll")
           for name in names):
        raise ValueError("The bundle contains the unused OpenCV video plugin")
    licenses = embedded("vghks_bot/licenses/THIRD_PARTY_NOTICES.txt")
    notices = ("vghks-sheet-core", "google-auth", "cryptography", f"vghks-sdk {sdk_version}",
               f"vghks-sdk commit {sdk_commit}", f"vghks-sheet-core commit {core_commit}",
               f"biometry-parser commit {biometry_commit}", *ocr_models)
    if any(notice.encode() not in licenses for notice in notices):
        raise ValueError("Embedded third-party notices are incomplete")
    with exe.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"ok": True, "variant": variant, "executable": exe.name, "size_bytes": exe.stat().st_size,
            "sha256": checksum,
            "assets_identical": [path.relative_to(root / "vghks_bot/static").as_posix() for path in static],
            "credentials_blank": True, "third_party_notices": True,
            "ocr_model": OCR_MODEL_NAME if ocr_models else None, "ocr_weights_sha256": ocr_models,
            "sdk_version": sdk_version, "sdk_commit": sdk_commit, "sheet_core_commit": core_commit,
            "tag_scopes": {tag["id"]: tag["scope"] for tag in defaults["categories"]}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=tuple(variant.kind for variant in VARIANTS), default="basic")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    variant = next(variant for variant in VARIANTS if variant.kind == args.variant)
    target = args.report or ROOT / ".build" / variant.verification
    report = verify(ROOT / "dist" / variant.executable, variant=variant.kind)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
