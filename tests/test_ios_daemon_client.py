"""The CLI's side of the iOS device daemon (ID-06, ID-07, ID-08, ID-10, ID-16).

`ios_idb.run_idb` serves six `idb ui` subcommands through the simulator's
device daemon and keeps everything else on the idb process. The oracles are
two logs: the fake idb's argv log (what reached the idb process) and the fake
daemon's request log (`tests/fakes/fake_iosd.py`, what reached the daemon). A
case counts both, and checks the reply's `transport` / `transport_reason`.

The test harness turns the daemon off by default (`AUTONOM_IOS_DAEMON=off`);
every case here turns it on and points the CLI at a fake daemon, either one
already serving (its registry in the temporary AUTONOM_HOME) or one the CLI
starts itself through `AUTONOM_NODE`. The CLI subprocesses get a PATH with
only a `python3` link and the system directories, so neither a real idb nor
a real node can be reached.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_IDB = ROOT / "tests/fakes/fake_idb.py"
FAKE_IOSD = ROOT / "tests/fakes/fake_iosd.py"
DAEMON_MJS = ROOT / "plugins/autonom/skills/android-emulator-browser/scripts/ios-device-daemon.mjs"
IOS_FIXTURE = ROOT / "tests/fixtures/idb_describe_all_sample.json"
UDID = "DDDDDDDD-1111-2222-3333-EEEEEEEEEEEE"
BUNDLE = "com.example.app"
BOOTED = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
    {"udid": UDID, "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}]}}
TRANSPORT = ("transport", "transport_reason", "ax_backend")

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, ios_daemon, ios_idb, processes, session  # noqa: E402
from autonom_lib.platform import IOS, Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


def _verb(argv: list[str]) -> list[str]:
    rest = list(argv)
    while rest and rest[0] in ("--companion", "--companion-path", "--log", "--compression"):
        rest = rest[2:]
    return rest


def _wait_until(predicate: Any, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class DaemonCase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.iosd_log = self.root / "iosd.jsonl"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.set_env(
            AUTONOM_FAKE_STATE=str(self.state), AUTONOM_FAKE_LOG=str(self.log),
            AUTONOM_HOME=str(self.home), AUTONOM_IOS_DAEMON="on", AUTONOM_NODE=None,
            AUTONOM_FAKE_IOSD_CONFIG=None,
            AUTONOM_ADB=None, AUTONOM_SIMCTL=None, AUTONOM_IDB=None,
            AUTONOM_AXE=None, AUTONOM_IOS_HID="idb", AUTONOM_IDB_COMPANION=None,
            AUTONOM_IDB_STATE_FILE=str(self.root / "idb-state.json"), AUTONOM_UI_BACKEND=None,
        )
        self.env = dict(os.environ)
        self.env["PATH"] = os.pathsep.join([str(self.bin), "/usr/bin", "/bin"])
        self.set_state()
        ios_daemon.reset()
        ios_idb.reset_transport()
        self.addCleanup(ios_daemon.reset)
        self.addCleanup(ios_idb.reset_transport)

    # --- fakes ----------------------------------------------------------------------

    def set_state(self, **kwargs: Any) -> None:
        kwargs.setdefault("simctl_devices", BOOTED)
        kwargs.setdefault("idb_describe_all", str(IOS_FIXTURE))
        self.state.write_text(json.dumps(kwargs), encoding="utf-8")

    def config(self, **config: Any) -> Path:
        config.setdefault("log", str(self.iosd_log))
        config.setdefault("tree_file", str(IOS_FIXTURE))
        config.setdefault("max_seconds", 60)
        path = self.root / "iosd-config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def start_daemon(self, **config: Any) -> dict[str, Any]:
        """A fake daemon already serving U, found through its registry."""
        process = subprocess.Popen(
            [sys.executable, str(FAKE_IOSD), "serve", "--udid", UDID,
             "--state-base", os.path.abspath(str(self.home)), "--config", str(self.config(**config))],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, start_new_session=True)

        def stop() -> None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()

        self.addCleanup(stop)
        status = json.loads(process.stdout.readline())
        self.assertEqual(status["status"], config.get("status", "serving"))
        return status

    def ios(self, *argv: str, env: dict[str, str] | None = None) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB),
             "--udid", UDID, *argv],
            capture_output=True, text=True, env=env or self.env, timeout=120, cwd=str(self.root),
        )
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.returncode in (0, 1) else completed.stderr
        return completed.returncode, json.loads(stream.strip().splitlines()[-1])

    def idb_calls(self, *verb: str) -> list[list[str]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        calls = [_verb(row["argv"]) for row in rows if row["tool"] == "idb"]
        return [argv for argv in calls if argv[:len(verb)] == list(verb)]

    def daemon_requests(self, op: str | None = None) -> list[dict[str, Any]]:
        if not self.iosd_log.exists():
            return []
        rows = [json.loads(line) for line in self.iosd_log.read_text(encoding="utf-8").splitlines()]
        return [row for row in rows if op is None or row.get("op") == op]


# --- the CLI's start: modules few verbs need are imported on first use ------------------


class CliStartTests(unittest.TestCase):
    LAZY = ("autonom_lib.flow.executor", "autonom_lib.flow.compiler", "autonom_lib.flow.canonical",
            "autonom_lib.flow.validator", "autonom_lib.emulator", "autonom_lib.teach",
            "autonom_lib.app_skills", "autonom_lib.proof")

    def run_python(self, body: str) -> dict[str, Any]:
        script = (
            "import importlib.util, json, sys\n"
            f"spec = importlib.util.spec_from_file_location('autonom_cli', {str(CLI)!r})\n"
            "cli = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(cli)\n" + body)
        completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                   timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_loading_the_cli_imports_none_of_the_lazy_modules(self) -> None:
        result = self.run_python(
            f"print(json.dumps([name for name in {self.LAZY!r} if name in sys.modules]))\n")
        self.assertEqual(result, [])

    def test_a_lazy_module_reads_writes_and_patches_through_to_the_module(self) -> None:
        result = self.run_python(
            "from unittest import mock\n"
            "import autonom_lib.flow.executor as real\n"
            "same = cli.flow_executor.Executor is real.Executor\n"
            "original = real.Executor.run\n"
            "with mock.patch.object(cli.flow_executor, 'Executor') as fake:\n"
            "    patched = real.Executor is fake and cli.flow_executor.Executor is fake\n"
            "restored = real.Executor.run is original and 'Executor' in vars(real)\n"
            "cli.emulator_mod.SENTINEL_FOR_TEST = 7\n"
            "import autonom_lib.emulator as emulator\n"
            "written = emulator.SENTINEL_FOR_TEST == 7\n"
            "del cli.emulator_mod.SENTINEL_FOR_TEST\n"
            "deleted = not hasattr(emulator, 'SENTINEL_FOR_TEST')\n"
            "print(json.dumps([same, patched, restored, written, deleted]))\n")
        self.assertEqual(result, [True, True, True, True, True])


# --- ID-06: the daemon serves the six subcommands -------------------------------------


class SeamTests(DaemonCase):
    def test_tree_read_through_the_daemon(self) -> None:  # ID-06-S01
        off_env = {**self.env, "AUTONOM_IOS_DAEMON": "off"}
        code, through_idb = self.ios("ui", "tree", env=off_env)
        self.assertEqual(code, 0, through_idb)
        self.assertEqual(len(self.idb_calls("ui", "describe-all")), 1)
        self.log.unlink()
        self.start_daemon()
        code, through_daemon = self.ios("ui", "tree")
        self.assertEqual(code, 0, through_daemon)
        self.assertEqual(len(self.daemon_requests("tree")), 1)
        self.assertEqual(self.idb_calls("ui", "describe-all"), [])
        self.assertEqual((through_daemon["transport"], through_daemon["ax_backend"]),
                         ("daemon", "ax"))
        self.assertNotIn("transport_reason", through_daemon)
        self.assertEqual((through_idb["transport"], through_idb["transport_reason"]),
                         ("subprocess", "daemon_disabled"))
        strip = lambda payload: {k: v for k, v in payload.items() if k not in TRANSPORT}  # noqa: E731
        self.assertEqual(strip(through_daemon), strip(through_idb))

    def test_text_stays_on_idb(self) -> None:  # ID-06-S02
        self.start_daemon()
        code, payload = self.ios("ui", "type", "hello", "--no-observe")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.idb_calls("ui", "text"), [["ui", "text", "hello", "--udid", UDID]])
        ops = [row.get("op") for row in self.daemon_requests()]
        self.assertNotIn("text", ops)
        self.assertNotIn("hello", json.dumps(self.daemon_requests()))
        # the focus read before it was the daemon's
        self.assertEqual(ops, ["tree"])
        self.assertEqual(self.idb_calls("ui", "describe-all"), [])

    def test_inputs_through_the_daemon(self) -> None:
        self.start_daemon()
        for argv, op in ((("ui", "tap", "--x", "10", "--y", "20"), "tap"),
                         (("ui", "swipe", "--from", "10,20", "--to", "30,40", "--duration", "0.4"),
                          "swipe"),
                         (("ui", "key", "HOME"), "button"),
                         (("ui", "key", "40"), "key")):
            with self.subTest(op=op):
                code, payload = self.ios(*argv, "--no-observe")
                self.assertEqual(code, 0, payload)
                self.assertEqual(payload["transport"], "daemon")
                self.assertEqual(len(self.daemon_requests(op)), 1)
        self.assertEqual(self.daemon_requests("tap")[0]["x"], 10)
        self.assertEqual(self.daemon_requests("swipe")[0]["duration"], 0.4)
        self.assertEqual(self.daemon_requests("button")[0]["name"], "HOME")
        self.assertEqual(self.daemon_requests("key")[0]["code"], 40)
        for verb in ("tap", "swipe", "button", "key"):
            self.assertEqual(self.idb_calls("ui", verb), [], verb)

    def test_settled_wait_uses_the_quiet_stream(self) -> None:
        self.start_daemon()
        code, payload = self.ios("ui", "wait", "--settled", "--timeout-ms", "2000",
                                 "--quiet-ms", "300")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["settled"], payload["method"], payload["snapshots"]),
                         (True, "quiet-stream", 1))
        [quiet] = self.daemon_requests("quiet")
        self.assertEqual((quiet["timeout_ms"], quiet["quiet_ms"]), (2000, 300))
        self.assertEqual(len(self.daemon_requests("tree")), 1)
        self.assertEqual(self.idb_calls("ui"), [])

    def test_settled_wait_not_quiet_is_unsettled(self) -> None:
        self.start_daemon(replies={"quiet": {"state": "busy", "busy_signals": ["run_loop_idle"],
                                             "timed_out": True}})
        code, payload = self.ios("ui", "wait", "--settled", "--timeout-ms", "1000")
        self.assertEqual(code, 1, payload)
        self.assertEqual((payload["settled"], payload["method"]), (False, "quiet-stream"))
        self.assertEqual((payload["quiet_state"], payload["busy_signals"]),
                         ("busy", ["run_loop_idle"]))

    def test_run_idb_gives_the_idb_stdout(self) -> None:
        self.start_daemon()
        completed = ios_idb.run_idb(str(FAKE_IDB), ["ui", "describe-all", "--json"], udid=UDID)
        fixture = IOS_FIXTURE.read_text(encoding="utf-8")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.rstrip("\n"), fixture.rstrip("\n"))
        self.assertEqual(ios_idb.transport_of(UDID), {"transport": "daemon", "ax_backend": "ax"})

    def test_unserved_shapes_stay_on_idb(self) -> None:
        for argv in (["ui", "describe-all"], ["ui", "text", "x"], ["ui", "describe-point", "1", "2"],
                     ["ui", "key", "HOME"], ["ui", "button", "VOLUME"], ["crash", "list"],
                     ["ui", "quiet", "1", "--json"], ["ui", "tap", "a", "b"]):
            with self.subTest(argv=argv):
                self.assertIsNone(ios_idb._daemon_call(argv))
        self.assertEqual(ios_idb._daemon_call(["ui", "tap", "1", "2", "--duration", "0.6"]),
                         {"op": "tap", "x": 1, "y": 2, "duration": 0.6})



class XcuitestBackendTests(DaemonCase):
    """ID-06: a session on the XCUITest backend never starts or uses a daemon."""

    def test_button_on_xcuitest_starts_no_daemon(self) -> None:
        env = {**self.env, "AUTONOM_NODE": str(FAKE_IOSD), "AUTONOM_UI_BACKEND": "xcuitest",
               "AUTONOM_FAKE_IOSD_CONFIG": str(self.config())}
        for argv, verb in ((("ui", "key", "LOCK"), ("ui", "button", "LOCK")),
                           (("ui", "key", "40"), ("ui", "key", "40"))):
            with self.subTest(argv=argv):
                code, payload = self.ios(*argv, "--no-observe", env=env)
                self.assertEqual(code, 0, payload)
                self.assertEqual(payload.get("ui_backend"), "xcuitest")
                self.assertEqual(payload["transport"], "subprocess")
                self.assertNotIn("transport_reason", payload)
                self.assertEqual(len(self.idb_calls(*verb)), 1)
        self.assertFalse((self.home / "iosd" / f"{UDID}.json").exists(),
                         "an XCUITest session started a device daemon")
        self.assertEqual(self.daemon_requests(), [])

    def test_xcuitest_session_ignores_a_live_daemon(self) -> None:
        self.start_daemon()
        self.set_env(AUTONOM_UI_BACKEND="xcuitest")
        completed = ios_idb.run_idb(str(FAKE_IDB), ["ui", "button", "LOCK"], udid=UDID)
        self.assertEqual(completed.returncode, 0)
        self.assertNotEqual(getattr(completed, "transport", None), "daemon")
        self.assertEqual(self.daemon_requests(), [])
        self.assertEqual(len(self.idb_calls("ui", "button", "LOCK")), 1)
        self.assertEqual(ios_idb.transport_of(UDID), {"transport": "subprocess"})

    def test_auto_session_switched_to_xcuitest(self) -> None:
        self.assertFalse(ios_idb.xcuitest_session(UDID))
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=BUNDLE)
        record["ui_observation"] = {"ui_backend": "xcuitest"}
        session.save(record)
        self.assertTrue(ios_idb.xcuitest_session(UDID))
        self.start_daemon()
        ios_idb.run_idb(str(FAKE_IDB), ["ui", "describe-all", "--json"], udid=UDID)
        self.assertEqual(self.daemon_requests(), [])
        self.assertEqual(len(self.idb_calls("ui", "describe-all")), 1)

    def test_an_unreadable_backend_starts_no_daemon(self) -> None:
        self.set_env(AUTONOM_UI_BACKEND="bogus")
        self.assertTrue(ios_idb.xcuitest_session(UDID))

    def test_observed_xcuitest_input_is_not_armed(self) -> None:
        from autonom_lib import observe

        target = Target(IOS, UDID, str(FAKE_SIMCTL))
        self.set_env(AUTONOM_UI_BACKEND="xcuitest")
        observe.check_timeout(None)
        observe.wanted(True, None)
        observe.read_mark(target)
        self.assertIsNone(getattr(ios_idb._ACT, "armed", None))
        self.set_env(AUTONOM_UI_BACKEND="idb")
        observe.check_timeout(None)
        observe.wanted(True, None)
        observe.read_mark(target)
        self.assertEqual(ios_idb._ACT.armed["udid"], UDID)
        ios_idb.disarm_act()


class ReadBudgetTests(DaemonCase):
    """RISK-012: a read the daemon kept and never answered leaves the idb
    process only what is left of the caller's budget."""

    def test_stuck_quiet_is_not_waited_for_twice(self) -> None:
        self.start_daemon(delay_on={"quiet": 30})
        original = ios_idb.QUIET_GRACE_S
        ios_idb.QUIET_GRACE_S = 0.5
        self.addCleanup(setattr, ios_idb, "QUIET_GRACE_S", original)
        target = Target(IOS, UDID, str(FAKE_SIMCTL))
        started = time.monotonic()
        with self.assertRaises(errors.AutonomError) as caught:
            ios_idb.ui_quiet(target, 300, 100, idb_path=str(FAKE_IDB))
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertIn("nothing was left", caught.exception.message)
        self.assertEqual(len(self.daemon_requests("quiet")), 1)
        self.assertEqual(self.idb_calls("ui", "quiet"), [])
        # the budget (0.3 s) plus one grace (0.5 s), with room for a slow host
        self.assertLess(elapsed, 0.3 + 0.5 + 2.0)

    def test_stuck_tree_read_is_not_read_again_with_a_full_timeout(self) -> None:
        self.start_daemon(delay_on={"tree": 30})
        started = time.monotonic()
        with self.assertRaises(errors.AutonomError) as caught:
            ios_idb.run_idb(str(FAKE_IDB), ["ui", "describe-all", "--json"], udid=UDID,
                            timeout=1.5)
        self.assertLess(time.monotonic() - started, 1.5 + 2.0)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertEqual(len(self.daemon_requests("tree")), 1)
        self.assertEqual(self.idb_calls("ui", "describe-all"), [])

    def test_remaining_budget(self) -> None:
        argv = ["ui", "describe-all", "--json"]
        self.assertEqual(ios_idb._remaining_budget(argv, 20, {"after_send_s": 5.0}, None),
                         (argv, 15.0))
        self.assertEqual(ios_idb._remaining_budget(argv, None, {"after_send_s": 5.0}, None),
                         (argv, None))
        with self.assertRaises(errors.AutonomError):
            ios_idb._remaining_budget(argv, 20, {"after_send_s": 19.5}, None)
        again = (["ui", "quiet", "0.2", "--json", "--quiet-window-ms", "100"], 5.2)
        self.assertEqual(ios_idb._remaining_budget(argv, 20, {"after_send_s": 1}, lambda: again),
                         again)
        with self.assertRaises(errors.AutonomError):
            ios_idb._remaining_budget(argv, 20, {"after_send_s": 1}, lambda: None)


