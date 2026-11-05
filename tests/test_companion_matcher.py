"""Exact idb companion matching (CO-02).

A process is the idb companion of a simulator only when its program is
`idb_companion` (argv[0]'s base name, or argv[1]'s when argv[0] is an
interpreter, as a shebang companion runs) and `--udid` is followed by
exactly that UDID. A decoy whose command line merely contains both strings
is neither claimed, nor reported, nor killed.

- the matcher itself, on command lines as `ps` prints them: the Homebrew
  companion, a shebang companion under a framework Python and under
  `/usr/bin/python3`, and decoys of every near-miss shape;
- registry rows: `entry_matches` holds an `idb_companion` row to the exact
  rule, so `terminate_entry`, `reap_session` and `cleanup --all` answer
  `pid_reused` for a row whose pid runs a decoy and never signal it;
- CO-02-S01 with real processes: a running decoy
  (`python3 -c ... idb_companion --udid <UDID>`) does not count as the
  companion "already running", so the shebang companion an idb call starts
  is registered and stopped with the session, while the decoy keeps
  running; once through the library and the fake idb, once through the CLI
  (`session start`, `ui tree`, `session stop`).

- the live script `tests/live/ios_companion_live.py` (CASE-004), run in
  process with the simulator, the machine's companions and the CLI faked;
  its approved command line (`--udid ... --boot --evidence-dir ...`) parses,
  and `--boot` boots and shuts down only the given simulator, only when it
  was shut down; after that boot `ui tree` is tried again every 2 s for at
  most 60 s (a fake clock), only for `backend_failed` or a call that hung
  to the CLI timeout (any other error code fails at once, exit 1), and
  every attempt is reported; when the retry runs out the read is recorded
  as failed and the rest of the case still runs, so a simulator that never
  answers is an environment fault (exit 3) only when every other check
  passed; one already booted gets a single attempt;
  its report says `ok` only for a run that reached its last step with every
  check recorded; Ctrl-C, a real SIGINT and an unexpected exception are
  recorded as `error`, a second Ctrl-C does not cut the cleanup short, and
  the decoy is stopped even when its own start step fails or is interrupted.

No real idb, simulator or device: the companions are shebang scripts named
`idb_companion`, started by the test or by `tests/fakes/fake_idb.py`.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_IDB = ROOT / "tests/fakes/fake_idb.py"
IOS_FIXTURE = ROOT / "tests/fixtures/idb_describe_all_sample.json"
BUNDLE = "com.example.app"
SLEEP = "import time; time.sleep(120)"
COMPANION = f"#!{sys.executable}\nimport time\ntime.sleep(120)\n"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import ios_idb, processes, session  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


def _udid() -> str:
    return f"FAKE-{uuid.uuid4().hex[:8].upper()}-0000-0000-0000-000000000000"


def _wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --- the matcher ---------------------------------------------------------------------------


U = "FAKE-1234ABCD-0000-0000-0000-000000000000"
FRAMEWORK_PYTHON = ("/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/"
                    "Versions/3.14/Resources/Python.app/Contents/MacOS/Python")

COMPANIONS = {
    "Homebrew, as idb starts it":
        f"/opt/homebrew/bin/idb_companion --udid {U} --grpc-domain-sock "
        f"/tmp/idb/{U}_companion.sock --only simulator",
    "Cellar path, --udid later": f"/opt/homebrew/Cellar/idb-companion/1.6.4/bin/idb_companion "
                                 f"--grpc-port 10882 --udid {U}",
    "bare program name": f"idb_companion --udid {U}",
    "shebang script, framework Python": f"{FRAMEWORK_PYTHON} /var/folders/x/T/tmp1/"
                                        f"idb_companion --udid {U}",
    "shebang script, Linux python3": f"/usr/bin/python3 /tmp/tmp1/bin/idb_companion --udid {U}",
    "shebang script, python3.11": f"/usr/local/bin/python3.11 /tmp/idb_companion --udid {U}",
    "shell script": f"/bin/sh /tmp/bin/idb_companion --udid {U}",
}

DECOYS = {
    "VER-004 decoy": f"python3 -c import time; time.sleep(600) idb_companion --udid {U}",
    "the same under a framework Python":
        f"{FRAMEWORK_PYTHON} -c import time; time.sleep(120) idb_companion --udid {U}",
    "argv[1] idb_companion, argv[0] no interpreter": f"/bin/echo idb_companion --udid {U}",
    "a sleep naming both": f"/bin/sleep 600 idb_companion --udid {U}",
    "grep for it": f"grep idb_companion --udid {U}",
    "tail of its socket log": f"tail -f /tmp/idb/{U}_companion.log idb_companion",
    "another script naming it": f"/usr/bin/python3 /tmp/probe.py idb_companion --udid {U}",
    "python -m": f"/usr/bin/python3 -m idb_companion --udid {U}",
    "interpreter flag before the script": f"/usr/bin/python3 --udid {U} /tmp/idb_companion",
    "a longer program name": f"/opt/homebrew/bin/idb_companion_wrapper --udid {U}",
    "a backup of the binary": f"/opt/homebrew/bin/idb_companion.bak --udid {U}",
    "a directory named like it": f"/tmp/idb_companion/run --udid {U}",
    "a longer UDID": f"/opt/homebrew/bin/idb_companion --udid {U}-EXTRA",
    "--udid= form": f"/opt/homebrew/bin/idb_companion --udid={U}",
    "the UDID after another flag": f"/opt/homebrew/bin/idb_companion --notify {U} --udid OTHER",
    "the UDID before --udid": f"/opt/homebrew/bin/idb_companion {U} --udid",
    "another simulator's companion":
        f"/opt/homebrew/bin/idb_companion --udid OTHER --grpc-domain-sock /tmp/idb/{U}.sock",
    # `ps` joins arguments with spaces: a program path holding one cannot be
    # told apart from a decoy, so it is never claimed (documented limit)
    "a program path with a space": f"/Applications/My Tools/idb_companion --udid {U}",
}


class MatcherTests(unittest.TestCase):
    def test_real_companions_match(self) -> None:
        for name, command in COMPANIONS.items():
            with self.subTest(name):
                self.assertTrue(processes.is_companion_command(command, U), command)

    def test_decoys_never_match(self) -> None:
        for name, command in DECOYS.items():
            with self.subTest(name):
                self.assertFalse(processes.is_companion_command(command, U), command)

    def test_the_old_substring_rule_took_every_decoy(self) -> None:
        # the shape of the defect: each decoy holds both marks
        for name, command in DECOYS.items():
            with self.subTest(name):
                self.assertIn(processes.COMPANION_MARKER, command)
                self.assertIn(U, command)

    def test_nothing_to_match(self) -> None:
        good = COMPANIONS["Homebrew, as idb starts it"]
        for command, udid in ((good, ""), (good, None), ("", U), (None, U),
                              ("   ", U), (good, "OTHER")):
            with self.subTest(command=command, udid=udid):
                self.assertFalse(processes.is_companion_command(command, udid))

    def test_discovery_keeps_only_real_companions(self) -> None:
        table = [(100 + index, command) for index, command in enumerate(
            [*COMPANIONS.values(), *DECOYS.values()])]
        with mock.patch.object(processes, "_running_processes", return_value=table):
            found = processes.discover_companions(U)
        self.assertEqual([item["pid"] for item in found],
                         [100 + index for index in range(len(COMPANIONS))])
        self.assertEqual({item["udid"] for item in found}, {U})


class RowMatchTests(unittest.TestCase):
    def test_a_companion_row_is_held_to_the_exact_rule(self) -> None:
        row = {"kind": "idb_companion", "udid": U,
               "signature": [processes.COMPANION_MARKER, U]}
        for command in COMPANIONS.values():
            with self.subTest(command=command):
                self.assertTrue(processes.entry_matches(row, command))
        for command in DECOYS.values():
            with self.subTest(command=command):
                self.assertFalse(processes.entry_matches(row, command))
                # the marks alone still say yes: the substring check is not enough
                if U in command and processes.COMPANION_MARKER in command:
                    self.assertTrue(processes.signature_matches(row["signature"], command))

    def test_the_simulator_of_a_row(self) -> None:
        good = COMPANIONS["Homebrew, as idb starts it"]
        decoy = DECOYS["VER-004 decoy"]
        cases = {
            "udid and signature": ({"udid": U, "signature": [processes.COMPANION_MARKER, U]},
                                   True),
            "signature only": ({"signature": [processes.COMPANION_MARKER, U]}, True),
            "another simulator's udid": ({"udid": "OTHER",
                                          "signature": [processes.COMPANION_MARKER, U]}, False),
            "the program name alone": ({"signature": processes.COMPANION_MARKER}, False),
        }
        for name, (detail, expected) in cases.items():
            with self.subTest(name):
                row = {"kind": "idb_companion", **detail}
                self.assertEqual(processes.entry_matches(row, good), expected)
                self.assertFalse(processes.entry_matches(row, decoy))

    def test_other_rows_keep_their_rule(self) -> None:
        # only `idb_companion` rows are held to the companion rule
        row = {"kind": "canvas", "signature": [processes.COMPANION_MARKER, U]}
        self.assertTrue(processes.entry_matches(row, DECOYS["VER-004 decoy"]))


# --- real processes ------------------------------------------------------------------------


class _Processes(EnvSandboxMixin, unittest.TestCase):
    """A private AUTONOM_HOME, an iOS session on a fresh fake UDID, a running
    decoy for that UDID and a shebang companion script."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.udid = _udid()
        self.set_env(AUTONOM_HOME=str(self.home), AUTONOM_IDB_COMPANION=None,
                     AUTONOM_IDB=None, AUTONOM_SIMCTL=None, AUTONOM_UI_BACKEND=None,
                     AUTONOM_IOS_HID=None, AUTONOM_AXE=None, AUTONOM_FAKE_LOG=None,
                     AUTONOM_FAKE_STATE=None, AUTONOM_IDB_STATE_FILE=None)
        token = session.select(None)
        self.addCleanup(session._SELECTED.reset, token)  # noqa: SLF001
        # Machine-wide by design: without this a `cleanup()` here could see
        # another run's proxies instead of only this test's rows.
        patcher = mock.patch.object(processes, "discover_proxies", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.companion = self.root / "bin-companion" / "idb_companion"
        self.companion.parent.mkdir()
        self.companion.write_text(COMPANION, encoding="utf-8")
        self.companion.chmod(0o755)
        self.decoy = self.spawn(sys.executable, "-c", SLEEP, "idb_companion",
                                "--udid", self.udid)
        # `ps` must show the decoy as it is, with both marks, before anything
        # is asked of the matcher: a decoy not yet listed proves nothing
        self.assertTrue(_wait_until(lambda: self.shows_both_marks(self.decoy.pid)),
                        processes.command_of(self.decoy.pid))

    def shows_both_marks(self, pid: int) -> bool:
        command = processes.command_of(pid) or ""
        return processes.COMPANION_MARKER in command and self.udid in command

    def spawn(self, *argv: str) -> subprocess.Popen:
        child = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop, child)
        return child

    @staticmethod
    def _stop(child: subprocess.Popen) -> None:
        if child.poll() is None:
            child.kill()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def start_companion(self) -> subprocess.Popen:
        process = self.spawn(str(self.companion), "--udid", self.udid)
        self.assertTrue(_wait_until(lambda: any(
            item["pid"] == process.pid for item in processes.discover_companions(self.udid))))
        return process

    def ios_session(self) -> dict:
        return session.start_session("/fake/xcrun", platform="ios", target_id=self.udid,
                                     app_id=BUNDLE)

    def assert_decoy_alive(self) -> None:
        time.sleep(0.2)  # a signal sent to it would have landed by now
        self.assertIsNone(self.decoy.poll(), "the decoy was killed")


