"""Regressions from the live WoolBox runs: logs, processes, network, launch.

Each class names the defect it pins. Everything runs against fakes — the
scripted fake adb, stubbed simctl calls, throwaway sleeper processes — never a
real device, simulator, idb or mitmproxy.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import adb as adb_mod  # noqa: E402
from autonom_lib import errors, follow, ios_simctl, logs, processes, session  # noqa: E402
from autonom_lib.network import attachment, device_proxy_ios, mitm_addon, mocks  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

SERIAL = "emulator-5554"
BUNDLE = "ru.skywool.knix"
RUNNER_IMAGE = ("/Users/qa/Library/Developer/CoreSimulator/Devices/D/data/Containers/"
                "Bundle/Application/A/Runner.app/Runner")


def _udid() -> str:
    return f"FAKE-{uuid.uuid4().hex[:8].upper()}-0000-0000-0000-000000000000"


def _ios_line(image: str, message: str, subsystem: str = "") -> str:
    return json.dumps({"timestamp": "2026-09-26 12:00:00.000000+0300",
                       "processImagePath": image, "senderImagePath": image,
                       "subsystem": subsystem, "messageType": "Default",
                       "eventMessage": message})


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _gone(pid: int) -> bool:
    try:
        os.waitpid(pid, os.WNOHANG)  # our own exited child would linger as a zombie
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


def _kill_quietly(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


class CliCase(EnvSandboxMixin, unittest.TestCase):
    """The CLI against the fake adb, with a private home, state and argv log."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.state_path = root / "state.json"
        self.log_path = root / "argv.jsonl"
        self.state_path.write_text("{}", encoding="utf-8")
        self.set_env(AUTONOM_HOME=str(self.home), AUTONOM_FAKE_STATE=str(self.state_path),
                     AUTONOM_FAKE_LOG=str(self.log_path),
                     AUTONOM_ADB=None, AUTONOM_SIMCTL=None, AUTONOM_IDB=None)

    def write_state(self, **state) -> None:
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def adb_calls(self) -> list[list[str]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line)["argv"] for line in
                self.log_path.read_text(encoding="utf-8").splitlines()
                if json.loads(line)["tool"] == "adb"]

    def android(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--adb", str(FAKE_ADB), "--serial", SERIAL, *argv],
            capture_output=True, text=True, env=dict(os.environ), timeout=120,
            cwd=self.tmp.name)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.returncode in (0, 1) else completed.stderr
        return completed.returncode, json.loads(stream)


# --- 1. iOS: a Flutter app's logs (executable `Runner`) ------------------------

OTHER_BUNDLE = "com.example.otherflutter"


class IosFlutterLogTests(unittest.TestCase):
    """`logs tail --package ru.skywool.knix` returned only the `log show`
    trailer: the predicate looked for "knix" while every line came from a
    process image named Runner. Matching now belongs to `ios_simctl`, keyed
    to the installed bundle, so a second Flutter app (also `Runner`) on the
    same simulator never leaks in."""

    TARGET = Target("ios", "UDID-FLUTTER", "/fake/xcrun", {"udid": "UDID-FLUTTER"})

    def setUp(self) -> None:
        ios_simctl.reset_caches()
        self.addCleanup(ios_simctl.reset_caches)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        apps = Path(tmp.name) / "Containers" / "Bundle" / "Application"
        self.bundle = apps / "AAAA" / "Runner.app"
        self.other = apps / "BBBB" / "Runner.app"
        for bundle in (self.bundle, self.other):
            bundle.mkdir(parents=True)
        self.images = {BUNDLE: ("Runner", str(self.bundle)),
                       OTHER_BUNDLE: ("Runner", str(self.other))}
        self.app_paths: list = []

        def fake_app_image(_xcrun, _udid, bundle_id, *, app_path=None):
            self.app_paths.append(app_path)
            return self.images.get(bundle_id, (None, None))

        patcher = mock.patch.object(ios_simctl, "app_image", side_effect=fake_app_image)
        patcher.start()
        self.addCleanup(patcher.stop)

    def mine(self, message: str) -> str:
        return _ios_line(f"{self.bundle}/Runner", message)

    def theirs(self, message: str) -> str:
        return _ios_line(f"{self.other}/Runner", message)

    def expected_predicate(self) -> str:
        return ios_simctl.log_predicate(BUNDLE, executable="Runner",
                                        bundle_path=str(self.bundle))

    def test_log_show_predicate_is_the_bundle_and_drops_the_trailer(self) -> None:
        seen: list[str] = []

        def fake_simctl(_xcrun, args, **_kwargs):
            predicate = args[args.index("--predicate") + 1] if "--predicate" in args else ""
            seen.append(predicate)
            # Like the real unified log: only records the predicate selects.
            body = [line for line in (self.mine("flutter: [Network] GET /home"),
                                      self.theirs("flutter: other app"))
                    if any(f'BEGINSWITH "{prefix}"' in predicate
                           for prefix in ios_simctl.bundle_prefixes(
                               json.loads(line)["processImagePath"].rsplit("/", 1)[0]))]
            body.append('{"count":1,"finished":1}')
            return types.SimpleNamespace(returncode=0, stdout="\n".join(body) + "\n",
                                         stderr="")

        with mock.patch.object(ios_simctl, "run_simctl", fake_simctl):
            entries, warnings = logs.tail(self.TARGET, package=BUNDLE, since_seconds=30)
        self.assertEqual(seen, [self.expected_predicate()])
        self.assertEqual(warnings, [])
        self.assertEqual([e["line"].endswith("flutter: [Network] GET /home") for e in entries],
                         [True])
        self.assertNotIn("finished", json.dumps(entries))

    def test_two_flutter_apps_on_one_simulator_only_the_session_app_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stream = Path(tmp) / "stream.ndjson"
            stream.write_text("\n".join([
                'Filtering the log data using "subsystem == \\"ru.skywool.knix\\""',
                self.mine("flutter: cart updated"),
                self.theirs("flutter: someone else's cart"),
                _ios_line("/usr/libexec/locationd", "unrelated", "com.apple.locationd"),
            ]) + "\n", encoding="utf-8")
            detail = logs.tail_detailed(self.TARGET, stream_path=stream, package=BUNDLE)
        self.assertEqual([e["line"].endswith("flutter: cart updated")
                          for e in detail["entries"]], [True])
        self.assertEqual(detail["executable"], "Runner")
        # the follow side, live and from the stream file
        keep = logs.ios_line_filter(self.TARGET, BUNDLE)
        self.assertTrue(keep(self.mine("hello")))
        self.assertFalse(keep(self.theirs("hello")), "another Runner leaked in")
        self.assertFalse(keep('Filtering the log data using "..."'))
        argv = logs.ios_follow_argv(self.TARGET, BUNDLE)
        predicate = argv[argv.index("--predicate") + 1]
        self.assertEqual(predicate, self.expected_predicate())
        self.assertNotIn(str(self.other), predicate)

    def test_session_log_stream_gets_the_bundle_and_the_install_path(self) -> None:
        captured: dict = {}

        def fake_start(xcrun, udid, destination, **kwargs):
            captured.update(kwargs, udid=udid)
            return None

        record = {"app_id": BUNDLE, "install_path": "/builds/Runner.app",
                  "artifacts_dir": tempfile.mkdtemp(), "background": {}, "streams": []}
        self.addCleanup(lambda: __import__("shutil").rmtree(record["artifacts_dir"], True))
        with mock.patch.object(ios_simctl, "start_log_stream", fake_start):
            logs.start_session_log_stream(self.TARGET, record)
        self.assertEqual((captured["executable"], captured["bundle_path"]),
                         ("Runner", str(self.bundle)))
        self.assertEqual(captured["app_path"], "/builds/Runner.app")
        self.assertIn("/builds/Runner.app", self.app_paths)

    def test_without_a_filter_only_the_log_tools_chatter_is_dropped(self) -> None:
        keep = logs.ios_line_filter(self.TARGET)
        self.assertTrue(keep(self.theirs("anything")))
        self.assertFalse(keep('{"count":0,"finished":1}'))

    def test_an_unresolvable_app_falls_back_without_failing(self) -> None:
        with mock.patch.object(ios_simctl, "run_simctl",
                               return_value=types.SimpleNamespace(
                                   returncode=0, stdout="", stderr="")) as run:
            entries, _ = logs.tail(self.TARGET, package="com.example.gone")
        self.assertEqual(entries, [])
        predicate = run.call_args[0][1][-1]
        self.assertEqual(predicate, ios_simctl.log_predicate("com.example.gone"))