# --- ID-07: the reply says how it was served -----------------------------------------


class TransportReasonTests(DaemonCase):
    def test_disabled(self) -> None:  # ID-07-S01
        self.start_daemon()
        code, payload = self.ios("ui", "tree", env={**self.env, "AUTONOM_IOS_DAEMON": "off"})
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["transport"], payload["transport_reason"]),
                         ("subprocess", "daemon_disabled"))
        self.assertEqual(len(self.idb_calls("ui", "describe-all")), 1)
        self.assertEqual(self.daemon_requests(), [])

    def test_no_node(self) -> None:  # ID-07-S02
        code, payload = self.ios("ui", "tap", "--x", "10", "--y", "20", "--no-observe")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.idb_calls("ui", "tap"), [["ui", "tap", "10", "20", "--udid", UDID]])
        self.assertEqual((payload["transport"], payload["transport_reason"]),
                         ("subprocess", "node_missing"))

    def test_old_companion(self) -> None:
        self.start_daemon(status="unsupported")
        code, payload = self.ios("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["transport"], payload["transport_reason"]),
                         ("subprocess", "companion_too_old"))
        self.assertEqual(len(self.idb_calls("ui", "describe-all")), 1)
        self.assertEqual(self.daemon_requests("tree"), [])

    def test_refused_before_send_falls_back_once(self) -> None:
        self.start_daemon(replies={"tree": {"ok": False, "error": {
            "code": "daemon_unreachable", "stage": "before_send",
            "message": "The companion is being restarted."}}})
        code, payload = self.ios("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["transport"], payload["transport_reason"]),
                         ("subprocess", "daemon_unreachable"))
        self.assertEqual(len(self.daemon_requests("tree")), 1)
        self.assertEqual(len(self.idb_calls("ui", "describe-all")), 1)

    def test_a_read_lost_after_send_is_read_again_through_idb(self) -> None:
        self.start_daemon(close_on=["tree"])
        code, payload = self.ios("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["transport_reason"], "daemon_unreachable")
        self.assertEqual(len(self.idb_calls("ui", "describe-all")), 1)

    def test_start_failure(self) -> None:
        env = {**self.env, "AUTONOM_NODE": str(FAKE_IOSD),
               "AUTONOM_FAKE_IOSD_CONFIG": str(self.config(status="failed"))}
        code, payload = self.ios("ui", "tree", env=env)
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["transport"], payload["transport_reason"]),
                         ("subprocess", "daemon_start_failed"))
        self.assertEqual(len(self.idb_calls("ui", "describe-all")), 1)

    def test_remote_companion_is_never_the_daemons(self) -> None:
        self.start_daemon()
        code, payload = self.ios("ui", "tree",
                                 env={**self.env, "AUTONOM_IDB_COMPANION": "10.0.0.2:10882"})
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["transport"], "subprocess")
        self.assertNotIn("transport_reason", payload)
        self.assertEqual(self.daemon_requests(), [])


