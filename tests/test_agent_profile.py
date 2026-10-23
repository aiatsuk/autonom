"""The per-session agent profile (AL-09).

`session start --profile {default,agent}` stores the profile in the session
record; `session show` and `session list` report it. In an agent-profile
session `ui tree` prints the outline and the action verbs observe (AL-11);
explicit flags win. A session started without `--profile`, with `--profile
default`, or by an older version (a record without the key) behaves exactly
as before.

Every CLI run is a subprocess against `tests/fakes/fake_adb.py` (and the iOS
fakes for one case) in a private AUTONOM_HOME. No real device.
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
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_IDB = ROOT / "tests/fakes/fake_idb.py"
UI_DUMP = ROOT / "tests/fixtures/ui_dump.xml"
IOS_FIXTURE = ROOT / "tests/fixtures/idb_describe_all_sample.json"
SERIAL = "emulator-5620"
SECOND = "emulator-5622"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"
BOOTED = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
    {"udid": UDID, "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}]}}
# The keys of a `session list` entry before this change, in order.
LIST_KEYS = ["session_id", "platform", "target_id", "label", "started_at", "stopped_at",
             "started_by", "current", "live"]

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


def load_cli(name: str = "autonom_cli_agent_profile"):
    spec = importlib.util.spec_from_file_location(name, CLI)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ProfileCase(EnvSandboxMixin, unittest.TestCase):

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
        self.state.write_text(json.dumps({
            "devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"],
                        [SECOND, "device", "product:sdk_gphone64_arm64"]],
            "ui_dump": str(UI_DUMP), "simctl_devices": BOOTED,
            "idb_describe_all": str(IOS_FIXTURE)}), encoding="utf-8")
        self.set_env(AUTONOM_HOME=str(self.home), AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.log), AUTONOM_ADB=None, AUTONOM_SIMCTL=None,
                     AUTONOM_IDB=None, AUTONOM_AXE=None, AUTONOM_IOS_HID=None,
                     AUTONOM_IDB_COMPANION=None, AUTONOM_UI_BACKEND=None,
                     AUTONOM_IDB_STATE_FILE=str(self.root / "idb-state.json"),
                     AUTONOM_CONTROL_HOLDER=None, AUTONOM_CONTROL_GENERATION=None)
        self.env = dict(os.environ)
        self.env["PATH"] = os.pathsep.join([str(self.bin), "/usr/bin", "/bin"])
        token = session_mod.select(None)
        self.addCleanup(session_mod._SELECTED.reset, token)  # noqa: SLF001

    def cli(self, *argv: str) -> tuple[int, dict[str, Any]]:
        completed = subprocess.run(
            [sys.executable, str(CLI), *argv], cwd=self.root, env=self.env, text=True,
            stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=120)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.stdout.strip() else completed.stderr
        return completed.returncode, json.loads(stream)

    def android(self, *argv: str, serial: str = SERIAL) -> tuple[int, dict[str, Any]]:
        return self.cli("--adb", str(FAKE_ADB), "--serial", serial, *argv)

    def start(self, *extra: str, serial: str = SERIAL) -> dict[str, Any]:
        code, payload = self.android("session", "start", "--app-id", "com.example.app",
                                     *extra, serial=serial)
        self.assertEqual(code, 0, payload)
        return payload["session"]

    def dumps(self) -> int:
        if not self.log.exists():
            return 0
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        return sum(1 for row in rows if row["argv"][-3:] == ["uiautomator", "dump", "/dev/tty"])

    def forget_calls(self) -> None:
        self.log.write_text("", encoding="utf-8")

    def stored(self, session: dict[str, Any]) -> dict[str, Any]:
        path = self.home / "sessions" / session["session_id"] / "session.json"
        return json.loads(path.read_text(encoding="utf-8"))


class AgentProfileTests(ProfileCase):

    def test_start_stores_and_reports_the_profile(self) -> None:
        session = self.start("--profile", "agent")
        self.assertEqual(session["profile"], "agent")
        self.assertEqual(self.stored(session)["profile"], "agent")
        code, shown = self.android("session", "show")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown["session"]["profile"], "agent")
        code, listed = self.cli("session", "list")
        self.assertEqual(code, 0, listed)
        [entry] = listed["sessions"]
        self.assertEqual(list(entry), [*LIST_KEYS, "profile"])
        self.assertEqual(entry["profile"], "agent")
        code, listed = self.cli("session", "list", "--all")
        self.assertEqual(listed["sessions"][0]["profile"], "agent")

    def test_agent_tree_is_the_outline(self) -> None:  # AL-09-S01
        self.start("--profile", "agent")
        code, payload = self.android("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["format"], "outline")
        self.assertIn("outline", payload)
        self.assertNotIn("nodes", payload)
        self.assertIn('"Flutter Save Button"', payload["outline"])
        self.assertEqual(payload["count"], 5)
        # the saved history still keeps the full nodes
        saved = json.loads(Path(payload["saved"]).read_text(encoding="utf-8"))
        self.assertEqual(len(saved["nodes"]), 5)

    def test_explicit_json_wins_in_an_agent_session(self) -> None:
        self.start("--profile", "agent")
        code, payload = self.android("ui", "tree", "--format", "json")
        self.assertEqual(code, 0, payload)
        self.assertIn("nodes", payload)
        self.assertNotIn("outline", payload)
        self.assertNotIn("format", payload)

    def test_agent_dump_read_is_the_outline_too(self) -> None:
        self.start("--profile", "agent")
        code, payload = self.cli("ui", "tree", "--dump", str(UI_DUMP))
        self.assertEqual(code, 0, payload)
        self.assertNotIn("nodes", payload)
        self.assertEqual(payload["format"], "outline")

    def test_agent_actions_observe_and_no_observe_wins(self) -> None:
        self.start("--profile", "agent")
        self.forget_calls()
        code, payload = self.android("ui", "tap", "--x", "10", "--y", "10")
        self.assertEqual(code, 0, payload)
        self.assertIn("screen", payload)
        self.assertIn("foreground", payload)
        self.assertGreaterEqual(self.dumps(), 1)
        self.forget_calls()
        code, payload = self.android("ui", "tap", "--x", "10", "--y", "10", "--no-observe")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("screen", payload)
        self.assertEqual(self.dumps(), 0)

    def test_the_profile_belongs_to_its_own_target(self) -> None:
        """An agent session on one emulator does not change replies for a
        verb aimed at another emulator that has a default session."""
        self.start("--profile", "agent")
        self.start("--alongside", serial=SECOND)
        code, payload = self.android("ui", "tree", serial=SECOND)
        self.assertEqual(code, 0, payload)
        self.assertIn("nodes", payload)
        self.forget_calls()
        code, payload = self.android("ui", "tap", "--x", "10", "--y", "10", serial=SECOND)
        self.assertEqual(code, 0, payload)
        self.assertNotIn("screen", payload)
        self.assertEqual(self.dumps(), 0)
        code, payload = self.android("ui", "tree")
        self.assertEqual(payload["format"], "outline")

    def test_ios_agent_tree_is_the_outline(self) -> None:
        def ios(*argv: str) -> tuple[int, dict[str, Any]]:
            return self.cli("--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB),
                            "--udid", UDID, *argv)
        code, payload = ios("session", "start", "--profile", "agent")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["session"]["profile"], "agent")
        code, payload = ios("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("nodes", payload)
        self.assertIn('"General"', payload["outline"])


class DefaultProfileTests(ProfileCase):

    def test_default_session_tree_has_nodes(self) -> None:  # AL-09-S02
        session = self.start()
        self.assertEqual(session["profile"], "default")
        code, payload = self.android("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertIn("nodes", payload)
        self.assertEqual(len(payload["nodes"]), 5)
        self.assertNotIn("outline", payload)
        self.assertNotIn("format", payload)

    def test_explicit_default_is_the_default(self) -> None:
        session = self.start("--profile", "default")
        self.assertEqual(self.stored(session)["profile"], "default")
        code, payload = self.android("ui", "tree")
        self.assertIn("nodes", payload)
        self.forget_calls()
        code, payload = self.android("ui", "tap", "--x", "10", "--y", "10")
        self.assertEqual(code, 0, payload)
        # the keys a session's tap always had (the action detail included)
        self.assertEqual(list(payload), ["ok", "x", "y", "ref", "backend", "detail",
                                         "platform", "target_id", "serial"])
        self.assertEqual(self.dumps(), 0)

    def test_a_default_list_entry_keeps_its_keys(self) -> None:
        self.start()
        code, listed = self.cli("session", "list")
        self.assertEqual(code, 0, listed)
        self.assertEqual(list(listed["sessions"][0]), LIST_KEYS)

    def test_outline_is_still_there_on_request(self) -> None:
        self.start()
        code, payload = self.android("ui", "tree", "--format", "outline")
        self.assertEqual(payload["format"], "outline")
        self.assertNotIn("nodes", payload)

    def test_an_older_record_without_a_profile_behaves_as_before(self) -> None:
        session = self.start("--profile", "agent")
        # what a version before the profile wrote: no `profile` key at all
        for path in (self.home / "sessions" / session["session_id"] / "session.json",
                     self.home / "sessions" / "current.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            record.pop("profile", None)
            path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        code, payload = self.android("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertIn("nodes", payload)
        self.forget_calls()
        code, payload = self.android("ui", "tap", "--x", "10", "--y", "10")
        self.assertNotIn("screen", payload)
        self.assertEqual(self.dumps(), 0)
        code, listed = self.cli("session", "list")
        self.assertEqual(list(listed["sessions"][0]), LIST_KEYS)
        code, shown = self.android("session", "show")
        self.assertNotIn("profile", shown["session"])

    def test_an_unknown_profile_is_refused_and_starts_nothing(self) -> None:
        code, payload = self.android("session", "start", "--profile", "fast")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.USAGE_ERROR)
        self.assertFalse((self.home / "sessions" / "current.json").exists())

    def test_a_record_with_an_unknown_profile_is_a_default_session(self) -> None:
        self.assertEqual(session_mod.profile_of({"profile": "turbo"}), "default")
        self.assertEqual(session_mod.profile_of({}), "default")
        self.assertEqual(session_mod.profile_of(None), "default")
        self.assertEqual(session_mod.profile_of({"profile": "agent"}), "agent")
        with self.assertRaises(errors.AutonomError) as caught:
            session_mod.validate_profile("turbo")
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)


class ProfileSpellingTests(unittest.TestCase):
    """`--profile` is matched only when spelled in full, so `--p` keeps
    reaching `--platform` on `session start` as it always did."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.parser = load_cli().build_parser()

    def parse(self, argv: list[str]):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            try:
                return self.parser.parse_args(argv)
            except SystemExit:
                return None

    def test_full_spelling(self) -> None:
        namespace = self.parse(["session", "start", "--profile", "agent"])
        self.assertEqual(namespace.profile, "agent")
        self.assertIsNone(self.parse(["session", "start"]).profile)

    def test_abbreviations_do_not_reach_it(self) -> None:
        self.assertIsNone(self.parse(["session", "start", "--prof", "agent"]))
        namespace = self.parse(["session", "start", "--p", "android"])
        self.assertIsNotNone(namespace)
        self.assertEqual(namespace.platform, "android")
        self.assertIsNone(namespace.profile)

    def test_tree_format_has_no_fixed_default(self) -> None:
        self.assertIsNone(self.parse(["ui", "tree"]).format)
        self.assertEqual(self.parse(["ui", "tree", "--format", "json"]).format, "json")


if __name__ == "__main__":
    unittest.main()
