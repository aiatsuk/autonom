"""Several live sessions, one per target, beside the machine's current one.

A Canvas workspace starts a session for every device it attaches, while the
person (or agent) at the CLI keeps their own session in `current.json`. These
tests pin the rules that make that safe, against the fake tools only:

- `session start --alongside` never writes `current.json`;
- `--started-by canvas:<port>:<pid>` is validated and recorded;
- one live session per target, also when two starts race;
- command binding: `--session-id`, then explicit target flags, then
  `current.json`;
- `session stop` by target or id stops only that session, with no promotion;
- stale target pointers are dropped;
- the Canvas bridge and tools journal into the session on their own target;
- a proxy start retries a picked port that mitmdump lost to another process.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
UI_DUMP = ROOT / "tests/fixtures/ui_dump.xml"
PRIMARY = "emulator-5554"
CANVAS = "emulator-5556"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, journal as journal_mod, processes  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402
from autonom_lib.network import proxy  # noqa: E402
from autonom_lib.platform import ANDROID, Target  # noqa: E402
import autonom_canvas_bridge as bridge  # noqa: E402
import autonom_canvas_tools as canvas_tools  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


class MultiSessionCase(EnvSandboxMixin, unittest.TestCase):
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
        self.write_state()
        self.set_env(AUTONOM_HOME=str(self.home), AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.log), AUTONOM_ADB=None,
                     AUTONOM_SIMCTL=None, AUTONOM_IDB=None)
        self.env = dict(os.environ)
        self.env["PATH"] = str(self.bin)
        token = session_mod.select(None)
        self.addCleanup(session_mod._SELECTED.reset, token)

    def write_state(self, **extra) -> None:
        state = {"devices": [[PRIMARY, "device", "product:sdk_gphone64_arm64"],
                             [CANVAS, "device", "product:sdk_gphone64_arm64"]],
                 "ui_dump": str(UI_DUMP)}
        state.update(extra)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def cli(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--adb", str(FAKE_ADB), *argv],
            cwd=self.root, env=self.env, text=True, stdin=subprocess.DEVNULL,
            capture_output=True, check=False, timeout=120)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.returncode in (0, 1) else completed.stderr
        return completed.returncode, json.loads(stream)

    def start(self, serial: str, *extra: str) -> dict:
        code, payload = self.cli("--serial", serial, "session", "start",
                                 "--app-id", "com.example.app", *extra)
        self.assertEqual(code, 0, payload)
        return payload["session"]

    @property
    def current_file(self) -> Path:
        return self.home / "sessions" / "current.json"

    def current_id(self) -> str | None:
        if not self.current_file.exists():
            return None
        return json.loads(self.current_file.read_text(encoding="utf-8"))["session_id"]

    def record(self, session_id: str) -> dict:
        path = self.home / "sessions" / session_id / "session.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def journal_verbs(self, session_id: str) -> list[str]:
        path = self.home / "sessions" / session_id / journal_mod.JOURNAL_FILE
        if not path.exists():
            return []
        return [json.loads(line).get("verb") for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]


class AlongsideStartTests(MultiSessionCase):
    def test_alongside_with_nothing_live_never_writes_current(self) -> None:
        started = self.start(CANVAS, "--alongside")
        self.assertFalse(self.current_file.exists())
        self.assertIs(started["primary"], False)
        self.assertIsNone(started["started_by"])
        self.assertIsNone(session_mod.load_current())
        live = session_mod.live_for_target(CANVAS, "android")
        self.assertEqual(live["session_id"], started["session_id"])

    def test_alongside_beside_a_primary_keeps_current(self) -> None:
        primary = self.start(PRIMARY)
        self.assertIs(primary["primary"], True)
        before = self.current_file.read_bytes()
        other = self.start(CANVAS, "--alongside")
        self.assertIs(other["primary"], False)
        self.assertEqual(self.current_file.read_bytes(), before)
        self.assertEqual(self.current_id(), primary["session_id"])
        self.assertIsNone(self.record(other["session_id"]).get("stopped_at"))

    def test_an_explicit_other_target_starts_beside_the_primary(self) -> None:
        primary = self.start(PRIMARY)
        other = self.start(CANVAS)  # no --alongside: the live primary makes it one
        self.assertIs(other["primary"], False)
        self.assertEqual(self.current_id(), primary["session_id"])
        # its own saves and its journal entry went to the new session only
        self.assertIn("session start", self.journal_verbs(other["session_id"]))
        self.assertEqual(self.journal_verbs(primary["session_id"]).count("session start"), 1)

    def test_started_by_is_recorded(self) -> None:
        started = self.start(CANVAS, "--alongside", "--started-by", "canvas:3277:4242")
        expected = {"kind": "canvas", "port": 3277, "pid": 4242}
        self.assertEqual(started["started_by"], expected)
        self.assertEqual(self.record(started["session_id"])["started_by"], expected)
        code, stopped = self.cli("--serial", CANVAS, "session", "stop")
        self.assertEqual(code, 0, stopped)
        self.assertEqual(stopped["started_by"], expected)

    def test_started_by_is_validated(self) -> None:
        code, payload = self.cli("--serial", CANVAS, "session", "start",
                                 "--started-by", "canvas:3277:4242")
        self.assertEqual((code, payload["error_code"]), (2, "usage_error"))
        self.assertIn("--alongside", payload["error"])
        for bad in ("canvas:abc:1", "tool:1:2", "canvas:3277", "canvas:70000:1",
                    "canvas:1:0", "canvas:123456:1", " canvas:1:2"):
            with self.subTest(value=bad):
                code, payload = self.cli("--serial", CANVAS, "session", "start",
                                         "--alongside", "--started-by", bad)
                self.assertEqual((code, payload["error_code"]), (2, "usage_error"), payload)
        sessions = [path for path in (self.home / "sessions").iterdir() if path.is_dir()]
        self.assertEqual(sessions, [], "a refused start created a session")

    def test_a_second_start_on_the_same_target_names_the_starter(self) -> None:
        first = self.start(CANVAS, "--alongside", "--started-by", "canvas:3277:4242")
        for extra in ((), ("--alongside",)):
            with self.subTest(extra=extra):
                code, payload = self.cli("--serial", CANVAS, "session", "start", *extra)
                self.assertEqual((code, payload["error_code"]), (2, "session_already_active"))
                self.assertEqual(payload["session_id"], first["session_id"])
                self.assertEqual(payload["target_id"], CANVAS)
                self.assertEqual(payload["started_by"],
                                 {"kind": "canvas", "port": 3277, "pid": 4242})
                self.assertIn("session stop", payload["hint"])

    def test_two_racing_starts_on_one_target_leave_one_session(self) -> None:
        argv = [sys.executable, str(CLI), "--adb", str(FAKE_ADB), "--serial", CANVAS,
                "session", "start", "--alongside"]
        children = [subprocess.Popen(argv + ["--started-by", f"canvas:{3000 + n}:{100 + n}"],
                                     cwd=self.root, env=self.env, text=True,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
                    for n in range(2)]
        results = []
        for child in children:
            stdout, stderr = child.communicate(timeout=120)
            results.append((child.returncode, stdout, stderr))
        codes = sorted(code for code, _, _ in results)
        self.assertEqual(codes, [0, 2], results)
        winner = json.loads(next(out for code, out, _ in results if code == 0))["session"]
        refusal = json.loads(next(err for code, _, err in results if code == 2))
        self.assertEqual(refusal["error_code"], "session_already_active")
        self.assertEqual(refusal["session_id"], winner["session_id"])
        self.assertEqual(refusal["started_by"], winner["started_by"])
        sessions = [path for path in (self.home / "sessions").iterdir() if path.is_dir()]
        self.assertEqual(len(sessions), 1, sessions)
        self.assertFalse(self.current_file.exists())
        # whether it lost before or under the store lock, the refused start
        # is journaled in the session holding the target (and nowhere else)
        self.assertEqual(self.journal_verbs(winner["session_id"]).count("session start"), 2)

    def test_two_processes_starting_in_the_library_leave_one_session(self) -> None:
        # No CLI pre-check here: only the check under the store lock can
        # refuse the second start, whatever the timing.
        script = textwrap.dedent("""\
            import json, sys
            sys.path.insert(0, sys.argv[1])
            from autonom_lib import errors, session
            try:
                record = session.start_session(
                    "adb", serial=sys.argv[2], alongside=True,
                    started_by={"kind": "canvas", "port": int(sys.argv[3]), "pid": 7})
                print(json.dumps({"ok": True, "session_id": record["session_id"]}))
            except errors.AutonomError as exc:
                print(json.dumps(exc.as_dict()))
        """)
        children = [subprocess.Popen([sys.executable, "-c", script, str(ROOT / "scripts"),
                                      CANVAS, str(3000 + n)],
                                     env=self.env, text=True, stdout=subprocess.PIPE)
                    for n in range(2)]
        results = [json.loads(child.communicate(timeout=60)[0]) for child in children]
        winners = [item for item in results if item.get("ok")]
        losers = [item for item in results if not item.get("ok")]
        self.assertEqual((len(winners), len(losers)), (1, 1), results)
        self.assertEqual(losers[0]["error_code"], "session_already_active")
        self.assertEqual(losers[0]["session_id"], winners[0]["session_id"])
        self.assertEqual(losers[0]["started_by"]["kind"], "canvas")
        self.assertFalse(self.current_file.exists())

    def test_the_library_refuses_under_its_lock_too(self) -> None:
        first = session_mod.start_session("adb", serial=CANVAS, alongside=True,
                                          started_by={"kind": "canvas", "port": 1, "pid": 2})
        with self.assertRaises(errors.AutonomError) as caught:
            session_mod.start_session("adb", serial=CANVAS, exclusive=True)
        self.assertEqual(caught.exception.code, errors.SESSION_ALREADY_ACTIVE)
        self.assertEqual(caught.exception.extra["session_id"], first["session_id"])
        self.assertEqual(caught.exception.extra["started_by"],
                         {"kind": "canvas", "port": 1, "pid": 2})
        self.assertFalse(self.current_file.exists())

    def test_the_old_refusal_without_target_flags_is_unchanged(self) -> None:
        primary = self.start(PRIMARY)
        completed = subprocess.run(
            [sys.executable, str(CLI), "--adb", str(FAKE_ADB), "session", "start"],
            cwd=self.root, env=self.env, text=True, capture_output=True, timeout=120)
        payload = json.loads(completed.stderr)
        self.assertEqual((completed.returncode, payload["error_code"]),
                         (2, "session_already_active"))
        self.assertEqual(payload["session_id"], primary["session_id"])
        self.assertNotIn("started_by", payload)

    def test_a_failed_launch_rolls_back_only_the_new_session(self) -> None:
        primary = self.start(PRIMARY)
        self.write_state(fail={f"-s {CANVAS} shell am start": [1, "Error: Activity not started"]})
        code, payload = self.cli("--serial", CANVAS, "session", "start", "--alongside",
                                 "--app-id", "com.example.app", "--launch")
        self.assertEqual(code, 2, payload)
        rolled = payload["session_rolled_back"]
        self.assertTrue(self.record(rolled).get("stopped_at"))
        self.assertIsNone(self.record(primary["session_id"]).get("stopped_at"))
        self.assertEqual(self.current_id(), primary["session_id"])
        self.assertIsNone(session_mod.live_for_target(CANVAS))
        # the failed start is journaled nowhere: never in the primary on the
        # other device (as a rolled-back primary start never was either)
        self.assertEqual(self.journal_verbs(primary["session_id"]), ["session start"])
        self.assertEqual(self.journal_verbs(rolled), [])

    def test_a_start_that_loses_under_the_lock_journals_into_the_holder(self) -> None:
        # The CLI's own check passes (nothing live yet); a start by another
        # process wins in between, so only the check under the lock refuses.
        holder = {}
        real = session_mod.start_session

        def racing(*args, **kwargs):
            holder.update(real("adb", serial=CANVAS, alongside=True,
                               started_by={"kind": "canvas", "port": 1, "pid": 2}))
            return real(*args, **kwargs)

        import autonom  # noqa: PLC0415 - the CLI module, run in-process here
        argv = ["--adb", str(FAKE_ADB), "--serial", CANVAS, "session", "start", "--alongside"]
        with mock.patch.object(autonom.session_mod, "start_session", side_effect=racing), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            code = autonom.main(argv)
        self.assertEqual(code, 2)
        self.assertEqual(self.journal_verbs(holder["session_id"]), ["session start"])
        self.assertFalse(self.current_file.exists())


class BindingTests(MultiSessionCase):
    def setUp(self) -> None:
        super().setUp()
        self.primary = self.start(PRIMARY)
        self.canvas = self.start(CANVAS, "--alongside", "--started-by", "canvas:3277:4242")

    def show(self, *argv: str) -> str:
        code, payload = self.cli(*argv, "session", "show")
        self.assertEqual(code, 0, payload)
        return payload["session"]["session_id"]

    def test_binding_order(self) -> None:
        # 3. no flags: current.json
        self.assertEqual(self.show(), self.primary["session_id"])
        # 2. explicit target flags: the live session on that target
        self.assertEqual(self.show("--serial", CANVAS), self.canvas["session_id"])
        self.assertEqual(self.show("--target", CANVAS), self.canvas["session_id"])
        self.assertEqual(self.show("--platform", "android", "--serial", CANVAS),
                         self.canvas["session_id"])
        self.assertEqual(self.show("--serial", PRIMARY), self.primary["session_id"])
        # a target with no live session of its own falls back to current.json
        self.assertEqual(self.show("--serial", "emulator-5560"), self.primary["session_id"])
        # 1. --session-id wins over target flags
        self.assertEqual(self.show("--session-id", self.primary["session_id"],
                                   "--serial", CANVAS), self.primary["session_id"])

    def test_target_flags_journal_into_that_targets_session(self) -> None:
        code, payload = self.cli("--serial", CANVAS, "ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertIn("ui tree", self.journal_verbs(self.canvas["session_id"]))
        self.assertNotIn("ui tree", self.journal_verbs(self.primary["session_id"]))
        self.assertEqual(self.current_id(), self.primary["session_id"])

    def test_session_id_against_another_target_is_still_a_mismatch(self) -> None:
        code, payload = self.cli("--session-id", self.primary["session_id"],
                                 "--serial", CANVAS, "ui", "tree")
        self.assertEqual((code, payload["error_code"]), (2, "session_target_mismatch"))

    def test_stop_by_target_stops_only_that_session(self) -> None:
        code, payload = self.cli("--serial", CANVAS, "session", "stop")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["session"]["session_id"], self.canvas["session_id"])
        self.assertTrue(self.record(self.canvas["session_id"]).get("stopped_at"))
        self.assertIsNone(self.record(self.primary["session_id"]).get("stopped_at"))
        self.assertEqual(self.current_id(), self.primary["session_id"])
        self.assertFalse(session_mod.target_pointer_path("android", CANVAS).exists())
        self.assertTrue(session_mod.target_pointer_path("android", PRIMARY).exists())
        self.assertIsNone(session_mod.live_for_target(CANVAS))

    def test_stop_by_id(self) -> None:
        code, payload = self.cli("--session-id", self.canvas["session_id"], "session", "stop")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["session"]["session_id"], self.canvas["session_id"])
        self.assertEqual(self.current_id(), self.primary["session_id"])
        self.assertIsNone(session_mod.live_for_target(CANVAS))

    def test_stopping_the_primary_promotes_nothing(self) -> None:
        code, payload = self.cli("session", "stop")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["session"]["session_id"], self.primary["session_id"])
        self.assertIsNone(payload["started_by"])
        self.assertFalse(self.current_file.exists())
        live = session_mod.live_for_target(CANVAS)
        self.assertEqual(live["session_id"], self.canvas["session_id"])
        # verbs without target flags have no session now
        code, payload = self.cli("session", "show")
        self.assertEqual((code, payload["error_code"]), (2, "no_active_session"))
        # and a new start without flags becomes the primary again
        again = self.start(PRIMARY)
        self.assertIs(again["primary"], True)

    def test_live_sessions_lists_both_in_start_order(self) -> None:
        listed = session_mod.live_sessions()
        self.assertEqual([item["session_id"] for item in listed],
                         [self.primary["session_id"], self.canvas["session_id"]])
        self.assertEqual([item["primary"] for item in listed], [True, False])


class PointerTests(MultiSessionCase):
    def test_pointer_file_shape_and_mode(self) -> None:
        record = session_mod.start_session("adb", serial="127.0.0.1:5555", alongside=True)
        path = session_mod.target_pointer_path("android", "127.0.0.1:5555")
        digest = hashlib.sha256(b"127.0.0.1:5555").hexdigest()[:12]
        # a changed id carries a hash of the exact id; a clean one does not
        self.assertEqual(path.name, f"target-android__127.0.0.1_5555-{digest}.json")
        self.assertEqual(session_mod.target_pointer_path("android", CANVAS).name,
                         f"target-android__{CANVAS}.json")
        self.assertEqual(path.parent, session_mod.sessions_home())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        pointer = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(pointer, {"schema": 1, "session_id": record["session_id"],
                                   "platform": "android", "target_id": "127.0.0.1:5555",
                                   "started_at": record["started_at"]})
        # the session store's subdirectories are still sessions only
        dirs = [p.name for p in session_mod.sessions_home().iterdir() if p.is_dir()]
        self.assertEqual(dirs, [record["session_id"]])

    def test_a_pointer_to_a_missing_session_is_stale(self) -> None:
        path = session_mod.target_pointer_path("android", CANVAS)
        path.write_text(json.dumps({"schema": 1, "session_id": "s_gone000000",
                                    "platform": "android", "target_id": CANVAS}),
                        encoding="utf-8")
        self.assertIsNone(session_mod.live_for_target(CANVAS, "android"))
        self.assertFalse(path.exists())

    def test_a_pointer_to_a_stopped_session_is_stale(self) -> None:
        record = session_mod.start_session("adb", serial=CANVAS, alongside=True)
        path = session_mod.target_pointer_path("android", CANVAS)
        stored = self.record(record["session_id"])
        stored["stopped_at"] = "2026-10-07T10:00:00Z"
        (Path(record["artifacts_dir"]) / "session.json").write_text(json.dumps(stored),
                                                                    encoding="utf-8")
        self.assertIsNone(session_mod.live_for_target(CANVAS))
        self.assertFalse(path.exists())

    def test_a_pointer_naming_another_target_is_stale(self) -> None:
        record = session_mod.start_session("adb", serial=PRIMARY, alongside=True)
        path = session_mod.target_pointer_path("android", CANVAS)
        path.write_text(json.dumps({"schema": 1, "session_id": record["session_id"],
                                    "platform": "android", "target_id": CANVAS}),
                        encoding="utf-8")
        self.assertIsNone(session_mod.live_for_target(CANVAS))
        self.assertFalse(path.exists())
        self.assertEqual(session_mod.live_for_target(PRIMARY)["session_id"],
                         record["session_id"])

    def test_a_pointer_file_of_another_target_is_left_alone(self) -> None:
        record = session_mod.start_session("adb", serial=PRIMARY, alongside=True)
        path = session_mod.target_pointer_path("android", CANVAS)
        pointer = {"schema": 1, "session_id": record["session_id"],
                   "platform": "android", "target_id": PRIMARY}
        path.write_text(json.dumps(pointer), encoding="utf-8")
        self.assertIsNone(session_mod.live_for_target(CANVAS))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), pointer)

    def test_similar_target_ids_never_share_or_erase_a_pointer(self) -> None:
        wifi, lookalike = "192.168.1.5:5555", "192.168.1.5_5555"
        self.assertNotEqual(session_mod.target_pointer_path("android", wifi),
                            session_mod.target_pointer_path("android", lookalike))
        first = session_mod.start_session("adb", serial=wifi, alongside=True,
                                          started_by={"kind": "canvas", "port": 1, "pid": 2})
        # a command naming the look-alike binds nothing and erases nothing
        self.cli("--serial", lookalike, "session", "show")
        self.assertIsNone(session_mod.live_for_target(lookalike))
        self.assertEqual(session_mod.live_for_target(wifi)["session_id"], first["session_id"])
        with self.assertRaises(errors.AutonomError) as caught:
            session_mod.start_session("adb", serial=wifi, exclusive=True)
        self.assertEqual(caught.exception.code, errors.SESSION_ALREADY_ACTIVE)
        other = session_mod.start_session("adb", serial=lookalike, alongside=True)
        self.assertEqual(session_mod.live_for_target(lookalike)["session_id"],
                         other["session_id"])
        self.assertEqual(session_mod.live_for_target(wifi)["session_id"], first["session_id"])

    def test_a_stale_pointer_rewritten_meanwhile_is_kept(self) -> None:
        old = session_mod.start_session("adb", serial=CANVAS, alongside=True)
        stored = self.record(old["session_id"])
        stored["stopped_at"] = "2026-10-07T10:00:00Z"
        (Path(old["artifacts_dir"]) / "session.json").write_text(json.dumps(stored),
                                                                 encoding="utf-8")
        newer = {}
        real_read = session_mod._read_target_pointer
        calls = []

        def read(path):
            value = real_read(path)
            calls.append(path)
            if len(calls) == 1:
                # between the lookup's read and its delete, a new session on
                # the target writes its own pointer
                newer.update(session_mod.start_session("adb", serial=CANVAS,
                                                       alongside=True))
            return value

        with mock.patch.object(session_mod, "_read_target_pointer", side_effect=read):
            self.assertIsNone(session_mod.live_for_target(CANVAS, "android"))
        path = session_mod.target_pointer_path("android", CANVAS)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["session_id"],
                         newer["session_id"])
        self.assertEqual(session_mod.live_for_target(CANVAS)["session_id"],
                         newer["session_id"])

    def test_a_stop_never_deletes_the_pointer_of_a_start_racing_it(self) -> None:
        old = session_mod.start_session("adb", serial=CANVAS, alongside=True)
        started = {}
        real_read = session_mod._read_target_pointer
        hooked = []

        def start_beside() -> None:
            started.update(session_mod.start_session("adb", serial=CANVAS, alongside=True,
                                                     exclusive=True))

        thread = threading.Thread(target=start_beside)

        def read(path):
            value = real_read(path)
            if not hooked:
                # the stop has read its pointer; a start on the same target
                # runs now (it waits for the target lock the stop holds)
                hooked.append(path)
                thread.start()
                time.sleep(0.5)
            return value

        with mock.patch.object(session_mod, "_read_target_pointer", side_effect=read):
            session_mod.stop_session(session_id=old["session_id"], reap=False)
            thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        live = session_mod.live_for_target(CANVAS)
        self.assertEqual(live["session_id"], started["session_id"])
        path = session_mod.target_pointer_path("android", CANVAS)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["session_id"],
                         started["session_id"])
        with self.assertRaises(errors.AutonomError) as caught:
            session_mod.start_session("adb", serial=CANVAS, exclusive=True)
        self.assertEqual(caught.exception.code, errors.SESSION_ALREADY_ACTIVE)

    def test_an_unreadable_pointer_is_dropped(self) -> None:
        path = session_mod.target_pointer_path("android", CANVAS)
        path.write_text("{trunc", encoding="utf-8")
        self.assertIsNone(session_mod.live_for_target(CANVAS))
        self.assertFalse(path.exists())

    def test_a_session_from_before_pointers_is_found_through_current(self) -> None:
        record = session_mod.start_session("adb", serial=PRIMARY)
        session_mod.target_pointer_path("android", PRIMARY).unlink()
        self.assertEqual(session_mod.live_for_target(PRIMARY)["session_id"],
                         record["session_id"])
        self.assertIsNone(session_mod.live_for_target(PRIMARY, "ios"))
        self.assertIsNone(session_mod.live_for_target(CANVAS))

    def test_a_platform_mismatch_is_not_live(self) -> None:
        record = session_mod.start_session("xcrun", platform="ios", target_id=UDID,
                                           alongside=True)
        self.assertIsNone(session_mod.live_for_target(UDID, "android"))
        self.assertEqual(session_mod.live_for_target(UDID, "ios")["session_id"],
                         record["session_id"])
        self.assertEqual(session_mod.live_for_target(UDID)["session_id"],
                         record["session_id"])

    def test_stop_keeps_a_newer_sessions_pointer(self) -> None:
        old = session_mod.start_session("adb", serial=CANVAS, alongside=True)
        newer = session_mod.start_session("adb", serial=CANVAS)  # library default: no check
        session_mod.stop_session(session_id=old["session_id"], reap=False)
        self.assertEqual(session_mod.live_for_target(CANVAS)["session_id"],
                         newer["session_id"])

    def test_stopping_a_stopped_session_clears_its_pointers(self) -> None:
        record = session_mod.start_session("adb", serial=CANVAS, alongside=True)
        first = session_mod.stop_session(session_id=record["session_id"], reap=False)
        again = session_mod.stop_session(session_id=record["session_id"], reap=False)
        self.assertEqual(again["stopped_at"], first["stopped_at"])
        self.assertFalse(session_mod.target_pointer_path("android", CANVAS).exists())

    def test_live_sessions_skip_old_records_nobody_points_at(self) -> None:
        orphan = session_mod.start_session("adb", serial=CANVAS, alongside=True)
        session_mod.target_pointer_path("android", CANVAS).unlink()
        self.assertEqual(session_mod.live_sessions(), [])
        self.assertIsNone(self.record(orphan["session_id"]).get("stopped_at"))


class CanvasJournalTests(MultiSessionCase):
    """Each Canvas device journals into the session on its own target."""

    def setUp(self) -> None:
        super().setUp()
        self.primary = session_mod.start_session(str(FAKE_ADB), serial=PRIMARY,
                                                 app_id="com.example.app")
        self.canvas = session_mod.start_session(
            str(FAKE_ADB), serial=CANVAS, app_id="com.example.canvas", alongside=True,
            started_by={"kind": "canvas", "port": 3277, "pid": os.getpid()})

    def target(self, serial: str) -> Target:
        return Target(ANDROID, serial, str(FAKE_ADB), {"serial": serial})

    def test_the_bridge_finds_each_targets_session(self) -> None:
        self.assertEqual(bridge.journal_session(self.target(CANVAS))["session_id"],
                         self.canvas["session_id"])
        self.assertEqual(bridge.journal_session(self.target(PRIMARY))["session_id"],
                         self.primary["session_id"])
        self.assertIsNone(bridge.journal_session(self.target("emulator-5560")))

    def test_a_record_journals_into_the_canvas_session_only(self) -> None:
        result = bridge.dispatch(self.target(CANVAS), {
            "op": "record", "origin": "human",
            "payload": {"kind": "key", "key": "KEYCODE_BACK", "transport": "scrcpy"}})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.journal_verbs(self.canvas["session_id"]), ["ui key"])
        self.assertEqual(self.journal_verbs(self.primary["session_id"]), [])
        self.assertEqual(self.current_id(), self.primary["session_id"])

    def test_a_save_after_the_lookup_never_moves_current(self) -> None:
        record = bridge.journal_session(self.target(CANVAS))
        record["app_id"] = "com.example.changed"
        session_mod.save(record)
        self.assertEqual(self.current_id(), self.primary["session_id"])
        self.assertEqual(self.record(self.canvas["session_id"])["app_id"],
                         "com.example.changed")
        # back on the primary's target the binding follows current.json again
        bridge.journal_session(self.target(PRIMARY))
        self.assertIsNone(session_mod._SELECTED.get())

    def test_tools_use_the_canvas_session(self) -> None:
        tools = canvas_tools.Tools(self.target(CANVAS))
        self.assertEqual(tools.session()["session_id"], self.canvas["session_id"])
        session_mod.stop_session(session_id=self.canvas["session_id"], reap=False)
        self.assertIsNone(tools.session())
        with self.assertRaises(errors.AutonomError) as caught:
            tools.require_session()
        self.assertEqual(caught.exception.code, errors.NO_ACTIVE_SESSION)
        self.assertEqual(self.current_id(), self.primary["session_id"])


FAKE_MITMDUMP = textwrap.dedent("""\
    #!{python}
    import os, socket, sys, time
    counter = os.environ["FAKE_MITM_COUNTER"]
    with open(counter, "a", encoding="utf-8") as handle:
        handle.write("x")
    calls = len(open(counter, encoding="utf-8").read())
    if calls <= int(os.environ.get("FAKE_MITM_FAILS", "0")):
        sys.stderr.write("Error starting proxy server: address already in use\\n")
        sys.exit(1)
    port = int(sys.argv[sys.argv.index("--listen-port") + 1])
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("127.0.0.1", port))
        server.listen()
    except OSError as exc:
        sys.stderr.write("Error starting proxy server: " + str(exc) + "\\n")
        if os.environ.get("FAKE_MITM_STAY"):
            time.sleep(60)  # a mitmdump that keeps running without its port
        sys.exit(1)
    time.sleep(60)
