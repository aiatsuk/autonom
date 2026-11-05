"""TEST-002: the host client of the on-device Android helper (AH-02..AH-07, AU-05).

Every case runs against `tests/fakes/fake_adb.py` (which logs the exact argv
of every adb call and models the helper's device side: push, forward,
the app_process start, pidof, /proc/<pid>/cmdline, kill, ls and rm) and
`tests/fakes/fake_android_helper.py` (an in-process TCP server speaking
protocol 1, with fault modes). The build is replaced by a fixed dex unless a
case is about the toolchain, and then the process environment leads to no
real JDK or SDK. No device, no emulator, no javac or d8.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
SERIAL = "emulator-5612"
BUILD = "0123456789abcdef"
OLD_BUILD = "fedcba9876543210"
DEX = f"/data/local/tmp/autonom-helper-{BUILD}.dex"

sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests/fakes"))
sys.path.insert(0, str(ROOT / "tests"))
from autonom_lib import android_helper as helper_mod  # noqa: E402
from autonom_lib import android_helper_build as build_mod  # noqa: E402
from autonom_lib import errors  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402
import fake_android_helper  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


class HelperCase(EnvSandboxMixin, unittest.TestCase):
    """A private state directory, the fake adb's state and log, a fake helper
    on a local port, and a PATH that holds only python3 (for the fakes)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "state"
        self.state = self.root / "adb-state.json"
        self.log = self.root / "adb-log.jsonl"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        (self.root / "user").mkdir()
        self.set_env(AUTONOM_HOME=str(self.home), AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.log), PATH=str(self.bin),
                     HOME=str(self.root / "user"), JAVA_HOME=None, ANDROID_HOME=None,
                     ANDROID_SDK_ROOT=None, AUTONOM_ADB=None,
                     AUTONOM_ANDROID_UI_BACKEND=None)
        self.helper = fake_android_helper.FakeHelper(state_path=self.state).start()
        self.addCleanup(self.helper.close)
        self.write_state()
        self.target = Target("android", SERIAL, str(FAKE_ADB), {"serial": SERIAL})
        self.dex = self.root / "classes.dex"
        self.dex.write_bytes(b"dex\n035\0fake")
        built = build_mod.Build(self.dex, BUILD, None, None)
        self.build_patch = mock.patch.object(helper_mod.build_mod, "ensure", return_value=built)
        self.build = self.build_patch.start()
        self.addCleanup(self.build_patch.stop)

    # -- the fake device

    def write_state(self, **extra) -> None:
        state = {"devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"]],
                 "helper_port": self.helper.port,
                 "device_files": ["other.txt", "autonom-helper.log"]}
        state.update(extra)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def device(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def update_device(self, **changes) -> None:
        state = self.device()
        state.update(changes)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def running_helper(self, pid: int = 100, build: str = BUILD, *, port: int | None = None
                       ) -> None:
        """A helper already on the device and in the record, as an earlier
        call left it."""
        port = self.helper.port if port is None else port
        state = self.device()
        state["helper_procs"] = {str(pid): "autonom-helper\0dev.autonom.helper.Main\0"}
        state["helper_running"] = {"pid": pid, "build": build}
        state["forwards"] = [f"{SERIAL} tcp:{port} localabstract:autonom-helper"]
        state["device_files"] = [f"autonom-helper-{build}.dex"]
        self.state.write_text(json.dumps(state), encoding="utf-8")
        self.write_record(port=port, pid=pid, build=build)

    def write_record(self, **fields) -> Path:
        path = helper_mod.record_path(SERIAL)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"serial": SERIAL, **fields}), encoding="utf-8")
        return path

    def record(self) -> dict:
        return helper_mod.load_record(SERIAL)

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        return [row["argv"][2:] if row["argv"][:1] == ["-s"] else row["argv"] for row in rows]

    def clear_calls(self) -> None:
        self.log.unlink(missing_ok=True)

    def starts(self) -> list[dict]:
        return self.device().get("helper_starts") or []

    def shell_words(self, call: list[str]) -> list[str]:
        import shlex
        return shlex.split(" ".join(call[1:])) if call[:1] == ["shell"] else []

    def ensure(self, **kwargs) -> helper_mod.Helper:
        helper = helper_mod.ensure(self.target, **kwargs)
        self.addCleanup(helper.close)
        return helper


# --- preference (AU-05) ------------------------------------------------------------

class PreferenceTests(unittest.TestCase):
    def test_flag_then_variable_then_session_then_auto(self) -> None:
        env = {"AUTONOM_ANDROID_UI_BACKEND": "uiautomator"}
        self.assertEqual(helper_mod.preference(env={}), "auto")
        self.assertEqual(helper_mod.preference(env=env), "uiautomator")
        self.assertEqual(helper_mod.preference("helper", env=env), "helper")
        self.assertEqual(helper_mod.preference(" Helper ", env={}), "helper")
        self.assertEqual(helper_mod.preference(env={}, session_value="uiautomator"),
                         "uiautomator")
        self.assertEqual(helper_mod.preference(env=env, session_value="helper"),
                         "uiautomator")
        # an iOS session's stored choice is not an Android one
        self.assertEqual(helper_mod.preference(env={}, session_value="idb"), "auto")
        self.assertEqual(helper_mod.preference(env={"AUTONOM_ANDROID_UI_BACKEND": ""}), "auto")

    def test_an_unknown_value_is_invalid_value(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            helper_mod.preference("xcuitest", env={})
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertEqual(caught.exception.extra["flag"], "--ui-backend")
        with self.assertRaises(errors.AutonomError) as caught:
            helper_mod.preference(env={"AUTONOM_ANDROID_UI_BACKEND": "fast"})
        self.assertEqual(caught.exception.extra["variable"], "AUTONOM_ANDROID_UI_BACKEND")
        self.assertIn("uiautomator", caught.exception.hint)

    def test_host_timeouts(self) -> None:
        self.assertEqual(helper_mod.timeout_for("type"), 12.0)
        self.assertEqual(helper_mod.timeout_for("set_text"), 12.0)
        for op in ("tap", "key", "read", "display", "screenshot"):
            self.assertEqual(helper_mod.timeout_for(op), 8.0, op)
        self.assertEqual(helper_mod.timeout_for("hello"), 2.0)
        self.assertEqual(helper_mod.timeout_for("quit"), 2.0)
        self.assertEqual(helper_mod.timeout_for("long_press", {"duration_ms": 1500}), 9.5)
        self.assertEqual(helper_mod.timeout_for("swipe", {"duration_ms": 300}), 8.3)


# --- availability before any device call (AH-02) -----------------------------------

class AvailabilityTests(HelperCase):
    def test_a_host_without_tools_makes_no_adb_call(self) -> None:
        self.build_patch.stop()  # the real build module, in an environment with no JDK or SDK
        self.addCleanup(self.build_patch.start)  # undone before setUp's own stop
        with self.assertRaises(helper_mod.HelperUnavailable) as caught:
            helper_mod.ensure(self.target)
        error = caught.exception
        self.assertEqual(error.code, errors.ANDROID_HELPER_UNAVAILABLE)
        self.assertEqual(error.reason, "javac not found")
        self.assertEqual(error.as_dict()["reason"], "javac not found")
        self.assertIn("JDK", error.hint)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.helper.ops(), [])

    def test_a_failed_build_names_the_failure_and_touches_no_device(self) -> None:
        self.build.return_value = build_mod.Build(None, BUILD, "helper build failed: javac exited 1",
                                                  "See the failure file.")
        with self.assertRaises(helper_mod.HelperUnavailable) as caught:
            helper_mod.ensure(self.target, force=True)
        self.assertEqual(caught.exception.reason, "helper build failed: javac exited 1")
        self.assertEqual(caught.exception.hint, "See the failure file.")
        self.assertEqual(self.calls(), [])

    def test_a_remembered_busy_device_is_skipped_before_the_build(self) -> None:
        self.write_record(unavailable={"code": "uiautomation_busy", "reason": "uiautomation_busy: x",
                                       "hint": "stop the other tool", "until": time.time() + 30})
        with self.assertRaises(helper_mod.HelperUnavailable) as caught:
            helper_mod.ensure(self.target)
        self.assertEqual(caught.exception.reason, "remembered: uiautomation_busy: x")
        self.assertTrue(caught.exception.extra["remembered"])
        self.build.assert_not_called()
        self.assertEqual(self.calls(), [])
        # --ui-backend helper tries again
        helper = self.ensure(force=True)
        self.assertTrue(helper.started)
        self.assertNotIn("unavailable", self.record())

    def test_an_unwritable_device_record_is_unavailable_before_any_adb_call(self) -> None:
        devices = helper_mod.devices_dir()
        devices.parent.mkdir(parents=True, exist_ok=True)
        devices.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(helper_mod.HelperUnavailable) as caught:
            helper_mod.ensure(self.target)
        self.assertIn("cannot be written", caught.exception.reason)
        self.assertIn(str(devices), caught.exception.hint)
        self.assertEqual(self.calls(), [])

    def test_an_expired_busy_entry_is_tried_again(self) -> None:
        self.write_record(unavailable={"code": "uiautomation_busy", "reason": "busy",
                                       "hint": "h", "until": time.time() - 1})
        helper = self.ensure()
        self.assertTrue(helper.started)
        self.assertNotIn("unavailable", self.record())