# --- 2. Android: `--package` of an app that is not running --------------------

AM = "1000  1100 I ActivityManager"
LOGCAT = [
    "09-26 12:00:00.000  4242  4242 I flutter : [Network] GET /catalog",
    f"09-26 12:00:01.000  {AM}: Start proc 4242:com.example.app/u0a123 for top-activity",
    "09-26 12:00:02.000  5555  5555 I chatty  : another app talking",
    f"09-26 12:00:03.000  {AM}: Start proc 5555:com.other/u0a200 for service",
    "09-26 12:00:04.000  4242  4242 E AndroidRuntime: FATAL EXCEPTION: main",
    "09-26 12:00:04.001  4242  4242 E AndroidRuntime: Process: com.example.app, PID: 4242",
    f"09-26 12:00:05.000  {AM}: Process com.example.app (pid 4242) has died: fg TOP",
    f"09-26 12:00:06.000  {AM}: Start proc 6000:com.example.app.debug/u0a300 for activity",
]
UID_STATE = {
    "getprop": {"ro.build.version.sdk": "34"},
    "packages": {"com.example.app": 10123, "com.other": 10200,
                 "com.example.app.debug": 10300},
    "pid_uids": {"4242": 10123, "5555": 10200, "1000": 1000, "6000": 10300},
    "pidof": {},
    "logcat": LOGCAT,
}
EXPECTED_UID_LINES = [LOGCAT[0], LOGCAT[1], LOGCAT[4], LOGCAT[5], LOGCAT[6]]


class AndroidLogFilterCliTests(CliCase):
    def test_not_running_app_is_filtered_by_uid_with_its_lifecycle_lines(self) -> None:
        """Before: pidof found nothing, so every device line came back."""
        self.write_state(**UID_STATE)
        code, payload = self.android("logs", "tail", "--package", "com.example.app",
                                     "--since", "0")
        self.assertEqual(code, 0, payload)
        self.assertEqual([entry["line"] for entry in payload["lines"]], EXPECTED_UID_LINES)
        self.assertNotIn("warnings", payload)
        uid_calls = [call for call in self.adb_calls() if "--uid=10123" in call]
        self.assertEqual(len(uid_calls), 1, self.adb_calls())

    def test_old_api_with_the_app_running_degrades_to_pid_and_says_so(self) -> None:
        self.write_state(**{**UID_STATE, "getprop": {"ro.build.version.sdk": "29"},
                            "pidof": {"com.example.app": "4242"}})
        code, payload = self.android("logs", "tail", "--package", "com.example.app",
                                     "--since", "0")
        self.assertEqual(code, 0, payload)
        self.assertEqual([entry["line"] for entry in payload["lines"]], EXPECTED_UID_LINES)
        warning = payload["warnings"][0]
        self.assertEqual((warning["code"], warning["filter"]), ("log_filter_degraded", "pid"))
        self.assertFalse(any(arg.startswith("--uid") for call in self.adb_calls()
                             for arg in call))

    def test_old_api_with_the_app_stopped_keeps_only_lines_naming_it(self) -> None:
        self.write_state(**{**UID_STATE, "getprop": {"ro.build.version.sdk": "29"}})
        code, payload = self.android("logs", "tail", "--package", "com.example.app",
                                     "--since", "0")
        self.assertEqual(code, 0, payload)
        self.assertEqual([entry["line"] for entry in payload["lines"]],
                         [LOGCAT[1], LOGCAT[5], LOGCAT[6]])
        self.assertEqual(payload["warnings"][0]["filter"], "none")

    def test_unknown_package_is_app_not_installed(self) -> None:
        self.write_state(**UID_STATE)
        code, payload = self.android("logs", "tail", "--package", "com.typo.app")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.APP_NOT_INSTALLED)


class ScriptedRunAdb:
    """`adb.run_adb` stand-in answering by joined-argv prefix."""

    def __init__(self, rules: dict[str, str]) -> None:
        self.rules = rules
        self.calls: list[list[str]] = []

    def __call__(self, _adb, args, *, serial=None, timeout=30, check=True, binary=False):
        argv = list(args)
        self.calls.append(argv)
        joined = " ".join(argv)
        stdout = next((out for prefix, out in self.rules.items()
                       if joined.startswith(prefix)), "")
        return subprocess.CompletedProcess(argv, 0, stdout, "")


