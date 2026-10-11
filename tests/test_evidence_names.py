"""Collision-safe action detail names and evidence-write warnings (contract 1.3).

`actions.record_detail` used to name a record after the count of files in
`actions/`, so two concurrent writers could take the same name and the second
overwrote the first. Names now build on the highest existing index, are
published with a hard link (or an exclusive create) so no name is ever shared,
and a failed write becomes an `evidence_write_failed` warning in the command's
JSON instead of being lost silently. The device input is never repeated.

Fakes only (`fake_adb.py`); every store lives under a temporary
`AUTONOM_HOME`. No device is used.
"""
from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CLI = SCRIPTS / "autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
UI_DUMP = ROOT / "tests/fixtures/ui_dump.xml"
SERIAL = "emulator-5582"

sys.path.insert(0, str(SCRIPTS))
from autonom_lib import actions  # noqa: E402
from autonom_lib import journal as journal_mod  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402
from autonom_lib.platform import ANDROID, Target  # noqa: E402
import autonom_canvas_bridge as bridge  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

# One writer process: waits for the start signal, then writes COUNT records.
WRITER = textwrap.dedent("""\
    import os, sys, time
    sys.path.insert(0, sys.argv[1])
    from autonom_lib import actions
    directory, marker, count, go = sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]
    record = {"session_id": "s_writer", "artifacts_dir": directory}
    deadline = time.monotonic() + 30
    while not os.path.exists(go) and time.monotonic() < deadline:
        time.sleep(0.001)
    failed = 0
    for index in range(count):
        path, warning = actions.record_detail_checked(
            record, "tap", {"writer": marker, "n": index})
        if path is None:
            failed += 1
            print(warning, file=sys.stderr)
    sys.exit(1 if failed else 0)
    """)