# --- start, reuse and replacement (AH-03, AH-05) ----------------------------------------

class StartTests(HelperCase):
    def test_first_use_pushes_starts_forwards_and_greets(self) -> None:
        self.update_device(device_files=["other.txt", "autonom-helper.log",
                                         f"autonom-helper-{OLD_BUILD}.dex",
                                         "autonom-helper-old.dex.bak"])
        helper = self.ensure()
        self.assertTrue(helper.started)
        self.assertFalse(helper.replaced)
        self.assertEqual((helper.build, helper.pid, helper.port),
                         (BUILD, 4242, self.helper.port))
        calls = self.calls()
        self.assertIn(["push", str(self.dex), DEX], calls)
        # other builds go by exact name; nothing else is touched
        removed = [call for call in calls if self.shell_words(call)[:1] == ["rm"]]
        self.assertEqual(removed, [["shell", "rm", "-f",
                                    f"/data/local/tmp/autonom-helper-{OLD_BUILD}.dex"]])
        self.assertEqual(sorted(self.device()["device_files"]),
                         sorted(["other.txt", "autonom-helper.log", "autonom-helper-old.dex.bak",
                                 f"autonom-helper-{BUILD}.dex"]))
        (start,) = self.starts()
        self.assertEqual({key: start[key] for key in ("build", "classpath", "idle_ms", "socket",
                                                      "nice_name")},
                         {"build": BUILD, "classpath": DEX, "idle_ms": "120000",
                          "socket": "autonom-helper", "nice_name": "autonom-helper"})
        self.assertNotIn("exited", start)
        launch = next(self.shell_words(call) for call in calls
                      if "app_process" in self.shell_words(call))
        self.assertEqual(launch[:3], [f"CLASSPATH={DEX}", "nohup", "app_process"])
        self.assertEqual(launch[-3:], [">/data/local/tmp/autonom-helper.log", "2>&1", "&"])
        self.assertIn(["forward", "tcp:0", "localabstract:autonom-helper"], calls)
        # push before the start, the start before the forward
        order = [index for index, call in enumerate(calls)
                 if call[:1] == ["push"] or "app_process" in self.shell_words(call)
                 or call[:2] == ["forward", "tcp:0"]]
        self.assertEqual(order, sorted(order))
        record = self.record()
        self.assertEqual((record["port"], record["pid"], record["build"]),
                         (self.helper.port, 4242, BUILD))
        self.assertTrue(record["started_at"].endswith("Z"))
        self.assertEqual([item["op"] for item in self.helper.ops()], ["hello"])

    def test_a_warm_call_makes_no_adb_call(self) -> None:
        self.ensure().close()
        self.clear_calls()
        helper = self.ensure()
        self.assertFalse(helper.started)
        self.assertEqual(self.calls(), [])
        self.assertEqual(len(self.starts()), 1)
        reply = helper.call("read")
        self.assertTrue(reply["ok"])
        self.assertIn("<hierarchy", reply["xml"])

    def test_every_request_has_a_unique_32_hex_id(self) -> None:
        helper = self.ensure()
        for _ in range(3):
            helper.call("display")
        ids = [item["id"] for item in self.helper.ops()]
        self.assertEqual(len(ids), len(set(ids)))
        for value in ids:
            self.assertRegex(value, r"^[0-9a-f]{32}$")

    def test_a_helper_of_another_build_is_quit_and_replaced_before_any_request(self) -> None:
        self.running_helper(pid=100, build=OLD_BUILD)
        helper = self.ensure()
        self.assertTrue(helper.started)
        self.assertTrue(helper.replaced)
        self.assertEqual(helper.build, BUILD)
        self.assertEqual(helper.pid, 4242)
        self.assertEqual([item["op"] for item in self.helper.ops()], ["hello", "quit", "hello"])
        self.assertNotIn("100", self.device().get("helper_procs", {}))
        # quit was enough: no kill
        self.assertFalse(any(self.shell_words(call)[:1] == ["kill"] for call in self.calls()))
        self.assertEqual([start["build"] for start in self.starts()], [BUILD])
        self.assertEqual(self.record()["build"], BUILD)

    def test_a_killed_helper_is_restarted_exactly_once(self) -> None:
        self.running_helper(pid=100)
        self.update_device(helper_procs={}, helper_running=None)  # killed on the device
        helper = self.ensure()
        self.assertTrue(helper.started)
        self.assertEqual(len(self.starts()), 1)
        # its pid was checked, and nothing was killed: it is gone
        self.assertIn(["shell", "cat", "/proc/100/cmdline"], self.calls())
        self.assertFalse(any(self.shell_words(call)[:1] == ["kill"] for call in self.calls()))
        # the recorded forward is still listed: reused, no new one
        self.assertNotIn(["forward", "tcp:0", "localabstract:autonom-helper"], self.calls())

    def test_eof_on_hello_kills_the_verified_pid_and_starts_once(self) -> None:
        self.running_helper(pid=100)
        self.helper.eof_on_hello = 1
        helper = self.ensure()
        self.assertTrue(helper.started)
        self.assertIn(["shell", "kill", "100"], self.calls())
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(helper.pid, 4242)

    def test_a_dropped_forward_is_made_again(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
        probe.close()
        self.running_helper(pid=100, port=closed)
        self.update_device(forwards=[])  # an adb server restart dropped it
        helper = self.ensure()
        # the recorded port is not listed for the serial: never tried; a fresh
        # forward reaches the helper that still runs, so nothing is restarted
        self.assertFalse(helper.started)
        self.assertEqual(helper.pid, 100)
        self.assertIn(["forward", "tcp:0", "localabstract:autonom-helper"], self.calls())
        self.assertFalse(any(self.shell_words(call)[:1] == ["kill"] for call in self.calls()))
        self.assertFalse(any(call[:2] == ["forward", "--remove"] for call in self.calls()))
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.record()["port"], self.helper.port)

    def test_a_recorded_port_listed_for_another_device_is_never_used(self) -> None:
        """After an adb server restart the recorded host port forwards to
        another device's helper: nothing of this device's call goes there,
        and that forward is left alone (it is not ours)."""
        other = fake_android_helper.FakeHelper(build=BUILD, pid=900).start()
        self.addCleanup(other.close)
        self.running_helper(pid=100, port=other.port)
        theirs = f"emulator-5580 tcp:{other.port} localabstract:autonom-helper"
        self.update_device(forwards=[theirs])
        reply = helper_mod.request(self.target, "tap", x=10, y=20)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(other.ops(), [], "not even a hello reached the other device's helper")
        self.assertEqual(len(self.helper.ops("tap")), 1)
        self.assertEqual(reply["helper"]["pid"], 100)
        self.assertIn(["forward", "tcp:0", "localabstract:autonom-helper"], self.calls())
        self.assertFalse(any(call[:2] == ["forward", "--remove"] for call in self.calls()))
        self.assertIn(theirs, self.device()["forwards"])
        self.assertEqual(self.record()["port"], self.helper.port)

    def test_status_and_stop_send_nothing_through_another_device_s_port(self) -> None:
        other = fake_android_helper.FakeHelper(build=BUILD, pid=900).start()
        self.addCleanup(other.close)
        self.running_helper(pid=100, port=other.port)
        theirs = f"emulator-5580 tcp:{other.port} localabstract:autonom-helper"
        self.update_device(forwards=[theirs])
        result = helper_mod.status(self.target)
        self.assertIs(result["answering"], False)
        self.assertIn("not forwarded to this device's helper", result["error"])
        stopped = helper_mod.stop(self.target)
        self.assertFalse(stopped["quit"])
        self.assertTrue(stopped["killed"], "this device's recorded helper, verified, on its serial")
        self.assertFalse(stopped["forward_removed"])
        self.assertEqual(other.ops(), [], "no hello, no quit to the other device's helper")
        self.assertEqual(self.device()["forwards"], [theirs])

    def test_the_forward_list_is_read_once_per_process_and_device(self) -> None:
        self.running_helper(pid=100)
        for _ in range(3):
            self.assertTrue(helper_mod.request(self.target, "display")["ok"])
        self.assertEqual(self.calls().count(["forward", "--list"]), 1)
        helper_mod.reset_forward_cache()  # what a new CLI invocation does
        helper_mod.request(self.target, "display")
        self.assertEqual(self.calls().count(["forward", "--list"]), 2)
        # a long-lived process (the Canvas bridge) reads it again once it is old
        with mock.patch.object(helper_mod, "FORWARD_CONFIRM_TTL_S", 0):
            helper_mod.request(self.target, "display")
        self.assertEqual(self.calls().count(["forward", "--list"]), 3)

    def assert_left_running(self, caught: Any, pid: int) -> None:
        """A busy helper that ran before the launch: not quit, not killed;
        the start raised `HelperBusy` naming it and remembered nothing."""
        self.assertIsInstance(caught.exception, helper_mod.HelperBusy)
        self.assertEqual(caught.exception.foreign_pids, [pid])
        self.assertEqual(caught.exception.extra["helper_foreign_pids"], [pid])
        self.assertIs(caught.exception.extra["helper_busy"], True)
        self.assertTrue(caught.exception.reason.startswith("helper_busy: "))
        self.assertIn(str(pid), self.device()["helper_procs"])
        self.assertEqual(self.device()["helper_running"]["pid"], pid)
        self.assertNotIn(["shell", "kill", str(pid)], self.calls())
        self.assertEqual(self.helper.ops("quit"), [])
        self.assertEqual(self.device().get("forwards") or [], [], "our forward went")
        record = self.record()
        self.assertNotIn("unavailable", record)
        self.assertNotIn("port", record)

    def test_an_unrecorded_helper_of_another_build_on_the_socket_is_quit_and_replaced(self) -> None:
        # a helper no record names serves the socket with another build (ours
        # exits at once) and answers hello at once: it is idle, so it is quit
        # and the expected build serves the request (AH-03)
        self.update_device(helper_procs={"77": "autonom-helper\0x\0"},
                           helper_running={"pid": 77, "build": OLD_BUILD})
        reply = helper_mod.request(self.target, "tap", x=10, y=20)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual({key: reply["helper"][key] for key in ("build", "pid", "started",
                                                                 "replaced")},
                         {"build": BUILD, "pid": 4243, "started": True, "replaced": True})
        self.assertEqual([(start["build"], start.get("exited")) for start in self.starts()],
                         [(BUILD, "socket_in_use"), (BUILD, None)])
        ops = self.helper.ops()
        self.assertEqual([item["op"] for item in ops], ["hello", "quit", "hello", "tap"])
        # one quit, on the old helper's own connection, and nothing else reached it
        old = ops[0]["connection"]
        self.assertEqual([item["op"] for item in ops if item["connection"] == old],
                         ["hello", "quit"])
        self.assertEqual(ops[3]["connection"], ops[2]["connection"])
        self.assertNotEqual(ops[2]["connection"], old)
        self.assertNotIn("77", self.device()["helper_procs"])
        self.assertNotIn(["shell", "kill", "77"], self.calls(), "the quit was enough")
        record = self.record()
        self.assertEqual((record["pid"], record["build"]), (4243, BUILD))
        self.assertNotIn("unavailable", record)

    def test_an_unrecorded_helper_of_another_build_that_stays_after_quit_is_killed(self) -> None:
        self.update_device(helper_procs={"77": "autonom-helper\0x\0"},
                           helper_running={"pid": 77, "build": OLD_BUILD})
        self.helper.drop_after = {"quit": 1}  # it reads the quit and keeps running
        with mock.patch.object(helper_mod, "QUIT_EXIT_WAIT_S", 0.3):
            helper = self.ensure()
        self.assertEqual((helper.build, helper.pid, helper.replaced), (BUILD, 4243, True))
        self.assertEqual(len(self.helper.ops("quit")), 1)
        calls = self.calls()
        self.assertLess(calls.index(["shell", "cat", "/proc/77/cmdline"]),
                        calls.index(["shell", "kill", "77"]), "killed after its check")
        self.assertNotIn("77", self.device()["helper_procs"])

    def test_a_start_replaces_another_build_only_once(self) -> None:
        # every helper on the socket answers with another build: the first is
        # quit and ours launched again; the second ends the start (no loop),
        # and what our launches added is ended
        self.helper.wrong_build = OLD_BUILD
        with self.assertRaises(helper_mod.HelperUnavailable) as caught:
            helper_mod.ensure(self.target)
        self.assertNotIsInstance(caught.exception, helper_mod.HelperBusy)
        self.assertIn("not replaced twice", caught.exception.reason)
        self.assertEqual([start["pid"] for start in self.starts()], [4242, 4243])
        self.assertEqual([item["op"] for item in self.helper.ops()], ["hello", "quit", "hello"])
        calls = self.calls()
        self.assertLess(calls.index(["shell", "cat", "/proc/4243/cmdline"]),
                        calls.index(["shell", "kill", "4243"]))
        self.assertEqual(self.device().get("helper_procs"), {})
        self.assertEqual(self.device().get("forwards") or [], [])
        record = self.record()
        self.assertNotIn("unavailable", record)
        self.assertNotIn("port", record)

    def test_an_unrecorded_helper_of_this_build_on_the_socket_is_used(self) -> None:
        self.update_device(helper_procs={"77": "autonom-helper\0x\0"},
                           helper_running={"pid": 77, "build": BUILD})
        helper = self.ensure()
        self.assertEqual((helper.pid, helper.build), (77, BUILD))
        self.assertEqual([start.get("exited") for start in self.starts()], ["socket_in_use"])
        self.assertNotIn(["shell", "kill", "77"], self.calls())

    def test_a_start_queued_behind_another_helper_s_call_kills_nothing(self) -> None:
        # a helper that ran before the launch is in another state directory's
        # call: ours exits (socket_in_use) and our hellos wait in its backlog
        self.update_device(helper_procs={"77": "autonom-helper\0x\0"},
                           helper_running={"pid": 77, "build": BUILD})
        self.helper.hung_hellos = -1
        with mock.patch.object(helper_mod, "START_BUDGET_S", 0.4), \
                mock.patch.dict(helper_mod.TIMEOUTS_S, {"hello": 0.2}):
            with self.assertRaises(helper_mod.HelperUnavailable) as caught:
                helper_mod.ensure(self.target)
        self.assert_left_running(caught, 77)

    def test_a_failed_start_ends_only_what_its_launch_added(self) -> None:
        # pid 77 runs but does not serve the socket (it is starting, or
        # exiting); our helper 4242 starts, then the forward fails
        self.update_device(helper_procs={"77": "autonom-helper\0x\0"},
                           fail={f"-s {SERIAL} forward tcp:0":
                                 [1, "adb: error: cannot bind listener"]})
        with self.assertRaises(helper_mod.HelperUnavailable):
            helper_mod.ensure(self.target)
        calls = self.calls()
        self.assertLess(calls.index(["shell", "pidof", "autonom-helper"]),
                        next(index for index, call in enumerate(calls)
                             if "app_process" in self.shell_words(call)),
                        "the helpers already running are listed before the launch")
        self.assertIn(["shell", "kill", "4242"], calls)
        self.assertNotIn(["shell", "kill", "77"], calls)
        self.assertEqual(list(self.device()["helper_procs"]), ["77"])

    def test_a_start_that_never_answers_is_remembered_for_the_boot(self) -> None:
        self.helper.eof_on_hello = 10_000
        with mock.patch.object(helper_mod, "START_BUDGET_S", 0.6):
            with self.assertRaises(helper_mod.HelperUnavailable) as caught:
                helper_mod.ensure(self.target)
        self.assertIn("did not answer hello", caught.exception.reason)
        # nothing of the attempt is left: its process and its forward are gone
        self.assertEqual(self.device().get("helper_procs"), {})
        self.assertEqual(self.device().get("forwards"), [])
        entry = self.record()["unavailable"]
        self.assertEqual(entry["code"], "start_timeout")
        self.assertEqual(entry["boot_id"], "5f3c2a10-0000-4000-8000-000000000001")
        # auto: one boot-id read, no second attempt
        self.clear_calls()
        with self.assertRaises(helper_mod.HelperUnavailable) as caught:
            helper_mod.ensure(self.target)
        self.assertTrue(caught.exception.reason.startswith("remembered: "))
        self.assertEqual(self.calls(), [["shell", "cat", "/proc/sys/kernel/random/boot_id"]])
        # another boot: tried again
        self.helper.eof_on_hello = 0
        self.update_device(boot_ids={SERIAL: "5f3c2a10-0000-4000-8000-000000000002"})
        helper = self.ensure()
        self.assertTrue(helper.started)
        self.assertNotIn("unavailable", self.record())

    def test_uiautomation_busy_is_remembered_for_60_seconds(self) -> None:
        self.helper.busy_hellos = 1
        with self.assertRaises(helper_mod.HelperUnavailable) as caught:
            helper_mod.ensure(self.target)
        self.assertIn("uiautomation_busy", caught.exception.reason)
        self.assertIn("UiAutomation", caught.exception.hint)
        entry = self.record()["unavailable"]
        self.assertEqual(entry["code"], "uiautomation_busy")
        self.assertAlmostEqual(entry["until"], time.time() + 60, delta=5)
        self.assertNotIn("port", self.record())
        # ended once: by the pid its hello named (it exited by itself), not
        # a pidof sweep after that (the one pidof lists, before the launch,
        # the helpers that a start must leave alone)
        calls = self.calls()
        self.assertIn(["shell", "cat", "/proc/4242/cmdline"], calls)
        self.assertEqual(calls.count(["shell", "pidof", "autonom-helper"]), 1)
        self.assertLess(calls.index(["shell", "pidof", "autonom-helper"]),
                        calls.index(["shell", "cat", "/proc/4242/cmdline"]))
        self.clear_calls()
        self.build.reset_mock()
        with self.assertRaises(helper_mod.HelperUnavailable):
            helper_mod.ensure(self.target)
        self.assertEqual(self.calls(), [])
        self.build.assert_not_called()

    def test_uiautomation_failed_is_remembered_for_the_boot(self) -> None:
        self.helper.failed_hellos = 1
        with self.assertRaises(helper_mod.HelperUnavailable) as caught:
            helper_mod.ensure(self.target)
        self.assertIn("uiautomation_failed", caught.exception.reason)
        self.assertEqual(self.record()["unavailable"]["code"], "uiautomation_failed")
        self.clear_calls()
        with self.assertRaises(helper_mod.HelperUnavailable):
            helper_mod.ensure(self.target)
        self.assertFalse(any(call[:1] == ["push"] for call in self.calls()))

    def test_a_device_below_api_26_is_remembered_and_gets_nothing(self) -> None:
        self.update_device(getprop={"ro.build.version.sdk": "25"})
        with self.assertRaises(helper_mod.HelperUnavailable) as caught:
            helper_mod.ensure(self.target)
        self.assertEqual(caught.exception.reason, "device API 25 is below 26")
        self.assertFalse(any(call[:1] == ["push"] for call in self.calls()))
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.record()["unavailable"]["code"], "api_too_low")

    def test_an_adb_failure_while_starting_is_unavailable_not_a_crash(self) -> None:
        self.update_device(fail={f"-s {SERIAL} push": [1, "adb: error: failed to copy"]})
        with self.assertRaises(helper_mod.HelperUnavailable) as caught:
            helper_mod.ensure(self.target)
        self.assertIn("adb push", caught.exception.reason)
        self.assertEqual(self.starts(), [])

    # A start that fails after its launch ends what it launched (INV-3): no
    # helper is left holding UiAutomation that no record names.

    def assert_the_launched_helper_was_ended(self) -> None:
        self.assertEqual([start["pid"] for start in self.starts()], [4242])
        self.assertEqual(self.device().get("helper_procs"), {})
        self.assertIsNone(self.device().get("helper_running"))
        self.assertEqual(self.device().get("forwards") or [], [])
        calls = self.calls()
        self.assertLess(calls.index(["shell", "cat", "/proc/4242/cmdline"]),
                        calls.index(["shell", "kill", "4242"]))
        self.assertFalse(any("--remove-all" in call for call in calls))
        # nothing is remembered for it, and the record names no pid or port
        self.assertFalse(helper_mod.has_record(SERIAL))
        self.clear_calls()
        self.assertFalse(helper_mod.stop(self.target)["was_running"])
        self.assertEqual(self.calls(), [])

    def test_a_forward_failure_after_the_launch_ends_the_launched_helper(self) -> None:
        self.update_device(fail={f"-s {SERIAL} forward tcp:0":
                                 [1, "adb: error: cannot bind listener"]})
        with self.assertRaises(helper_mod.HelperUnavailable) as caught:
            helper_mod.ensure(self.target)
        self.assertIn("adb forward did not give a port", caught.exception.reason)
        self.assert_the_launched_helper_was_ended()
        # the cause may pass: the next call starts the helper again
        self.update_device(fail={})
        helper = self.ensure()
        self.assertTrue(helper.started)
        self.assertEqual(helper.pid, 4243)

    def test_an_adb_timeout_after_the_launch_ends_the_launched_helper(self) -> None:
        real = helper_mod.adb_mod.run_adb

        def forward_hangs(tool, args, **kwargs):
            if list(args[:2]) == ["forward", "tcp:0"]:
                raise subprocess.TimeoutExpired([tool, *args], kwargs.get("timeout"))
            return real(tool, args, **kwargs)

        with mock.patch.object(helper_mod.adb_mod, "run_adb", side_effect=forward_hangs):
            with self.assertRaises(helper_mod.HelperUnavailable) as caught:
                helper_mod.ensure(self.target)
        self.assertIn("adb failed", caught.exception.reason)
        self.assertIn("did not finish", caught.exception.reason)
        self.assert_the_launched_helper_was_ended()

    def test_a_record_that_cannot_be_saved_after_the_launch_ends_the_launched_helper(self) -> None:
        with mock.patch.object(helper_mod, "_save_record",
                               side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(helper_mod.HelperUnavailable) as caught:
                helper_mod.ensure(self.target)
        self.assertIn("cannot be written", caught.exception.reason)
        # the forward made for it went too
        self.assertIn(["forward", "tcp:0", "localabstract:autonom-helper"], self.calls())
        self.assertIn(["forward", "--remove", f"tcp:{self.helper.port}"], self.calls())
        self.assert_the_launched_helper_was_ended()

    def test_a_record_that_cannot_be_saved_after_hello_ends_the_launched_helper(self) -> None:
        real = helper_mod._save_record
        saves = []

        def second_save_fails(record, env):
            saves.append(dict(record))
            if len(saves) == 2:  # the save of the ready helper, after its hello
                raise OSError(28, "No space left on device")
            return real(record, env)

        with mock.patch.object(helper_mod, "_save_record", side_effect=second_save_fails):
            with self.assertRaises(helper_mod.HelperUnavailable):
                helper_mod.ensure(self.target)
        self.assertEqual(saves[1]["pid"], 4242)
        self.assertEqual([item["op"] for item in self.helper.ops()], ["hello"])
        self.assert_the_launched_helper_was_ended()


# --- requests: single fire and the read retry (AH-05, AH-06) -------------------------------

class RequestTests(HelperCase):
    def test_a_tap_without_a_reply_is_uncertain_and_never_sent_again(self) -> None:
        self.ensure().close()
        self.helper.drop_after = {"tap": 1}
        self.clear_calls()
        connections = self.helper.connections
        with self.assertRaises(errors.AutonomError) as caught:
            helper_mod.request(self.target, "tap", x=100, y=200)
        error = caught.exception
        self.assertEqual(error.code, errors.UI_ACTION_UNCERTAIN)
        self.assertEqual(error.extra["op"], "tap")
        self.assertEqual(error.extra["backend"], "helper")
        taps = self.helper.ops("tap")
        self.assertEqual(len(taps), 1)
        self.assertEqual(error.extra["request_id"], taps[0]["id"])
        self.assertEqual((taps[0]["x"], taps[0]["y"]), (100, 200))
        # no other route, no restart, no second connection after the tap
        self.assertFalse(any("input" in self.shell_words(call) for call in self.calls()))
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.helper.connections, connections + 1)

    def test_a_slow_tap_reply_past_the_host_timeout_is_uncertain(self) -> None:
        self.ensure().close()
        self.helper.slow = {"tap": 1.0}
        with mock.patch.dict(helper_mod.TIMEOUTS_S, {"tap": 0.3}):
            with self.assertRaises(errors.AutonomError) as caught:
                helper_mod.request(self.target, "tap", x=1, y=2)
        self.assertEqual(caught.exception.code, errors.UI_ACTION_UNCERTAIN)
        self.assertIn("no reply within", str(caught.exception))
        self.assertEqual(len(self.helper.ops("tap")), 1)

    def test_every_input_op_is_single_fire(self) -> None:
        self.ensure().close()
        for op, args in (("long_press", {"x": 1, "y": 2, "duration_ms": 10}),
                         ("swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration_ms": 10}),
                         ("key", {"keycode": 4}), ("type", {"text": "é東"}),
                         ("set_text", {"text": "abc"})):
            with self.subTest(op=op):
                self.helper.drop_after = {op: 1}
                with self.assertRaises(errors.AutonomError) as caught:
                    helper_mod.request(self.target, op, **args)
                self.assertEqual(caught.exception.code, errors.UI_ACTION_UNCERTAIN)
                self.assertEqual(len(self.helper.ops(op)), 1)

    def test_an_op_not_known_as_a_read_is_never_retried(self) -> None:
        self.ensure().close()
        self.helper.drop_after = {"vibrate": 1}
        with self.assertRaises(errors.AutonomError) as caught:
            helper_mod.request(self.target, "vibrate")
        self.assertEqual(caught.exception.code, errors.UI_ACTION_UNCERTAIN)
        self.assertEqual(len(self.helper.ops("vibrate")), 1)
        self.assertEqual(len(self.starts()), 1)

    def test_an_input_reply_comes_back_with_the_helper_provenance(self) -> None:
        reply = helper_mod.request(self.target, "tap", x=5, y=6, read_after=True)
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["performed"])
        self.assertIn("read", reply)
        self.assertEqual(reply["helper"], {"build": BUILD, "pid": 4242, "port": self.helper.port,
                                           "started": True, "replaced": False,
                                           "retried": False})

    def test_an_error_reply_is_returned_not_retried(self) -> None:
        self.helper.replies = {"type": {"error": {"code": "no_focus", "message": "no field"}}}
        reply = helper_mod.request(self.target, "type", text="x")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "no_focus")
        self.assertEqual(len(self.helper.ops("type")), 1)

    def test_a_read_without_a_reply_is_sent_once_more_to_a_restarted_helper(self) -> None:
        self.ensure().close()
        self.helper.drop_after = {"read": 1}
        reply = helper_mod.request(self.target, "read")
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["helper"]["retried"])
        self.assertEqual(len(self.helper.ops("read")), 2)
        self.assertIn(["shell", "kill", "4242"], self.calls())
        self.assertEqual([start["pid"] for start in self.starts()], [4242, 4243])
        self.assertEqual(reply["helper"]["pid"], 4243)

    def test_a_read_unanswered_twice_is_unavailable(self) -> None:
        self.ensure().close()
        self.helper.drop_after = {"read": -1}
        with self.assertRaises(helper_mod.HelperUnavailable) as caught:
            helper_mod.request(self.target, "read")
        self.assertIn("not answered twice", caught.exception.reason)
        self.assertEqual(len(self.helper.ops("read")), 2)
        self.assertEqual(len(self.starts()), 2)

    def test_a_read_retry_never_kills_the_helper_another_caller_started(self) -> None:
        # Caller A's read on H1 (pid 4242) goes unanswered: H1 crashed. Before
        # A restarts it, caller B finds H1 dead, starts H2 (pid 4243) and taps
        # on it. A's restart must leave H2 alone, and A's read goes to H2.
        self.ensure().close()
        self.helper.drop_after = {"read": 1}
        real_restart = helper_mod.restart
        seen: dict = {}

        def other_caller_first(target, pid, *, env=None):
            self.update_device(helper_procs={}, helper_running=None)  # H1 crashed
            other = helper_mod.ensure(target)  # caller B
            self.addCleanup(other.close)
            seen["other"] = other.pid
            seen["restart"] = real_restart(target, pid, env=env)
            seen["tap"] = other.call("tap", x=1, y=2)  # B's tap, after A's restart
            other.close()
            return seen["restart"]

        with mock.patch.object(helper_mod, "restart", side_effect=other_caller_first):
            reply = helper_mod.request(self.target, "read")
        self.assertEqual(seen["other"], 4243)
        self.assertEqual(seen["restart"], {"pid": 4242, "killed": False,
                                           "skipped": "record_changed", "record_pid": 4243})
        self.assertTrue(seen["tap"]["ok"])
        self.assertTrue(seen["tap"]["performed"])
        self.assertNotIn(["shell", "kill", "4243"], self.calls())
        self.assertIn("4243", self.device()["helper_procs"])
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["helper"]["retried"])
        self.assertEqual(reply["helper"]["pid"], 4243)
        self.assertFalse(reply["helper"]["started"])  # B's helper, not a third one
        self.assertEqual([start["pid"] for start in self.starts()], [4242, 4243])
        self.assertEqual(len(self.helper.ops("read")), 2)
        self.assertEqual(self.record()["pid"], 4243)

    def test_the_unanswered_read_s_connection_is_held_until_its_helper_is_ended(self) -> None:
        # While A still holds the connection, that helper serves no other
        # caller, so nobody can be mid-input on it when it is killed.
        self.ensure().close()
        self.helper.slow = {"read": 1.0}
        events: list[str] = []
        real_close = helper_mod.Connection.close
        real_restart = helper_mod.restart

        def close(connection):
            events.append("close")
            real_close(connection)

        def restart(target, pid, *, env=None):
            events.append(f"restart {pid}")
            self.helper.slow = {}
            return real_restart(target, pid, env=env)

        with mock.patch.dict(helper_mod.TIMEOUTS_S, {"read": 0.3}), \
                mock.patch.object(helper_mod.Connection, "close", autospec=True,
                                  side_effect=close), \
                mock.patch.object(helper_mod, "restart", side_effect=restart):
            reply = helper_mod.request(self.target, "read")
        self.assertEqual(events[:2], ["restart 4242", "close"])
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["helper"]["retried"])
        self.assertIn(["shell", "kill", "4242"], self.calls())

    def test_a_restart_that_cannot_write_the_record_is_unavailable(self) -> None:
        self.ensure().close()
        self.helper.drop_after = {"read": 1}
        real_lock = helper_mod._device_lock
        locks: list[str] = []

        def restart_lock_fails(serial, env):
            locks.append(serial)
            if len(locks) == 2:  # the first is ensure's, the second the restart's
                raise OSError(13, "Permission denied")
            return real_lock(serial, env)

        with mock.patch.object(helper_mod, "_device_lock", side_effect=restart_lock_fails):
            with self.assertRaises(helper_mod.HelperUnavailable) as caught:
                helper_mod.request(self.target, "read")
        self.assertIn("not answered", caught.exception.reason)
        self.assertIn("cannot be written", caught.exception.reason)
        self.assertEqual(len(self.helper.ops("read")), 1)

    def test_hello_and_quit_are_the_client_s_own(self) -> None:
        for op in ("hello", "quit"):
            with self.assertRaises(ValueError):
                helper_mod.request(self.target, op)