class AndroidLogFilterLibraryTests(unittest.TestCase):
    def _plan(self, sdk: str, pid: str = "") -> tuple[dict, ScriptedRunAdb]:
        fake = ScriptedRunAdb({
            "shell cmd package list packages": "package:com.example.app uid:10123\n"
                                               "package:com.example.app.debug uid:10300\n",
            "shell getprop ro.build.version.sdk": sdk + "\n",
            "shell pidof": pid + "\n",
            "shell date": "1790424351\n",
        })
        with mock.patch.object(adb_mod, "run_adb", fake):
            return logs.android_follow_plan("adb", SERIAL, "com.example.app"), fake

    def test_tail_reports_the_filter_it_used(self) -> None:
        fake = ScriptedRunAdb({
            "shell cmd package list packages": "package:com.example.app uid:10123\n",
            "shell getprop ro.build.version.sdk": "34\n",
        })
        with mock.patch.object(adb_mod, "run_adb", fake):
            detail = logs.tail_logcat_detailed("adb", SERIAL, package="com.example.app",
                                               since_seconds=0)
        self.assertEqual((detail["filter"], detail["uid"]), ("uid", 10123))

    def test_follow_on_api_31_runs_a_uid_logcat_and_a_lifecycle_logcat(self) -> None:
        plan, _ = self._plan("34")
        self.assertEqual(plan["filter"], "uid")
        (own, own_filter, ended), (lifecycle, lifecycle_filter) = plan["streams"]
        self.assertIn("--uid=10123", own)
        self.assertIsNone(own_filter)
        self.assertEqual(ended["reason"], "uid_stream_ended")
        self.assertIn("ActivityManager:V", lifecycle)
        prefix = "09-26 12:00:01.000 I/ActivityManager( 1000): Start proc"
        self.assertTrue(lifecycle_filter(f"{prefix} 4242:com.example.app/u0a123"))
        self.assertFalse(lifecycle_filter(f"{prefix} 6000:com.example.app.debug/u0a300"))

    def test_follow_on_an_old_api_filters_the_running_pid_and_warns(self) -> None:
        plan, _ = self._plan("29", pid="4242")
        self.assertEqual(plan["filter"], "pid")
        self.assertEqual(plan["warnings"][0]["code"], "log_filter_degraded")
        [(argv, keep)] = plan["streams"]
        self.assertFalse(any(arg.startswith("--uid") for arg in argv))
        self.assertTrue(keep("09-26 12:00:00.000 I/flutter ( 4242): hi"))
        self.assertFalse(keep("09-26 12:00:00.000 I/chatty  ( 5555): other"))

    def test_package_listing_is_matched_exactly(self) -> None:
        fake = ScriptedRunAdb({"shell cmd package list packages":
                               "package:com.example.app.debug uid:10300\n"})
        with mock.patch.object(adb_mod, "run_adb", fake):
            self.assertEqual(logs.package_uid("adb", SERIAL, "com.example.app"), (False, None))


class FollowProcessesTests(unittest.TestCase):
    def test_two_streams_merge_once_with_warnings_first_and_detail_last(self) -> None:
        emitted: list = []
        first = [sys.executable, "-c", "print('mine'); print('shared')"]
        second = [sys.executable, "-c",
                  "import time; time.sleep(0.3); print('shared'); print('lifecycle')"]
        eof = follow.follow_processes(
            [(first, None), (second, None)], source="device", emit=emitted.append,
            max_seconds=20, warnings=[{"code": "log_filter_degraded", "error": "x"}],
            detail={"filter": "uid"})
        self.assertEqual(emitted[0]["kind"], "warning")
        texts = [e["text"] for e in emitted if e.get("kind") == "line"]
        self.assertEqual(sorted(texts), ["lifecycle", "mine", "shared"])
        self.assertEqual((eof["reason"], eof["filter"], eof["lines"]), ("stream_ended", "uid", 3))


# --- 3. unmanaged processes -----------------------------------------------------