class DecoyProcessTests(_Processes):
    def test_the_decoy_is_never_discovered(self) -> None:
        self.assertEqual(processes.discover_companions(self.udid), [])
        companion = self.start_companion()
        self.assertEqual([item["pid"] for item in processes.discover_companions(self.udid)],
                         [companion.pid])

    def test_a_decoy_does_not_hide_the_companion_an_idb_call_starts(self) -> None:
        """CO-02-S01. Before: the decoy counted as a companion that was
        already running, so the one the call started was never registered,
        and `session stop` left it running."""
        record = self.ios_session()
        with processes.track_idb_companions(self.udid, record=record):
            spawned = self.start_companion()  # what idb does on first use
        rows = [e for e in processes.entries() if e["kind"] == "idb_companion"]
        self.assertEqual([(row["pid"], row["owner"], row["udid"]) for row in rows],
                         [(spawned.pid, record["session_id"], self.udid)])
        result = processes.reap_session(record)
        self.assertEqual([(item["pid"], item["result"]) for item in result["terminated"]],
                         [(spawned.pid, "terminated")])
        self.assertEqual(result["companion_left_running"], [])
        self.assertTrue(_wait_until(lambda: spawned.poll() is not None))
        self.assert_decoy_alive()
        self.assertEqual(processes.entries(), [])

    def test_a_row_naming_the_decoy_is_never_signalled(self) -> None:
        record = self.ios_session()
        row = {"kind": "idb_companion", "pid": self.decoy.pid, "udid": self.udid,
               "signature": [processes.COMPANION_MARKER, self.udid]}
        self.assertEqual(processes.terminate_entry(row), "pid_reused")
        self.assert_decoy_alive()
        # registered under the session: neither `session stop` nor `cleanup --all` kills it
        processes.register("idb_companion", self.decoy.pid, owner=record["session_id"],
                           session_id=record["session_id"],
                           artifacts_dir=record["artifacts_dir"], udid=self.udid,
                           signature=[processes.COMPANION_MARKER, self.udid],
                           spawned_by="idb")
        result = processes.cleanup(include_live=True)
        self.assertEqual([(item["pid"], item["result"]) for item in result["actions"]],
                         [(self.decoy.pid, "pid_reused")])
        self.assert_decoy_alive()
        processes.register("idb_companion", self.decoy.pid, owner=record["session_id"],
                           session_id=record["session_id"],
                           artifacts_dir=record["artifacts_dir"], udid=self.udid,
                           signature=[processes.COMPANION_MARKER, self.udid],
                           spawned_by="idb")
        result = processes.reap_session(record)
        self.assertEqual([(item["pid"], item["result"]) for item in result["terminated"]],
                         [(self.decoy.pid, "pid_reused")])
        self.assertEqual(result["companion_left_running"], [])
        self.assert_decoy_alive()

    def test_an_already_running_companion_is_still_reported_not_killed(self) -> None:
        record = self.ios_session()
        foreign = self.start_companion()
        with processes.track_idb_companions(self.udid, record=record):
            pass
        self.assertEqual(processes.entries(), [])
        result = processes.reap_session(record)
        self.assertEqual([item["pid"] for item in result["companion_left_running"]],
                         [foreign.pid])
        self.assert_decoy_alive()
        self.assertIsNone(foreign.poll(), "a companion Autonom did not start was killed")