class RestartTests(HelperCase):
    """`restart` ends only the helper that failed, never one the record now
    names for another caller."""

    def test_only_the_pid_the_record_still_names_is_killed(self) -> None:
        self.running_helper(pid=100)
        self.clear_calls()
        self.assertEqual(helper_mod.restart(self.target, 99),
                         {"pid": 99, "killed": False, "skipped": "record_changed",
                          "record_pid": 100})
        self.assertEqual(helper_mod.restart(self.target, None),
                         {"pid": None, "killed": False, "skipped": "pid_unknown"})
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.record()["pid"], 100)
        self.assertEqual(helper_mod.restart(self.target, 100), {"pid": 100, "killed": True})
        self.assertEqual(self.calls(), [["shell", "cat", "/proc/100/cmdline"],
                                        ["shell", "kill", "100"]])
        self.assertNotIn("100", self.device()["helper_procs"])
        # the forward stays for the next start to reuse
        self.assertNotIn("pid", self.record())
        self.assertEqual(self.record()["port"], self.helper.port)
        self.assertEqual(len(self.device()["forwards"]), 1)

    def test_a_recorded_pid_that_is_no_longer_the_helper_is_not_killed(self) -> None:
        self.running_helper(pid=100)
        self.update_device(helper_running=None, helper_procs={"100": "com.example.app\0"})
        result = helper_mod.restart(self.target, 100)
        self.assertEqual(result, {"pid": 100, "killed": False, "verification": "not_running"})
        self.assertNotIn(["shell", "kill", "100"], self.calls())

    def test_no_record_nothing_is_killed(self) -> None:
        self.update_device(helper_procs={"100": "autonom-helper\0x\0"})
        self.assertEqual(helper_mod.restart(self.target, 100),
                         {"pid": 100, "killed": False, "skipped": "no_record"})
        self.assertEqual(self.calls(), [])
        self.assertIn("100", self.device()["helper_procs"])