class _RegistryCase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.home = self.sandbox_home()
        self.sleepers: list[subprocess.Popen] = []
        # Signature discovery is machine-wide by design: without this, a
        # `cleanup()` here would see (and reap) another run's proxies — or a
        # developer's real orphan — instead of only this test's rows.
        patcher = mock.patch.object(processes, "discover_proxies", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def sleeper(self, *argv: str) -> subprocess.Popen:
        child = subprocess.Popen(list(argv) or [sys.executable, "-c",
                                                "import time; time.sleep(120)"])
        self.sleepers.append(child)
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


class LogStreamRegistryTests(_RegistryCase):
    """The iOS log-stream writer was invisible to `processes`, `cleanup --all`
    and — once `background.log_stream_pid` was lost — to `session stop`."""

    def _session(self) -> tuple[Target, dict]:
        udid = _udid()
        record = session.start_session("/fake/xcrun", platform="ios", target_id=udid,
                                       app_id=BUNDLE)
        return Target("ios", udid, "/fake/xcrun", {"udid": udid}), record

    def _start(self, target: Target, record: dict) -> int:
        def fake_start(_xcrun, _udid, destination, **_kwargs):
            # The real bounded writer carries its destination in its argv,
            # which is what the registry checks before it kills a pid.
            return self.sleeper(sys.executable, "-c", "import time; time.sleep(120)",
                                str(destination)).pid

        with mock.patch.object(ios_simctl, "start_log_stream", fake_start), \
                mock.patch.object(ios_simctl, "app_image", return_value=("Runner", None)):
            pid = logs.start_session_log_stream(target, record)
        session.save(record)
        return pid

    def test_a_reused_pid_is_never_killed(self) -> None:
        _target, record = self._session()
        stranger = self.sleeper()
        processes.register("log_stream", stranger.pid, owner=record["session_id"],
                           artifacts_dir=record["artifacts_dir"],
                           signature="/nowhere/logs/stream.ndjson")
        result = processes.reap_session(record)
        self.assertEqual(result["terminated"][0]["result"], "pid_reused")
        self.assertIsNone(stranger.poll())

    def test_writer_is_registered_to_the_session_and_listed(self) -> None:
        target, record = self._session()
        pid = self._start(target, record)
        self.assertEqual(record["background"]["log_stream_pid"], pid)
        self.assertEqual(record["background"]["log_stream_executable"], "Runner")
        background = {entry["pid"]: entry for entry in processes.scan()["background"]}
        self.assertEqual(background[pid]["kind"], "log_stream")
        self.assertEqual(background[pid]["owner"], record["session_id"])

    def test_session_stop_terminates_it_even_without_the_background_pid(self) -> None:
        target, record = self._session()
        pid = self._start(target, record)
        record["background"]["log_stream_pid"] = None  # the CLI's own list forgot it
        session.save(record)
        stopped = session.stop_session()
        self.assertTrue(_wait_until(lambda: _gone(pid)),
                        f"log stream outlived its session: {stopped.get('process_teardown')}")
        self.assertEqual([item["pid"] for item in stopped["process_teardown"]["terminated"]],
                         [pid])
        self.assertEqual(processes.entries(), [])

    def test_cleanup_all_stops_it(self) -> None:
        target, record = self._session()
        pid = self._start(target, record)
        result = processes.cleanup(include_live=True)
        self.assertIn(pid, [a["pid"] for a in result["actions"] if a["result"] == "terminated"])
        self.assertTrue(_wait_until(lambda: _gone(pid)))

    def test_a_stopped_sessions_process_is_an_orphan(self) -> None:
        _target, record = self._session()
        child = self.sleeper()
        processes.register("log_stream", child.pid, owner=record["session_id"],
                           artifacts_dir=record["artifacts_dir"])
        session.stop_session(reap=False)
        orphans = {entry["pid"]: entry for entry in processes.scan()["orphans"]}
        self.assertEqual(orphans[child.pid]["reason"], "its session has stopped")

    def test_ensure_log_stream_restarts_a_dead_writer(self) -> None:
        target, record = self._session()
        dead = self.sleeper()
        record["background"]["log_stream_pid"] = dead.pid
        dead.kill()
        dead.wait(timeout=10)
        writer = self.sleeper()
        with mock.patch.object(ios_simctl, "start_log_stream", return_value=writer.pid):
            result = logs.ensure_log_stream(target, record)
        self.assertEqual((result["restarted"], result["pid"], result["previous_pid"]),
                         (True, writer.pid, dead.pid))


LEADER_EXITS = textwrap.dedent("""\
    import pathlib, subprocess, sys
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    pathlib.Path(sys.argv[1]).write_text(str(worker.pid))
    worker.returncode = 0  # leave it running: no "still running" warning at exit
""")


def _kill_group_quietly(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


CHILD = textwrap.dedent("""\
    import pathlib, subprocess, sys, time
    grand = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    pathlib.Path(sys.argv[1]).write_text(str(grand.pid))
    time.sleep(120)
""")
SUPERVISOR = textwrap.dedent("""\
    import sys
    sys.path.insert(0, sys.argv[1])
    from autonom_lib import processes
    raise SystemExit(processes.run_supervised(
        [sys.executable, sys.argv[2], sys.argv[3]], kind="canvas", target_id="emulator-5554"))
""")


class SupervisedCanvasTests(_RegistryCase):
    """`canvas serve` ran node with `subprocess.run`: unregistered, and
    killing the CLI left node (and node's children) running."""

    def _start(self) -> tuple[subprocess.Popen, int, int]:
        tmp = Path(tempfile.mkdtemp(dir=self.home))
        script = tmp / "bridge.py"
        script.write_text(CHILD, encoding="utf-8")
        pidfile = tmp / "grandchild.pid"
        supervisor = subprocess.Popen(
            [sys.executable, "-c", SUPERVISOR, str(ROOT / "scripts"), str(script),
             str(pidfile)], env=dict(os.environ))
        self.addCleanup(self._stop, supervisor)
        self.assertTrue(_wait_until(lambda: pidfile.exists() and pidfile.read_text()))
        self.assertTrue(_wait_until(lambda: len(processes.entries()) == 2))
        kinds = {entry["kind"]: entry for entry in processes.entries()}
        child = kinds["canvas_child"]["pid"]
        grandchild = int(pidfile.read_text())
        self.addCleanup(_kill_quietly, child)
        self.addCleanup(_kill_quietly, grandchild)
        self.assertEqual(kinds["canvas"]["pid"], supervisor.pid)
        self.assertEqual(kinds["canvas_child"]["parent_pid"], supervisor.pid)
        return supervisor, child, grandchild

    def test_pair_is_listed_and_terminating_the_supervisor_takes_the_group(self) -> None:
        supervisor, child, grandchild = self._start()
        listed = {entry["pid"] for entry in processes.scan()["background"]}
        self.assertLessEqual({supervisor.pid, child}, listed)
        supervisor.send_signal(signal.SIGTERM)
        self.assertEqual(supervisor.wait(timeout=20), 128 + signal.SIGTERM)
        self.assertTrue(_wait_until(lambda: _gone(child) and _gone(grandchild)),
                        "the node child or its own child survived the canvas")
        self.assertEqual(processes.entries(), [])

    def test_a_killed_supervisor_leaves_an_orphan_cleanup_reaps_by_group(self) -> None:
        supervisor, child, grandchild = self._start()
        supervisor.kill()
        supervisor.wait(timeout=10)
        orphans = {entry["pid"]: entry for entry in processes.scan()["orphans"]}
        self.assertIn(child, orphans)
        self.assertIn("supervisor", orphans[child]["reason"])
        processes.cleanup()
        self.assertTrue(_wait_until(lambda: _gone(child) and _gone(grandchild)))


class CompanionTests(_RegistryCase):
    """An idb_companion idb spawned for the session's simulator survived
    `session stop`; one Autonom did not start must never be killed."""

    def setUp(self) -> None:
        super().setUp()
        self.udid = _udid()
        self.record = session.start_session("/fake/xcrun", platform="ios",
                                            target_id=self.udid, app_id=BUNDLE)
        bindir = Path(tempfile.mkdtemp(dir=self.home))
        self.companion = bindir / "idb_companion"
        self.companion.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(120)\n",
                                  encoding="utf-8")
        self.companion.chmod(0o755)

    def _companion(self) -> subprocess.Popen:
        process = self.sleeper(str(self.companion), "--udid", self.udid)
        self.assertTrue(_wait_until(lambda: any(
            item["pid"] == process.pid for item in processes.discover_companions(self.udid))))
        return process

    def test_a_companion_our_idb_call_started_is_stopped_with_the_session(self) -> None:
        with processes.track_idb_companions(self.udid, record=self.record):
            spawned = self._companion()  # what `idb ui describe-all` does on first use
        [entry] = [e for e in processes.entries() if e["kind"] == "idb_companion"]
        self.assertEqual((entry["pid"], entry["owner"]), (spawned.pid, self.record["session_id"]))
        result = processes.reap_session(self.record)
        self.assertEqual([item["pid"] for item in result["terminated"]], [spawned.pid])
        self.assertTrue(_wait_until(lambda: spawned.poll() is not None))
        self.assertEqual(result["companion_left_running"], [])

    def test_a_companion_that_was_already_running_is_reported_not_killed(self) -> None:
        foreign = self._companion()
        with processes.track_idb_companions(self.udid, record=self.record):
            pass
        self.assertEqual(processes.entries(), [])
        result = processes.reap_session(self.record)
        self.assertEqual([item["pid"] for item in result["companion_left_running"]],
                         [foreign.pid])
        self.assertIsNone(foreign.poll(), "a companion Autonom did not start was killed")


# --- 4. network status evidence -------------------------------------------------


def _flow(client_ip: str | None, agent: str = "curl/8.7.1") -> dict:
    return {"id": "f_0001", "host": "backend.example.net", "method": "GET",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "client_ip": client_ip, "request_headers_preview": {"user-agent": agent}}


class AttachmentEvidenceTests(unittest.TestCase):
    ATTACHED = {"network": {"attached": True, "device_proxy": "10.0.2.2:8080"}}

    def test_ios_host_traffic_is_never_attachment(self) -> None:
        """Before: host curl flows made status say `attached: true`."""
        result = attachment.attachment_evidence(
            {"network": {"attached": True, "platform_manual": True}}, platform="ios",
            flows=[_flow("127.0.0.1"), _flow("127.0.0.1")])
        self.assertEqual((result["attached"], result["evidence"]),
                         ("unknown", "host_traffic_indistinguishable"))
        self.assertEqual(result["unattributed_flow_count"], 2)
        self.assertIn("curl/8.7.1", result["recent_user_agents"])
        self.assertIn("host", result["reason"])

    def test_android_guest_flows_prove_attachment(self) -> None:
        result = attachment.attachment_evidence(self.ATTACHED, platform="android",
                                                flows=[_flow("10.0.2.15", "okhttp/4")])
        self.assertEqual((result["attached"], result["evidence"]), (True, "target_flows"))

    def test_android_loopback_flows_defer_to_the_device_setting(self) -> None:
        flows = [_flow("127.0.0.1")]
        confirmed = attachment.attachment_evidence(
            self.ATTACHED, platform="android", flows=flows,
            observe_setting=lambda: "10.0.2.2:8080")
        self.assertEqual((confirmed["attached"], confirmed["evidence"]), (True, "device_setting"))
        cleared = attachment.attachment_evidence(
            self.ATTACHED, platform="android", flows=flows, observe_setting=lambda: ":0")
        self.assertEqual((cleared["attached"], cleared["evidence"]),
                         (False, "device_proxy_cleared_externally"))
        self.assertEqual(cleared["target_flow_count"], 0)

    def test_not_attached_stays_not_attached(self) -> None:
        result = attachment.attachment_evidence({"network": {}}, platform="android",
                                                flows=[_flow("10.0.2.15")])
        self.assertEqual((result["attached"], result["evidence"]), (False, "not_attached"))

    def test_the_addon_records_the_client_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recorder = mitm_addon.AutonomRecorder()
            recorder.directory = tmp
            headers = types.SimpleNamespace(items=lambda: [("Host", "h.example")])
            flow = types.SimpleNamespace(
                request=types.SimpleNamespace(method="GET", pretty_url="https://h.example/x",
                                              host="h.example", path="/x", headers=headers,
                                              content=b"", timestamp_start=1.0),
                response=types.SimpleNamespace(status_code=200, content=b"",
                                               timestamp_end=1.1, headers=headers),
                client_conn=types.SimpleNamespace(peername=("127.0.0.1", 50123)),
                metadata={},
            )
            recorder.response(flow)
            written = json.loads((Path(tmp) / "flows.jsonl").read_text("utf-8"))
        self.assertEqual(written["client_ip"], "127.0.0.1")


# --- 5. iOS attach wording ------------------------------------------------------


class IosAttachWordingTests(unittest.TestCase):
    def _attach(self, record: dict) -> dict:
        target = Target("ios", "UDID-ATTACH", "/fake/xcrun", {"udid": "UDID-ATTACH"})
        with mock.patch.object(device_proxy_ios.consent, "require", return_value={}), \
                mock.patch.object(device_proxy_ios.consent, "record"), \
                mock.patch.object(device_proxy_ios.proxy_mod, "ca_certificate",
                                  return_value=Path("/tmp/mitmproxy-ca-cert.cer")):
            return device_proxy_ios.attach(target, record, port=61179, acknowledged=True)

    def test_mode_agrees_with_attach_state_and_status(self) -> None:
        record = {"network": {}}
        result = self._attach(record)
        self.assertEqual((result["mode"], result["attach_state"]), ("manual", "manual"))
        self.assertEqual(record["network"]["attach_state"], "manual")
        device_proxy_ios.detach(Target("ios", "UDID-ATTACH", "/fake/xcrun", {}), record)
        self.assertIsNone(record["network"]["attach_state"])

    def test_flutter_is_not_claimed_as_covered_by_the_environment(self) -> None:
        result = self._attach({"network": {}})
        codes = {warning["code"]: warning for warning in result["warnings"]}
        self.assertNotIn("Flutter", codes["urlsession_not_covered"]["error"])
        self.assertIn("findProxy", codes["flutter_proxy_hook_required"]["error"])
        self.assertIn("in-app", codes["flutter_proxy_hook_required"]["hint"])
        self.assertNotIn("Dart/Flutter HttpClient.findProxyFromEnvironment, curl",
                         device_proxy_ios.__doc__ or "")

    def test_the_ca_step_names_the_simulator(self) -> None:
        steps = "\n".join(self._attach({"network": {}})["manual_steps"])
        self.assertIn("xcrun simctl keychain UDID-ATTACH add-root-cert", steps)
        self.assertNotIn("<udid>", steps)


# --- 6. mocks -----------------------------------------------------------------


class MockTests(CliCase):
    def test_mock_show_reports_hits_like_mock_list(self) -> None:
        code, started = self.android("session", "start", "--app-id", "com.example.app")
        self.assertEqual(code, 0, started)
        flows = Path(started["session"]["artifacts_dir"]) / "network" / "flows.jsonl"
        code, added = self.android("network", "mock", "add", "--url",
                                   "https://api.example.net/v1/items", "--json", "{}")
        self.assertEqual(code, 0, added)
        rule_id = added["mock"]["id"] if "mock" in added else added["id"]
        flows.parent.mkdir(parents=True, exist_ok=True)
        flows.write_text("".join(json.dumps({"id": f"f_{n}", "mock_id": rule_id}) + "\n"
                                 for n in range(2)), encoding="utf-8")
        code, shown = self.android("network", "mock", "show", rule_id)
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown["mock"]["hits"], 2)
        code, listed = self.android("network", "mock", "list")
        self.assertEqual(listed["mocks"][0]["hits"], shown["mock"]["hits"])

    def test_persistent_mocks_warning(self) -> None:
        registry = self.home / "reg"
        registry.mkdir(parents=True)
        self.assertIsNone(mocks.persistent_mocks_warning(registry=registry))
        mocks.add(url_glob="*/x", registry=registry)
        warning = mocks.persistent_mocks_warning(registry=registry)
        self.assertEqual(warning["code"], "persistent_mocks_active")
        self.assertIn("1 mock rule", warning["error"])


