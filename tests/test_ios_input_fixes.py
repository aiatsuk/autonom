"""iOS input quick fixes (AL-01..AL-04): one accessibility read per tap or
swipe, no process scan while idb already has a live companion, AXe taps held
as a touch, and recovery from a dead companion inside the failing call.

The oracle is what was dispatched: the fake idb and fake AXe log every argv,
so each case counts `ui describe-all`, `ui tap`, `list-targets` and the AXe
argv, and the in-process cases count `processes._ps_output` calls. The CLI
subprocesses get a PATH holding only a ``python3`` link and the system
directories, so a real idb or AXe on the machine cannot be reached.
"""
from __future__ import annotations

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
FAKE_AXE = ROOT / "tests/fakes/fake_axe.py"
IOS_FIXTURE = ROOT / "tests/fixtures/idb_describe_all_sample.json"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"
BOOTED = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
    {"udid": UDID, "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}]}}
# What a killed companion leaves behind, in the two shapes fb-idb prints it.
ENOENT = ("Failed to connect to companion at address DomainSocketAddress(path='/tmp/idb/"
          f"{UDID}_companion.sock'): [Errno 2] No such file or directory")
ENOENT_TRACEBACK = ("Exception thrown in main\nTraceback (most recent call last):\n"
                    "idb.common.types.IdbConnectionException: No such file or directory")
REFUSED = ("Failed to connect to companion at address DomainSocketAddress(...): "
           "[Errno 61] Connection refused")
RESET = "Connection reset by peer"
STREAM = "grpclib.exceptions.StreamTerminatedError: Connection lost"
UNAVAILABLE = "GRPCError: StatusCode.UNAVAILABLE"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, ios_geometry, ios_idb, ios_xctest, processes  # noqa: E402
from autonom_lib import session, ui, ui_ios  # noqa: E402
from autonom_lib.platform import IOS, Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


def _dead_pid() -> int:
    """A pid that belonged to a process which has already been reaped."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _verb(argv: list[str]) -> list[str]:
    """The verb of a logged idb argv, with top-level options stripped."""
    rest = list(argv)
    while rest and rest[0] in ("--companion", "--companion-path", "--log", "--compression"):
        rest = rest[2:]
    return rest[:2]


def _scaled_tree(directory: Path, name: str, *, root: tuple[int, int] | None = None,
                 contents: int = 1) -> Path:
    """The sample tree with its Application root resized to ``root`` and/or
    every other frame multiplied by ``contents``."""
    payload = json.loads(IOS_FIXTURE.read_text(encoding="utf-8"))
    for element in payload:
        frame = element["frame"]
        if element.get("type") == "Application":
            if root:
                frame["width"], frame["height"] = root
            continue
        for key in ("x", "y", "width", "height"):
            frame[key] *= contents
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class CliBase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.registry = self.root / "idb-state.json"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.set_env(
            AUTONOM_FAKE_STATE=str(self.state), AUTONOM_FAKE_LOG=str(self.log),
            AUTONOM_HOME=str(self.root / "home"),
            AUTONOM_ADB=None, AUTONOM_SIMCTL=None, AUTONOM_IDB=None,
            AUTONOM_AXE=None, AUTONOM_IOS_HID=None, AUTONOM_IDB_COMPANION=None,
            AUTONOM_IDB_STATE_FILE=str(self.registry), AUTONOM_UI_BACKEND=None,
        )
        self.env = dict(os.environ)
        self.env["PATH"] = os.pathsep.join([str(self.bin), "/usr/bin", "/bin"])
        self.set_state()

    def set_state(self, **kwargs) -> None:
        kwargs.setdefault("simctl_devices", BOOTED)
        kwargs.setdefault("idb_describe_all", str(IOS_FIXTURE))
        self.state.write_text(json.dumps(kwargs), encoding="utf-8")

    def write_registry(self, entries: list[dict]) -> None:
        self.registry.write_text(json.dumps(entries), encoding="utf-8")

    def ios(self, *argv: str, globals_: tuple[str, ...] = ()) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB),
             "--udid", UDID, *globals_, *argv],
            capture_output=True, text=True, env=self.env, timeout=120, cwd=self.tmp.name,
        )
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.returncode in (0, 1) else completed.stderr
        return completed.returncode, json.loads(stream)

    def forget_calls(self) -> None:
        self.log.write_text("", encoding="utf-8")

    def argv_log(self, tool: str) -> list[list[str]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [row["argv"] for row in rows if row["tool"] == tool]

    def count(self, *verb: str) -> int:
        return [_verb(argv)[:len(verb)] for argv in self.argv_log("idb")].count(list(verb))


# --- AL-01 -------------------------------------------------------------------------------------


class OneReadPerTapTests(CliBase):
    """AL-01 — the guard reuses the read the command already made."""

    def test_selector_tap_reads_once(self) -> None:  # AL-01-S01
        code, payload = self.ios("ui", "tap", "--desc", "General", "--mode", "exact")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.count("ui", "describe-all"), 1)
        self.assertEqual(self.count("ui", "tap"), 1)
        # the geometry the guard used still reaches the result, as before
        self.assertEqual(payload["geometry"]["width"], 402)

    def test_selector_long_press_reads_once(self) -> None:
        code, payload = self.ios("ui", "tap", "--desc", "General", "--mode", "exact",
                                 "--duration", "800")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.count("ui", "describe-all"), 1)
        [tap] = [argv for argv in self.argv_log("idb") if _verb(argv) == ["ui", "tap"]]
        self.assertEqual(tap[:6], ["ui", "tap", "201", "406", "--duration", "0.8"])

    def test_a_point_outside_the_screen_is_still_refused(self) -> None:  # AL-01-S01
        tripled = _scaled_tree(self.root, "tripled.json", contents=3)
        self.set_state(idb_describe_all=str(tripled))
        code, payload = self.ios("ui", "tap", "--desc", "General", "--mode", "exact")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.COORDINATE_SPACE_MISMATCH)
        self.assertEqual(payload["screen"], [402, 874])
        self.assertEqual(self.count("ui", "tap"), 0)
        # the read, plus the one fresh lookup a refusal is decided on (INV-2)
        self.assertEqual(self.count("ui", "describe-all"), 2)

    def test_coordinate_tap_after_a_read_reads_nothing(self) -> None:  # AL-01-S02
        self.assertEqual(self.ios("session", "start")[0], 0)
        self.assertEqual(self.ios("ui", "tree")[0], 0)
        self.forget_calls()
        code, payload = self.ios("ui", "tap", "--x", "100", "--y", "200")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.count("ui", "describe-all"), 0)
        self.assertEqual(self.count("ui", "tap"), 1)

    def test_swipe_after_a_read_reads_nothing(self) -> None:
        self.assertEqual(self.ios("session", "start")[0], 0)
        self.assertEqual(self.ios("ui", "tree")[0], 0)
        self.forget_calls()
        code, payload = self.ios("ui", "swipe", "--from", "100,600", "--to", "100,200",
                                 "--duration", "0.3")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.count("ui", "describe-all"), 0)
        self.assertEqual(self.count("ui", "swipe"), 1)

    def test_stale_recorded_geometry_is_checked_fresh_once(self) -> None:  # AL-01-S03
        small = _scaled_tree(self.root, "small.json", root=(100, 200))
        self.set_state(idb_describe_all=str(small))
        self.assertEqual(self.ios("session", "start")[0], 0)
        self.assertEqual(self.ios("ui", "tree")[0], 0)
        self.set_state()  # the real screen is 402x874
        self.forget_calls()
        code, payload = self.ios("ui", "tap", "--x", "300", "--y", "500")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.count("ui", "describe-all"), 1, "one fresh lookup")
        self.assertEqual(self.count("ui", "tap"), 1, "sent, not refused")

    def test_stale_geometry_still_refuses_after_the_fresh_lookup(self) -> None:
        small = _scaled_tree(self.root, "small.json", root=(100, 200))
        self.set_state(idb_describe_all=str(small))
        self.assertEqual(self.ios("session", "start")[0], 0)
        self.assertEqual(self.ios("ui", "tree")[0], 0)
        self.set_state()
        self.forget_calls()
        code, payload = self.ios("ui", "tap", "--x", "4000", "--y", "4000")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.COORDINATE_SPACE_MISMATCH)
        self.assertEqual(payload["screen"], [402, 874], "decided on the fresh size")
        self.assertEqual(self.count("ui", "describe-all"), 1)
        self.assertEqual(self.count("ui", "tap"), 0)

    def test_swipe_with_one_end_outside_stale_geometry_looks_up_once(self) -> None:
        small = _scaled_tree(self.root, "small.json", root=(100, 200))
        self.set_state(idb_describe_all=str(small))
        self.assertEqual(self.ios("session", "start")[0], 0)
        self.assertEqual(self.ios("ui", "tree")[0], 0)
        self.set_state()
        self.forget_calls()
        code, payload = self.ios("ui", "swipe", "--from", "50,100", "--to", "50,700",
                                 "--duration", "0.3")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.count("ui", "describe-all"), 1)
        self.assertEqual(self.count("ui", "swipe"), 1)

    def test_without_a_read_or_geometry_tap_and_swipe_look_up_once(self) -> None:
        code, payload = self.ios("ui", "tap", "--x", "100", "--y", "200")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.count("ui", "describe-all"), 1)
        self.forget_calls()
        code, payload = self.ios("ui", "swipe", "--from", "100,600", "--to", "100,200",
                                 "--duration", "0.3")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.count("ui", "describe-all"), 1, "once for both endpoints")
        self.assertEqual(self.count("ui", "swipe"), 1)


class GuardInProcessTests(EnvSandboxMixin, unittest.TestCase):
    """AL-01 — which size the guard takes, counted on mocked backends."""

    TARGET = Target(IOS, UDID, str(FAKE_SIMCTL), {"udid": UDID})

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.set_env(AUTONOM_HOME=tmp.name, AUTONOM_IOS_HID="idb", AUTONOM_AXE=None,
                     AUTONOM_UI_BACKEND=None, AUTONOM_IDB=str(FAKE_IDB),
                     AUTONOM_IDB_COMPANION=None, AUTONOM_FAKE_STATE=None,
                     AUTONOM_FAKE_LOG=None)
        token = session.select(None)
        self.addCleanup(session._SELECTED.reset, token)  # noqa: SLF001
        self.reset()
        self.addCleanup(self.reset)
        self.payload = IOS_FIXTURE.read_text(encoding="utf-8")

    @staticmethod
    def reset() -> None:
        ui_ios.set_preference(None)
        ui_ios.reset_hid_probe()
        if hasattr(ui._LAST_SNAPSHOT, "value"):  # noqa: SLF001
            del ui._LAST_SNAPSHOT.value  # noqa: SLF001

    def test_snapshot_then_tap_is_one_read(self) -> None:
        with mock.patch.object(ui_ios.ios_idb, "describe_all",
                               return_value=self.payload) as describe, \
                mock.patch.object(ui_ios.ios_idb, "tap") as tap:
            ui.snapshot(self.TARGET)
            ui.tap(self.TARGET, 201, 406)
            ui.long_press(self.TARGET, 201, 406, 800)
            ui.double_tap(self.TARGET, 201, 406)
        self.assertEqual(describe.call_count, 1)
        self.assertEqual(tap.call_count, 4)

    def test_another_targets_snapshot_is_not_used(self) -> None:
        other = Target(IOS, "OTHER-UDID", str(FAKE_SIMCTL), {"udid": "OTHER-UDID"})
        with mock.patch.object(ui_ios.ios_idb, "describe_all",
                               return_value=self.payload) as describe, \
                mock.patch.object(ui_ios.ios_idb, "tap"):
            ui.snapshot(other)
            ui_ios.set_preference(None)  # forget recorded geometry: only the snapshot is left
            ui.tap(self.TARGET, 201, 406)
        self.assertEqual(describe.call_count, 2, "the tap looked its own screen up")

    def test_explicit_screen_is_never_looked_up(self) -> None:
        with mock.patch.object(ui, "screen_size") as lookup, \
                mock.patch.object(ui_ios.ios_idb, "swipe") as swipe:
            ui.swipe(self.TARGET, 10, 20, 30, 40, 0.3, screen=(400, 800))
            with self.assertRaises(errors.AutonomError) as caught:
                ui.swipe(self.TARGET, 10, 20, 30, 4000, 0.3, screen=(400, 800))
        self.assertEqual(caught.exception.code, errors.COORDINATE_SPACE_MISMATCH)
        lookup.assert_not_called()
        self.assertEqual(swipe.call_count, 1)

    def test_a_refusal_on_a_known_size_is_decided_on_one_lookup(self) -> None:
        with mock.patch.object(ui_ios.ios_idb, "describe_all", return_value=self.payload), \
                mock.patch.object(ui_ios.ios_idb, "tap") as tap, \
                mock.patch.object(ui, "screen_size", return_value=(1000, 1000)) as lookup:
            ui.snapshot(self.TARGET)  # a 402x874 root
            ui.tap(self.TARGET, 900, 900)
        lookup.assert_called_once_with(self.TARGET)
        tap.assert_called_once()

    def test_unknown_fresh_size_sends_on_the_idb_route(self) -> None:
        # Refused on the known size, unknown on the fresh one: the idb route
        # sends unguarded, exactly as it does with no size at all (INV-2).
        with mock.patch.object(ui_ios.ios_idb, "describe_all", return_value=self.payload), \
                mock.patch.object(ui_ios.ios_idb, "tap") as tap, \
                mock.patch.object(ui, "screen_size", return_value=None):
            ui.snapshot(self.TARGET)
            ui.tap(self.TARGET, 900, 900)
        tap.assert_called_once()

    def test_xcuitest_route_keeps_refusing_without_geometry(self) -> None:
        zero = json.dumps([{"type": "Application",
                            "frame": {"x": 0, "y": 0, "width": 0, "height": 0}}])
        verbs = []

        def runner(target, verb, **values):
            verbs.append(verb)
            return {"screen": {}}

        # a snapshot with a usable root is held, but the runner's route ignores it
        ui._LAST_SNAPSHOT.value = (IOS, UDID, ui_ios.parse_all(self.payload))  # noqa: SLF001
        ui_ios.set_preference("xcuitest")
        with mock.patch.object(ios_xctest, "request", side_effect=runner), \
                mock.patch.object(ui_ios.ios_idb, "describe_all", return_value=zero), \
                mock.patch.object(ios_geometry, "measure", return_value=None), \
                mock.patch.object(ui_ios.ios_idb, "tap") as idb_tap:
            with self.assertRaises(errors.AutonomError) as caught:
                ui.tap(self.TARGET, 200, 278)
        self.assertEqual(caught.exception.code, errors.DISPLAY_GEOMETRY_UNAVAILABLE)
        self.assertEqual(verbs, ["geometry"], "no tap reached the runner")
        idb_tap.assert_not_called()

    def test_recorded_geometry_comes_from_the_session(self) -> None:
        session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID)
        record = session.load_current()
        record["ui_observation"] = {"ui_backend": "idb", "geometry": {
            "width": 402, "height": 874, "units": "points"}}
        session.save(record)
        self.assertEqual(ui_ios.recorded_screen_size(self.TARGET), (402, 874))
        for broken in ({"width": 0, "height": 874}, {"width": True, "height": 1},
                       {"width": "402", "height": "874"}, {"width": 0.5, "height": 874},
                       {"width": float("inf"), "height": 874},
                       {"width": 402, "height": float("nan")}, None):
            record["ui_observation"] = {"ui_backend": "idb", "geometry": broken}
            session.save(record)
            with self.subTest(geometry=broken):
                self.assertIsNone(ui_ios.recorded_screen_size(self.TARGET))
        with self.subTest("observation is not an object"):
            record["ui_observation"] = ["idb"]
            session.save(record)
            self.assertIsNone(ui_ios.recorded_screen_size(self.TARGET))

    def test_root_size_needs_a_depth_zero_app_with_area(self) -> None:
        nodes = [{"depth": 1, "role": "app", "bounds": [0, 0, 402, 874]},
                 {"depth": 0, "role": "button", "bounds": [0, 0, 402, 874]},
                 {"depth": 0, "role": "app", "bounds": [0, 0, 0, 0]}]
        self.assertIsNone(ui_ios.screen_size_from_nodes(nodes))
        nodes.append({"depth": 0, "role": "window", "bounds": [0, 0, 402, 874]})
        self.assertEqual(ui_ios.screen_size_from_nodes(nodes), (402, 874))


# --- AL-02 -------------------------------------------------------------------------------------


class NoProcessScanTests(EnvSandboxMixin, unittest.TestCase):
    """AL-02 — `ps` only runs when the call could start a companion."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.registry = self.root / "idb-state.json"
        self.state = self.root / "state.json"
        self.state.write_text(json.dumps({"idb_describe_all": str(IOS_FIXTURE)}),
                              encoding="utf-8")
        self.udid = f"FAKE-{uuid.uuid4().hex[:8].upper()}-0000-0000-0000-000000000000"
        self.set_env(AUTONOM_HOME=str(self.root / "home"), AUTONOM_IDB=str(FAKE_IDB),
                     AUTONOM_IDB_STATE_FILE=str(self.registry), AUTONOM_IDB_COMPANION=None,
                     AUTONOM_FAKE_STATE=str(self.state), AUTONOM_FAKE_LOG=None)
        token = session.select(None)
        self.addCleanup(session._SELECTED.reset, token)  # noqa: SLF001
        self.record = session.start_session(str(FAKE_SIMCTL), platform="ios",
                                            target_id=self.udid)

    def write_registry(self, entries: list[dict]) -> None:
        self.registry.write_text(json.dumps(entries), encoding="utf-8")

    def ps_calls(self) -> int:
        with mock.patch.object(processes, "_ps_output", wraps=processes._ps_output) as ps:  # noqa: SLF001
            ios_idb.run_idb(str(FAKE_IDB), ["ui", "describe-all", "--json"], udid=self.udid)
        return ps.call_count

    def test_live_registered_companion_means_no_ps(self) -> None:  # AL-02-S01
        self.write_registry([{"udid": self.udid, "pid": os.getpid(), "is_local": True,
                              "path": f"/tmp/idb/{self.udid}_companion.sock"}])
        self.assertEqual(self.ps_calls(), 0)

    def test_anything_less_than_a_live_local_entry_is_tracked(self) -> None:
        cases = {
            "no entry": [],
            "dead pid": [{"udid": self.udid, "pid": _dead_pid(), "is_local": True}],
            "remote entry": [{"udid": self.udid, "pid": os.getpid(), "is_local": False}],
            "another simulator": [{"udid": "OTHER", "pid": os.getpid(), "is_local": True}],
            "pid 0": [{"udid": self.udid, "pid": 0, "is_local": True}],
            "pid -1": [{"udid": self.udid, "pid": -1, "is_local": True}],
            "pid true": [{"udid": self.udid, "pid": True, "is_local": True}],
            "pid as text": [{"udid": self.udid, "pid": str(os.getpid()), "is_local": True}],
        }
        for name, entries in cases.items():
            with self.subTest(name):
                self.write_registry(entries)
                self.assertGreater(self.ps_calls(), 0)
        with self.subTest("unreadable registry"):
            self.registry.write_text("{not json", encoding="utf-8")
            self.assertGreater(self.ps_calls(), 0)

    def test_unknown_registry_is_tracked(self) -> None:
        # a pinned client without AUTONOM_IDB_STATE_FILE: its registry is unknown
        self.set_env(AUTONOM_IDB_STATE_FILE=None)
        self.assertGreater(self.ps_calls(), 0)

    def fake_companion(self, **state) -> Path:
        """A companion program the fake idb starts for an unlisted simulator;
        returns the file its pid is written to (stopped at cleanup)."""
        companion = self.root / "bin" / "idb_companion"
        companion.parent.mkdir(exist_ok=True)
        companion.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n",
                             encoding="utf-8")
        companion.chmod(0o755)
        pid_file = self.root / "companion.pid"
        self.state.write_text(json.dumps({
            "idb_describe_all": str(IOS_FIXTURE),
            "idb_spawn_companion": {"path": str(companion), "pid_file": str(pid_file)},
            **state}), encoding="utf-8")
        self.addCleanup(self._kill, pid_file)
        return pid_file

    def tracked_companions(self) -> list[tuple[int, str, str]]:
        rows = [entry for entry in processes.entries() if entry["kind"] == "idb_companion"]
        return [(row["pid"], row["owner"], row["udid"]) for row in rows]

    def test_a_spawned_companion_is_registered_and_then_not_rescanned(self) -> None:
        # AL-02-S02: no entry, the call spawns one — registered as before.
        pid_file = self.fake_companion()
        self.write_registry([])
        self.assertGreater(self.ps_calls(), 0)
        spawned = int(pid_file.read_text(encoding="utf-8"))
        self.assertEqual(self.tracked_companions(),
                         [(spawned, self.record["session_id"], self.udid)])
        # the registry now lists that live companion: the next call scans nothing
        self.assertEqual(self.ps_calls(), 0)

    def test_a_companion_spawned_by_the_recovery_retry_is_registered(self) -> None:
        # AL-04 with INV-5: a dead registration fails the call before any
        # connection; the prune drops it, the retry spawns a fresh companion,
        # and that companion is still registered to the session.
        pid_file = self.fake_companion(
            idb_fail_until_pruned={"ui describe-all": [1, ENOENT]})
        self.write_registry([{"udid": self.udid, "pid": _dead_pid(), "is_local": True}])
        completed = ios_idb.run_idb(str(FAKE_IDB), ["ui", "describe-all", "--json"],
                                    udid=self.udid)
        self.assertEqual(completed.returncode, 0)
        spawned = int(pid_file.read_text(encoding="utf-8"))
        self.assertEqual(self.tracked_companions(),
                         [(spawned, self.record["session_id"], self.udid)])

    @staticmethod
    def _kill(pid_file: Path) -> None:
        try:
            os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)
        except (OSError, ValueError):
            pass


