"""One-line JSON (AL-10): every JSON reply on stdout is one line, for every
caller and profile, with the same keys, values and key order the indented
form had. Error envelopes on stderr stay one line.

Two oracles: a subprocess run shows what really reaches stdout (one line,
one trailing newline, a JSON object), and an in-process run of the same verb
compares that line with the object the CLI handed to `emit`, printed the way
0.36.2 printed it (`indent=2`): both parse to the same object with the same
key order. Fakes only, in a private AUTONOM_HOME, with a PATH that holds no
real tool.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
UI_DUMP = ROOT / "tests/fixtures/ui_dump.xml"
FLOW = ROOT / "tests/fixtures/flows/contract_pass.yaml"
SERIAL = "emulator-5630"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import control as control_mod  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

UNICODE_DUMP = """<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.example.app" content-desc="" clickable="false" enabled="true" focusable="false" scrollable="false" bounds="[0,0][1080,1920]">
    <node index="0" text="Café ☕ über" resource-id="com.example.app:id/title" class="android.widget.TextView" package="com.example.app" content-desc="" clickable="true" enabled="true" focusable="true" scrollable="false" bounds="[40,100][440,220]" />
  </node>
</hierarchy>
"""


def load_cli(name: str = "autonom_cli_one_line"):
    spec = importlib.util.spec_from_file_location(name, CLI)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class OneLineCase(EnvSandboxMixin, unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.cli_module = load_cli()

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.unicode_dump = self.root / "unicode.xml"
        self.unicode_dump.write_text(UNICODE_DUMP, encoding="utf-8")
        self.state.write_text(json.dumps({
            "devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"]],
            "ui_dump": str(UI_DUMP)}), encoding="utf-8")
        self.set_env(AUTONOM_HOME=str(self.home), AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.log), AUTONOM_ADB=None, AUTONOM_SIMCTL=None,
                     AUTONOM_IDB=None, AUTONOM_AXE=None, AUTONOM_IOS_HID=None,
                     AUTONOM_UI_BACKEND=None, AUTONOM_CONTROL_HOLDER=None,
                     AUTONOM_CONTROL_GENERATION=None)
        self.env = dict(os.environ)
        # only the python3 link: no real adb, xcrun or idb can be reached
        self.env["PATH"] = str(self.bin)
        token = session_mod.select(None)
        self.addCleanup(session_mod._SELECTED.reset, token)  # noqa: SLF001
        control_mod.end_invocation()
        self.addCleanup(control_mod.end_invocation)
        self.cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.cwd)

    def run_cli(self, *argv: str) -> subprocess.CompletedProcess:
        completed = subprocess.run(
            [sys.executable, str(CLI), *argv], cwd=self.root, env=self.env, text=True,
            stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=120)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        return completed

    def assert_one_line(self, text: str) -> dict[str, Any]:
        self.assertTrue(text.endswith("\n"), repr(text[-40:]))
        self.assertEqual(text.count("\n"), 1, "one line, one trailing newline")
        payload = json.loads(text)
        self.assertIsInstance(payload, dict)
        return payload

    def in_process(self, *argv: str) -> tuple[str, Any]:
        """stdout of `main(argv)` in this process, and the object `emit` got."""
        cli = self.cli_module
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(argv))
        self.assertEqual(code, 0, out.getvalue())
        return out.getvalue(), cli._LAST_EMIT  # noqa: SLF001

    def assert_same_as_indented(self, *argv: str) -> dict[str, Any]:
        stdout, emitted = self.in_process(*argv)
        payload = self.assert_one_line(stdout)
        indented = json.dumps(emitted, indent=2, ensure_ascii=False)  # how 0.36.2 printed it
        self.assertGreater(indented.count("\n"), 1)
        old = json.loads(indented)
        self.assertEqual(payload, old)
        self.assertEqual(list(payload), list(old), "same key order")
        self.assertEqual(stdout, json.dumps(emitted, ensure_ascii=False,
                                            separators=(",", ":")) + "\n")
        return payload


class OfflineVerbTests(OneLineCase):
    """AL-10-S01 on verbs that need no device."""

    def test_offline_verbs_print_one_line(self) -> None:
        for argv in (("version",),
                     ("ui", "tree", "--dump", str(UI_DUMP)),
                     ("ui", "tree", "--dump", str(UI_DUMP), "--format", "outline"),
                     ("ui", "find", "--dump", str(UI_DUMP), "--desc", "Flutter Save Button"),
                     ("flow", "check", str(FLOW)),
                     ("session", "list")):
            with self.subTest(argv=argv):
                completed = self.run_cli(*argv)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assert_one_line(completed.stdout)

    def test_offline_verbs_keep_the_indented_object(self) -> None:
        for argv in (("version",),
                     ("ui", "tree", "--dump", str(UI_DUMP)),
                     ("ui", "tree", "--dump", str(UI_DUMP), "--format", "outline"),
                     ("ui", "find", "--dump", str(UI_DUMP), "--desc", "Flutter Save Button"),
                     ("flow", "check", str(FLOW)),
                     ("session", "list")):
            with self.subTest(argv=argv):
                self.assert_same_as_indented(*argv)

    def test_non_ascii_stays_readable(self) -> None:
        completed = self.run_cli("ui", "tree", "--dump", str(self.unicode_dump))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = self.assert_one_line(completed.stdout)
        self.assertIn("Café ☕ über", completed.stdout)
        self.assertNotIn("\\u00e9", completed.stdout)
        self.assertEqual(payload["nodes"][0]["text"], "Café ☕ über")

    def test_an_outline_with_many_lines_is_still_one_stdout_line(self) -> None:
        completed = self.run_cli("ui", "tree", "--dump", str(UI_DUMP), "--format", "outline")
        payload = self.assert_one_line(completed.stdout)
        self.assertEqual(payload["outline"].count("\n"), 4, "five nodes, five outline lines")


class DeviceVerbTests(OneLineCase):
    """AL-10-S01 on verbs that drive the fake adb, both profiles."""

    def android(self, *argv: str) -> subprocess.CompletedProcess:
        return self.run_cli("--adb", str(FAKE_ADB), "--serial", SERIAL, *argv)

    def test_device_verbs_print_one_line(self) -> None:
        completed = self.android("session", "start", "--app-id", "com.example.app")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assert_one_line(completed.stdout)
        for argv in (("devices",), ("session", "show"), ("ui", "tree"),
                     ("ui", "tap", "--x", "1", "--y", "1"), ("ui", "key", "KEYCODE_BACK"),
                     ("ui", "swipe", "--from", "10,10", "--to", "10,500"),
                     ("ui", "type", "hello"), ("screenshot",), ("session", "stop")):
            with self.subTest(argv=argv):
                completed = self.android(*argv)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assert_one_line(completed.stdout)

    def test_agent_profile_replies_are_one_line(self) -> None:
        completed = self.android("session", "start", "--app-id", "com.example.app",
                                 "--profile", "agent")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assert_one_line(completed.stdout)
        for argv in (("ui", "tree"), ("ui", "tap", "--x", "1", "--y", "1"),
                     ("ui", "key", "KEYCODE_BACK")):
            with self.subTest(argv=argv):
                completed = self.android(*argv)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                payload = self.assert_one_line(completed.stdout)
                # `ui tree` prints its outline; an action its observed screen
                outline = payload.get("outline") or payload["screen"]["outline"]
                self.assertGreater(outline.count("\n"), 0, "a many-line outline, one line")

    def test_device_verbs_keep_the_indented_object(self) -> None:
        base = ("--adb", str(FAKE_ADB), "--serial", SERIAL)
        self.assert_same_as_indented(*base, "session", "start", "--app-id", "com.example.app")
        for argv in (("ui", "tap", "--x", "1", "--y", "1"), ("ui", "key", "KEYCODE_BACK"),
                     ("session", "show")):
            with self.subTest(argv=argv):
                self.assert_same_as_indented(*base, *argv)

    def test_error_envelopes_stay_one_line_on_stderr(self) -> None:
        for argv in (("ui", "tap"),                       # no selector
                     ("ui", "swipe", "--from", "1,1", "--to", "2,2", "--duration", "0"),
                     ("ui", "tap", "--x", "1", "--y", "1", "--observe-timeout-ms", "-1"),
                     ("ui", "nosuchverb")):
            with self.subTest(argv=argv):
                completed = self.android(*argv)
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(completed.stdout, "")
                payload = self.assert_one_line(completed.stderr)
                self.assertFalse(payload["ok"])
                self.assertIn("error_code", payload)


if __name__ == "__main__":
    unittest.main()