# --- 7. session outputs -----------------------------------------------------------


class SessionOutputsTests(CliCase):
    def test_metrics_and_recordings_are_listed(self) -> None:
        code, started = self.android("session", "start", "--app-id", "com.example.app")
        self.assertEqual(code, 0, started)
        base = Path(started["session"]["artifacts_dir"])
        (base / "metrics").mkdir(exist_ok=True)
        (base / "metrics/20260926T120907Z-feed-snapshot.json").write_text("{}", "utf-8")
        (base / "metrics/20260926T121130Z-feed-meminfo.txt").write_text("x\n", "utf-8")
        (base / "metrics/20260926T115512Z-hitches.trace").mkdir()
        (base / "recordings/feed-scroll.mp4").write_bytes(b"\x00\x00")
        code, payload = self.android("session", "outputs")
        self.assertEqual(code, 0, payload)
        by_id = {stream["id"]: stream for stream in payload["streams"]}
        self.assertEqual(by_id["recordings:feed-scroll.mp4"]["kind"], "recording")
        self.assertFalse(by_id["recordings:feed-scroll.mp4"]["followable"])
        self.assertEqual(by_id["metrics:20260926T120907Z-feed-snapshot.json"]["kind"], "metrics")
        self.assertTrue(by_id["metrics:20260926T121130Z-feed-meminfo.txt"]["followable"])
        trace = by_id["metrics:20260926T115512Z-hitches.trace"]
        self.assertTrue(trace["exists"] and trace["directory"])
        self.assertNotIn("follow_hint", trace)


# --- 8. Android resume launch ------------------------------------------------------