def unwritable(test: unittest.TestCase, path: Path) -> None:
    """chmod 0500 (restored on cleanup); skip where permissions are moot."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(stat.S_IRUSR | stat.S_IXUSR)
    test.addCleanup(path.chmod, stat.S_IRWXU)
    if hasattr(os, "geteuid") and os.geteuid() == 0 or os.access(path, os.W_OK):
        test.skipTest("the test user can write to a read-only directory")


class DetailCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.record = {"session_id": "s_names", "artifacts_dir": str(self.root)}
        self.actions_dir = self.root / "actions"

    def names(self) -> list[str]:
        return sorted(os.listdir(self.actions_dir))


class NameAllocationTests(DetailCase):
    def test_two_processes_never_share_a_name(self) -> None:
        self.actions_dir.mkdir()
        go = self.root / "go"
        writers = [
            subprocess.Popen(
                [sys.executable, "-c", WRITER, str(SCRIPTS), str(self.root),
                 marker, "200", str(go)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            for marker in ("alpha", "beta")
        ]
        time.sleep(0.2)
        go.write_text("go", encoding="utf-8")
        for writer in writers:
            out, err = writer.communicate(timeout=120)
            self.assertEqual(writer.returncode, 0, out + err)
        names = self.names()
        self.assertEqual(len(names), 400, names)
        self.assertFalse([name for name in names if not name.endswith(".json")])
        seen: dict[str, set[int]] = {"alpha": set(), "beta": set()}
        for name in names:
            body = json.loads((self.actions_dir / name).read_text(encoding="utf-8"))
            seen[body["writer"]].add(body["n"])
            self.assertEqual(stat.S_IMODE((self.actions_dir / name).stat().st_mode), 0o600)
        self.assertEqual(seen, {"alpha": set(range(200)), "beta": set(range(200))})
        indexes = sorted(int(name.split("_", 1)[0]) for name in names)
        self.assertEqual(len(set(indexes)), 400)

    def test_the_next_name_follows_the_highest_index_not_the_count(self) -> None:
        self.actions_dir.mkdir()
        (self.actions_dir / "0001_a.json").write_text("{}", encoding="utf-8")
        (self.actions_dir / "0005_b.json").write_text("{}", encoding="utf-8")
        path = actions.record_detail(self.record, "Tap!", {"kind": "tap"})
        self.assertEqual(path, "actions/0006_tap.json")

    def test_the_first_detail_is_still_0001(self) -> None:
        path, warning = actions.record_detail_checked(self.record, "find", {"k": 1})
        self.assertEqual((path, warning), ("actions/0001_find.json", None))
        self.assertEqual(self.names(), ["0001_find.json"])

    def test_a_squatter_on_the_next_name_moves_the_writer_on(self) -> None:
        real_link = os.link
        squatted: list[str] = []

        def link_after_squatter(src, dst, *args, **kwargs):
            if not squatted:
                Path(dst).write_text('{"squatter": true}', encoding="utf-8")
                squatted.append(str(dst))
            return real_link(src, dst, *args, **kwargs)

        with mock.patch.object(actions.os, "link", side_effect=link_after_squatter):
            path = actions.record_detail(self.record, "tap", {"writer": "me"})
        self.assertEqual(Path(squatted[0]).name, "0001_tap.json")
        self.assertEqual(path, "actions/0002_tap.json")
        self.assertEqual(json.loads((self.actions_dir / "0001_tap.json").read_text()),
                         {"squatter": True})
        self.assertEqual(json.loads((self.actions_dir / "0002_tap.json").read_text()),
                         {"writer": "me"})
        self.assertEqual(self.names(), ["0001_tap.json", "0002_tap.json"])

    def test_a_writer_left_far_behind_still_finds_a_name(self) -> None:
        # Another writer saves 100 records while this one sits between listing
        # and linking: stepping one name at a time would exhaust the attempts.
        real_link = os.link
        ahead: list[int] = []

        def link_after_a_burst(src, dst, *args, **kwargs):
            if not ahead:
                for index in range(1, 101):
                    (self.actions_dir / f"{index:04d}_tap.json").write_text(
                        '{"other": true}', encoding="utf-8")
                ahead.append(1)
            return real_link(src, dst, *args, **kwargs)

        with mock.patch.object(actions.os, "link", side_effect=link_after_a_burst):
            path, warning = actions.record_detail_checked(self.record, "tap", {"me": 1})
        self.assertEqual((path, warning), ("actions/0101_tap.json", None))

    def test_without_hard_links_the_exclusive_create_keeps_names_distinct(self) -> None:
        def no_links(src, dst, *args, **kwargs):
            raise OSError(errno.EPERM, "Operation not permitted")

        self.actions_dir.mkdir()
        (self.actions_dir / "0002_tap.json").write_text("{}", encoding="utf-8")
        real_open = os.open
        raced: list[int] = []

        def open_after_squatter(path, flags, *args, **kwargs):
            # one racing writer takes the name right before the exclusive create
            if flags & os.O_EXCL and str(path).endswith(".json") and not raced:
                raced.append(1)
                Path(path).write_text('{"racer": true}', encoding="utf-8")
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(actions.os, "link", side_effect=no_links), \
                mock.patch.object(actions.os, "open", side_effect=open_after_squatter):
            paths = [actions.record_detail(self.record, "tap", {"n": n}) for n in range(5)]
        self.assertEqual(paths, [f"actions/{index:04d}_tap.json" for index in range(4, 9)])
        self.assertEqual(json.loads((self.actions_dir / "0003_tap.json").read_text()),
                         {"racer": True})
        for n, path in enumerate(paths):
            self.assertEqual(json.loads((self.root / path).read_text()), {"n": n})
        self.assertEqual(len(self.names()), 7)
        self.assertFalse([name for name in self.names() if name.endswith(".tmp")])

    def test_exhausted_names_are_a_warning_not_an_exception(self) -> None:
        with mock.patch.object(actions.os, "link",
                               side_effect=FileExistsError(errno.EEXIST, "File exists")) as link:
            path, warning = actions.record_detail_checked(self.record, "tap", {"secret": "pin"})
        self.assertIsNone(path)
        self.assertEqual(link.call_count, actions.MAX_NAME_ATTEMPTS)
        self.assertEqual(warning["code"], actions.EVIDENCE_WRITE_FAILED)
        self.assertIn("64", warning["error"])
        self.assertEqual(self.names(), [])

    def test_no_session_is_neither_a_path_nor_a_warning(self) -> None:
        self.assertEqual(actions.record_detail_checked(None, "tap", {}), (None, None))
        self.assertIsNone(actions.record_detail(None, "tap", {}))


class WriteFailureTests(DetailCase):
    def test_an_unwritable_actions_dir_is_a_warning(self) -> None:
        unwritable(self, self.actions_dir)
        path, warning = actions.record_detail_checked(
            self.record, "type", {"text": "hunter2-typed-secret", "nodes": ["n1"]})
        self.assertIsNone(path)
        self.assertEqual(set(warning), {"code", "error", "hint", "artifact", "hint_path"})
        self.assertEqual(warning["code"], "evidence_write_failed")
        self.assertEqual(warning["artifact"], "actions")
        self.assertEqual(warning["hint_path"], str(self.actions_dir))
        self.assertIn(str(self.actions_dir), warning["hint"])
        self.assertTrue(warning["hint"].startswith("The device action ran;"))
        self.assertLessEqual(len(warning["error"]), 300)
        self.assertNotIn("\n", warning["error"])
        self.assertNotIn("hunter2", json.dumps(warning))
        self.assertIsNone(actions.record_detail(self.record, "type", {"text": "x"}))

    def test_an_unserializable_payload_is_a_warning(self) -> None:
        path, warning = actions.record_detail_checked(self.record, "tap", {"bad": object()})
        self.assertIsNone(path)
        self.assertEqual(warning["code"], "evidence_write_failed")
        self.assertFalse(self.actions_dir.exists() and self.names())

    def test_the_error_is_one_line_of_at_most_300_chars(self) -> None:
        with mock.patch.object(actions, "_publish",
                               side_effect=OSError("line one\nline two " + "x" * 600)):
            _, warning = actions.record_detail_checked(self.record, "tap", {})
        self.assertNotIn("\n", warning["error"])
        self.assertEqual(len(warning["error"]), 300)


class CommandWarningTests(EnvSandboxMixin, unittest.TestCase):
    """The CLI and the Canvas bridge report the warning; the input runs once."""

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
            "devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"]],
            "ui_dump": str(UI_DUMP)}), encoding="utf-8")
        self.set_env(AUTONOM_HOME=str(self.home), AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.log), AUTONOM_ADB=None,
                     AUTONOM_SIMCTL=None, AUTONOM_IDB=None)
        self.env = dict(os.environ)
        self.env["PATH"] = str(self.bin)
        token = session_mod.select(None)
        self.addCleanup(session_mod._SELECTED.reset, token)

    def cli(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--adb", str(FAKE_ADB), "--serial", SERIAL, *argv],
            cwd=self.root, env=self.env, text=True, stdin=subprocess.DEVNULL,
            capture_output=True, check=False, timeout=120)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.returncode in (0, 1) else completed.stderr
        return completed.returncode, json.loads(stream)

    def input_taps(self) -> int:
        if not self.log.exists():
            return 0
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        return sum(1 for row in rows if row.get("tool") == "adb"
                   and "input" in row["argv"]
                   and row["argv"][row["argv"].index("input") + 1:][:1] == ["tap"])

    def test_cli_tap_reports_the_warning_and_taps_once(self) -> None:
        code, started = self.cli("session", "start", "--app-id", "com.example.app")
        self.assertEqual(code, 0, started)
        unwritable(self, Path(started["session"]["artifacts_dir"]) / "actions")
        code, payload = self.cli("ui", "tap", "--x", "1", "--y", "1")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertNotIn("detail", payload)
        self.assertEqual([w["code"] for w in payload["warnings"]], ["evidence_write_failed"])
        self.assertEqual(self.input_taps(), 1)

    def test_cli_find_and_type_append_the_warning_last(self) -> None:
        code, started = self.cli("session", "start", "--app-id", "com.example.app")
        self.assertEqual(code, 0, started)
        unwritable(self, Path(started["session"]["artifacts_dir"]) / "actions")
        code, found = self.cli("ui", "find", "--resource-id", "no.such:id/none")
        self.assertEqual(code, 0, found)
        codes = [w["code"] for w in found["warnings"]]
        self.assertEqual(codes[-1], "evidence_write_failed")
        self.assertEqual(codes.count("evidence_write_failed"), 1)
        code, typed = self.cli("ui", "type", "hello")
        self.assertEqual(code, 0, typed)
        codes = [w["code"] for w in typed["warnings"]]
        self.assertEqual(codes[-1], "evidence_write_failed")
        self.assertEqual(codes.count("evidence_write_failed"), 1)
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        self.assertEqual(sum(1 for row in rows if "text" in row["argv"]
                             and "input" in row["argv"]), 1)

    def test_cli_tap_with_a_writable_dir_has_no_warning(self) -> None:
        code, started = self.cli("session", "start", "--app-id", "com.example.app")
        self.assertEqual(code, 0, started)
        code, payload = self.cli("ui", "tap", "--x", "1", "--y", "1")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["detail"], "actions/0001_tap.json")
        self.assertNotIn("warnings", payload)

    def test_bridge_tap_reports_the_warning_and_taps_once(self) -> None:
        record = session_mod.start_session(str(FAKE_ADB), serial=SERIAL,
                                           app_id="com.example.app")
        unwritable(self, Path(record["artifacts_dir"]) / "actions")
        target = Target(ANDROID, SERIAL, str(FAKE_ADB), {"serial": SERIAL})
        result = bridge.dispatch(target, {"op": "tap", "origin": "agent",
                                          "payload": {"x": 1, "y": 1}})
        self.assertTrue(result["ok"], result)
        self.assertNotIn("detail", result)
        self.assertEqual(result["warnings"][0]["code"], "evidence_write_failed")
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(self.input_taps(), 1)
        journal = journal_mod.journal_path(record)
        entries = [json.loads(line) for line in
                   journal.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([entry.get("verb") for entry in entries], ["ui tap"])

    def test_bridge_record_reports_the_warning(self) -> None:
        record = session_mod.start_session(str(FAKE_ADB), serial=SERIAL,
                                           app_id="com.example.app")
        unwritable(self, Path(record["artifacts_dir"]) / "actions")
        target = Target(ANDROID, SERIAL, str(FAKE_ADB), {"serial": SERIAL})
        result = bridge.dispatch(target, {
            "op": "record", "origin": "human",
            "payload": {"kind": "key", "key": "KEYCODE_BACK", "transport": "scrcpy"}})
        self.assertTrue(result["ok"], result)
        self.assertEqual([w["code"] for w in result["warnings"]], ["evidence_write_failed"])
        self.assertEqual(self.input_taps(), 0)


if __name__ == "__main__":
    unittest.main()