# --- stop and status (AH-07) ----------------------------------------------------------

class StopTests(HelperCase):
    DECOYS = [f"{SERIAL} tcp:7001 tcp:8001", "emulator-5580 tcp:7002 localabstract:scrcpy"]

    def test_stop_quits_and_removes_only_its_own_forward(self) -> None:
        self.running_helper(pid=100)
        self.update_device(forwards=self.device()["forwards"] + self.DECOYS)
        self.clear_calls()
        result = helper_mod.stop(self.target)
        self.assertTrue(result["stopped"])
        self.assertTrue(result["was_running"])
        self.assertTrue(result["quit"])
        self.assertFalse(result["killed"])
        self.assertTrue(result["forward_removed"])
        self.assertEqual((result["pid"], result["port"], result["build"]),
                         (100, self.helper.port, BUILD))
        self.assertEqual([item["op"] for item in self.helper.ops()], ["hello", "quit"])
        calls = self.calls()
        removals = [call for call in calls if call[:2] == ["forward", "--remove"]]
        self.assertEqual(removals, [["forward", "--remove", f"tcp:{self.helper.port}"]])
        self.assertFalse(any("--remove-all" in call for call in calls))
        self.assertEqual(self.device()["forwards"], self.DECOYS)
        self.assertFalse(helper_mod.has_record(SERIAL))

    def test_a_reused_pid_is_never_killed(self) -> None:
        self.running_helper(pid=100)
        # the helper died and pid 100 now runs an app
        self.update_device(helper_running=None, helper_procs={"100": "com.example.app\0"})
        result = helper_mod.stop(self.target)
        self.assertFalse(result["killed"])
        self.assertEqual(result["kill_verification"], "not_running")
        self.assertFalse(result["stopped"])
        self.assertIn(["shell", "cat", "/proc/100/cmdline"], self.calls())
        self.assertNotIn(["shell", "kill", "100"], self.calls())
        self.assertIn("100", self.device()["helper_procs"])

    def test_a_helper_that_does_not_quit_is_killed_after_its_check(self) -> None:
        self.running_helper(pid=100)
        self.helper.drop_after = {"quit": 1}
        result = helper_mod.stop(self.target)
        self.assertTrue(result["killed"])
        self.assertTrue(result["stopped"])
        calls = self.calls()
        self.assertLess(calls.index(["shell", "cat", "/proc/100/cmdline"]),
                        calls.index(["shell", "kill", "100"]))

    def test_no_record_no_device_call(self) -> None:
        result = helper_mod.stop(self.target)
        self.assertEqual(result, {"stopped": False, "was_running": False, "pid": None,
                                  "port": None, "build": None})
        self.assertEqual(self.calls(), [])

    def test_a_forward_no_longer_listed_is_not_removed(self) -> None:
        self.running_helper(pid=100)
        self.update_device(forwards=self.DECOYS)
        result = helper_mod.stop(self.target)
        self.assertFalse(result["forward_removed"])
        self.assertFalse(any(call[:2] == ["forward", "--remove"] for call in self.calls()))
        self.assertEqual(self.device()["forwards"], self.DECOYS)

    def test_stop_keeps_a_remembered_failure(self) -> None:
        self.running_helper(pid=100)
        record = self.record()
        record["unavailable"] = {"code": "uiautomation_busy", "reason": "r", "hint": "h",
                                 "until": time.time() + 30}
        self.write_record(**{key: value for key, value in record.items() if key != "serial"})
        helper_mod.stop(self.target)
        self.assertEqual(self.record(), {"serial": SERIAL, "unavailable": record["unavailable"]})

    def test_forget_drops_the_record_without_a_device_call(self) -> None:
        self.running_helper(pid=100)
        self.assertTrue(helper_mod.forget(SERIAL))
        self.assertFalse(helper_mod.has_record(SERIAL))
        self.assertEqual(self.calls(), [])
        self.assertFalse(helper_mod.forget(SERIAL))