class ResumeLaunchCliTests(CliCase):
    def test_resume_uses_am_start_and_leaves_the_orientation_alone(self) -> None:
        """Before: monkey ran, whose freeze/thaw reset a pinned rotation."""
        self.write_state(settings_system={"user_rotation": "1",
                                          "accelerometer_rotation": "0"})
        code, payload = self.android("session", "launch", "com.example.app")
        self.assertEqual(code, 0, payload)
        joined = [" ".join(call) for call in self.adb_calls()]
        starts = [call for call in joined if call.startswith(f"-s {SERIAL} shell am start")]
        self.assertEqual(len(starts), 1, joined)
        self.assertIn("-W -n com.example.app/.MainActivity", starts[0])
        self.assertNotIn(session.FRESH_TASK_FLAGS, starts[0], "a resume never clears the task")
        self.assertFalse(any("monkey" in call for call in joined), joined)
        self.assertEqual(self.state()["settings_system"],
                         {"user_rotation": "1", "accelerometer_rotation": "0"})


class ResumeLaunchLibraryTests(CliCase):
    def test_launch_report_is_parsed_from_the_wait_output(self) -> None:
        detail = session.launch_app(str(FAKE_ADB), SERIAL, "com.example.app")
        self.assertEqual(detail["component"], "com.example.app/.MainActivity")
        self.assertEqual(detail["launch"]["launch_state"], "cold")
        self.assertEqual(detail["launch"]["activity"], "com.example.app/.MainActivity")
        self.assertEqual((detail["launch"]["total_time_ms"], detail["launch"]["wait_time_ms"]),
                         (412, 430))
        resolve = next(call for call in self.adb_calls() if "resolve-activity" in call)
        self.assertIn("android.intent.action.MAIN", resolve)

    def test_brought_to_front_is_reported_as_such(self) -> None:
        self.write_state(am_launch_state="UNKNOWN (0)")
        detail = session.launch_app(str(FAKE_ADB), SERIAL, "com.example.app")
        self.assertEqual(detail["launch"]["launch_state"], "unknown")
        self.assertTrue(detail["launch"]["brought_to_front"])

    def test_monkey_is_only_the_fallback(self) -> None:
        self.write_state(no_launcher=["com.example.app"])
        detail = session.launch_app(str(FAKE_ADB), SERIAL, "com.example.app")
        self.assertIsNone(detail["launch"])
        self.assertTrue(any("monkey" in call for call in self.adb_calls()))

    def test_fresh_launch_carries_the_report_too(self) -> None:
        detail = session.launch_app_fresh(str(FAKE_ADB), SERIAL, "com.example.app")
        self.assertEqual((detail["mode"], detail["launch"]["launch_state"]), ("fresh", "cold"))


# --- fake adb state: torn reads (the CI flake) ----------------------------------


def _load_fake_adb() -> types.ModuleType:
    import importlib.util

    spec = importlib.util.spec_from_file_location("fake_adb_under_test", FAKE_ADB)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeAdbStateTests(EnvSandboxMixin, unittest.TestCase):
    """CI died on JSONDecodeError inside the fake adb: it read the state file
    after another fake had truncated it and before it was refilled."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.set_env(AUTONOM_FAKE_STATE=str(self.path))
        self.fake = _load_fake_adb()

    def test_a_torn_state_is_waited_out(self) -> None:
        import threading

        self.path.write_text('{"devices": [["emulator-5554", "dev', encoding="utf-8")

        def finish() -> None:
            time.sleep(0.3)
            self.path.write_text('{"devices": [["emulator-5554", "device"]]}\n',
                                 encoding="utf-8")

        writer = threading.Thread(target=finish)
        writer.start()
        try:
            state = self.fake.load_state()
        finally:
            writer.join()
        self.assertEqual(state, {"devices": [["emulator-5554", "device"]]})

    def test_a_state_that_stays_corrupt_still_raises(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        with mock.patch.object(self.fake, "READ_PATIENCE_SECONDS", 0.1):
            with self.assertRaises(json.JSONDecodeError):
                self.fake.load_state()

    def test_a_missing_state_is_empty(self) -> None:
        self.assertEqual(self.fake.load_state(), {})

    def test_write_state_replaces_the_file_and_leaves_no_temp_behind(self) -> None:
        self.path.write_text("{}\n", encoding="utf-8")
        before = self.path.stat().st_ino
        self.fake.write_state({"night_mode": "yes"})
        self.assertNotEqual(self.path.stat().st_ino, before,
                            "rewritten in place: a reader could see it truncated")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         {"night_mode": "yes"})
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()),
                         ["state.json"])

    def test_a_failed_write_keeps_the_old_state_and_no_temp(self) -> None:
        self.path.write_text('{"kept": true}\n', encoding="utf-8")
        with mock.patch.object(self.fake.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.fake.write_state({"kept": False})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"kept": True})
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()),
                         ["state.json"])


# --- review round 1 -----------------------------------------------------------

TOKEN = "SECRET-CANVAS-TOKEN-4242"
TOKEN_SUPERVISOR = textwrap.dedent("""\
    import sys
    sys.path.insert(0, sys.argv[1])
    from autonom_lib import processes
    raise SystemExit(processes.run_supervised(
        [sys.executable, sys.argv[2], sys.argv[3], "--token", sys.argv[4], "--port", "18765"],
        kind="canvas", target_id="emulator-5554"))