class FakeIdbRouteTests(_Processes):
    """The companion is started by the fake idb, as real idb starts one for a
    simulator its registry does not list."""

    def setUp(self) -> None:
        super().setUp()
        self.registry = self.root / "idb-state.json"
        self.state = self.root / "state.json"
        self.pid_file = self.root / "companion.pid"
        self.addCleanup(self._kill, self.pid_file)
        self.set_env(AUTONOM_IDB=str(FAKE_IDB), AUTONOM_IDB_STATE_FILE=str(self.registry),
                     AUTONOM_FAKE_STATE=str(self.state))

    def write_state(self, **extra) -> None:
        self.state.write_text(json.dumps({
            "idb_describe_all": str(IOS_FIXTURE),
            "idb_spawn_companion": {"path": str(self.companion),
                                    "pid_file": str(self.pid_file)},
            **extra}), encoding="utf-8")

    @staticmethod
    def _kill(pid_file: Path) -> None:
        try:
            os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)
        except (OSError, ValueError):
            pass

    def test_the_companion_idb_starts_beside_a_decoy_is_stopped_with_the_session(self) -> None:
        self.write_state()
        record = session.start_session("/fake/xcrun", platform="ios", target_id=self.udid,
                                       app_id=BUNDLE)
        completed = ios_idb.run_idb(str(FAKE_IDB), ["ui", "describe-all", "--json"],
                                    udid=self.udid)
        self.assertEqual(completed.returncode, 0)
        spawned = int(self.pid_file.read_text(encoding="utf-8"))
        rows = [e for e in processes.entries() if e["kind"] == "idb_companion"]
        self.assertEqual([(row["pid"], row["owner"], row["udid"]) for row in rows],
                         [(spawned, record["session_id"], self.udid)])
        result = processes.reap_session(record)
        self.assertEqual([item["pid"] for item in result["terminated"]], [spawned])
        self.assertEqual(result["companion_left_running"], [])
        self.assertTrue(_wait_until(lambda: not _alive(spawned)))
        self.assert_decoy_alive()


