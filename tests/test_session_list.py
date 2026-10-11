"""`autonom session list` and `session start --label` (contract section 3).

Everything runs against the fake adb in a private AUTONOM_HOME:

- two live sessions on two fake targets (primary and `--alongside`) are both
  listed, `current` only for the primary, with their labels;
- a stopped session leaves the default list and shows under `--all` with
  `live: false` and `stopped_at`;
- a corrupt `session.json` is skipped with a `session_unreadable` warning;
- target flags filter by string, without any device call;
- `--session-id <stopped>` is accepted (a read-only verb);
- `--label` is validated, and `--la` still reaches `--launch`;
- a v1 record lists with `label: null`.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
UI_DUMP = ROOT / "tests/fixtures/ui_dump.xml"
V1_FIXTURE = ROOT / "tests/fixtures/session_v1.json"
FIRST = "emulator-5560"
SECOND = "emulator-5562"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors  # noqa: E402
from autonom_lib.journal import JOURNAL_FILE  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

ENTRY_KEYS = ["session_id", "platform", "target_id", "label", "started_at",
              "stopped_at", "started_by", "current", "live"]


class SessionListCase(EnvSandboxMixin, unittest.TestCase):
    """A private AUTONOM_HOME and a fake adb listing two emulators."""

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
            "devices": [[FIRST, "device", "product:sdk_gphone64_arm64"],
                        [SECOND, "device", "product:sdk_gphone64_arm64"]],
            "ui_dump": str(UI_DUMP)}), encoding="utf-8")
        self.set_env(AUTONOM_HOME=str(self.home), AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.log), AUTONOM_ADB=None,
                     AUTONOM_SIMCTL=None, AUTONOM_IDB=None)
        self.env = dict(os.environ)
        self.env["PATH"] = str(self.bin)
        token = session_mod.select(None)
        self.addCleanup(session_mod._SELECTED.reset, token)

    @property
    def store(self) -> Path:
        return self.home / "sessions"

    def cli(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--adb", str(FAKE_ADB), *argv],
            cwd=self.root, env=self.env, text=True, stdin=subprocess.DEVNULL,
            capture_output=True, check=False, timeout=120)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.stdout.strip() else completed.stderr
        return completed.returncode, json.loads(stream)

    def start(self, serial: str, *extra: str) -> dict:
        code, payload = self.cli("--serial", serial, "session", "start",
                                 "--app-id", "com.example.app", *extra)
        self.assertEqual(code, 0, payload)
        return payload["session"]

    def session_list(self, *extra: str) -> dict:
        code, payload = self.cli("session", "list", *extra)
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], len(payload["sessions"]))
        for entry in payload["sessions"]:
            self.assertEqual(list(entry), ENTRY_KEYS)
        return payload

    def adb_calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def by_id(self, payload: dict) -> dict[str, dict]:
        return {entry["session_id"]: entry for entry in payload["sessions"]}


class TwoSessionsTests(SessionListCase):

    def setUp(self) -> None:
        super().setUp()
        self.first = self.start(FIRST, "--label", "checkout bug")
        self.second = self.start(SECOND, "--alongside", "--started-by", "canvas:3277:4242",
                                 "--label", "canvas tile")
        self.log.write_text("", encoding="utf-8")

    def test_start_records_and_reports_the_label(self) -> None:
        self.assertEqual(self.first["label"], "checkout bug")
        self.assertEqual(self.second["label"], "canvas tile")
        stored = json.loads((self.store / self.first["session_id"] / "session.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(stored["label"], "checkout bug")

    def test_lists_both_live_sessions(self) -> None:
        payload = self.session_list()
        self.assertEqual(payload["count"], 2)
        self.assertNotIn("warnings", payload)
        entries = self.by_id(payload)
        first = entries[self.first["session_id"]]
        second = entries[self.second["session_id"]]
        self.assertEqual((first["platform"], first["target_id"], first["label"]),
                         ("android", FIRST, "checkout bug"))
        self.assertEqual((second["target_id"], second["label"]), (SECOND, "canvas tile"))
        self.assertTrue(first["current"])
        self.assertFalse(second["current"])
        self.assertTrue(first["live"] and second["live"])
        self.assertIsNone(first["stopped_at"])
        self.assertIsNone(first["started_by"])
        self.assertEqual(second["started_by"], {"kind": "canvas", "port": 3277, "pid": 4242})
        order = [(entry["started_at"], entry["session_id"]) for entry in payload["sessions"]]
        self.assertEqual(order, sorted(order))
        self.assertEqual(self.adb_calls(), [])

    def test_stopped_session_only_with_all(self) -> None:
        code, payload = self.cli("--serial", SECOND, "session", "stop")
        self.assertEqual(code, 0, payload)
        self.log.write_text("", encoding="utf-8")

        default = self.session_list()
        self.assertEqual([entry["session_id"] for entry in default["sessions"]],
                         [self.first["session_id"]])

        everything = self.session_list("--all")
        self.assertEqual(everything["count"], 2)
        entries = self.by_id(everything)
        stopped = entries[self.second["session_id"]]
        self.assertFalse(stopped["live"])
        self.assertFalse(stopped["current"])
        self.assertTrue(stopped["stopped_at"])
        self.assertEqual(stopped["label"], "canvas tile")
        live = entries[self.first["session_id"]]
        self.assertTrue(live["live"] and live["current"])
        self.assertIsNone(live["stopped_at"])

        # A stopped session may be named: `session list` is read-only.
        code, payload = self.cli("--session-id", self.second["session_id"], "session", "list")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(self.adb_calls(), [])

    def test_corrupt_record_is_a_warning(self) -> None:
        broken = self.store / "s_brokenrec1"
        broken.mkdir(parents=True)
        (broken / "session.json").write_text("{not json", encoding="utf-8")
        not_object = self.store / "s_notobject1"
        not_object.mkdir(parents=True)
        (not_object / "session.json").write_text("[1, 2]", encoding="utf-8")
        # Not session directories at all: never read, never warned about.
        (self.store / "s_with-dash").mkdir()
        (self.store / "s_with-dash" / "session.json").write_text("{", encoding="utf-8")

        payload = self.session_list("--all")
        self.assertEqual(payload["count"], 2)
        warnings = {item["session_id"]: item for item in payload["warnings"]}
        self.assertEqual(set(warnings), {"s_brokenrec1", "s_notobject1"})
        for item in warnings.values():
            self.assertEqual(item["code"], "session_unreadable")
            self.assertEqual(list(item), ["code", "session_id", "error"])
            self.assertTrue(item["error"])
        # The live-only list does not scan the store.
        self.assertNotIn("warnings", self.session_list())

    def test_target_flags_filter_without_device_calls(self) -> None:
        payload = self.session_list("--serial", SECOND)
        self.assertEqual([entry["session_id"] for entry in payload["sessions"]],
                         [self.second["session_id"]])
        payload = self.session_list("--target", FIRST)
        self.assertEqual([entry["session_id"] for entry in payload["sessions"]],
                         [self.first["session_id"]])
        self.assertEqual(self.session_list("--serial", "emulator-5999")["count"], 0)
        self.assertEqual(self.session_list("--platform", "ios")["count"], 0)
        self.assertEqual(self.session_list("--platform", "android")["count"], 2)
        self.assertEqual(self.session_list("--udid", FIRST)["count"], 0)
        code, payload = self.cli("session", "list", "--serial", FIRST, "--udid", FIRST)
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["error_code"], errors.CONFLICTING_TARGET_FLAGS)
        self.assertEqual(self.adb_calls(), [])

    def test_list_writes_nothing_in_the_store(self) -> None:
        def snapshot() -> dict[str, bytes]:
            return {str(path.relative_to(self.store)): path.read_bytes()
                    for path in self.store.rglob("*") if path.is_file()
                    and path.name != JOURNAL_FILE}
        before = snapshot()
        self.session_list("--all")
        self.session_list()
        self.assertEqual(snapshot(), before)


class EmptyStoreTests(SessionListCase):

    def test_no_session_at_all(self) -> None:
        for extra in ((), ("--all",)):
            payload = self.session_list(*extra)
            self.assertEqual(payload["sessions"], [])
            self.assertEqual(payload["count"], 0)
            self.assertNotIn("warnings", payload)
        self.assertEqual(self.adb_calls(), [])

    def test_unknown_session_id_is_refused(self) -> None:
        code, payload = self.cli("--session-id", "s_doesnotexist", "session", "list")
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["error_code"], errors.SESSION_NOT_FOUND)

    def test_v1_record_lists_with_null_label(self) -> None:
        record = json.loads(V1_FIXTURE.read_text(encoding="utf-8"))
        directory = self.store / record["session_id"]
        directory.mkdir(parents=True)
        record["artifacts_dir"] = str(directory)
        text = json.dumps(record)
        (directory / "session.json").write_text(text, encoding="utf-8")
        (self.store / "current.json").write_text(text, encoding="utf-8")

        payload = self.session_list()
        self.assertEqual(payload["count"], 1)
        entry = payload["sessions"][0]
        self.assertEqual(entry["session_id"], record["session_id"])
        self.assertIsNone(entry["label"])
        self.assertEqual(entry["platform"], "android")
        self.assertEqual(entry["target_id"], record["serial"])
        self.assertIsNone(entry["started_by"])
        self.assertTrue(entry["current"] and entry["live"])

        everything = self.session_list("--all")
        self.assertEqual(everything["sessions"], payload["sessions"])
        # The v1 file was only read, never upgraded on disk.
        self.assertEqual((directory / "session.json").read_text(encoding="utf-8"), text)
        self.assertEqual(self.adb_calls(), [])

    def test_v1_record_without_platform_in_library_scan(self) -> None:
        record = json.loads(V1_FIXTURE.read_text(encoding="utf-8"))
        record.pop("platform")
        record["stopped_at"] = "2026-01-15T10:00:00Z"
        directory = self.store / record["session_id"]
        directory.mkdir(parents=True)
        (directory / "session.json").write_text(json.dumps(record), encoding="utf-8")
        entries, warnings = session_mod.list_sessions(include_stopped=True)
        self.assertEqual(warnings, [])
        self.assertEqual(entries, [{
            "session_id": record["session_id"], "platform": "android",
            "target_id": record["serial"], "label": None,
            "started_at": record["started_at"], "stopped_at": "2026-01-15T10:00:00Z",
            "started_by": None, "current": False, "live": False}])
        self.assertEqual(session_mod.list_sessions(), ([], []))


class LabelTests(SessionListCase):

    def test_label_is_validated_before_the_session_exists(self) -> None:
        for bad in ("", "x" * 65, "two\nlines", "tab\there"):
            code, payload = self.cli("--serial", FIRST, "session", "start",
                                     "--app-id", "com.example.app", "--label", bad)
            self.assertNotEqual(code, 0, bad)
            self.assertEqual(payload["error_code"], errors.INVALID_VALUE, bad)
        self.assertFalse(list(self.store.glob("s_*")) if self.store.exists() else [])
        self.assertEqual(self.adb_calls(), [])

    def test_longest_label_and_no_label(self) -> None:
        first = self.start(FIRST, "--label", "y" * 64)
        self.assertEqual(first["label"], "y" * 64)
        second = self.start(SECOND, "--alongside")
        self.assertIn("label", second)
        self.assertIsNone(second["label"])
        labels = {entry["session_id"]: entry["label"]
                  for entry in self.session_list()["sessions"]}
        self.assertEqual(labels, {first["session_id"]: "y" * 64, second["session_id"]: None})

    def test_validate_label_library(self) -> None:
        self.assertIsNone(session_mod.validate_label(None))
        self.assertEqual(session_mod.validate_label("a b"), "a b")
        with self.assertRaises(errors.AutonomError) as caught:
            session_mod.validate_label("\x1b[31m")
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)


class ParserTests(unittest.TestCase):
    """`--label` is exact-only on `session start`: `--la` keeps reaching
    `--launch`, as it did before `--label` existed."""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("autonom_cli_session_list", CLI)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        cls.module = module

    def parse(self, *argv: str):
        return self.module.build_parser().parse_args(list(argv))

    def test_la_still_means_launch(self) -> None:
        args = self.parse("session", "start", "--la", "com.example.app")
        self.assertEqual(args.launch, "com.example.app")
        self.assertIsNone(args.label)
        args = self.parse("session", "start", "--lau", "com.example.app")
        self.assertEqual(args.launch, "com.example.app")

    def test_label_only_in_full(self) -> None:
        args = self.parse("session", "start", "--label", "x")
        self.assertEqual(args.label, "x")
        self.assertIsNone(args.launch)
        for spelling in ("--lab", "--labe"):
            with contextlib.redirect_stderr(io.StringIO()) as stderr, \
                    self.assertRaises(SystemExit) as caught:
                self.parse("session", "start", spelling, "x")
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(json.loads(stderr.getvalue())["error_code"], errors.USAGE_ERROR)

    def test_list_leaf(self) -> None:
        args = self.parse("session", "list")
        self.assertFalse(args.all)
        self.assertIs(args.func, self.module.cmd_session_list)
        args = self.parse("session", "list", "--all", "--serial", FIRST)
        self.assertTrue(args.all)
        self.assertEqual(args.serial, FIRST)
        self.assertIn("list", self.module._READONLY_SESSION_COMMANDS)


if __name__ == "__main__":
    unittest.main()