class UnverifiedPidTests(HelperCase):
    """INV-3 when adb cannot tell (a failed or timed-out `cat
    /proc/<pid>/cmdline`): `restart`, the end of a stuck helper and `stop`
    kill nothing, keep the pid in the device record and fail, so the caller
    fails or falls back; a later call, with adb answering, ends that pid."""

    HOW = ("exit", "timeout")

    @contextlib.contextmanager
    def cmdline_fails_once(self, pid: int, how: str):
        """The next `adb shell cat /proc/<pid>/cmdline` fails: ``exit`` makes
        the fake adb exit 1 printing adb's "error: device offline", ``timeout``
        makes the call time out; every other call (and the next ones) is
        answered as usual. Yields the list of failures injected."""
        real = helper_mod.adb_mod.run_adb
        failed: list[str] = []

        def run_adb(adb, args, **kwargs):
            if not failed and list(args) == ["shell", "cat", f"/proc/{pid}/cmdline"]:
                failed.append(how)
                if how == "timeout":
                    raise subprocess.TimeoutExpired([adb, *args], kwargs.get("timeout"))
                self.update_device(fail={f"-s {SERIAL} shell cat /proc/{pid}/cmdline":
                                         [1, "error: device offline"]})
                try:
                    return real(adb, args, **kwargs)
                finally:
                    state = self.device()
                    state.pop("fail", None)
                    self.state.write_text(json.dumps(state), encoding="utf-8")
            return real(adb, args, **kwargs)

        with mock.patch.object(helper_mod.adb_mod, "run_adb", side_effect=run_adb):
            yield failed

    def kills(self) -> list[list[str]]:
        return [call for call in self.calls() if self.shell_words(call)[:1] == ["kill"]]

    def test_the_cmdline_check_tells_a_failed_call_from_a_gone_pid(self) -> None:
        self.update_device(helper_procs={"100": "autonom-helper\0x\0",
                                         "200": "com.example.app\0"})
        self.assertIs(helper_mod._cmdline_is_helper(self.target, 100), True)  # noqa: SLF001
        self.assertIs(helper_mod._cmdline_is_helper(self.target, 200), False)  # noqa: SLF001
        self.assertIs(helper_mod._cmdline_is_helper(self.target, 300), False,  # noqa: SLF001
                      "No such file or directory: the device answered, nothing runs")
        for how in self.HOW:
            with self.subTest(how=how), self.cmdline_fails_once(100, how) as failed:
                self.assertIsNone(helper_mod._cmdline_is_helper(self.target, 100))  # noqa: SLF001
                self.assertEqual(failed, [how])
        self.assertIs(helper_mod._cmdline_is_helper(self.target, 100), True)  # noqa: SLF001

    def test_restart_keeps_the_pid_when_adb_cannot_tell(self) -> None:
        for how in self.HOW:
            with self.subTest(how=how):
                self.running_helper(pid=100)
                self.clear_calls()
                with self.cmdline_fails_once(100, how) as failed, \
                        self.assertRaises(errors.AutonomError) as caught:
                    helper_mod.restart(self.target, 100)
                self.assertEqual(failed, [how])
                self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
                self.assertIn("pid 100", caught.exception.message)
                self.assertEqual(self.kills(), [], "nothing killed")
                self.assertEqual(self.record()["pid"], 100, "the record still names it")
                self.assertIn("100", self.device()["helper_procs"])
                # adb answers again: the pid the record kept is ended
                self.assertEqual(helper_mod.restart(self.target, 100),
                                 {"pid": 100, "killed": True})
                self.assertNotIn("100", self.device()["helper_procs"])

    def test_an_unanswered_read_whose_restart_cannot_check_the_pid_is_unavailable(self) -> None:
        for how in self.HOW:
            with self.subTest(how=how):
                self.update_device(helper_procs={}, helper_running=None, helper_starts=[],
                                   forwards=[])
                helper_mod.forget(SERIAL)
                self.ensure().close()
                pid = self.record()["pid"]
                self.helper.drop_after = {"read": 1}
                self.clear_calls()
                with self.cmdline_fails_once(pid, how), \
                        self.assertRaises(helper_mod.HelperUnavailable) as caught:
                    helper_mod.request(self.target, "read")
                self.assertIn("could not be restarted", caught.exception.reason)
                self.assertEqual(self.kills(), [])
                self.assertEqual(len(self.starts()), 1, "no second helper beside it")
                self.assertEqual(self.record()["pid"], pid)
                self.assertIn(str(pid), self.device()["helper_procs"])
                self.helper.drop_after = {}

    def test_a_stuck_helper_adb_cannot_check_is_neither_killed_nor_replaced(self) -> None:
        for how in self.HOW:
            with self.subTest(how=how):
                self.running_helper(pid=100)
                port = self.record()["port"]
                self.helper.hung_hellos = 1
                self.clear_calls()
                with self.cmdline_fails_once(100, how) as failed, \
                        self.assertRaises(helper_mod.HelperUnavailable) as caught:
                    helper_mod.request(self.target, "read")
                self.assertEqual(failed, [how])
                self.assertIn("pid 100", caught.exception.reason)
                self.assertEqual(self.kills(), [])
                self.assertFalse(any(call[:2] == ["forward", "--remove"]
                                     for call in self.calls()), "its forward stays")
                self.assertEqual(self.starts(), [], "no second helper beside it")
                self.assertEqual((self.record()["pid"], self.record()["port"]), (100, port))
                self.assertEqual(self.helper.ops("read"), [])
                # adb answers again and the helper is still stuck: the
                # recorded pid is ended and one helper started (AH-05)
                self.helper.hung_hellos = 1
                self.ensure().close()
                self.assertIn(["shell", "kill", "100"], self.calls())
                self.assertEqual(len(self.starts()), 1)
                helper_mod.stop(self.target)
                self.update_device(helper_starts=[])

    def test_stop_keeps_the_pid_when_adb_cannot_tell(self) -> None:
        for how in self.HOW:
            with self.subTest(how=how):
                self.running_helper(pid=100)
                port = self.record()["port"]
                self.helper.hung_hellos = 1  # stuck: the kill needs the check
                self.clear_calls()
                with self.cmdline_fails_once(100, how) as failed, \
                        self.assertRaises(errors.AutonomError) as caught:
                    helper_mod.stop(self.target)
                self.assertEqual(failed, [how])
                self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
                self.assertEqual(self.kills(), [])
                self.assertFalse(any(call[:2] == ["forward", "--remove"]
                                     for call in self.calls()))
                self.assertEqual((self.record()["pid"], self.record()["port"]), (100, port))
                self.assertIn("100", self.device()["helper_procs"])
                # adb answers again: the next stop ends it
                result = helper_mod.stop(self.target)
                self.assertTrue(result["stopped"], result)
                self.assertNotIn("100", self.device()["helper_procs"])
                self.assertFalse(helper_mod.has_record(SERIAL))

    def test_ui_helper_stop_reports_the_failure(self) -> None:
        self.running_helper(pid=100)
        self.helper.hung_hellos = 1
        self.update_device(fail={f"-s {SERIAL} shell cat /proc/100/cmdline":
                                 [1, "error: device offline"]})
        completed = subprocess.run(
            [sys.executable, str(CLI), "--adb", str(FAKE_ADB), "--serial", SERIAL,
             "ui", "helper", "stop"],
            cwd=self.root, env=dict(os.environ), text=True, stdin=subprocess.DEVNULL,
            capture_output=True, check=False, timeout=120)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        envelope = json.loads(completed.stdout if completed.stdout.strip() else completed.stderr)
        self.assertEqual((completed.returncode, envelope["error_code"]),
                         (2, errors.BACKEND_FAILED), envelope)
        self.assertIn("pid 100", envelope["error"])
        self.assertEqual(self.kills(), [])
        self.assertEqual(self.record()["pid"], 100)

    # Fix round 5: the replacement of a dead helper or one of another build
    # (`_retire`) and the cleanup after a failed start or hello
    # (`_cleanup_after_failure`) keep a pid adb cannot check, too.

    @contextlib.contextmanager
    def cmdline_fails(self, pid: int, how: str):
        """Every `adb shell cat /proc/<pid>/cmdline` in the block fails:
        ``exit`` as adb exiting 1 with "error: device offline", ``timeout``
        as a call that does not finish. Every other call is answered as
        usual. Yields the failures injected."""
        real = helper_mod.adb_mod.run_adb
        failed: list[str] = []

        def run_adb(adb, args, **kwargs):
            if list(args) == ["shell", "cat", f"/proc/{pid}/cmdline"]:
                failed.append(how)
                if how == "timeout":
                    raise subprocess.TimeoutExpired([adb, *args], kwargs.get("timeout"))
                return subprocess.CompletedProcess([adb, *args], 1, "error: device offline\n")
            return real(adb, args, **kwargs)

        with mock.patch.object(helper_mod.adb_mod, "run_adb", side_effect=run_adb):
            yield failed

    def fresh_device(self) -> None:
        """No helper, no forward, no record: the next case starts clean."""
        self.update_device(helper_procs={}, helper_running=None, helper_starts=[],
                           forwards=[], fail={})
        helper_mod.forget(SERIAL)
        helper_mod.reset_forward_cache()  # as a new CLI invocation does
        self.helper.drop_after = {}
        self.clear_calls()

    def test_a_helper_of_another_build_adb_cannot_check_is_neither_killed_nor_replaced(self
                                                                                      ) -> None:
        """The record names pid 77 of another build, its quit is ignored and
        `cat /proc/77/cmdline` keeps failing: at 808a7b7 ensure() started
        another helper and the record no longer named 77."""
        for how in self.HOW:
            with self.subTest(how=how):
                self.fresh_device()
                self.running_helper(pid=77, build=OLD_BUILD)
                port = self.record()["port"]
                self.helper.drop_after = {"quit": 1}  # it reads the quit and keeps running
                with mock.patch.object(helper_mod, "QUIT_EXIT_WAIT_S", 0.3), \
                        self.cmdline_fails(77, how) as failed, \
                        self.assertRaises(helper_mod.HelperUnavailable) as caught:
                    helper_mod.ensure(self.target)
                self.assertTrue(failed and set(failed) == {how}, failed)
                self.assertIn("pid 77", caught.exception.reason)
                self.assertIn("the device record keeps the pid", caught.exception.reason)
                self.assertEqual(self.kills(), [])
                self.assertEqual(self.starts(), [], "no helper started beside it")
                self.assertEqual((self.record()["pid"], self.record()["port"]), (77, port))
                self.assertIn("77", self.device()["helper_procs"])
                # adb answers again: quit, gone, and the expected build started once
                with mock.patch.object(helper_mod, "QUIT_EXIT_WAIT_S", 0.3):
                    helper = self.ensure()
                self.assertEqual((helper.build, helper.started, helper.replaced),
                                 (BUILD, True, True))
                self.assertNotIn("77", self.device()["helper_procs"])
                self.assertEqual(len(self.starts()), 1)
                helper.close()
                helper_mod.stop(self.target)

    def test_a_dead_helper_adb_cannot_check_is_not_replaced(self) -> None:
        for how in self.HOW:
            with self.subTest(how=how):
                self.fresh_device()
                self.running_helper(pid=100)
                self.helper.eof_on_hello = 1  # its connection ends at once
                with self.cmdline_fails(100, how), \
                        self.assertRaises(helper_mod.HelperUnavailable) as caught:
                    helper_mod.ensure(self.target)
                self.assertIn("pid 100", caught.exception.reason)
                self.assertEqual(self.kills(), [])
                self.assertEqual(self.starts(), [], "no helper started beside it")
                self.assertEqual(self.record()["pid"], 100)
                self.helper.eof_on_hello = 0

    def test_a_start_that_cannot_check_another_build_on_the_socket_launches_once(self) -> None:
        """A helper of another build that ran before the launch holds the
        socket, ignores its quit, and adb cannot check its pid: the start
        ends there; at 808a7b7 it launched the expected build a second time."""
        for how in self.HOW:
            with self.subTest(how=how):
                self.fresh_device()
                self.update_device(helper_procs={"77": "autonom-helper\0x\0"},
                                   helper_running={"pid": 77, "build": OLD_BUILD})
                self.helper.drop_after = {"quit": 1}
                with mock.patch.object(helper_mod, "QUIT_EXIT_WAIT_S", 0.3), \
                        self.cmdline_fails(77, how), \
                        self.assertRaises(helper_mod.HelperUnavailable) as caught:
                    helper_mod.ensure(self.target)
                self.assertIn("pid 77", caught.exception.reason)
                # it is not this record's: the message claims no record of it
                self.assertNotIn("the device record keeps the pid", caught.exception.reason)
                self.assertEqual([start.get("exited") for start in self.starts()],
                                 ["socket_in_use"], "one launch, no second")
                self.assertEqual(self.kills(), [])
                self.assertIn("77", self.device()["helper_procs"])

    def test_a_busy_recorded_helper_adb_cannot_check_stays_in_the_record(self) -> None:
        for how in self.HOW:
            with self.subTest(how=how):
                self.fresh_device()
                self.running_helper(pid=100)
                port = self.record()["port"]
                self.helper.busy_hellos = 1  # uiautomation_busy, then it exits
                with self.cmdline_fails(100, how) as failed, \
                        self.assertRaises(helper_mod.HelperUnavailable) as caught:
                    helper_mod.ensure(self.target)
                self.assertEqual(failed, [how])
                self.assertIn("uiautomation_busy", caught.exception.reason)
                record = self.record()
                self.assertEqual((record.get("pid"), record.get("port")), (100, port),
                                 "the pid adb could not check is still named, with its forward")
                self.assertEqual(record["unavailable"]["code"], "uiautomation_busy")
                self.assertEqual(self.kills(), [])
                self.assertFalse(any(call[:2] == ["forward", "--remove"]
                                     for call in self.calls()))
                self.assertEqual(self.starts(), [])
                # adb answers again: a stop checks the pid (gone) and clears it
                result = helper_mod.stop(self.target)
                self.assertEqual(result.get("kill_verification"), "not_running", result)
                self.assertEqual(self.record(), {"serial": SERIAL,
                                                 "unavailable": record["unavailable"]})
                self.assertEqual(self.device()["forwards"], [])

    def test_a_launched_helper_adb_cannot_check_after_a_start_timeout_stays_named(self) -> None:
        for how in self.HOW:
            with self.subTest(how=how):
                self.fresh_device()
                pid = self.next_pid()
                self.helper.eof_on_hello = 10_000
                with mock.patch.object(helper_mod, "START_BUDGET_S", 0.6), \
                        self.cmdline_fails(pid, how), \
                        self.assertRaises(helper_mod.HelperUnavailable) as caught:
                    helper_mod.ensure(self.target)
                self.assertEqual([start["pid"] for start in self.starts()], [pid])
                self.assertIn("did not answer hello", caught.exception.reason)
                record = self.record()
                self.assertEqual((record.get("pid"), record.get("port")),
                                 (pid, self.helper.port))
                self.assertEqual(record["unavailable"]["code"], "start_timeout")
                self.assertIn(str(pid), self.device()["helper_procs"])
                self.assertEqual(self.kills(), [])
                self.assertEqual(len(self.device()["forwards"]), 1, "its forward stays")
                # adb answers again: the stop ends the pid it kept
                result = helper_mod.stop(self.target)
                self.assertTrue(result["killed"], result)
                self.assertNotIn(str(pid), self.device()["helper_procs"])
                self.assertEqual(self.record(), {"serial": SERIAL,
                                                 "unavailable": record["unavailable"]})
                self.helper.eof_on_hello = 0

    def next_pid(self) -> int:
        """The pid the fake adb gives the next helper start."""
        return int(self.device().get("helper_next_pid", 4242))

    def test_a_launched_helper_adb_cannot_check_without_a_forward_is_checked_first(self) -> None:
        for how in self.HOW:
            with self.subTest(how=how):
                self.fresh_device()
                pid = self.next_pid()
                self.update_device(fail={f"-s {SERIAL} forward tcp:0":
                                         [1, "adb: error: cannot bind listener"]})
                with self.cmdline_fails(pid, how), \
                        self.assertRaises(helper_mod.HelperUnavailable) as caught:
                    helper_mod.ensure(self.target)
                self.assertIn("adb forward did not give a port", caught.exception.reason)
                self.assertEqual(self.record(), {"serial": SERIAL, "pid": pid})
                self.assertIn(str(pid), self.device()["helper_procs"])
                self.assertEqual(self.kills(), [])
                # the forward works again, adb still cannot check the pid:
                # nothing is started beside it
                self.update_device(fail={})
                with self.cmdline_fails(pid, how), \
                        self.assertRaises(helper_mod.HelperUnavailable) as caught:
                    helper_mod.ensure(self.target)
                self.assertIn(f"pid {pid}", caught.exception.reason)
                self.assertEqual(len(self.starts()), 1, "no second helper beside it")
                self.assertEqual(self.record(), {"serial": SERIAL, "pid": pid})
                self.assertEqual(self.kills(), [])
                # adb answers again: that pid is ended, then one helper started
                helper = self.ensure()
                self.assertIn(["shell", "kill", str(pid)], self.calls())
                self.assertEqual((helper.pid, len(self.starts())), (pid + 1, 2))
                self.assertEqual(self.record()["pid"], pid + 1)
                helper.close()
                helper_mod.stop(self.target)