class StartTests(DaemonCase):
    def test_first_use_starts_one_supervised_daemon(self) -> None:
        env = {**self.env, "AUTONOM_NODE": str(FAKE_IOSD),
               "AUTONOM_FAKE_IOSD_CONFIG": str(self.config())}
        registry = self.home / "iosd" / f"{UDID}.json"
        self.assertFalse(registry.exists())
        code, payload = self.ios("ui", "tree", env=env)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["transport"], "daemon")
        self.assertTrue(registry.exists())
        record = json.loads(registry.read_text(encoding="utf-8"))
        self.addCleanup(lambda: _alive(record["pid"]) and os.kill(record["pid"], 15))
        # the supervisor and its child are registered like every long-lived child
        kinds = {entry.get("kind") for entry in processes.entries()}
        self.assertTrue({"iosd", "iosd_child"} <= kinds, kinds)
        # a second command reuses it: no second start, one more request
        code, again = self.ios("ui", "tree", env=env)
        self.assertEqual(code, 0, again)
        self.assertEqual(json.loads(registry.read_text(encoding="utf-8"))["pid"], record["pid"])
        self.assertEqual(len(self.daemon_requests("tree")), 2)
        self.assertEqual(self.idb_calls("ui", "describe-all"), [])
        # stopping it takes the supervisor down too
        ios_daemon.request(record["socket"], {"op": "stop"}, timeout_s=5)
        self.assertTrue(_wait_until(lambda: not _alive(record["pid"])))
        self.assertTrue(_wait_until(lambda: not any(
            entry.get("kind") == "iosd" for entry in processes.entries())))

    def test_start_lock_of_a_dead_holder_is_stale(self) -> None:
        lock = self.home / "lock"
        lock.write_text("999999", encoding="utf-8")
        self.assertTrue(ios_daemon._try_lock(lock))
        self.assertEqual(lock.read_text(encoding="utf-8"), str(os.getpid()))
        self.assertFalse(ios_daemon._try_lock(lock), "a live holder keeps it")
        ios_daemon._release_lock(lock)
        self.assertFalse(lock.exists())

    def test_a_dead_registry_is_not_live(self) -> None:
        (self.home / "iosd").mkdir()
        (self.home / "iosd" / f"{UDID}.json").write_text(json.dumps({
            "schema": "autonom-iosd/v1", "udid": UDID, "pid": 999999,
            "socket": str(self.root / "nothing.sock")}), encoding="utf-8")
        self.assertIsNone(ios_daemon.live_registry(ios_daemon.state_base(), UDID))


