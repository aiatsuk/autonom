"""Exercise the shipped setup helper without network or user-home writes."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "plugins/autonom/skills/autonom-setup/scripts/install_runtime.py"
spec = importlib.util.spec_from_file_location("install_runtime", HELPER)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def bundle(version="1.2.3", extra=()):
    source = (
        "import json, sys\n"
        f"print(json.dumps({{'version': {version!r}}} if sys.argv[1] == 'version' "
        "else {'ok': True, 'sessions': []}))\n"
    ).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for name, data, kind in [
            ("autonom-1.2.3/scripts/autonom.py", source, tarfile.REGTYPE), *extra,
        ]:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(data) if kind == tarfile.REGTYPE else 0
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = "/tmp/outside"
            tar.addfile(info, io.BytesIO(data) if info.isfile() else None)
    return output.getvalue()


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="autonom setup ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtimes = self.root / "runtimes"
        self.bin = self.root / "bin space"
        self.bin.mkdir()
        self.launcher = self.bin / "autonom"
        # A release asset is immutable; regenerating gzip can change its mtime
        # header between calls and accidentally test two different releases.
        self.asset = bundle()
        self.env = patch.dict(os.environ, {"AUTONOM_HOME": str(self.root / "home")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def install(self, data=None, checksum=None):
        data = self.asset if data is None else data
        checksum = checksum or hashlib.sha256(data).hexdigest()
        with patch.object(setup, "download", side_effect=[
            f"{checksum}  autonom-1.2.3.tgz\n".encode(), data,
        ]) as fetch:
            result = setup.install("1.2.3", self.runtimes, self.bin)
        self.assertIn("/v1.2.3/SHA256SUMS", fetch.call_args_list[0].args[0])
        return result

    def assert_error(self, code, **kwargs):
        with self.assertRaises(setup.InstallError) as caught:
            self.install(**kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertFalse(self.launcher.exists())

    def test_install_and_repeat_runs_real_launcher_with_spaces(self):
        result = self.install()
        completed = subprocess.run([result["executable"], "version"],
                                   text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(completed.stdout)["version"], "1.2.3")
        self.assertEqual(self.install()["runtime"], result["runtime"])
        self.assertEqual(len(list(self.runtimes.iterdir())), 1)

    def test_install_precompiles_runtime_for_launcher_interpreter(self):
        result = self.install()
        cli = Path(result["runtime"]) / "scripts/autonom.py"
        self.assertTrue(Path(importlib.util.cache_from_source(str(cli))).is_file())
        self.assertNotIn(" -B ", self.launcher.read_text())

    def test_precompile_failure_never_blocks_install(self):
        for error in (OSError("read-only runtime"), RuntimeError("compiler crashed")):
            with self.subTest(error=error), patch.object(
                    setup.compileall, "compile_dir", side_effect=error) as compile_dir:
                result = self.install()
                completed = subprocess.run([result["executable"], "version"],
                                           text=True, capture_output=True, check=True)
                self.assertEqual(json.loads(completed.stdout)["version"], "1.2.3")
                compile_dir.assert_called_once_with(Path(result["runtime"]), quiet=2)

    def test_precompile_is_silent_and_reports_failures(self):
        tree = self.root / "tree"
        tree.mkdir()
        (tree / "good.py").write_text("VALUE = 1\n")
        (tree / "warns.py").write_text('PATTERN = "\\d"\n')  # invalid escape warning
        (tree / "broken.py").write_text("def (:\n")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            self.assertFalse(setup.precompile(tree))
            setup.precompile(tree / "missing")  # nothing to compile: no raise, no output
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
        for name in ("good.py", "warns.py"):
            self.assertTrue(Path(importlib.util.cache_from_source(str(tree / name))).is_file())
        (tree / "broken.py").unlink()
        self.assertTrue(setup.precompile(tree))

    def test_mismatch_does_not_touch_existing_launcher(self):
        previous = f"#!/bin/sh\n{setup.MARKER}\nexit 0\n"
        self.launcher.write_text(previous)
        with self.assertRaisesRegex(setup.InstallError, "SHA-256"):
            self.install(checksum="0" * 64)
        self.assertEqual(self.launcher.read_text(), previous)
        self.assertFalse(self.runtimes.exists())

    def test_checksum_missing_duplicate_and_invalid(self):
        for value in ("", "abc  autonom-1.2.3.tgz", ("a" * 64 + "  autonom-1.2.3.tgz\n") * 2):
            with self.subTest(value=value), self.assertRaises(setup.InstallError):
                setup.expected_checksum(value, "autonom-1.2.3.tgz")

    def test_archive_refuses_traversal_links_devices_and_duplicates(self):
        for name, kind in [
            ("autonom-1.2.3/../../escape", tarfile.REGTYPE),
            ("/tmp/escape", tarfile.REGTYPE),
            ("other/scripts/autonom.py", tarfile.REGTYPE),
            ("autonom-1.2.3/link", tarfile.SYMTYPE),
            ("autonom-1.2.3/link", tarfile.LNKTYPE),
            ("autonom-1.2.3/pipe", tarfile.FIFOTYPE),
            ("autonom-1.2.3/scripts/autonom.py", tarfile.REGTYPE),
        ]:
            with self.subTest(name=name, kind=kind):
                self.assert_error("unsafe_archive", data=bundle(extra=[(name, b"x", kind)]))

    def test_archive_size_limit(self):
        with patch.object(setup, "MAX_UNPACKED", 1):
            self.assert_error("unsafe_archive")

    def test_version_mismatch(self):
        self.assert_error("version_mismatch", data=bundle(version="9.9.9"))

    def test_unrelated_file_and_symlink_are_preserved(self):
        self.launcher.write_text("unrelated")
        with self.assertRaises(setup.InstallError):
            self.install()
        self.assertEqual(self.launcher.read_text(), "unrelated")
        self.launcher.unlink()
        other = self.root / "other"
        other.write_text("other")
        self.launcher.symlink_to(other)
        with self.assertRaises(setup.InstallError):
            self.install()
        self.assertEqual(self.launcher.resolve(), other.resolve())

    def test_corrupted_existing_runtime_is_preserved_and_refused(self):
        result = self.install()
        cli = Path(result["runtime"]) / "scripts/autonom.py"
        cli.write_text("broken")
        previous = self.launcher.read_text()
        with self.assertRaisesRegex(setup.InstallError, "Existing runtime differs"):
            self.install()
        self.assertEqual(cli.read_text(), "broken")
        self.assertEqual(self.launcher.read_text(), previous)

    def test_active_or_uncertain_session_refuses_switch(self):
        for sessions, code in [
            ({"ok": True, "sessions": [{"live": True}]}, "active_session"),
            ({"ok": True, "sessions": [], "warnings": ["damaged"]}, "session_check_failed"),
            ({"ok": True}, "session_check_failed"),
        ]:
            with self.subTest(sessions=sessions), patch.object(
                setup, "run_cli", side_effect=[{"version": "1.2.3"}, sessions],
            ):
                self.assert_error(code)

    def test_real_damaged_pointer_blocks_without_changing_store(self):
        store = self.root / "home/sessions"
        store.mkdir(parents=True)
        for name in ("current.json", "target-android-device.json"):
            for data in ("{broken", "[]", '{"session_id": "../escape"}',
                         '{"session_id": "s_missing"}'):
                pointer = store / name
                pointer.write_text(data)
                with self.subTest(name=name, data=data):
                    self.assert_error("session_check_failed")
                    self.assertEqual(pointer.read_text(), data)
                pointer.unlink()

    def test_real_active_and_stopped_record(self):
        store = self.root / "home/sessions"
        (store / "s_one").mkdir(parents=True)
        pointer = store / "current.json"
        record = store / "s_one/session.json"
        pointer.write_text('{"session_id": "s_one", "target_id": "device"}')
        record.write_text('{"session_id": "s_one", "target_id": "device"}')
        self.assert_error("active_session")
        record.write_text('{"session_id": "s_one", "target_id": "device", "stopped_at": "2026-10-20"}')
        self.assertTrue(self.install()["ok"])

    def test_truncated_archive_returns_json_error(self):
        output = io.StringIO()
        data = b"\x1f\x8b\x08\x00"
        digest = hashlib.sha256(data).hexdigest()
        with patch.object(sys, "argv", [str(HELPER), "--runtime-dir", str(self.runtimes),
                                       "--bin-dir", str(self.bin)]), \
                patch.object(setup, "plugin_version", return_value="1.2.3"), \
                patch.object(setup, "download", side_effect=[
                    f"{digest}  autonom-1.2.3.tgz\n".encode(), data]), \
                patch.object(sys, "stderr", output):
            self.assertEqual(setup.main(), 2)
        self.assertEqual(json.loads(output.getvalue())["error_code"], "install_failed")
        self.assertFalse(self.launcher.exists())

    def test_launcher_failure_preserves_previous(self):
        self.launcher.write_text(f"#!/bin/sh\n{setup.MARKER}\nexit 0\n")
        previous = self.launcher.read_text()
        with patch.object(setup.os, "replace", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(self.launcher.read_text(), previous)
        self.assertEqual(list(self.bin.iterdir()), [self.launcher])

    def test_download_errors_and_size_limit(self):
        for error in [urllib.error.HTTPError("url", 404, "missing", {}, None),
                      urllib.error.URLError("offline"), TimeoutError("timeout")]:
            with self.subTest(error=error), patch.object(
                setup.urllib.request, "urlopen", side_effect=error,
            ), self.assertRaises(setup.InstallError):
                setup.download("https://github.com/file", 5)
        response = io.BytesIO(b"123456")
        response.url = "https://github.com/file"
        with patch.object(setup.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(setup.InstallError, "size limit"):
                setup.download("https://github.com/file", 5)

    def test_cli_error_is_json(self):
        for error in [OSError("denied"), tarfile.ReadError("invalid tar"),
                      subprocess.TimeoutExpired("runtime", 30),
                      setup.InstallError("checksum_mismatch", "wrong digest")]:
            output = io.StringIO()
            with patch.object(sys, "argv", [str(HELPER)]), \
                    patch.object(setup, "install", side_effect=error), \
                    patch.object(sys, "stderr", output):
                self.assertEqual(setup.main(), 2)
            payload = json.loads(output.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error_code"], getattr(error, "code", "install_failed"))
            self.assertTrue(payload["error"])

    def test_invalid_flags_return_json(self):
        for args in (["--unknown"], ["--bin-dir"], ["--runtime-dir"]):
            output = io.StringIO()
            with patch.object(sys, "argv", [str(HELPER), *args]), \
                    patch.object(sys, "stderr", output):
                self.assertEqual(setup.main(), 2)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["error_code"], "invalid_argument")
            self.assertFalse(payload["ok"])

    def test_manifest_version_matches_runtime(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import autonom_lib
        self.assertEqual(setup.plugin_version(), autonom_lib.__version__)


if __name__ == "__main__":
    unittest.main()