class GoneTextTests(HelperCase):
    """`/proc/<pid>/cmdline` of a pid that is gone, in the words of the
    device's `cat` and shell: "gone" (False) whatever the wording; adb's own
    failure stays unknown (None)."""

    GONE = (
        "cat: /proc/300/cmdline: No such file or directory",              # toybox
        "cat: can't open '/proc/300/cmdline': No such file or directory",  # busybox
        "/proc/300/cmdline: No such file or directory",                    # toolbox
        "/system/bin/sh: cat: /proc/300/cmdline: No such file or directory",  # mksh
        "sh: can't open '/proc/300/cmdline': No such file or directory",
        "cat: /proc/300/cmdline: No such process",
        "cat: read error: No such process",
    )
    ADB_FAILURES = (
        "error: device offline",
        "error: device 'emulator-5612' not found",
        "error: no devices/emulators found",
        "error: closed",
        "adb: device offline",
        "adb: error: failed to get feature set: device offline",
        "adb: error: cannot stat '/proc/300/cmdline': No such file or directory",
        "error: protocol fault: No such file or directory",
        "* daemon not running; starting now at tcp:5037\n* daemon started successfully\n"
        "error: device offline",
        "cat: /proc/300/cmdline: Permission denied",
    )

    def cat_prints(self, pid: int, text: str) -> None:
        self.update_device(fail={f"-s {SERIAL} shell cat /proc/{pid}/cmdline": [1, text]})

    def test_every_wording_of_a_gone_pid_is_gone(self) -> None:
        for text in self.GONE:
            with self.subTest(text=text):
                self.cat_prints(300, text)
                self.assertIs(helper_mod._cmdline_is_helper(self.target, 300), False)  # noqa: SLF001

    def test_adb_s_own_failures_stay_unknown(self) -> None:
        for text in self.ADB_FAILURES:
            with self.subTest(text=text):
                self.cat_prints(300, text)
                self.assertIsNone(helper_mod._cmdline_is_helper(self.target, 300))  # noqa: SLF001

    def test_an_adb_that_cannot_run_is_unknown(self) -> None:
        missing = Target("android", SERIAL, str(self.root / "no-adb"), {"serial": SERIAL})
        self.assertIsNone(helper_mod._cmdline_is_helper(missing, 300))  # noqa: SLF001
        with mock.patch.object(helper_mod.adb_mod, "run_adb",
                               side_effect=PermissionError(13, "Permission denied")):
            self.assertIsNone(helper_mod._cmdline_is_helper(self.target, 300))  # noqa: SLF001

    def test_stop_ends_a_helper_whatever_its_cat_says_when_it_is_gone(self) -> None:
        """At 808a7b7 a busybox-style `cat` made every stop wait and fail
        with backend_failed, and the record kept the pid."""
        for text in self.GONE:
            with self.subTest(text=text):
                helper_mod.reset_forward_cache()  # each stop is a new CLI invocation
                self.running_helper(pid=300)
                self.cat_prints(300, text)
                result = helper_mod.stop(self.target)
                self.assertTrue(result["stopped"] and result["quit"], result)
                self.assertFalse(result["killed"])
                self.assertFalse(helper_mod.has_record(SERIAL))
                self.assertNotIn("300", self.device().get("helper_procs", {}))