# --- ID-08: an input is never sent twice ------------------------------------------------


class NoResendTests(DaemonCase):
    def test_daemon_dies_after_the_tap_was_written(self) -> None:  # ID-08-S01
        self.start_daemon(close_on=["tap"])
        code, payload = self.ios("ui", "tap", "--x", "10", "--y", "20", "--no-observe")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.IDB_COMPANION_UNAVAILABLE)
        self.assertEqual(payload["stage"], "after_connection")
        self.assertIn("was not sent again", payload["hint"])
        self.assertEqual(len(self.daemon_requests("tap")), 1)
        self.assertEqual(self.idb_calls("ui", "tap"), [])

    def test_daemon_says_after_send(self) -> None:
        self.start_daemon(replies={"tap": {"ok": False, "error": {
            "code": "backend_failed", "stage": "after_send",
            "message": "The input failed after it was sent: stream reset"}}})
        code, payload = self.ios("ui", "tap", "--x", "10", "--y", "20", "--no-observe")
        self.assertEqual((code, payload["error_code"]), (2, errors.IDB_COMPANION_UNAVAILABLE))
        self.assertEqual(self.idb_calls("ui", "tap"), [])

    def test_refused_before_send_is_sent_once_through_idb(self) -> None:
        self.start_daemon(replies={"tap": {"ok": False, "error": {
            "code": "daemon_unreachable", "stage": "before_send",
            "message": "The companion is being restarted."}}})
        code, payload = self.ios("ui", "tap", "--x", "10", "--y", "20", "--no-observe")
        self.assertEqual(code, 0, payload)
        self.assertEqual(len(self.idb_calls("ui", "tap")), 1)
        self.assertEqual(payload["transport_reason"], "daemon_unreachable")

    def test_display_not_lit_is_reported_not_read_through_idb(self) -> None:  # ID-13 (CLI side)
        self.start_daemon(replies={"tree": {"ok": False, "error": {
            "code": "ios_display_not_lit", "stage": "after_send", "reads": 2,
            "message": "The accessibility read hit a display that is not lit yet.",
            "hint": "Wait for the display to settle."}}})
        code, payload = self.ios("ui", "tree")
        self.assertEqual((code, payload["error_code"]), (2, errors.IOS_DISPLAY_NOT_LIT))
        self.assertIn("display", payload["hint"])
        self.assertEqual(len(self.daemon_requests("tree")), 1)
        self.assertEqual(self.idb_calls("ui", "describe-all"), [])