# --- AL-03 -------------------------------------------------------------------------------------


class AxeHeldTapTests(CliBase):
    """AL-03 — an AXe tap is a touch held for 0.12 s."""

    def axe_globals(self) -> tuple[str, ...]:
        return ("--ios-hid", "axe", "--axe", str(FAKE_AXE))

    def test_axe_tap_argv(self) -> None:  # AL-03-S01
        code, payload = self.ios("ui", "tap", "--x", "10", "--y", "20",
                                 globals_=self.axe_globals())
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["backend"], "axe")
        self.assertEqual(self.argv_log("axe"), [
            ["touch", "-x", "10", "-y", "20", "--down", "--up", "--delay", "0.12",
             "--udid", UDID]])
        self.assertEqual(self.count("ui", "tap"), 0)

    def test_long_press_keeps_its_duration(self) -> None:
        code, payload = self.ios("ui", "tap", "--x", "10", "--y", "20", "--duration", "800",
                                 globals_=self.axe_globals())
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.argv_log("axe"), [
            ["touch", "-x", "10", "-y", "20", "--down", "--up", "--delay", "0.8",
             "--udid", UDID]])

    def test_double_tap_is_two_held_touches(self) -> None:
        self.set_env(AUTONOM_IOS_HID="axe", AUTONOM_AXE=str(FAKE_AXE))
        target = Target(IOS, UDID, str(FAKE_SIMCTL), {"udid": UDID})
        ui_ios.reset_hid_probe()
        self.addCleanup(ui_ios.reset_hid_probe)
        self.assertEqual(ui.double_tap(target, 10, 20, screen=(402, 874)), "axe")
        held = ["touch", "-x", "10", "-y", "20", "--down", "--up", "--delay", "0.12",
                "--udid", UDID]
        self.assertEqual(self.argv_log("axe"), [held, held])

    def test_fake_axe_refuses_a_touch_that_never_goes_down(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(FAKE_AXE), "touch", "-x", "1", "-y", "2", "--udid", UDID],
            capture_output=True, text=True, env=self.env, timeout=30, check=False)
        self.assertEqual(completed.returncode, 64)
        completed = subprocess.run(
            [sys.executable, str(FAKE_AXE), "touch", "-x", "1", "-y", "2", "--down", "--up",
             "--delay", "soon", "--udid", UDID],
            capture_output=True, text=True, env=self.env, timeout=30, check=False)
        self.assertEqual(completed.returncode, 64)