class StatusTests(HelperCase):
    def test_a_running_helper(self) -> None:
        self.running_helper(pid=100)
        record = self.record()
        record["last_request_at"] = time.time() - 5
        self.write_record(**{key: value for key, value in record.items() if key != "serial"})
        result = helper_mod.status(self.target)
        self.assertTrue(result["running"])
        self.assertEqual((result["build"], result["pid"], result["port"]),
                         (BUILD, 100, self.helper.port))
        self.assertGreaterEqual(result["idle_ms"], 5000)
        self.assertLess(result["idle_ms"], 60000)
        self.assertEqual(result["idle_limit_ms"], 120000)
        self.assertEqual(result["protocol"], 1)
        self.assertNotIn("other_pids", result)
        self.assertEqual(self.starts(), [])
        self.assertGreater(self.record()["last_request_at"], record["last_request_at"])

    def test_nothing_recorded_and_an_unknown_helper_on_the_device(self) -> None:
        self.update_device(helper_procs={"55": "autonom-helper\0x\0", "56": "com.app\0"})
        result = helper_mod.status(self.target)
        self.assertEqual({key: result[key] for key in ("running", "build", "pid", "port",
                                                        "idle_ms")},
                         {"running": False, "build": None, "pid": None, "port": None,
                          "idle_ms": None})
        self.assertEqual(result["other_pids"], [55])
        self.assertEqual(self.calls(), [["shell", "pidof", "autonom-helper"]])
        self.assertEqual(self.helper.ops(), [])

    def test_a_recorded_helper_that_does_not_answer(self) -> None:
        self.running_helper(pid=100)
        self.update_device(helper_running=None, helper_procs={})
        result = helper_mod.status(self.target)
        self.assertFalse(result["running"])
        self.assertFalse(result["answering"])
        self.assertEqual(result["pid"], 100)
        self.assertEqual(self.starts(), [])

    def test_a_remembered_failure_is_reported(self) -> None:
        self.write_record(unavailable={"code": "start_timeout", "reason": "no hello",
                                       "hint": "h", "boot_id": "b", "at": "2026-10-24T00:00:00Z"})
        result = helper_mod.status(self.target)
        self.assertEqual(result["unavailable"], {"code": "start_timeout", "reason": "no hello",
                                                 "at": "2026-10-24T00:00:00Z"})