UNCONFIRMED_REPLY = {"ok": False, "error": {
    "code": "input_unconfirmed", "stage": "after_send", "unconfirmed_events": 2,
    "message": "The input was sent but not confirmed: 2 HID events were sent but not "
               "confirmed by idb_companion (the HID call was cancelled before idb_companion "
               "confirmed it: the input's deadline passed); they may not have been performed.",
    "hint": "The input was sent once, but idb_companion did not confirm it: another client "
            "held the simulator's input past the safety limit. Check the screen first."}}


class UnconfirmedInputTests(DaemonCase):
    """REV-030: the daemon's `input_unconfirmed` (after_send) is the CLI's
    `input_unconfirmed`, with the daemon's message, hint and event count;
    the input is never sent again, through the daemon or idb."""

    def check(self, payload: dict[str, Any]) -> None:
        error = UNCONFIRMED_REPLY["error"]
        self.assertEqual(payload["error_code"], errors.INPUT_UNCONFIRMED, payload)
        self.assertEqual(payload["error"], error["message"])
        self.assertEqual(payload["hint"], error["hint"])
        self.assertEqual(payload["unconfirmed_events"], 2)
        self.assertEqual(payload["stage"], "after_connection")
        self.assertEqual(payload["transport"], "daemon")
        self.assertNotIn("connection to idb_companion broke", payload["hint"])

    def test_plain_tap(self) -> None:
        self.start_daemon(replies={"tap": UNCONFIRMED_REPLY})
        code, payload = self.ios("ui", "tap", "--x", "10", "--y", "20", "--no-observe")
        self.assertEqual(code, 2, payload)
        self.check(payload)
        self.assertEqual(len(self.daemon_requests("tap")), 1)
        self.assertEqual(self.idb_calls("ui"), [], "never sent again through idb")

    def test_observed_tap(self) -> None:
        self.start_daemon(replies={"act": UNCONFIRMED_REPLY})
        code, payload = self.ios("ui", "tap", "--x", "10", "--y", "20", "--observe")
        self.assertEqual(code, 2, payload)
        self.check(payload)
        self.assertEqual(len(self.daemon_requests("act")), 1)
        self.assertEqual(self.daemon_requests("tap"), [])
        self.assertEqual(self.idb_calls("ui", "tap"), [])

    def test_button_without_a_hint(self) -> None:
        reply = json.loads(json.dumps(UNCONFIRMED_REPLY))
        del reply["error"]["hint"]
        reply["error"]["unconfirmed_events"] = "two"  # not a count: left out
        self.start_daemon(replies={"button": reply})
        code, payload = self.ios("ui", "key", "HOME")  # a hardware button: daemon op `button`
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.INPUT_UNCONFIRMED)
        self.assertEqual(payload["hint"], ios_idb.UNCONFIRMED_HINT)
        self.assertNotIn("unconfirmed_events", payload)
        self.assertEqual(len(self.daemon_requests("button")), 1)
        self.assertEqual(self.idb_calls("ui", "button"), [])