# --- AL-04 -------------------------------------------------------------------------------------


class DeadCompanionCliTests(CliBase):
    """AL-04 — a dead companion is recovered inside the failing call."""

    def stale_registry(self) -> None:
        self.write_registry([{"udid": UDID, "pid": _dead_pid(), "is_local": True,
                              "path": f"/tmp/idb/{UDID}_companion.sock"}])

    def test_missing_socket_is_pruned_and_read_again(self) -> None:  # AL-04-S01
        for message in (ENOENT, ENOENT_TRACEBACK):
            with self.subTest(message=message[:40]):
                self.forget_calls()
                self.stale_registry()
                self.set_state(idb_fail_until_pruned={"ui describe-all": [1, message]})
                code, payload = self.ios("ui", "tree")
                self.assertEqual(code, 0, payload)
                self.assertGreaterEqual(payload["count"], 3)
                self.assertEqual(self.count("ui", "describe-all"), 2)
                self.assertEqual(self.count("list-targets"), 1)
                self.assertEqual(json.loads(self.registry.read_text(encoding="utf-8")), [])

    def test_retried_when_the_registry_no_longer_lists_the_simulator(self) -> None:
        # another client pruned first: nothing left to drop, still one retry
        self.write_registry([])
        self.set_state(idb_fail_until_pruned={"ui describe-all": [1, REFUSED]})
        code, payload = self.ios("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.count("ui", "describe-all"), 2)
        self.assertEqual(self.count("list-targets"), 1)

    def test_an_input_that_never_connected_is_sent_again(self) -> None:
        # nothing reached the device, so even a tap may be re-dispatched once
        self.stale_registry()
        self.set_state(idb_fail_until_pruned={"ui tap": [1, ENOENT]})
        code, payload = self.ios("ui", "tap", "--x", "1", "--y", "1")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.count("ui", "tap"), 2)
        self.assertEqual(self.count("list-targets"), 1)

    def test_reset_during_a_tap_is_never_resent(self) -> None:  # AL-04-S02
        self.stale_registry()  # even with something prunable
        self.set_state(idb_fail={"ui tap": [1, RESET]})
        code, payload = self.ios("ui", "tap", "--x", "1", "--y", "1")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.IDB_COMPANION_UNAVAILABLE)
        self.assertIn("tap may have been delivered", payload["hint"])
        self.assertIn("not sent again", payload["hint"])
        self.assertEqual(self.count("ui", "tap"), 1)
        self.assertEqual(self.count("list-targets"), 0)

    def test_reset_during_a_read_is_read_again(self) -> None:
        self.set_state(idb_fail_once={"ui describe-all": [1, RESET]})
        code, payload = self.ios("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.count("ui", "describe-all"), 2)

    def test_a_companion_that_stays_dead_is_tried_exactly_twice(self) -> None:
        self.stale_registry()
        self.set_state(idb_fail={"ui describe-all": [1, ENOENT]})
        code, payload = self.ios("ui", "tree")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.IDB_COMPANION_UNAVAILABLE)
        self.assertEqual(self.count("ui", "describe-all"), 2, "exactly one retry")
        self.assertEqual(self.count("list-targets"), 1)

    def test_remote_companion_is_never_pruned(self) -> None:
        self.set_state(idb_fail_once={"ui describe-all": [1, REFUSED]})
        code, payload = self.ios("ui", "tree", globals_=("--idb-host", "10.0.0.5"))
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.count("ui", "describe-all"), 2, "retried once")
        self.assertEqual(self.count("list-targets"), 0, "never pruned")