# --- the CLI: ui helper and session stop -----------------------------------------------

class CliTests(HelperCase):
    def setUp(self) -> None:
        super().setUp()
        self.env = dict(os.environ)

    def cli(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--adb", str(FAKE_ADB), "--serial", SERIAL, *argv],
            cwd=self.root, env=self.env, text=True, stdin=subprocess.DEVNULL,
            capture_output=True, check=False, timeout=120)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.stdout.strip() else completed.stderr
        return completed.returncode, json.loads(stream)

    def test_ui_helper_status_and_stop(self) -> None:
        self.running_helper(pid=100)
        code, payload = self.cli("ui", "helper", "status")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["running"])
        self.assertEqual((payload["pid"], payload["build"], payload["serial"]),
                         (100, BUILD, SERIAL))
        code, payload = self.cli("ui", "helper", "stop")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["stopped"])
        self.assertTrue(payload["forward_removed"])
        self.assertFalse(helper_mod.has_record(SERIAL))
        code, payload = self.cli("ui", "helper", "status")
        self.assertEqual(code, 0, payload)
        self.assertFalse(payload["running"])

    def test_ui_helper_refuses_ios(self) -> None:
        cli = __import__("autonom")
        target = Target("ios", "UDID-1", "xcrun", {"udid": "UDID-1"})
        out = io.StringIO()
        with mock.patch.object(cli, "_target", return_value=target), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(errors.AutonomError) as caught:
                cli.cmd_ui_helper(argparse.Namespace(action="status"))
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_ON_PLATFORM)

    def start_session(self) -> None:
        code, payload = self.cli("session", "start", "--app-id", "com.example.app")
        self.assertEqual(code, 0, payload)

    def helper_calls(self) -> list[list[str]]:
        return [call for call in self.calls()
                if call[:1] == ["forward"] or self.shell_words(call)[:1] in (["kill"], ["pidof"])
                or "/cmdline" in " ".join(call)]

    def test_session_stop_without_a_helper_record_sends_nothing_for_it(self) -> None:
        self.start_session()
        self.clear_calls()
        code, payload = self.cli("session", "stop")
        self.assertEqual(code, 0, payload)
        step = next(item for item in payload["teardown"] if item["action"] == "android_helper")
        self.assertEqual(step, {"action": "android_helper", "ok": True,
                                "detail": {"was_running": False}})
        self.assertEqual(self.helper_calls(), [])
        self.assertEqual(self.helper.ops(), [])
        self.assertNotIn("warnings", payload)

    def test_session_stop_stops_the_helper_after_the_network_steps(self) -> None:
        self.start_session()
        self.ensure().close()
        decoy = "emulator-5580 tcp:7002 localabstract:scrcpy"
        self.update_device(forwards=self.device()["forwards"] + [decoy])
        self.clear_calls()
        code, payload = self.cli("session", "stop")
        self.assertEqual(code, 0, payload)
        actions = [item["action"] for item in payload["teardown"]]
        self.assertGreater(actions.index("android_helper"), actions.index("network_stop"))
        step = payload["teardown"][actions.index("android_helper")]
        self.assertTrue(step["ok"], step)
        self.assertTrue(step["detail"]["stopped"])
        self.assertTrue(step["detail"]["quit"])
        self.assertEqual([call for call in self.calls() if call[:2] == ["forward", "--remove"]],
                         [["forward", "--remove", f"tcp:{self.helper.port}"]])
        self.assertEqual(self.device()["forwards"], [decoy])
        self.assertEqual(self.helper.ops("quit")[-1]["op"], "quit")
        self.assertFalse(helper_mod.has_record(SERIAL))
        self.assertNotIn("warnings", payload)


# --- the test harness default (INV-5) ---------------------------------------------------

class HarnessDefaultTests(unittest.TestCase):
    def test_parallel_workers_get_uiautomator_unless_set(self) -> None:
        import run_parallel
        env = run_parallel.worker_env("/tmp/h", {"PATH": "/bin"})
        self.assertEqual(env["AUTONOM_ANDROID_UI_BACKEND"], "uiautomator")
        self.assertEqual(env["AUTONOM_HOME"], "/tmp/h")
        kept = run_parallel.worker_env("/tmp/h", {"AUTONOM_ANDROID_UI_BACKEND": "helper"})
        self.assertEqual(kept["AUTONOM_ANDROID_UI_BACKEND"], "helper")

    def test_the_package_initializer_sets_the_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            runpy.run_path(str(ROOT / "tests/__init__.py"))
            self.assertEqual(os.environ["AUTONOM_ANDROID_UI_BACKEND"], "uiautomator")
        with mock.patch.dict(os.environ, {"AUTONOM_ANDROID_UI_BACKEND": "auto"}, clear=True):
            runpy.run_path(str(ROOT / "tests/__init__.py"))
            self.assertEqual(os.environ["AUTONOM_ANDROID_UI_BACKEND"], "auto")


if __name__ == "__main__":
    unittest.main()