class InputBudgetTests(DaemonCase):
    """REV-029: the CLI waits for a daemon input's reply at least
    DAEMON_INPUT_BUDGET_S (plus the input's duration), longer than the
    daemon's deadline plus its reply slack, whatever the idb timeout."""

    def budgets(self, run: Any) -> list[tuple[str, float]]:
        seen: list[tuple[str, float]] = []
        original = ios_daemon.request

        def spy(socket_path: str, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
            seen.append((payload["op"], timeout_s))
            return original(socket_path, payload, timeout_s=timeout_s)

        with mock.patch.object(ios_daemon, "request", spy):
            run()
        return seen

    def test_daemon_deadline_is_inside_the_cli_budget(self) -> None:
        source = DAEMON_MJS.read_text(encoding="utf-8")
        limit = re.search(r"^export const DEFAULT_HID_WAIT_MS = ([0-9_]+);$", source, re.M)
        margin = re.search(r"^export const ACT_REPLY_MARGIN_MS = ([0-9_]+);$", source, re.M)
        self.assertIsNotNone(limit)
        self.assertIsNotNone(margin)
        slack_s = 3.0  # INPUT_REPLY_SLACK_MS, pinned in tests/ios-device-daemon.test.mjs
        self.assertLess(int(limit.group(1).replace("_", "")) / 1000 + slack_s,
                        ios_idb.DAEMON_INPUT_BUDGET_S)
        self.assertLess(int(margin.group(1).replace("_", "")) / 1000 + slack_s,
                        ios_idb.ACT_MARGIN_S)

    def test_every_plain_input_gets_the_daemon_budget(self) -> None:
        self.start_daemon()
        target = Target(IOS, UDID, str(FAKE_SIMCTL))
        idb = str(FAKE_IDB)
        seen = self.budgets(lambda: (
            ios_idb.tap(target, 1, 2, idb_path=idb),
            ios_idb.tap(target, 1, 2, duration=1.5, idb_path=idb),
            ios_idb.swipe(target, 1, 2, 3, 4, 0.5, idb_path=idb),
            ios_idb.button(target, "HOME", idb_path=idb),  # idb's own timeout: 10 s
            ios_idb.key(target, "41", idb_path=idb),
        ))
        budget = ios_idb.DAEMON_INPUT_BUDGET_S
        self.assertEqual(seen, [("tap", budget), ("tap", budget + 1.5), ("swipe", budget + 0.5),
                                ("button", budget), ("key", budget)])
        self.assertEqual(self.idb_calls("ui"), [])

    def test_an_observed_input_gets_the_act_budget(self) -> None:
        self.start_daemon()
        target = Target(IOS, UDID, str(FAKE_SIMCTL))

        def observed() -> None:
            ios_idb.arm_act(UDID, 1500)
            ios_idb.tap(target, 1, 2, duration=0.5, idb_path=str(FAKE_IDB))

        seen = self.budgets(observed)
        self.assertEqual(seen, [("act", 1.5 + 0.5 + ios_idb.ACT_MARGIN_S)])
        self.assertIsNotNone(ios_idb.take_act(UDID))


# --- ID-10: observed actions are one act ---------------------------------------------------


class ObservedActTests(DaemonCase):
    def test_observed_tap_is_one_act(self) -> None:  # ID-10-S01 (CLI side)
        self.start_daemon()
        code, payload = self.ios("ui", "tap", "--desc", "General", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["input_confirmed"], payload["method"]), (True, "quiet-stream"))
        self.assertIn("General", payload["screen"]["outline"])
        [act] = self.daemon_requests("act")
        self.assertEqual(act["input"]["op"], "tap")
        self.assertEqual(act["timeout_ms"], 3000)
        self.assertTrue(act["read"])
        self.assertEqual(self.daemon_requests("tap"), [])
        self.assertEqual(self.daemon_requests("quiet"), [])
        self.assertEqual(len(self.daemon_requests("tree")), 1, "the selector's own read only")
        self.assertEqual(self.idb_calls("ui"), [])
        self.assertNotIn("input_unconfirmed", json.dumps(payload.get("warnings", [])))

    def test_unconfirmed_input_keeps_ok(self) -> None:  # ID-10-S03 (CLI side)
        self.start_daemon(replies={"act": {
            "input_confirmed": False, "confirmed_by": None,
            "warnings": [{"code": "input_unconfirmed", "hint": "Check the screen first.",
                          "dtuhidd_active": 0, "device_hub": True}]}})
        code, payload = self.ios("ui", "tap", "--x", "10", "--y", "20", "--observe",
                                 "--observe-timeout-ms", "1500")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["ok"], payload["input_confirmed"], payload["method"]),
                         (True, False, "quiet-stream"))
        [warning] = [item for item in payload["warnings"] if item["code"] == "input_unconfirmed"]
        self.assertEqual(warning["hint"], "Check the screen first.")
        self.assertIn("screen", payload)
        [act] = self.daemon_requests("act")
        self.assertEqual((act["input"], act["timeout_ms"]),
                         ({"op": "tap", "x": 10, "y": 20}, 1500))
        self.assertEqual(self.idb_calls("ui", "tap"), [])

    def test_blocked_input_names_the_cause(self) -> None:  # ID-12 (CLI side)
        self.start_daemon(replies={"act": {
            "input_confirmed": False,
            "warnings": [{"code": "ios_input_blocked", "hint": "Reboot the simulator.",
                          "dtuhidd_active": 1, "device_hub": False}]}})
        code, payload = self.ios("ui", "tap", "--x", "10", "--y", "20", "--observe")
        self.assertEqual(code, 0, payload)
        [warning] = [item for item in payload["warnings"] if item["code"] == "ios_input_blocked"]
        self.assertEqual((warning["hint"], warning["dtuhidd_active"]), ("Reboot the simulator.", 1))

    def test_a_read_failure_after_the_input_is_an_observe_error(self) -> None:
        self.start_daemon(replies={"act": {
            "ok": False, "input_confirmed": True, "settled": True, "warnings": [],
            "error": {"code": "ios_display_not_lit", "stage": "after_send",
                      "message": "The accessibility read hit a display that is not lit yet."}}})
        code, payload = self.ios("ui", "tap", "--x", "10", "--y", "20", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["observe_error"]["code"], "ios_display_not_lit")
        self.assertTrue(payload["input_confirmed"])
        self.assertEqual(self.idb_calls("ui", "tap"), [])

    def test_unobserved_tap_sends_a_plain_tap(self) -> None:
        self.start_daemon()
        code, payload = self.ios("ui", "tap", "--x", "10", "--y", "20", "--no-observe")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("input_confirmed", payload)
        self.assertEqual(len(self.daemon_requests("tap")), 1)
        self.assertEqual(self.daemon_requests("act"), [])

    def test_observed_type_settles_on_the_quiet_stream(self) -> None:
        self.start_daemon()
        code, payload = self.ios("ui", "type", "hi", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertEqual(len(self.idb_calls("ui", "text")), 1)
        self.assertEqual(self.daemon_requests("act"), [])
        self.assertEqual(len(self.daemon_requests("quiet")), 1)
        self.assertIn("screen", payload)


# --- ID-16: session stop leaves the daemon's companion alone ---------------------------


class SessionStopTests(DaemonCase):
    def test_session_stop_with_a_live_daemon(self) -> None:  # ID-16-S01
        companion = self.bin / "idb_companion"
        companion.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n",
                             encoding="utf-8")
        companion.chmod(0o755)
        process = subprocess.Popen([str(companion), "--udid", UDID], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.wait()))
        self.assertTrue(_wait_until(lambda: any(
            item["pid"] == process.pid for item in processes.discover_companions(UDID))))
        self.start_daemon(companion_pid=process.pid)
        session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID, app_id=BUNDLE)
        code, payload = self.ios("session", "stop")
        self.assertEqual(code, 0, payload)
        left = [item for warning in payload.get("warnings", [])
                if warning.get("code") == "companion_left_running"
                for item in warning.get("companions", [])]
        self.assertNotIn(process.pid, [item["pid"] for item in left])
        self.assertIsNone(process.poll(), "the daemon's companion was killed")

    def test_a_companion_the_daemon_only_adopted_is_still_stopped(self) -> None:
        companion = self.bin / "idb_companion"
        companion.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n",
                             encoding="utf-8")
        companion.chmod(0o755)
        process = subprocess.Popen([str(companion), "--udid", UDID], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.wait()))
        self.assertTrue(_wait_until(lambda: any(
            item["pid"] == process.pid for item in processes.discover_companions(UDID))))
        self.start_daemon(companion_pid=process.pid, companion_owned=False)
        self.assertEqual(ios_daemon.live_companion_pids(UDID), set())
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=BUNDLE)
        # the session's own idb call started it (track_idb_companions)
        processes.register("idb_companion", process.pid, owner=record["session_id"],
                           session_id=record["session_id"],
                           artifacts_dir=record.get("artifacts_dir"), udid=UDID,
                           signature=[processes.COMPANION_MARKER, UDID], spawned_by="idb")
        result = processes.reap_session(record)
        self.assertIn(process.pid, [item["pid"] for item in result["terminated"]])
        self.assertTrue(_wait_until(lambda: process.poll() is not None, 10),
                        "an adopted companion leaked")

    def test_an_owned_companion_is_listed_for_the_daemon(self) -> None:
        companion = self.bin / "idb_companion"
        companion.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n",
                             encoding="utf-8")
        companion.chmod(0o755)
        process = subprocess.Popen([str(companion), "--udid", UDID], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.wait()))
        self.start_daemon(companion_pid=process.pid)
        self.assertEqual(ios_daemon.live_companion_pids(UDID), {process.pid})

    def test_without_a_daemon_the_companion_is_still_reported(self) -> None:
        companion = self.bin / "idb_companion"
        companion.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n",
                             encoding="utf-8")
        companion.chmod(0o755)
        process = subprocess.Popen([str(companion), "--udid", UDID], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.wait()))
        self.assertTrue(_wait_until(lambda: any(
            item["pid"] == process.pid for item in processes.discover_companions(UDID))))
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=BUNDLE)
        result = processes.reap_session(record)
        self.assertEqual([item["pid"] for item in result["companion_left_running"]],
                         [process.pid])