class EmptyStderrCliTests(CliBase):
    """INV-1 — an idb input that fails with nothing on stderr is sent once,
    and its envelope never names what the caller passed (the typed text)."""

    # typed text that reads like a refused connection, and a connection word
    TYPED = ("pin errno 61 ok", "connect: Connection refused 4321")

    def test_a_failed_ui_text_without_stderr_is_sent_once(self) -> None:
        for typed in self.TYPED:
            for sensitive in ((), ("--sensitive",)):
                with self.subTest(typed=typed, sensitive=bool(sensitive)):
                    self.forget_calls()
                    self.set_state(idb_fail={"ui text": [1, ""]})
                    code, payload = self.ios("ui", "type", typed, *sensitive)
                    self.assertEqual(code, 2, payload)
                    self.assertEqual(payload["error_code"], errors.BACKEND_FAILED)
                    self.assertEqual(payload["error"], "idb ui text failed (1)")
                    self.assertTrue(payload["hint"])
                    self.assertNotIn(typed, json.dumps(payload))
                    self.assertNotIn("61", json.dumps(payload))
                    self.assertEqual(self.count("ui", "text"), 1, "typed exactly once")
                    self.assertEqual(self.count("list-targets"), 0, "no prune, no retry")

    def test_a_failed_tap_without_stderr_is_sent_once(self) -> None:
        self.set_state(idb_fail={"ui tap": [1, ""]})
        code, payload = self.ios("ui", "tap", "--x", "61", "--y", "200")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.BACKEND_FAILED)
        self.assertEqual(payload["error"], "idb ui tap failed (1)")
        self.assertEqual(self.count("ui", "tap"), 1)
        self.assertEqual(self.count("list-targets"), 0)

    def test_idb_own_stderr_is_still_classified(self) -> None:
        # the same typed text with a real refusal on stderr: retried once
        self.set_state(idb_fail_until_pruned={"ui text": [1, REFUSED]})
        code, payload = self.ios("ui", "type", self.TYPED[0])
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.count("ui", "text"), 2)
        self.assertEqual(self.count("list-targets"), 1)


