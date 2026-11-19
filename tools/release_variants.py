"""Shared names and checks for the Windows distribution variants."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from vghks_bot.biometry import BIOMETRY_EXECUTABLES

SOURCE_REPORT = "source-selftest.json"


@dataclass(frozen=True)
class ReleaseVariant:
    kind: str
    executable: str
    selftest: str
    verification: str

    def archive(self, version):
        return f"{self.executable.removesuffix('.exe')}-v{version}-windows-x64.zip"

    @property
    def reports(self):
        return SOURCE_REPORT, self.selftest, self.verification


VARIANTS = (
    ReleaseVariant("basic", BIOMETRY_EXECUTABLES["basic"], "exe-selftest.json", "bundle-verification.json"),
    ReleaseVariant("lite", BIOMETRY_EXECUTABLES["lite"], "exe-selftest-pdf.json", "bundle-verification-pdf.json"),
    ReleaseVariant("full", BIOMETRY_EXECUTABLES["full"], "exe-selftest-ocr.json", "bundle-verification-ocr.json"),
)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_report(report, name, variant, checksum):
    if report.get("ok") is not True:
        raise ValueError("Failed packaged validation: " + name)
    if name == SOURCE_REPORT:
        return
    if report.get("variant") != variant.kind:
        raise ValueError("Packaged validation names a different variant: " + name)
    if report.get("sha256") != checksum or report.get("executable") != variant.executable:
        raise ValueError("Packaged executable validation differs: " + name)
    if name == variant.verification and report.get("credentials_blank") is not True:
        raise ValueError("Packaged executable verification has non-blank login defaults: " + name)