class LiveScriptLogFilterTests(unittest.TestCase):
    """`tests/live/ios_daemon_live.py` copies a fixed set of the daemon's log
    events into its report (the log is deleted with the temporary home); the
    CASE-004 checks read the stuck-companion probes, suspects and backoff
    resets from that copy. Importing the script runs nothing (main guard)."""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ios_daemon_live_under_test", ROOT / "tests/live/ios_daemon_live.py")
        cls.live = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.live
        spec.loader.exec_module(cls.live)

    @classmethod
    def tearDownClass(cls) -> None:
        sys.modules.pop("ios_daemon_live_under_test", None)

    def _events(self, records: list[Any], raw: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "iosd").mkdir()
            lines = [json.dumps(item) for item in records] + list(raw)
            (home / "iosd" / f"{UDID}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
            return self.live.daemon_log_events(home, UDID)

    def test_the_stuck_companion_events_are_kept(self) -> None:
        records = [
            {"event": "quiet_probe", "when": "check", "result": "silent", "silent_run": 1, "ms": 2001},
            {"event": "quiet_probe", "when": "check", "result": "silent", "silent_run": 2, "ms": 2002},
            {"event": "quiet_suspect", "when": "check", "reason": "host_load", "load_1m": 90, "cpus": 10},
            {"event": "quiet_suspect", "when": "touch", "reason": "too_soon", "gap_ms": 60000},
            {"event": "quiet_backoff_reset", "why": "probe", "streak": 3},
            {"event": "quiet_stuck", "when": "check", "gap_ms": 30000},
        ]
        kept = self._events(records)
        self.assertEqual(kept, records)

    def test_events_that_may_carry_tree_or_typed_text_are_not_copied(self) -> None:
        text = "SECRET-TYPED-TEXT"
        records = [
            {"event": "tree", "text": text},
            {"event": "type", "text": text},
            {"event": "request", "op": "type", "text": text},
            {"event": "quiet_probe", "result": "ok"},
        ]
        kept = self._events(records, raw=("not json", json.dumps([1, 2])))
        self.assertEqual([item["event"] for item in kept], ["quiet_probe"])
        self.assertNotIn(text, json.dumps(kept))
        for name in ("tree", "type", "request"):
            self.assertNotIn(name, self.live.DAEMON_LOG_EVENTS)

    def test_a_missing_log_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.live.daemon_log_events(Path(tmp), UDID), [])


if __name__ == "__main__":
    unittest.main()