class RetryRuleTests(EnvSandboxMixin, unittest.TestCase):
    """AL-04 / DEC-005 — every command after a failure before the connection,
    only reads after one made mid-call; counted on `run_idb` directly."""

    INPUTS = (["ui", "tap", "1", "1"], ["ui", "swipe", "1", "2", "3", "4", "--duration", "0.2"],
              ["ui", "text", "hello"], ["ui", "key", "40"], ["ui", "button", "HOME"])
    READS = (["ui", "describe-all", "--json"], ["ui", "describe-point", "1", "1"],
             ["crash", "list"], ["list-apps"])

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.set_env(AUTONOM_HOME=str(self.root / "home"), AUTONOM_IDB=str(FAKE_IDB),
                     AUTONOM_IDB_STATE_FILE=str(self.root / "idb-state.json"),
                     AUTONOM_IDB_COMPANION=None, AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.log))

    def calls(self, args: list[str], message: str, **kwargs) -> tuple[int, int, errors.AutonomError | None]:
        """(dispatches of ``args``, list-targets calls, the error raised)."""
        self.log.write_text("", encoding="utf-8")
        verb = [arg for arg in args[:2] if not arg.startswith("-")]
        if verb[1:] and verb[0] not in ("ui", "crash", "file"):
            verb = verb[:1]  # `list-apps` is a verb on its own
        self.state.write_text(json.dumps({"idb_fail": {" ".join(verb): [1, message]}}),
                              encoding="utf-8")
        raised = None
        try:
            ios_idb.run_idb(str(FAKE_IDB), args, udid=UDID, **kwargs)
        except errors.AutonomError as exc:
            raised = exc
        verbs = [_verb(json.loads(line)["argv"])
                 for line in self.log.read_text(encoding="utf-8").splitlines()]
        sent = sum(v[:len(verb)] == verb for v in verbs)
        return sent, sum(v[:1] == ["list-targets"] for v in verbs), raised

    def test_after_connection_inputs_are_sent_once(self) -> None:
        for args in self.INPUTS:
            for message in (RESET, STREAM, UNAVAILABLE):
                with self.subTest(args=args[:2], message=message):
                    sent, pruned, raised = self.calls(args, message)
                    self.assertEqual((sent, pruned), (1, 0))
                    self.assertEqual(raised.code, errors.IDB_COMPANION_UNAVAILABLE)
                    self.assertIn("may have been delivered", raised.hint)

    def test_after_connection_reads_are_retried_once(self) -> None:
        for args in self.READS:
            for message in (RESET, STREAM, UNAVAILABLE):
                with self.subTest(args=args[:2], message=message):
                    sent, pruned, raised = self.calls(args, message)
                    self.assertEqual((sent, pruned), (2, 1))
                    self.assertEqual(raised.code, errors.IDB_COMPANION_UNAVAILABLE)
                    self.assertNotIn("may have been delivered", raised.hint)

    def test_before_connection_every_command_is_retried_once(self) -> None:
        for args in (*self.INPUTS, *self.READS, ["file", "pull", "--bundle-id", "x", "a", "b"]):
            for message in (ENOENT, ENOENT_TRACEBACK, REFUSED):
                with self.subTest(args=args[:2], message=message[:30]):
                    sent, pruned, raised = self.calls(args, message)
                    self.assertEqual((sent, pruned), (2, 1))
                    self.assertEqual(raised.code, errors.IDB_COMPANION_UNAVAILABLE)
                    # nothing reached the device either time
                    self.assertNotIn("may have been delivered", raised.hint)

    def test_a_retried_input_that_then_breaks_mid_call_is_not_sent_a_third_time(self) -> None:
        # First `ui tap` never connects (ENOENT), the retry connects and is
        # reset: two dispatches in all, and the hint says the tap may have
        # landed. Two prefixes, because the fake pops the first match per call.
        self.log.write_text("", encoding="utf-8")
        self.state.write_text(json.dumps({"idb_fail_once": {
            "ui tap": [1, ENOENT], "ui tap 1": [1, RESET]}}), encoding="utf-8")
        with self.assertRaises(errors.AutonomError) as caught:
            ios_idb.run_idb(str(FAKE_IDB), ["ui", "tap", "1", "1"], udid=UDID)
        verbs = [_verb(json.loads(line)["argv"])
                 for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(verbs.count(["ui", "tap"]), 2)
        self.assertEqual([v[:1] for v in verbs].count(["list-targets"]), 1)
        self.assertEqual(caught.exception.code, errors.IDB_COMPANION_UNAVAILABLE)
        self.assertIn("tap may have been delivered", caught.exception.hint)

    def test_remote_endpoint_retries_without_a_prune(self) -> None:
        sent, pruned, _ = self.calls(["ui", "describe-all", "--json"], REFUSED,
                                     companion="mac-farm.local:10882")
        self.assertEqual((sent, pruned), (2, 0))
        sent, pruned, _ = self.calls(["ui", "tap", "1", "1"], RESET,
                                     companion="mac-farm.local:10882")
        self.assertEqual((sent, pruned), (1, 0))

    def test_other_failures_are_not_retried(self) -> None:
        for message in ("Failed to connect to companion", "unexpected internal error",
                        "No such file or directory: '/tmp/missing/Runner.app'"):
            with self.subTest(message=message):
                sent, pruned, _ = self.calls(["ui", "describe-all", "--json"], message)
                self.assertEqual((sent, pruned), (1, 0))

    def test_failure_stage_table(self) -> None:
        cases = {
            ENOENT: ios_idb.BEFORE_CONNECTION,
            ENOENT_TRACEBACK: ios_idb.BEFORE_CONNECTION,
            REFUSED: ios_idb.BEFORE_CONNECTION,
            "connect: ECONNREFUSED": ios_idb.BEFORE_CONNECTION,
            RESET: ios_idb.AFTER_CONNECTION,
            STREAM: ios_idb.AFTER_CONNECTION,
            UNAVAILABLE: ios_idb.AFTER_CONNECTION,
            # both signs: read as after the connection, so no input is resent
            "Connection refused, then Connection reset by peer": ios_idb.AFTER_CONNECTION,
            "No such file or directory: '/tmp/missing/Runner.app'": None,
            "[Errno 22] Invalid argument (companion)": None,
            "Failed to connect to companion": None,
            "": None,
        }
        for message, stage in cases.items():
            with self.subTest(message=message):
                self.assertEqual(ios_idb.failure_stage(message), stage)

    def test_hid_framework_failure_is_still_not_retried(self) -> None:
        message = "SimulatorKit is required for HID interactions: connection refused"
        sent, pruned, raised = self.calls(["ui", "tap", "1", "1"], message)
        self.assertEqual((sent, pruned), (1, 0))
        self.assertEqual(raised.code, errors.IOS_HID_FRAMEWORK_MISSING)

    def test_an_empty_stderr_is_classified_on_nothing_the_caller_passed(self) -> None:
        # The argv holds the caller's values; with nothing on stderr none of
        # them is read for connection words or put in the message (INV-1).
        cases = {
            ("ui", "text", "pin errno 61 ok"): "idb ui text failed (1)",
            ("ui", "text", "Connection reset by peer"): "idb ui text failed (1)",
            ("ui", "text", "SimulatorKit is required for HID"): "idb ui text failed (1)",
            ("ui", "text", "companion socket: No such file or directory"):
                "idb ui text failed (1)",
            ("ui", "tap", "61", "1"): "idb ui tap failed (1)",
            ("ui", "describe-all", "--json"): "idb ui describe-all failed (1)",
            ("crash", "list"): "idb crash list failed (1)",
            ("file", "pull", "--bundle-id", "x", "errno 61", "b"): "idb file pull failed (1)",
        }
        for args, message in cases.items():
            with self.subTest(args=args[:3]):
                sent, pruned, raised = self.calls(list(args), "")
                self.assertEqual((sent, pruned), (1, 0))
                self.assertEqual(raised.code, errors.BACKEND_FAILED)
                self.assertEqual(raised.message, message)
                self.assertEqual(raised.hint, ios_idb.DEFAULT_HINT)

    def test_command_words(self) -> None:
        cases = {
            ("ui", "text", "secret value"): "ui text",
            ("ui", "key", "40"): "ui key",
            ("screenshot", "/tmp/a.png"): "screenshot",
            ("list-apps",): "list-apps",
            ("crash", "show", "Runner-2026.ips"): "crash show",
            ("connect", "10.0.0.5", "10882"): "connect",
            ("ui",): "ui",
            (): "command",
        }
        for args, words in cases.items():
            with self.subTest(args=args):
                self.assertEqual(ios_idb._command_words(list(args)), words)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