""")


class CanvasTokenTests(_RegistryCase):
    """`canvas serve --token X`: registry rows are printed whole by
    `processes` and `doctor`, so the token must never be stored."""

    def test_redaction_gaps_from_review(self) -> None:
        cases = {
            "--no-auth --token S1": "--no-auth --token <redacted>",
            "PASS=hunter2 PASSWD=pw2 USER=me": "PASS=<redacted> PASSWD=<redacted> USER=me",
            "curl --bearer B7 https://h/": "curl --bearer <redacted> https://h/",
            "mitmdump --mode upstream:http://u:p@ss@proxy:8080/a@b -q":
                "mitmdump --mode upstream:http://<redacted>@proxy:8080/a@b -q",
            "node\tbridge.mjs\t--token\tT9\t--port\t1":
                "node\tbridge.mjs\t--token\t<redacted>\t--port\t1",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(processes.redact_command(raw), expected)

    def test_redact_command_hides_secret_values_only(self) -> None:
        text = processes.redact_command(
            "node bridge.mjs --token abc --port 3277 --api-key=k1 --password p "
            "--set proxyauth=u:pw --udid U-1 --pct-syskeys 0")
        for secret in ("abc", "k1", " p ", "u:pw"):
            self.assertNotIn(secret, text)
        for kept in ("--port 3277", "--udid U-1", "--pct-syskeys 0", "--token <redacted>",
                     "--api-key=<redacted>", "proxyauth=<redacted>"):
            self.assertIn(kept, text)

    def test_neither_processes_nor_doctor_ever_show_the_token(self) -> None:
        from autonom_lib import doctor

        tmp = Path(tempfile.mkdtemp(dir=self.home))
        script = tmp / "bridge.py"
        script.write_text(CHILD, encoding="utf-8")
        pidfile = tmp / "grandchild.pid"
        supervisor = subprocess.Popen(
            [sys.executable, "-c", TOKEN_SUPERVISOR, str(ROOT / "scripts"), str(script),
             str(pidfile), TOKEN], env=dict(os.environ))
        self.addCleanup(self._stop, supervisor)
        self.assertTrue(_wait_until(lambda: pidfile.exists() and pidfile.read_text()))
        self.assertTrue(_wait_until(lambda: len(processes.entries()) == 2))
        child = next(e["pid"] for e in processes.entries() if e["kind"] == "canvas_child")
        self.addCleanup(_kill_quietly, child)
        self.addCleanup(_kill_quietly, int(pidfile.read_text()))

        def leaked() -> str:
            registry = processes.registry_file().read_text(encoding="utf-8")
            report = json.dumps([processes.scan(), doctor._runtime_state(None)])  # noqa: SLF001
            return registry + report

        self.assertNotIn(TOKEN, leaked())
        supervisor.kill()  # the child becomes an orphan doctor lists in full
        supervisor.wait(timeout=10)
        self.assertIn(child, [e["pid"] for e in processes.scan()["orphans"]])
        self.assertNotIn(TOKEN, leaked())
        processes.cleanup()


class VerifyBeforeKillTests(_RegistryCase):
    def test_a_signature_that_cannot_be_checked_is_skipped_not_killed(self) -> None:
        child = self.sleeper()
        processes.register("log_stream", child.pid, artifacts_dir=str(self.home / "gone"),
                           signature="stream.ndjson")
        with mock.patch.object(processes, "command_of", return_value=None):
            result = processes.cleanup()
        [action] = [a for a in result["actions"] if a["pid"] == child.pid]
        self.assertEqual(action["result"], "unverified_skipped")
        self.assertEqual(result["skipped_unverified"], 1)
        self.assertIsNone(child.poll(), "killed without verifying the pid")
        self.assertIn(child.pid, [e["pid"] for e in processes.entries()],
                      "the row is kept for a later attempt")

    def _detached_worker(self) -> tuple[int, int]:
        """The reviewer's shape: a leader in its own session spawns a worker
        and exits (a double-forking daemon — the adb fork-server, ssh/gpg
        agents), leaving a live group whose leader is dead. Returns
        (pgid, worker pid); the test always kills the group itself."""
        pidfile = Path(tempfile.mkdtemp(dir=self.home)) / "worker.pid"
        leader = subprocess.Popen([sys.executable, "-c", LEADER_EXITS, str(pidfile)],
                                  start_new_session=True)
        self.assertEqual(leader.wait(timeout=30), 0)  # reaped: the leader is gone
        self.assertTrue(_wait_until(lambda: pidfile.exists() and pidfile.read_text()))
        worker = int(pidfile.read_text())
        self.addCleanup(_kill_group_quietly, leader.pid)
        self.assertFalse(_gone(worker))
        return leader.pid, worker

    def test_a_group_without_its_leader_is_reported_not_killed(self) -> None:
        pgid, worker = self._detached_worker()
        processes.register("canvas_child", pgid, process_group=pgid, parent_pid=999999,
                           signature="-c")
        [remnant] = processes.scan()["group_remnants"]
        self.assertEqual(remnant["pid"], pgid)
        self.assertIn(worker, remnant["members"])
        self.assertIn(f"kill -- -{pgid}", remnant["hint"])
        self.assertNotIn(pgid, [e["pid"] for e in processes.scan()["orphans"]])
        for include_live in (False, True):
            result = processes.cleanup(include_live=include_live)
            self.assertEqual(result["actions"], [])
            self.assertEqual(result["skipped_group_remnants"], 1)
            self.assertEqual([r["pid"] for r in result["group_remnants"]], [pgid])
        self.assertFalse(_gone(worker), "cleanup signalled a group it cannot vouch for")
        self.assertIn(pgid, [e["pid"] for e in processes.entries()], "row kept, reported")

    def test_a_reused_group_id_survives_every_teardown(self) -> None:
        """A stale canvas_child row from an earlier run whose pid/pgid now
        belongs to an unrelated daemon's group: default cleanup killed it."""
        pgid, worker = self._detached_worker()
        record = session.start_session("/fake/xcrun", platform="ios", target_id=_udid(),
                                       app_id=BUNDLE)
        processes.register("canvas_child", pgid, process_group=pgid, parent_pid=999999,
                           owner=record["session_id"], artifacts_dir=record["artifacts_dir"],
                           signature="android-emulator-browser.mjs",
                           started_at="2026-01-01T00:00:00Z")
        processes.cleanup()
        processes.cleanup(include_live=True)
        teardown = processes.reap_session(record)
        self.assertEqual(teardown["terminated"], [])
        self.assertEqual([r["pid"] for r in teardown["group_remnants"]], [pgid])
        stopped = session.stop_session()
        self.assertEqual([r["pid"] for r in stopped["process_teardown"]["group_remnants"]],
                         [pgid])
        self.assertFalse(_gone(worker), "an unrelated daemon's worker was killed")

    def test_the_proxy_row_carries_a_signature_cleanup_can_check(self) -> None:
        from autonom_lib.network import proxy

        fake = Path(tempfile.mkdtemp(dir=self.home)) / "mitmdump"
        fake.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import socket, sys, time
            port = int(sys.argv[sys.argv.index("--listen-port") + 1])
            server = socket.socket()
            try:
                server.bind(("127.0.0.1", port))
                server.listen()
            except OSError:
                pass  # a parallel run took the port first: it is busy either way
            time.sleep(120)
        """), encoding="utf-8")
        fake.chmod(0o755)
        record = session.start_session("/fake/adb", serial=SERIAL, app_id="com.example.app")
        spawned: list[subprocess.Popen] = []
        real_popen = subprocess.Popen

        def keep(*args, **kwargs):
            spawned.append(real_popen(*args, **kwargs))
            return spawned[-1]

        # proxy.start deliberately leaves mitmdump running; the test owns the
        # Popen object so it can be reaped (no "still running" warning).
        with mock.patch.object(proxy.subprocess, "Popen", side_effect=keep):
            state = proxy.start(record, mitmdump=str(fake))
        for child in spawned:
            self.addCleanup(self._stop, child)
        [row] = [e for e in processes.entries() if e["kind"] == "proxy"]
        self.assertEqual(row["signature"], processes.ADDON_MARKER)
        self.assertEqual(processes.terminate_entry(row), "terminated")
        self.assertTrue(_wait_until(lambda: _gone(state["pid"])))


REGISTER_MANY = textwrap.dedent("""\
    import sys
    sys.path.insert(0, sys.argv[1])
    from autonom_lib import processes
    base = int(sys.argv[2])
    for n in range(int(sys.argv[3])):
        processes.register("log_stream", base + n, owner="s_concurrent")
