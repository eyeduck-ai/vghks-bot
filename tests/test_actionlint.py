"""Offline checks for the pinned workflow checker; the CLI also runs real parser fixtures."""
import contextlib
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from tools import check_workflows as lint


class ActionlintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workspace"
        self.root.mkdir()
        self.target = "windows_amd64"
        self.payload = b"synthetic executable; never executed"
        self.asset = {**lint.ASSETS[self.target], "executable_sha256": lint.digest(self.payload)}
        self.archive = self.zip_data({"actionlint.exe": self.payload, "README.md": b"synthetic"})
        self.asset["archive_sha256"] = lint.digest(self.archive)
        self.pins = patch.dict(lint.ASSETS, {self.target: self.asset})
        self.pins.start()
        self.addCleanup(self.pins.stop)
        workflows = self.root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        for name in ("windows.yml", "release.yml"):
            (workflows / name).write_text("name: synthetic\non: push\njobs: {}\n", encoding="utf-8")

    @staticmethod
    def zip_data(files):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        return stream.getvalue()

    def install(self):
        with patch.object(lint, "download_asset", return_value=self.archive):
            return lint.ensure_executable(self.root, install=True, target=self.target)[0]

    def test_bootstrap_verifies_both_hashes_and_reuses_offline_cache(self):
        with patch.object(lint, "download_asset", return_value=self.archive) as download:
            executable, asset = lint.ensure_executable(self.root, install=True, target=self.target)
            self.assertEqual(executable.read_bytes(), self.payload)
            self.assertEqual(lint.digest(executable.read_bytes()), asset["executable_sha256"])
            cached, _ = lint.ensure_executable(self.root, target=self.target)
            self.assertEqual(cached, executable)
            download.assert_called_once_with(lint.RELEASE + "/" + self.asset["archive"])
        self.assertEqual(list(executable.parent.glob(".actionlint-*")), [], "atomic download scratch file leaked")

    def test_modified_cached_executable_fails_closed_without_network_or_execution(self):
        executable = self.install()
        executable.write_bytes(b"modified cached executable")
        with patch.object(lint, "download_asset") as download, patch.object(lint, "invoke") as invoke:
            with self.assertRaisesRegex(RuntimeError, "checksum changed"):
                lint.check(self.root, target=self.target)
            download.assert_not_called()
            invoke.assert_not_called()

    def test_archive_and_executable_checksum_failures_never_install(self):
        for archive, executable_hash in ((b"not the pinned download", self.asset["executable_sha256"]),
                                         (self.archive, "0" * 64)):
            with self.subTest(archive_valid=archive == self.archive):
                spec = {**self.asset, "executable_sha256": executable_hash}
                with patch.dict(lint.ASSETS, {self.target: spec}), patch.object(lint, "download_asset", return_value=archive):
                    with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                        lint.ensure_executable(self.root, install=True, target=self.target)
                self.assertFalse(list(self.root.rglob("actionlint.exe")))

    def test_archive_paths_links_duplicates_and_expansion_are_rejected(self):
        symlink = io.BytesIO()
        with zipfile.ZipFile(symlink, "w") as archive:
            info = zipfile.ZipInfo("actionlint.exe")
            info.external_attr = 0o120777 << 16
            archive.writestr(info, "outside")
        duplicate = io.BytesIO()
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("actionlint.exe", self.payload)
            # Use another allowed entry twice so Python does not hide either one.
            archive.writestr("README.md", "first")
            with patch("warnings.warn"):
                archive.writestr("README.md", "second")
        for data, message in ((self.zip_data({"../outside": b"data"}), "Unexpected"),
                              (symlink.getvalue(), "Unexpected"), (duplicate.getvalue(), "Duplicate")):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                lint.executable_bytes(data, self.asset)
        with patch.object(lint, "MAX_EXPANDED_BYTES", 1), self.assertRaisesRegex(ValueError, "extraction limits"):
            lint.executable_bytes(self.archive, self.asset)
        self.assertFalse((self.root.parent / "outside").exists())

    def test_linux_archive_uses_the_same_regular_file_and_checksum_boundary(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            member = tarfile.TarInfo("actionlint")
            member.size = len(self.payload)
            archive.addfile(member, io.BytesIO(self.payload))
        spec = {**self.asset, "archive": "synthetic.tar.gz", "executable": "actionlint"}
        self.assertEqual(lint.executable_bytes(stream.getvalue(), spec), self.payload)
        linked = io.BytesIO()
        with tarfile.open(fileobj=linked, mode="w:gz") as archive:
            member = tarfile.TarInfo("actionlint")
            member.type, member.linkname = tarfile.SYMTYPE, "../outside"
            archive.addfile(member)
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            lint.executable_bytes(linked.getvalue(), spec)

    def test_cache_and_report_paths_cannot_escape_the_workspace(self):
        for path in (self.root.parent / "outside", self.root / ".build" / ".." / ".." / "outside"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "within the workspace"):
                lint.confined(path, self.root)

    def test_download_is_bounded_even_without_content_length(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"x" * 11
        with patch.object(lint, "MAX_ARCHIVE_BYTES", 10), patch("urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "download limit"):
                lint.download_asset(lint.RELEASE + "/" + self.asset["archive"])
        response.read.assert_called_once_with(11)

    def test_workflow_failure_propagates_with_explicit_files_and_stable_flags(self):
        executable = self.install()
        version = subprocess.CompletedProcess([], 0, stdout=lint.VERSION + "\n", stderr="")
        failure = subprocess.CompletedProcess([], 1, stdout="", stderr="synthetic invalid workflow expression")
        with patch.object(lint, "invoke", side_effect=[version, failure]) as invoke:
            status, report = lint.check(self.root, target=self.target)
        self.assertEqual(status, 1)
        self.assertFalse(report["ok"])
        self.assertIn("invalid workflow", report["diagnostics"])
        self.assertEqual(report["workflows"], [".github/workflows/release.yml", ".github/workflows/windows.yml"])
        arguments = invoke.call_args_list[1].args
        self.assertEqual(arguments[0], executable)
        self.assertEqual(arguments[1][:2], ["-shellcheck=", "-pyflakes="])
        self.assertEqual(arguments[1][2:], [self.root / name for name in report["workflows"]])

    def test_version_mismatch_prevents_workflow_execution(self):
        self.install()
        wrong = subprocess.CompletedProcess([], 0, stdout="unexpected version\n", stderr="")
        with patch.object(lint, "invoke", return_value=wrong) as invoke:
            with self.assertRaisesRegex(ValueError, "version mismatch"):
                lint.check(self.root, target=self.target)
        self.assertEqual(invoke.call_count, 1)

    def test_real_parser_smoke_contract_rejects_a_checker_that_accepts_bad_yaml(self):
        passed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        rejected = subprocess.CompletedProcess([], 1, stdout="synthetic error", stderr="")
        with patch.object(lint, "invoke", side_effect=[passed, rejected, rejected]):
            results = lint.smoke_tests(self.root / "synthetic-checker", self.root)
        self.assertEqual([row["name"] for row in results], ["valid-workflow", "invalid-yaml", "invalid-context"])
        with patch.object(lint, "invoke", return_value=passed), self.assertRaisesRegex(RuntimeError, "invalid-yaml"):
            lint.smoke_tests(self.root / "synthetic-checker", self.root)

    def test_missing_tool_writes_a_failed_report_with_install_instructions(self):
        with patch.object(lint, "ROOT", self.root), patch.object(lint, "target_platform", return_value=self.target), \
                patch("sys.argv", ["check_workflows.py"]), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(lint.main(), 1)
        report = json.loads((self.root / ".build" / "ci" / "actionlint.json").read_text(encoding="utf-8"))
        self.assertFalse(report["ok"])
        self.assertIn("--install", report["diagnostics"])

    def test_unreviewed_platform_cannot_select_an_unpinned_binary(self):
        with patch("platform.system", return_value="Windows"), patch("platform.machine", return_value="ARM64"):
            with self.assertRaisesRegex(ValueError, "reviewed official hashes"):
                lint.target_platform()