class CliSessionStopTests(_Processes):
    """CO-02-S01 through the CLI: `session start`, `ui tree` (the fake idb
    starts the companion), `session stop`."""

    def setUp(self) -> None:
        super().setUp()
        self.registry = self.root / "idb-state.json"
        self.state = self.root / "state.json"
        self.pid_file = self.root / "companion.pid"
        self.addCleanup(FakeIdbRouteTests._kill, self.pid_file)  # noqa: SLF001
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.state.write_text(json.dumps({
            "simctl_devices": {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
                {"udid": self.udid, "name": "iPhone 17 Pro", "state": "Booted",
                 "isAvailable": True}]}},
            "idb_describe_all": str(IOS_FIXTURE),
            "idb_spawn_companion": {"path": str(self.companion),
                                    "pid_file": str(self.pid_file)}}), encoding="utf-8")
        self.set_env(AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_IDB_STATE_FILE=str(self.registry))
        self.env = dict(os.environ)
        self.env["PATH"] = os.pathsep.join([str(self.bin), "/usr/bin", "/bin"])

    def ios(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB),
             "--udid", self.udid, *argv],
            capture_output=True, text=True, env=self.env, timeout=120, cwd=self.root,
            stdin=subprocess.DEVNULL, check=False)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.stdout.strip() else completed.stderr
        return completed.returncode, json.loads(stream)

    def test_session_stop_stops_the_companion_and_keeps_the_decoy(self) -> None:
        code, started = self.ios("session", "start", "--app-id", BUNDLE)
        self.assertEqual(code, 0, started)
        code, tree = self.ios("ui", "tree")
        self.assertEqual(code, 0, tree)
        spawned = int(self.pid_file.read_text(encoding="utf-8"))
        rows = [e for e in processes.entries() if e["kind"] == "idb_companion"]
        self.assertEqual([(row["pid"], row["owner"]) for row in rows],
                         [(spawned, started["session"]["session_id"])])
        code, stopped = self.ios("session", "stop")
        self.assertEqual(code, 0, stopped)
        (reaped,) = [item for item in stopped["teardown"]
                     if item["action"] == "session_processes"]
        self.assertEqual([(item["pid"], item["result"])
                          for item in reaped["detail"]["terminated"]],
                         [(spawned, "terminated")])
        self.assertNotIn("companion_left_running",
                         [w.get("code") for w in stopped.get("warnings", [])])
        self.assertTrue(_wait_until(lambda: not _alive(spawned)))
        self.assert_decoy_alive()


# --- the live script's report (CASE-004) ---------------------------------------------------


def _load_live():
    spec = importlib.util.spec_from_file_location(
        "ios_companion_live", ROOT / "tests/live/ios_companion_live.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


live = _load_live()
# above any macOS pid: `ps` and `kill(pid, 0)` find nothing, so nothing real is signalled
COMPANION_PID = 4_190_001
OTHER_PID = 4_190_002
OTHER_UDID = "FAKE-0THER000-0000-0000-0000-000000000000"
SESSION_ID = "live-fake-session"
# what the steps before the session record: they alone must never make a pass
BEFORE_SESSION = ("host_load_below_limit", "no_bench_companion_before", "decoy_names_both",
                  "decoy_not_named_idb_companion", "decoy_stopped_by_script", "state_restored",
                  "other_companions_unchanged", "no_bench_companion_at_end")


def _raise(exc: BaseException):
    def action() -> None:
        raise exc
    return action


class _Bench:
    """The simulator, the machine's idb companions and the Autonom CLI, faked
    for one in-process run of the live script. The decoy and `ps` are real.
    `hooks` runs an action at a named point ("companions", "bootstatus",
    "decoy started", "session start", "ui tree", "session stop").

    `ui tree` starts a companion when none serves the UDID (pid
    `companion_pid`, registered to the session), takes `tree_seconds` on the
    retry's fake clock (`clock`, `pause`) and fails with `tree_error_code`
    (`backend_failed` unless set; exit 2) for its first `tree_failures`
    calls (-1: every call); its first `tree_hangs` calls (-1: every call)
    hang instead: `hang_seconds` pass and the call raises
    `subprocess.TimeoutExpired`, as `run` does at CLI_TIMEOUT.
    `stop_leaves` makes `session stop` leave the companions running."""

    def __init__(self, udid: str, real_run) -> None:
        self.udid = udid
        self.real_run = real_run
        self.state = "Booted"
        self.running = {OTHER_PID: f"/opt/homebrew/bin/idb_companion --udid {OTHER_UDID}"}
        self.hooks: dict[str, object] = {}
        self.cli_calls: list[str] = []
        self.pgrep_calls = 0
        self.shutdowns = 0
        self.simctl: list[list[str]] = []  # every `xcrun simctl` boot or shutdown
        self.companion_pid = COMPANION_PID
        self.started: list[int] = []  # every companion `ui tree` started, in order
        self.tree_failures = 0
        self.tree_error_code = "backend_failed"
        self.tree_hangs = 0
        self.hang_seconds = float(live.CLI_TIMEOUT)
        self.tree_calls = 0
        self.tree_seconds = 1.0
        self.stop_leaves = False
        self.now = 1000.0
        self.pauses: list[float] = []

    def clock(self) -> float:
        return self.now

    def pause(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds

    def hook(self, name: str) -> None:
        action = self.hooks.get(name)
        if action is not None:
            action()

    def run(self, argv, *, timeout, env=None, check=True):
        if argv[0] == "ps":
            return self.real_run(argv, timeout=timeout, env=env, check=check)
        if argv[0] == "xcodebuild":
            return subprocess.CompletedProcess(argv, 0, "Xcode 26.0\nBuild version 17A1\n", "")
        if argv[:3] == ["xcrun", "simctl", "list"]:
            listing = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
                {"udid": self.udid, "state": self.state}]}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(listing), "")
        if argv[:3] in (["xcrun", "simctl", "bootstatus"], ["xcrun", "simctl", "shutdown"]):
            self.simctl.append(list(argv))
        if argv[:3] == ["xcrun", "simctl", "bootstatus"]:
            self.hook("bootstatus")
            self.state = "Booted"
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["xcrun", "simctl", "shutdown"]:
            self.shutdowns += 1
            self.state = "Shutdown"
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command {argv}")

    def companions(self) -> dict[int, str]:
        self.pgrep_calls += 1
        self.hook("companions")
        return dict(self.running)

    def start_companion(self, autonom) -> None:
        """What idb does on first use: start the companion, registered to the
        session (one more registry row per companion started)."""
        pid = self.companion_pid
        self.started.append(pid)
        self.running[pid] = (f"/opt/homebrew/bin/idb_companion --udid {self.udid}"
                             " --only simulator")
        registry = Path(autonom.env["AUTONOM_HOME"]) / "processes" / "processes.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"kind": "idb_companion", "pid": started, "udid": self.udid,
                 "owner": SESSION_ID, "signature": [processes.COMPANION_MARKER, self.udid],
                 "spawned_by": "idb"} for started in self.started]
        registry.write_text(json.dumps({"processes": rows}), encoding="utf-8")

    def cli(self, autonom, *argv: str, timeout: float = 0) -> dict:
        name = " ".join(argv[:2])
        self.cli_calls.append(name)
        self.hook(name)
        autonom.last_exit = 0
        if name == "session start":
            return {"ok": True, "session": {"session_id": SESSION_ID}}
        if name == "ui tree":
            self.tree_calls += 1
            hangs = self.tree_hangs < 0 or self.tree_calls <= self.tree_hangs
            self.now += self.hang_seconds if hangs else self.tree_seconds
            if not any(self.udid in command for command in self.running.values()):
                self.start_companion(autonom)
            if hangs:
                autonom.last_exit = None
                raise subprocess.TimeoutExpired(["autonom", *argv], timeout)
            if self.tree_failures < 0 or self.tree_calls <= self.tree_failures:
                autonom.last_exit = 2
                return {"ok": False, "error_code": self.tree_error_code,
                        "error": "No translation object returned for simulator"}
            return {"ok": True, "ui_backend": "idb", "nodes": [{"role": "Application"}]}
        if name == "session stop":
            terminated = []
            for pid in self.started:
                if not self.stop_leaves and self.running.pop(pid, None) is not None:
                    terminated.append({"pid": pid, "result": "terminated"})
            return {"ok": True, "teardown": [{"action": "session_processes",
                                              "detail": {"terminated": terminated}}]}
        raise AssertionError(f"unexpected CLI call {argv}")


class LiveReportTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.evidence = Path(tmp.name) / "evidence"
        self.udid = _udid()
        self.bench = _Bench(self.udid, live.run)
        self.decoys: list[subprocess.Popen] = []
        self.sigint = signal.getsignal(signal.SIGINT)
        real_popen = subprocess.Popen
        bench = self.bench

        def popen(argv, *args, **kwargs):
            child = real_popen(argv, *args, **kwargs)
            if "idb_companion" in argv:  # the decoy, the only process the script starts
                self.decoys.append(child)
                self.addCleanup(_Processes._stop, child)  # noqa: SLF001
                bench.hook("decoy started")
            return child

        def cli(autonom, *argv, timeout=live.CLI_TIMEOUT):
            return bench.cli(autonom, *argv, timeout=timeout)

        # a regression that kills this process leaks its decoy for a minute, not ten
        for target, name, new in ((live, "DECOY_SLEEP", "import time; time.sleep(60)"),
                                  (live, "run", bench.run),
                                  (live, "companions", bench.companions),
                                  (live, "clock", bench.clock),
                                  (live, "pause", bench.pause),
                                  (live, "BOOT_SETTLE_S", 0),
                                  (live.Autonom, "__call__", cli),
                                  (live.subprocess, "Popen", popen)):
            patcher = mock.patch.object(target, name, new)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_live(self, raises: type[BaseException] | None = None,
                 extra: tuple[str, ...] = ()) -> tuple[int, dict]:
        shown = io.StringIO()
        argv = ["--udid", self.udid, *extra, "--evidence-dir", str(self.evidence),
                "--idb", "/fake/idb", "--max-load", "1000000"]
        with contextlib.redirect_stdout(shown):
            if raises is None:
                code = live.main(argv)
            else:
                with self.assertRaises(raises):
                    live.main(argv)
                code = -1
        report = json.loads((self.evidence / "ios-companion.json").read_text(encoding="utf-8"))
        summary = json.loads(shown.getvalue())
        # however the run ended: the cleanup ran to its end, the printed
        # summary agrees with the file, the decoy is gone, the handlers are
        # back and another simulator's companion was left alone
        self.assertEqual(report["cleanup"], "done")
        self.assertEqual((summary["ok"], summary["error"], summary["missing_checks"]),
                         (report["ok"], report.get("error"), report["missing_checks"]))
        self.assertEqual(len(self.decoys), 1)
        self.assertIsNotNone(self.decoys[0].poll(), "the decoy was left running")
        self.assertTrue(report["checks"]["decoy_stopped_by_script"])
        self.assertEqual(report["steps"]["cleanup"]["decoy_pids"], [self.decoys[0].pid])
        self.assertEqual(signal.getsignal(signal.SIGINT), self.sigint)
        self.assertIn(OTHER_PID, self.bench.running)
        return code, report

    def assert_no_pass(self, report: dict, *never_ran: str) -> None:
        self.assertFalse(report["ok"])
        self.assertFalse(report["steps_completed"])
        self.assertIn("error", report)
        for name in never_ran:
            self.assertIn(name, report["missing_checks"])
            self.assertNotIn(name, report["checks"])

    def test_a_full_run_passes_with_every_check(self) -> None:
        code, report = self.run_live()
        self.assertEqual(code, 0, report)
        self.assertTrue(report["ok"])
        self.assertTrue(report["steps_completed"])
        self.assertNotIn("error", report)
        self.assertEqual(report["missing_checks"], [])
        # every check the run records is a required one, and each passed
        self.assertEqual(sorted(report["checks"]), sorted(live.REQUIRED_CHECKS))
        self.assertTrue(all(value is True for value in report["checks"].values()))
        self.assertEqual(self.bench.cli_calls, ["session start", "ui tree", "session stop"])
        self.assertEqual(self.bench.shutdowns, 0)  # it was Booted: left as it was

    def test_the_approved_command_line_parses(self) -> None:  # CASE-004
        approved = ["--udid", "A7F283F3-9CE1-4C57-9EE4-9BE0E363D381", "--boot",
                    "--evidence-dir", "/tmp/x"]
        args = live.build_parser().parse_args(approved)
        self.assertEqual((args.udid, args.boot, args.evidence_dir),
                         ("A7F283F3-9CE1-4C57-9EE4-9BE0E363D381", True, Path("/tmp/x")))
        self.assertFalse(live.build_parser().parse_args(approved[:2] + approved[3:]).boot)

    def test_boot_boots_and_shuts_down_only_the_given_simulator(self) -> None:
        self.bench.state = "Shutdown"
        with mock.patch.object(live, "BOOT_SETTLE_S", 0):
            code, report = self.run_live(extra=("--boot",))
        self.assertEqual(code, 0, report)
        self.assertTrue(report["ok"])
        self.assertEqual(self.bench.simctl, [["xcrun", "simctl", "bootstatus", self.udid, "-b"],
                                             ["xcrun", "simctl", "shutdown", self.udid]])
        self.assertEqual(report["steps"]["boot"], {"booted_here": True, "boot_requested": True})
        self.assertIs(report["boot_requested"], True)
        self.assertEqual(self.bench.state, "Shutdown", "left as it was found")

    def test_boot_leaves_a_booted_simulator_as_it_was(self) -> None:
        code, report = self.run_live(extra=("--boot",))
        self.assertEqual(code, 0, report)
        self.assertEqual(self.bench.simctl, [], "neither booted nor shut down")
        self.assertEqual(report["steps"]["boot"], {"booted_here": False, "boot_requested": True})
        self.assertEqual(self.bench.state, "Booted")

    def test_ctrl_c_during_the_boot_wait_is_no_pass(self) -> None:
        """Review finding: the eight checks recorded before the session all
        pass, yet the run never looked at a companion."""
        self.bench.state = "Shutdown"
        self.bench.hooks["bootstatus"] = _raise(KeyboardInterrupt())
        code, report = self.run_live()
        self.assertEqual(code, 130)
        self.assert_no_pass(report, "ui_tree_ok", "one_companion_started",
                            "companion_registered_to_session",
                            "session_stop_terminated_the_companion", "decoy_alive_after_stop")
        self.assertIn("KeyboardInterrupt", report["error"])
        self.assertEqual(sorted(report["checks"]), sorted(BEFORE_SESSION))
        self.assertTrue(all(report["checks"].values()), report["checks"])
        self.assertEqual(self.bench.cli_calls, [])
        self.assertEqual((self.bench.shutdowns, self.bench.state), (1, "Shutdown"))

    def test_ctrl_c_during_ui_tree_is_no_pass(self) -> None:
        self.bench.hooks["ui tree"] = _raise(KeyboardInterrupt())
        code, report = self.run_live()
        self.assertEqual(code, 130)
        self.assert_no_pass(report, "ui_tree_ok", "one_companion_started",
                            "companion_registered_to_session",
                            "session_stop_terminated_the_companion", "decoy_alive_after_stop")
        self.assertTrue(all(report["checks"].values()), report["checks"])
        # the cleanup stopped the session the interrupted run had started
        self.assertEqual(self.bench.cli_calls, ["session start", "ui tree", "session stop"])
        self.assertTrue(report["steps"]["cleanup"]["session_stop_ok"])

    def test_an_unexpected_exception_is_no_pass(self) -> None:
        self.bench.hooks["ui tree"] = _raise(AttributeError("'NoneType' has no attribute 'get'"))
        code, report = self.run_live()
        self.assertEqual(code, 1)
        self.assert_no_pass(report, "ui_tree_ok", "one_companion_started")
        self.assertTrue(report["error"].startswith("AttributeError: "), report["error"])
        self.assertIn("Traceback", report["traceback"])

    def test_a_real_sigint_stops_the_steps_and_a_second_one_not_the_cleanup(self) -> None:
        def ctrl_c() -> None:
            os.kill(os.getpid(), signal.SIGINT)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                time.sleep(0.01)  # the handler raises in here
            raise AssertionError("SIGINT did not stop the step")

        def second_ctrl_c() -> None:  # during the cleanup's `session stop`
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.2)

        self.bench.hooks["ui tree"] = ctrl_c
        self.bench.hooks["session stop"] = second_ctrl_c
        code, report = self.run_live()
        self.assertEqual(code, 128 + signal.SIGINT)
        self.assert_no_pass(report, "ui_tree_ok", "one_companion_started")
        self.assertEqual(report["error"], "Terminated: interrupted by SIGINT")
        self.assertEqual(self.bench.cli_calls, ["session start", "ui tree", "session stop"])
        # every cleanup step after the second Ctrl-C still ran, none failed
        self.assertNotIn("errors", report["steps"]["cleanup"])
        self.assertTrue(report["checks"]["state_restored"])
        self.assertTrue(report["checks"]["other_companions_unchanged"])

    def test_a_system_exit_in_a_step_goes_on_after_the_cleanup(self) -> None:
        self.bench.hooks["ui tree"] = _raise(SystemExit(5))
        _code, report = self.run_live(raises=SystemExit)
        self.assert_no_pass(report, "ui_tree_ok", "one_companion_started")
        self.assertEqual(report["error"], "SystemExit: 5")
        self.assertEqual(self.bench.cli_calls, ["session start", "ui tree", "session stop"])

    def test_the_decoy_is_stopped_when_its_own_step_fails(self) -> None:
        """Review finding: the second `pgrep` (inside the decoy step) fails
        after the decoy was started."""
        def second_pgrep_fails() -> None:
            if self.bench.pgrep_calls == 2:
                raise live.StepFailed("pgrep failed (3): ")
        self.bench.hooks["companions"] = second_pgrep_fails
        code, report = self.run_live()
        self.assertEqual(code, 1)
        self.assert_no_pass(report, "decoy_not_named_idb_companion", "ui_tree_ok")
        self.assertIn("pgrep failed (3)", report["error"])
        self.assertEqual(report["steps"]["decoy"]["pid"], self.decoys[0].pid)
        self.assertFalse(_alive(self.decoys[0].pid))
        self.assertEqual(self.bench.cli_calls, [])

    def test_a_signal_while_the_decoy_starts_waits_until_its_handle_is_kept(self) -> None:
        self.bench.hooks["decoy started"] = lambda: os.kill(os.getpid(), signal.SIGINT)
        code, report = self.run_live()
        self.assertEqual(code, 128 + signal.SIGINT)
        self.assert_no_pass(report, "decoy_names_both", "ui_tree_ok")
        self.assertEqual(report["error"], "Terminated: interrupted by SIGINT")
        self.assertFalse(_alive(self.decoys[0].pid))


    # -- `ui tree` after a boot this run made: retried, every attempt reported

    def attempts(self, report: dict) -> list[tuple]:
        return [(item["seconds"], item["exit"], item["error_code"], item["bench_companions"])
                for item in report["steps"]["ui_tree_attempts"]]

    def test_after_its_boot_ui_tree_is_tried_every_2_s_until_it_answers(self) -> None:
        self.bench.state = "Shutdown"
        self.bench.tree_failures = 2  # CASE-004 run 1: backend_failed right after the boot
        code, report = self.run_live(extra=("--boot",))
        self.assertEqual(code, 0, report)
        self.assertTrue(report["ok"])
        self.assertEqual(self.attempts(report), [
            (1.0, 2, "backend_failed", [COMPANION_PID]),
            (4.0, 2, "backend_failed", [COMPANION_PID]),
            (7.0, 0, None, [COMPANION_PID])])
        self.assertEqual(self.bench.pauses, [2, 2])
        self.assertEqual(report["steps"]["ui_tree"]["attempts"], 3)
        # CO-02 holds across the attempts: one companion, registered, stopped
        self.assertEqual(self.bench.started, [COMPANION_PID])
        self.assertEqual(sorted(report["checks"]), sorted(live.REQUIRED_CHECKS))
        self.assertTrue(all(value is True for value in report["checks"].values()))
        self.assertEqual(self.bench.cli_calls,
                         ["session start", "ui tree", "ui tree", "ui tree", "session stop"])
        self.assertEqual(self.bench.shutdowns, 1)

    def assert_judged(self, report: dict) -> None:
        """The run reached its last step and recorded every required check:
        the read failed, and the rest of the case was still judged."""
        self.assertTrue(report["steps_completed"])
        self.assertNotIn("error", report)
        self.assertNotIn("traceback", report)
        self.assertEqual(report["missing_checks"], [])
        self.assertEqual(sorted(report["checks"]), sorted(live.REQUIRED_CHECKS))
        self.assertIs(report["checks"]["ui_tree_ok"], False)
        self.assertFalse(report["ok"])
        for name in ("registry", "session_stop"):
            self.assertIn(name, report["steps"])

    def others_passed(self, report: dict) -> bool:
        return all(value is True for name, value in report["checks"].items()
                   if name != "ui_tree_ok")

    def test_a_simulator_that_never_answers_after_its_boot_is_an_environment_fault(self) -> None:
        self.bench.state = "Shutdown"
        self.bench.tree_failures = -1
        code, report = self.run_live(extra=("--boot",))
        self.assertEqual(code, live.EXIT_ENVIRONMENT)
        attempts = report["steps"]["ui_tree_attempts"]
        # every 2 s (each attempt takes 1 s here), none starting after 60 s
        self.assertEqual(len(attempts), 21)
        self.assertEqual([item["seconds"] for item in attempts],
                         [float(3 * number + 1) for number in range(21)])
        self.assertTrue(all(item["seconds"] - 1.0 <= live.UI_TREE_RETRY_S for item in attempts))
        self.assertEqual(self.bench.pauses, [2] * 20)
        self.assertTrue(all((item["exit"], item["error_code"]) == (2, "backend_failed")
                            for item in attempts))
        self.assertIn("21 attempts", report["environment_fault"])
        # fix round 5: the read is recorded from the last attempt, and the
        # rest of the case ran in the steps, so CO-02 was judged and passed
        self.assert_judged(report)
        self.assertEqual({key: report["steps"]["ui_tree"][key]
                          for key in ("ok", "error_code", "attempts", "timed_out")},
                         {"ok": False, "error_code": "backend_failed", "attempts": 21,
                          "timed_out": False})
        self.assertIn("21 attempts", report["steps"]["ui_tree"]["not_ready"])
        self.assertTrue(self.others_passed(report), report["checks"])
        self.assertEqual(report["steps"]["session_stop"]["terminated"],
                         [[COMPANION_PID, "terminated"]])
        self.assertEqual([row["pid"] for row in report["steps"]["registry"]["rows"]],
                         [COMPANION_PID])
        self.assertNotIn(COMPANION_PID, self.bench.running)
        # the steps stopped the session: the cleanup did not stop it again
        self.assertEqual(self.bench.cli_calls, ["session start"] + ["ui tree"] * 21
                         + ["session stop"])
        self.assertNotIn("session_stop_ok", report["steps"]["cleanup"])
        self.assertEqual(report["steps"]["cleanup"]["leftover_companions_stopped"], [])
        self.assertEqual((self.bench.shutdowns, self.bench.state), (1, "Shutdown"))

    def test_after_its_boot_only_backend_failed_is_retried(self) -> None:
        self.bench.state = "Shutdown"
        self.bench.tree_failures = -1
        self.bench.tree_error_code = "invalid_value"
        code, report = self.run_live(extra=("--boot",))
        self.assertEqual(code, 1)
        self.assertNotIn("environment_fault", report)
        self.assertEqual(self.attempts(report), [(1.0, 2, "invalid_value", [COMPANION_PID])])
        self.assertEqual(self.bench.pauses, [])
        # the full report: the rest of the case ran and passed
        self.assert_judged(report)
        self.assertNotIn("not_ready", report["steps"]["ui_tree"])
        self.assertTrue(self.others_passed(report), report["checks"])
        self.assertEqual(self.bench.cli_calls, ["session start", "ui tree", "session stop"])
        self.assertEqual(self.bench.shutdowns, 1)

    def test_a_companion_left_after_the_retry_is_no_environment_fault(self) -> None:
        self.bench.state = "Shutdown"
        self.bench.tree_failures = -1
        self.bench.stop_leaves = True  # Autonom's session stop leaves the companion
        with mock.patch.object(live, "STOP_WAIT_S", 0):
            code, report = self.run_live(extra=("--boot",))
        self.assertEqual(code, 1)
        self.assertNotIn("environment_fault", report)
        # CO-02 was judged after the retry ran out, and failed
        self.assert_judged(report)
        self.assertFalse(report["checks"]["session_stop_terminated_the_companion"])
        self.assertFalse(report["checks"]["no_bench_companion_after_stop"])
        self.assertFalse(report["checks"]["no_bench_companion_at_end"])

    def test_a_ui_tree_that_hangs_after_its_boot_is_retried_within_the_window(self) -> None:
        self.bench.state = "Shutdown"
        self.bench.tree_hangs = 1
        self.bench.hang_seconds = 20.0
        with mock.patch.object(live, "CLI_TIMEOUT", 20):
            code, report = self.run_live(extra=("--boot",))
        self.assertEqual(code, 0, report)
        self.assertTrue(report["ok"])
        attempts = report["steps"]["ui_tree_attempts"]
        self.assertEqual([(item["seconds"], item["exit"], item.get("timed_out"))
                          for item in attempts], [(20.0, None, True), (23.0, 0, None)])
        self.assertEqual(self.bench.pauses, [2])
        self.assertIn({"argv": ["ui", "tree"], "exit": None, "seconds": 20.0, "ok": False,
                       "error_code": None, "timed_out": True}, report["calls"])
        self.assertEqual(self.bench.started, [COMPANION_PID])
        self.assertTrue(all(value is True for value in report["checks"].values()))

    def test_a_ui_tree_that_hangs_past_the_window_is_an_environment_fault(self) -> None:
        self.bench.state = "Shutdown"
        self.bench.tree_hangs = -1  # every call hangs to CLI_TIMEOUT (180 s)
        code, report = self.run_live(extra=("--boot",))
        self.assertEqual(code, live.EXIT_ENVIRONMENT)
        self.assertEqual([(item["seconds"], item["exit"], item.get("timed_out"))
                          for item in report["steps"]["ui_tree_attempts"]],
                         [(float(live.CLI_TIMEOUT), None, True)])
        self.assertEqual(self.bench.pauses, [], "the window was over")
        self.assertIn("timed out", report["environment_fault"])
        self.assert_judged(report)
        self.assertIs(report["steps"]["ui_tree"]["timed_out"], True)
        self.assertTrue(self.others_passed(report), report["checks"])

    def test_a_ui_tree_that_hangs_on_an_already_booted_simulator_fails_once(self) -> None:
        self.bench.tree_hangs = -1
        code, report = self.run_live(extra=("--boot",))
        self.assertEqual(code, 1)
        self.assertNotIn("environment_fault", report)
        self.assertEqual([(item["seconds"], item.get("timed_out"))
                          for item in report["steps"]["ui_tree_attempts"]],
                         [(float(live.CLI_TIMEOUT), True)])
        self.assert_judged(report)
        self.assertTrue(self.others_passed(report), report["checks"])
        self.assertEqual(self.bench.cli_calls, ["session start", "ui tree", "session stop"])

    def test_a_second_companion_across_the_attempts_fails_the_check(self) -> None:
        self.bench.state = "Shutdown"
        self.bench.tree_failures = 1

        def first_companion_dies() -> None:
            if self.bench.tree_calls == 1:  # before the second attempt
                self.bench.running.pop(COMPANION_PID, None)
                self.bench.companion_pid = COMPANION_PID + 10
        self.bench.hooks["ui tree"] = first_companion_dies
        code, report = self.run_live(extra=("--boot",))
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertNotIn("environment_fault", report)
        self.assertTrue(report["checks"]["ui_tree_ok"])
        self.assertFalse(report["checks"]["one_companion_started"])
        self.assertEqual(report["steps"]["registry"]["bench_companions_seen"],
                         [COMPANION_PID, COMPANION_PID + 10])

    def test_an_already_booted_simulator_gets_one_ui_tree_attempt(self) -> None:
        self.bench.tree_failures = -1
        code, report = self.run_live(extra=("--boot",))
        self.assertEqual(code, 1)
        self.assertNotIn("environment_fault", report)
        self.assertEqual(self.attempts(report), [(1.0, 2, "backend_failed", [COMPANION_PID])])
        self.assertEqual(self.bench.pauses, [])
        self.assertFalse(report["checks"]["ui_tree_ok"])
        self.assertEqual(self.bench.cli_calls, ["session start", "ui tree", "session stop"])
        self.assertEqual(self.bench.shutdowns, 0)


if __name__ == "__main__":
    unittest.main()