""")


class ProxyRetryTests(MultiSessionCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake = self.root / "mitmdump"
        self.fake.write_text(FAKE_MITMDUMP.format(python=sys.executable), encoding="utf-8")
        self.fake.chmod(0o755)
        self.counter = self.root / "mitm-calls"
        self.record = session_mod.start_session("adb", serial=CANVAS, alongside=True)
        self.spawned: list[subprocess.Popen] = []
        real_popen = subprocess.Popen

        def keep(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            argv = args[0] if args else kwargs.get("args")
            if argv and str(argv[0]) == str(self.fake):  # not lsof or ps
                self.spawned.append(child)
                self.addCleanup(self._reap, child)
            return child

        patcher = mock.patch.object(proxy.subprocess, "Popen", side_effect=keep)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _reap(child: subprocess.Popen) -> None:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)

    def calls(self) -> int:
        return len(self.counter.read_text(encoding="utf-8")) if self.counter.exists() else 0

    def test_a_lost_picked_port_is_retried(self) -> None:
        self.set_env(FAKE_MITM_COUNTER=str(self.counter), FAKE_MITM_FAILS="2")
        state = proxy.start(self.record, mitmdump=str(self.fake))
        self.assertTrue(state["running"])
        self.assertEqual(self.calls(), 3)
        self.assertEqual(len(self.spawned), 3)
        self.assertEqual([child.poll() is not None for child in self.spawned],
                         [True, True, False])
        [row] = [e for e in processes.entries() if e["kind"] == "proxy"]
        self.assertEqual(row["pid"], self.spawned[-1].pid)
        self.assertEqual(row["session_id"], self.record["session_id"])

    def test_three_failures_are_backend_failed(self) -> None:
        self.set_env(FAKE_MITM_COUNTER=str(self.counter), FAKE_MITM_FAILS="9")
        with self.assertRaises(errors.AutonomError) as caught:
            proxy.start(self.record, mitmdump=str(self.fake))
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertEqual(self.calls(), proxy.PROXY_START_ATTEMPTS)
        self.assertFalse(proxy.proxy_file(self.record).exists())
        self.assertEqual([e for e in processes.entries() if e["kind"] == "proxy"], [])

    def test_an_explicit_port_is_tried_once(self) -> None:
        self.set_env(FAKE_MITM_COUNTER=str(self.counter), FAKE_MITM_FAILS="1")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        with self.assertRaises(errors.AutonomError) as caught:
            proxy.start(self.record, port=free, mitmdump=str(self.fake))
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertEqual(self.calls(), 1)

    def foreign_listener(self) -> int:
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen()
        return server.getsockname()[1]

    def pick_first(self, port: int) -> None:
        """The first pick returns `port` (as if another proxy took it right
        after the pick released it), later picks are real."""
        real_pick = proxy._pick_port
        queue = [port]

        def pick(requested):
            return queue.pop(0) if queue and not requested else real_pick(requested)

        patcher = mock.patch.object(proxy, "_pick_port", side_effect=pick)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_retried_past(self, taken: int, state: dict) -> None:
        self.assertTrue(state["running"])
        self.assertNotEqual(state["port"], taken)
        self.assertEqual(len(self.spawned), 2)
        self.assertIsNotNone(self.spawned[0].poll(), "the losing mitmdump was left running")
        self.assertIsNone(self.spawned[1].poll())
        self.assertEqual(state["pid"], self.spawned[1].pid)
        stored = json.loads(proxy.proxy_file(self.record).read_text(encoding="utf-8"))
        self.assertEqual((stored["pid"], stored["port"]), (state["pid"], state["port"]))
        [row] = [e for e in processes.entries() if e["kind"] == "proxy"]
        self.assertEqual((row["pid"], row["port"]), (state["pid"], state["port"]))

    def test_a_picked_port_another_process_listens_on_is_retried(self) -> None:
        self.set_env(FAKE_MITM_COUNTER=str(self.counter), FAKE_MITM_FAILS="0")
        taken = self.foreign_listener()
        self.pick_first(taken)
        state = proxy.start(self.record, mitmdump=str(self.fake))
        self.assert_retried_past(taken, state)

    def test_a_mitmdump_still_running_without_its_port_is_stopped(self) -> None:
        self.set_env(FAKE_MITM_COUNTER=str(self.counter), FAKE_MITM_FAILS="0",
                     FAKE_MITM_STAY="1")
        taken = self.foreign_listener()
        self.pick_first(taken)
        state = proxy.start(self.record, mitmdump=str(self.fake))
        self.assert_retried_past(taken, state)

    def test_without_an_owner_lookup_the_child_must_stay_up(self) -> None:
        self.set_env(FAKE_MITM_COUNTER=str(self.counter), FAKE_MITM_FAILS="0")
        taken = self.foreign_listener()
        self.pick_first(taken)
        with mock.patch.object(proxy, "_listener_owned_by", return_value=None):
            state = proxy.start(self.record, mitmdump=str(self.fake))
        self.assert_retried_past(taken, state)

    def test_the_owner_lookup_tells_ours_from_a_strangers(self) -> None:
        taken = self.foreign_listener()
        self.assertIs(proxy._listener_owned_by(taken, os.getpgrp()), True)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        self.assertIs(proxy._listener_owned_by(free, os.getpgrp()), False)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 start_new_session=True)
        self.addCleanup(self._reap, child)
        self.assertIs(proxy._listener_owned_by(taken, child.pid), False)

    def test_an_explicit_port_taken_during_the_start_is_port_unavailable(self) -> None:
        self.set_env(FAKE_MITM_COUNTER=str(self.counter), FAKE_MITM_FAILS="0")
        taken = self.foreign_listener()
        # the free-port check passed; the port was taken right after it
        with mock.patch.object(proxy, "_pick_port", side_effect=lambda requested: requested):
            with self.assertRaises(errors.AutonomError) as caught:
                proxy.start(self.record, port=taken, mitmdump=str(self.fake))
        self.assertEqual(caught.exception.code, errors.PORT_UNAVAILABLE)
        self.assertEqual(self.calls(), 1)
        self.assertFalse(proxy.proxy_file(self.record).exists())
        self.assertEqual([e for e in processes.entries() if e["kind"] == "proxy"], [])

    def test_an_explicit_busy_port_is_port_unavailable(self) -> None:
        self.set_env(FAKE_MITM_COUNTER=str(self.counter), FAKE_MITM_FAILS="0")
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            with self.assertRaises(errors.AutonomError) as caught:
                proxy.start(self.record, port=busy.getsockname()[1],
                            mitmdump=str(self.fake))
        self.assertEqual(caught.exception.code, errors.PORT_UNAVAILABLE)
        self.assertEqual(self.calls(), 0)


if __name__ == "__main__":
    unittest.main()