""")


class RegistryLockTests(_RegistryCase):
    def test_concurrent_writers_lose_no_rows(self) -> None:
        """Before: two read-modify-writes raced and one writer's rows vanished."""
        writers, each = 8, 25
        procs = [subprocess.Popen([sys.executable, "-c", REGISTER_MANY, str(ROOT / "scripts"),
                                   str(100000 + index * 1000), str(each)],
                                  env=dict(os.environ))
                 for index in range(writers)]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=120), 0)
        self.assertEqual(len(processes.entries()), writers * each)


class CleanupSemanticsTests(_RegistryCase):
    """What the default `cleanup` may kill, now that a stopped session's
    processes count as orphans."""

    def test_default_cleanup_spares_foreground_canvas_and_live_sessions(self) -> None:
        live = session.start_session("/fake/adb", serial=SERIAL, app_id="com.example.app")
        supervisor, child, lost_parent, lost_child, stream, stopped = (
            self.sleeper() for _ in range(6))
        # a canvas started outside any session
        processes.register("canvas", supervisor.pid, role="supervisor",
                           child_pid=child.pid, process_group=child.pid)
        processes.register("canvas_child", child.pid, parent_pid=supervisor.pid)
        # a canvas child whose supervisor row was lost, supervisor still alive
        processes.register("canvas_child", lost_child.pid, parent_pid=lost_parent.pid)
        # a live session's log stream
        processes.register("log_stream", stream.pid, owner=live["session_id"],
                           artifacts_dir=live["artifacts_dir"])
        # a process of a session that has stopped
        old = Path(tempfile.mkdtemp(dir=self.home))
        (old / "session.json").write_text(json.dumps(
            {"session_id": "s_stopped", "stopped_at": "2026-09-26T00:00:00Z"}), "utf-8")
        processes.register("log_stream", stopped.pid, owner="s_stopped",
                           artifacts_dir=str(old))
        result = processes.cleanup()
        self.assertEqual([a["pid"] for a in result["actions"]], [stopped.pid])
        for spared in (supervisor, child, lost_parent, lost_child, stream):
            self.assertIsNone(spared.poll())
        self.assertTrue(_wait_until(lambda: stopped.poll() is not None))


class SettingReadTests(CliCase):
    """`setting_unreadable` was unreachable: an adb failure read as "cleared"."""

    TARGET = Target("android", SERIAL, str(FAKE_ADB), {"serial": SERIAL})
    ATTACHED = {"network": {"attached": True, "device_proxy": "10.0.2.2:8080"}}

    def evidence(self) -> dict:
        from autonom_lib.network import device_proxy_android

        return attachment.attachment_evidence(
            self.ATTACHED, platform="android", flows=[],
            observe_setting=lambda: device_proxy_android.read_setting(self.TARGET))

    def test_adb_failure_is_unreadable(self) -> None:
        from autonom_lib.network import device_proxy_android

        self.write_state(fail={f"-s {SERIAL} shell settings get global http_proxy":
                               [1, "error: device offline"]})
        with self.assertRaises(errors.AutonomError) as caught:
            device_proxy_android.read_setting(self.TARGET)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        result = self.evidence()
        self.assertEqual((result["attached"], result["evidence"]),
                         ("unknown", "setting_unreadable"))

    def test_an_empty_setting_is_cleared(self) -> None:
        self.write_state(settings={})
        result = self.evidence()
        self.assertEqual((result["attached"], result["evidence"]),
                         (False, "device_proxy_cleared_externally"))

    def test_the_attached_setting_is_read_back(self) -> None:
        self.write_state(settings={"http_proxy": "10.0.2.2:8080"})
        self.assertEqual(self.evidence()["evidence"], "device_setting")


class ProcessNameTests(CliCase):
    def test_a_process_name_filters_by_its_packages_uid_and_says_so(self) -> None:
        """Before: `--package com.x:remote` was app_not_installed."""
        self.write_state(**UID_STATE)
        code, payload = self.android("logs", "tail", "--package", "com.example.app:remote",
                                     "--since", "0")
        self.assertEqual(code, 0, payload)
        self.assertEqual([entry["line"] for entry in payload["lines"]], EXPECTED_UID_LINES)
        self.assertEqual([w["code"] for w in payload["warnings"]],
                         ["process_name_not_filtered"])
        self.assertTrue(any("--uid=10123" in call for call in self.adb_calls()))

    def test_a_running_process_name_on_an_old_api_uses_its_own_pid(self) -> None:
        self.write_state(**{**UID_STATE, "getprop": {"ro.build.version.sdk": "29"},
                            "pidof": {"com.example.app:remote": "4242"}})
        code, payload = self.android("logs", "tail", "--package", "com.example.app:remote",
                                     "--since", "0")
        self.assertEqual(code, 0, payload)
        self.assertEqual([w["code"] for w in payload["warnings"]], ["log_filter_degraded"])
        self.assertEqual(payload["warnings"][0]["filter"], "pid")


class UidStreamEndedTests(unittest.TestCase):
    def test_a_uid_stream_that_ends_early_is_announced(self) -> None:
        emitted: list = []
        uid = [sys.executable, "-c", "print('app line')"]
        lifecycle = [sys.executable, "-c",
                     "import time; time.sleep(1.0); print('Start proc 1:com.example.app')"]
        follow.follow_processes(
            [(uid, None, logs.UID_STREAM_ENDED), (lifecycle, None)], source="device",
            emit=emitted.append, max_seconds=20)
        kinds = [(e["kind"], e.get("reason") or e.get("text")) for e in emitted]
        self.assertEqual(kinds[:3], [("line", "app line"), ("warning", "uid_stream_ended"),
                                     ("line", "Start proc 1:com.example.app")])

    def test_a_stream_ending_last_is_just_the_end(self) -> None:
        emitted: list = []
        follow.follow_processes([([sys.executable, "-c", "print('x')"], None,
                                  logs.UID_STREAM_ENDED)],
                                source="device", emit=emitted.append, max_seconds=20)
        self.assertEqual([e["kind"] for e in emitted], ["line", "eof"])


class OpenHintTests(unittest.TestCase):
    def _entry(self) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "recordings").mkdir()
            (base / "recordings/it's.mp4").write_bytes(b"\x00")
            return follow.catalog({"artifacts_dir": str(base), "streams": []})[0]

    def test_macos_uses_open(self) -> None:
        with mock.patch.object(follow.sys, "platform", "darwin"):
            self.assertTrue(self._entry()["shell_hint"].startswith("open '"))

    def test_linux_uses_xdg_open_or_no_hint(self) -> None:
        with mock.patch.object(follow.sys, "platform", "linux"), \
                mock.patch.object(follow.shutil, "which", return_value="/usr/bin/xdg-open"):
            self.assertTrue(self._entry()["shell_hint"].startswith("xdg-open "))
        with mock.patch.object(follow.sys, "platform", "linux"), \
                mock.patch.object(follow.shutil, "which", return_value=None):
            entry = self._entry()
        self.assertNotIn("shell_hint", entry)
        self.assertTrue(entry["abs_path"].endswith("recordings/it's.mp4"))


if __name__ == "__main__":
    unittest.main()
