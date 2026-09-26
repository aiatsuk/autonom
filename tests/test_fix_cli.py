"""CLI wiring of the live-testing fix round, end to end through the fakes.

The library fixes (UI resolution, iOS logs and device state, process
ownership, flows, metrics) landed below the CLI first; this module drives
each one through `scripts/autonom.py` against `tests/fakes`, plus the CLI's
own defects found in the same run:

1. `open <url>` on Android could not say which app took the URL — a deep
   link that landed in Chrome or the chooser read exactly like one that
   reached the app;
2. `network status` answered `attached: true` for a host `curl` through the
   proxy, and never raised `persistent_mocks_active`;
3. a single-file `flow run` had a different shape from a directory run;
4. `simulator keyboard pin reboot=true` left a live session's log stream
   dead after the reboot;
5. `session start --install --launch` did not report what it installed or
   how it launched.

And the integration items that had to touch the library: iOS nodes carry
`long_clickable`/`checkable` (C-03 key parity), `clipboard get` without
xcrun is `simctl_not_found`, idb calls track the companion they start, a
reboot or reinstall restarts the session's log stream, iOS log matching has
one resolved path, `element_offscreen` is a test failure, a suite skips flows
its target cannot run, `ui find --all` lists matches in `--index` order,
command-line redaction is linear, a supervised group is signalled while its
leader still pins the group id, and doctor names group remnants.

Hermetic: `AUTONOM_HOME` and every fake's state live in a temp dir, and no
real adb, simctl, idb or AXe is reached.
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_IDB = ROOT / "tests/fakes/fake_idb.py"
UI_DUMP = ROOT / "tests/fixtures/ui_dump.xml"
WOOLBOX = ROOT / "tests/fixtures/woolbox"
IOS_CATALOG = WOOLBOX / "ios_catalog_offscreen.json"
SERIAL = "emulator-5554"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"
BUNDLE = "com.example.app"
RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-26-0"

sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import (  # noqa: E402
    doctor, errors, ios_idb, ios_simctl, logs, processes, selector, session, simulator,
    ui, ui_android, ui_ios,
)
from autonom_lib.flow import schema as flow_schema  # noqa: E402
from autonom_lib.metrics import frames as metrics_frames  # noqa: E402
from autonom_lib.network import mocks, store  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


def _booted(udid: str = UDID) -> dict:
    return {"devices": {RUNTIME: [{"udid": udid, "name": "iPhone 17 Pro",
                                   "state": "Booted", "isAvailable": True}]}}


def _ios_line(image: str, message: str) -> str:
    return json.dumps({"timestamp": "2026-09-26 12:00:00.000000+0300",
                       "processImagePath": image, "senderImagePath": image,
                       "subsystem": "", "messageType": "Default",
                       "eventMessage": message})




def _flow(client_ip: str, agent: str = "curl/8.7.1") -> dict:
    return {"id": f"f_{uuid.uuid4().hex[:6]}", "host": "backend.example.net",
            "method": "GET", "status": 200, "url": "https://backend.example.net/x",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "client_ip": client_ip, "request_headers_preview": {"user-agent": agent}}


def _gone(pid: int) -> bool:
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


def _wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


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


def _bounded_writer(case: unittest.TestCase, destination: Path) -> subprocess.Popen:
    """`ios_simctl.spawn_bounded` — the log-stream writer's exact argv shape —
    over a child that waits; stopped with SIGTERM so the child goes too."""
    writer = ios_simctl.spawn_bounded(
        [sys.executable, "-c", "import time; time.sleep(120)"], destination)

    def stop() -> None:
        if writer.poll() is None:
            writer.terminate()
        try:
            writer.wait(timeout=10)
        except subprocess.TimeoutExpired:
            writer.kill()
            writer.wait(timeout=10)

    case.addCleanup(stop)
    assert _wait_until(lambda: processes.command_of(writer.pid) is not None)
    return writer


class _Cli(EnvSandboxMixin, unittest.TestCase):
    """The CLI against the fakes, with a private home, fake state and argv log."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.devices_dir = self.root / "Devices"
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        # installed bundles are matched only when they exist on this host,
        # as a simulator's containers do
        self.app_bundle = self.root / "Containers/Bundle/Application/A/Runner.app"
        self.other_bundle = self.root / "Containers/Bundle/Application/B/Runner.app"
        for bundle in (self.app_bundle, self.other_bundle):
            bundle.mkdir(parents=True)
        self.mine = _ios_line(f"{self.app_bundle}/Runner", "mine")
        self.theirs = _ios_line(f"{self.other_bundle}/Runner", "theirs")
        (self.bin_dir / "python3").symlink_to(sys.executable)
        self.write_state()
        self.set_env(
            AUTONOM_HOME=str(self.home), AUTONOM_FAKE_STATE=str(self.state),
            AUTONOM_FAKE_LOG=str(self.log),
            AUTONOM_ADB=None, AUTONOM_SIMCTL=None, AUTONOM_IDB=None, AUTONOM_MITMDUMP=None,
            AUTONOM_AXE=None, AUTONOM_IOS_HID="idb", AUTONOM_IDB_COMPANION=None,
            AUTONOM_IDB_STATE_FILE=str(self.root / "idb-state.json"),
            AUTONOM_IOS_LOG_MAX_MB=None, AUTONOM_CORESIMULATOR_DEVICES=str(self.devices_dir),
            DEVELOPER_DIR=None,
        )
        self.env = dict(os.environ)
        self.env["PATH"] = os.pathsep.join([str(self.bin_dir), "/usr/bin", "/bin"])
        reset = getattr(ios_simctl, "reset_caches", None)
        if reset is not None:
            reset()
            self.addCleanup(reset)

    # --- fakes ------------------------------------------------------------

    def write_state(self, **extra: Any) -> None:
        state = {"devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"]],
                 "ui_dump": str(UI_DUMP), "simctl_devices": _booted(),
                 "idb_describe_all": str(IOS_CATALOG)}
        state.update(extra)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def ios_app_state(self) -> dict[str, Any]:
        """The app under test, as `simctl appinfo` / `get_app_container` say."""
        return {"app_info": {BUNDLE: {"CFBundleExecutable": "Runner",
                                      "Path": str(self.app_bundle)}},
                "app_bundle": str(self.app_bundle)}

    def fake_state(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def calls(self, tool: str) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [entry["argv"] for entry in
                (json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines())
                if entry.get("tool") == tool]

    # --- the CLI ------------------------------------------------------------

    def run_raw(self, *argv: str, timeout: float = 120) -> subprocess.CompletedProcess:
        completed = subprocess.run(
            [sys.executable, str(CLI), *argv], cwd=self.root, env=self.env, text=True,
            stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=timeout)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        return completed

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        completed = self.run_raw(*argv)
        stream = completed.stdout if completed.returncode in (0, 1) else completed.stderr
        return completed.returncode, json.loads(stream)

    def run_stream(self, *argv: str) -> tuple[int, list[dict]]:
        completed = self.run_raw(*argv)
        return completed.returncode, [json.loads(line) for line in
                                      completed.stdout.splitlines() if line.strip()]

    def android(self, *argv: str) -> tuple[int, dict]:
        return self.run_cli("--adb", str(FAKE_ADB), "--serial", SERIAL, *argv)

    def ios(self, *argv: str) -> tuple[int, dict]:
        return self.run_cli("--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB),
                            "--udid", UDID, *argv)

    def ios_stream(self, *argv: str) -> tuple[int, list[dict]]:
        return self.run_stream("--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB),
                               "--udid", UDID, *argv)

    def prefs_dir(self, udid: str = UDID) -> Path:
        path = self.devices_dir / udid / "data/Library/Preferences"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def sleeper(self, *argv: str) -> subprocess.Popen:
        child = subprocess.Popen(list(argv) or [sys.executable, "-c",
                                                "import time; time.sleep(120)"])
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

    def write_flow(self, relative: str, body: str, app_id: str = BUNDLE,
                   extra_header: str = "") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        name = Path(relative).stem
        path.write_text(f"schema: autonom.dev/flow/v1\nappId: {app_id}\nid: {name}\n"
                        f"name: {name}\n{extra_header}---\n{body}", encoding="utf-8")
        return path


# --- 1. open <url> ------------------------------------------------------------


CHROME = "com.android.chrome/com.google.android.apps.chrome.Main"


def _am_wait(activity: str, *, before: str = "") -> str:
    return (f"Starting: Intent {{ act=android.intent.action.VIEW dat=https://example.net/... }}\n"
            f"{before}Status: ok\nLaunchState: COLD\nActivity: {activity}\n"
            "TotalTime: 812\nWaitTime: 830\nComplete\n")


class OpenUrlTests(_Cli):
    def view_starts(self) -> list[list[str]]:
        return [call for call in self.calls("adb") if call[2:5] == ["shell", "am", "start"]]

    def test_android_names_the_activity_that_took_the_url(self) -> None:
        """Before: `ok: true` and nothing else — a link Chrome swallowed read
        exactly like one that reached the app."""
        self.write_state(am_start_output=_am_wait(CHROME))
        code, payload = self.android("open", "https://example.net/product/42")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["handled_by"], CHROME)
        self.assertEqual(payload["launch"]["launch_state"], "cold")
        self.assertNotIn("warnings", payload)
        (start,) = self.view_starts()
        self.assertEqual(start[5:9], ["-W", "-a", "android.intent.action.VIEW", "-d"])

    def test_android_the_chooser_is_visible(self) -> None:
        resolver = "android/com.android.internal.app.ResolverActivity"
        self.write_state(am_start_output=_am_wait(resolver))
        code, payload = self.android("open", "myapp://product/42")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["handled_by"], resolver)
        self.assertEqual([w["code"] for w in payload["warnings"]], ["url_opened_chooser"])

    def test_android_a_url_nothing_handles_says_so(self) -> None:
        self.write_state(am_start_output=(
            "Starting: Intent { act=android.intent.action.VIEW dat=nope://x }\n"
            "Error: Activity not started, unable to resolve Intent "
            "{ act=android.intent.action.VIEW dat=nope://x flg=0x10000000 }\n"))
        code, payload = self.android("open", "nope://x")
        self.assertEqual(code, 0, payload)
        self.assertIsNone(payload["handled_by"])
        self.assertEqual([w["code"] for w in payload["warnings"]], ["url_not_handled"])

    def test_a_scheme_less_url_is_still_refused_before_dispatch(self) -> None:
        code, payload = self.android("open", "example.net/x")
        self.assertEqual((code, payload["error_code"]), (2, errors.INVALID_URL))
        self.assertEqual(self.view_starts(), [])

    def test_ios_says_it_cannot_tell(self) -> None:
        code, payload = self.ios("open", "myapp://product/42")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["handled_by"], "unknown")
        self.assertIn(["simctl", "openurl", UDID, "myapp://product/42"], self.calls("simctl"))


# --- 2. network status ----------------------------------------------------------


class NetworkStatusTests(_Cli):
    def attached_session(self, platform: str, flows: list[dict], **network: Any) -> dict:
        target_id = SERIAL if platform == "android" else UDID
        record = session.start_session(str(FAKE_ADB if platform == "android" else FAKE_SIMCTL),
                                       platform=platform, target_id=target_id, app_id=BUNDLE)
        record["network"].update({"attached": True, **network})
        session.save(record)
        path = store.flows_path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(flow) + "\n" for flow in flows), encoding="utf-8")
        return record

    def test_ios_host_traffic_is_not_attachment(self) -> None:
        """Before: a host `curl` through the proxy answered attached: true."""
        self.attached_session("ios", [_flow("127.0.0.1")], platform_manual=True)
        code, payload = self.ios("network", "status")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["attached"], payload["evidence"]),
                         ("unknown", "host_traffic_indistinguishable"))
        self.assertEqual((payload["recent_flow_count"], payload["target_flow_count"],
                          payload["unattributed_flow_count"]), (1, 0, 1))
        self.assertEqual(payload["recent_user_agents"], ["curl/8.7.1"])
        self.assertIn("next_action", payload)

    def test_android_guest_flows_prove_attachment(self) -> None:
        self.attached_session("android", [_flow("10.0.2.16", "okhttp/4.12")],
                              device_proxy="10.0.2.2:8080")
        code, payload = self.android("network", "status")
        self.assertEqual((payload["attached"], payload["evidence"]), (True, "target_flows"))
        self.assertEqual(payload["target_flow_count"], 1)

    def test_android_loopback_flows_defer_to_the_setting_read_back(self) -> None:
        self.write_state(settings={"http_proxy": "10.0.2.2:8080"})
        self.attached_session("android", [_flow("127.0.0.1")], device_proxy="10.0.2.2:8080")
        code, payload = self.android("network", "status")
        self.assertEqual((payload["attached"], payload["evidence"]), (True, "device_setting"))
        self.assertEqual(payload["unattributed_flow_count"], 1)
        self.assertIn("reason", payload)

    def test_an_unreadable_device_is_not_a_cleared_proxy(self) -> None:
        """Before: adb failing read as `device_proxy_cleared_externally`."""
        self.write_state(fail={f"-s {SERIAL} shell settings get global http_proxy":
                               [1, "error: device offline"]})
        self.attached_session("android", [], device_proxy="10.0.2.2:8080")
        code, payload = self.android("network", "status")
        self.assertEqual((payload["attached"], payload["evidence"]),
                         ("unknown", "setting_unreadable"))

    def test_status_raises_persistent_mocks_active(self) -> None:
        self.attached_session("android", [], device_proxy="10.0.2.2:8080")
        mocks.add(url_glob="*/v1/catalog")
        code, payload = self.android("network", "status")
        self.assertEqual(code, 0, payload)
        self.assertIn("persistent_mocks_active", [w["code"] for w in payload["warnings"]])


# --- 3. flow run: one shape for one file and a suite ----------------------------


SAVE_VISIBLE = "- assertVisible:\n    selector:\n      description: Flutter Save Button\n"


class FlowRunShapeTests(_Cli):
    def test_a_single_file_run_also_lists_its_run(self) -> None:
        flow = self.write_flow("flows/save.yaml", SAVE_VISIBLE)
        self.android("session", "start", "--app-id", BUNDLE)
        code, payload = self.android("flow", "run", str(flow))
        self.assertEqual(code, 0, payload)
        (run,) = payload["runs"]
        for key in ("status", "run_id", "flow", "name", "steps", "events"):
            self.assertEqual(run[key], payload[key], key)
        self.assertNotIn("ok", run)
        self.assertEqual(payload["status"], "passed")

    def test_a_suite_still_lists_every_run(self) -> None:
        self.write_flow("suite/a.yaml", SAVE_VISIBLE)
        self.write_flow("suite/b.yaml", SAVE_VISIBLE)
        self.android("session", "start", "--app-id", BUNDLE)
        code, payload = self.android("flow", "run", str(self.root / "suite"))
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["flows"], len(payload["runs"])), (2, 2))


class FlowSuitePlatformTests(_Cli):
    """G: a suite on iOS aborted at the first flow its pre-flight refused."""

    def test_flows_the_target_cannot_run_are_skipped_with_a_warning(self) -> None:
        android_only = self.write_flow("suite/a_back.yaml", "- back\n")
        self.write_flow("suite/b_catalog.yaml",
                        "- assertVisible:\n    selector:\n      description: Irina Weaver\n")
        code, started = self.ios("session", "start", "--app-id", BUNDLE)
        self.assertEqual(code, 0, started)
        code, payload = self.ios("flow", "run", str(self.root / "suite"))
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["skipped_flows"], [str(android_only.resolve())])
        (warning,) = [w for w in payload["warnings"] if w["code"] == "flow_skipped_for_platform"]
        self.assertEqual((warning["file"], warning["platforms"]),
                         (str(android_only.resolve()), ["android"]))
        self.assertEqual([run["name"] for run in payload["runs"]], ["b_catalog"])

    def test_a_suite_nothing_of_which_runs_is_refused(self) -> None:
        self.write_flow("suite/a_back.yaml", "- back\n")
        self.ios("session", "start", "--app-id", BUNDLE)
        code, payload = self.ios("flow", "run", str(self.root / "suite"))
        self.assertEqual((code, payload["error_code"]), (2, errors.FLOW_NO_FLOWS_FOUND))
        self.assertEqual(len(payload["skipped"]), 1)

    def test_a_single_file_is_still_refused_at_its_step(self) -> None:
        flow = self.write_flow("suite/a_back.yaml", "- back\n")
        self.ios("session", "start", "--app-id", BUNDLE)
        code, payload = self.ios("flow", "run", str(flow))
        self.assertEqual((code, payload["error_code"]), (2, errors.UNSUPPORTED_ON_PLATFORM))
        self.assertEqual(payload["line"], 6)


# --- 4. keyboard reboot / reinstall: the session's log stream -------------------


class LogStreamRestartTests(_Cli):
    def start_streaming_session(self) -> dict:
        self.prefs_dir()
        self.write_state(**self.ios_app_state(), ios_log=[self.mine])
        code, started = self.ios("session", "start", "--app-id", BUNDLE, "--install",
                                 str(self.root), "--log-stream")
        self.assertEqual(code, 0, started)
        pid = started["session"]["background"]["log_stream_pid"]
        self.assertTrue(pid)
        # the fake `log stream` prints its lines and ends: wait for the writer
        self.assertTrue(_wait_until(lambda: _gone(pid)))
        return started

    def current(self) -> dict:
        code, shown = self.run_cli("session", "show")
        self.assertEqual(code, 0, shown)
        return shown["session"]

    def test_keyboard_pin_with_reboot_restarts_the_stream(self) -> None:
        """Before: the reboot ended `log stream`; the session kept listing a
        stream that recorded nothing."""
        started = self.start_streaming_session()
        old = started["session"]["background"]["log_stream_pid"]
        code, payload = self.ios("simulator", "keyboard", "pin", "--value", "reboot=true")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["rebooted"])
        self.assertIs(payload["log_stream_restarted"], True)
        self.assertNotEqual(payload["log_stream_pid"], old)
        record = self.current()
        self.assertEqual(record["background"]["log_stream_pid"], payload["log_stream_pid"])
        streams = [s for s in record["streams"] if s["id"] == "log_stream"]
        self.assertEqual([s["pid"] for s in streams], [payload["log_stream_pid"]])
        spawned = [call for call in self.calls("simctl") if "stream" in call]
        self.assertEqual(len(spawned), 2, "one stream at start, one after the reboot")
        self.assertIn(f'processImagePath BEGINSWITH "{self.app_bundle}/"', spawned[-1][-1])

    def test_a_reboot_without_a_stream_reports_nothing_about_one(self) -> None:
        self.prefs_dir()
        self.ios("session", "start", "--app-id", BUNDLE)
        code, payload = self.ios("simulator", "keyboard", "pin", "--value", "reboot=true")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("log_stream_restarted", payload)

    def test_session_clear_reinstall_restarts_the_stream_on_the_new_container(self) -> None:
        started = self.start_streaming_session()
        old = started["session"]["background"]["log_stream_pid"]
        code, payload = self.ios("session", "clear", BUNDLE, "--strategy", "reinstall")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["log_stream_restarted"])
        self.assertNotEqual(payload["log_stream_pid"], old)

    def test_a_restart_that_fails_warns_log_stream_stopped(self) -> None:
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=BUNDLE)
        record["background"]["log_stream_pid"] = 999_999_999  # long gone
        session.save(record)
        target = Target("ios", UDID, str(FAKE_SIMCTL), {"udid": UDID})
        with mock.patch.object(ios_simctl, "start_log_stream", return_value=None), \
                mock.patch.object(ios_simctl, "app_image", return_value=("Runner", None)):
            result = simulator.restart_session_log_stream(target, cause="reboot")
        self.assertIs(result["log_stream_restarted"], False)
        (warning,) = result["warnings"]
        self.assertEqual(warning["code"], "log_stream_stopped")
        self.assertIn("reboot", warning["error"])
        self.assertIsNone(session.load_current()["background"]["log_stream_pid"])

    def record_pid(self, pid: int) -> dict:
        """Point the current session's `log_stream_pid` at `pid`, as a stale
        session file does once the pid has been reused."""
        record = session.load_current()
        record["background"]["log_stream_pid"] = pid
        session.save(record)
        return record

    def writer_like(self, record: dict) -> subprocess.Popen:
        """The real bounded writer of the session's stream file, over a child
        that just waits."""
        return _bounded_writer(self, Path(record["artifacts_dir"]) / "logs/stream.ndjson")

    def test_an_unrelated_pid_survives_a_reboot(self) -> None:
        """Review round 1: a stranger's pid recorded as `log_stream_pid` got
        SIGTERM (then SIGKILL) from `keyboard pin reboot=true`."""
        self.start_streaming_session()
        stranger = self.sleeper()
        self.record_pid(stranger.pid)
        code, payload = self.ios("simulator", "keyboard", "pin", "--value", "reboot=true")
        self.assertEqual(code, 0, payload)
        time.sleep(0.5)
        self.assertIsNone(stranger.poll(), "an unrelated process was signalled")
        # not the writer, so the session gets a real stream back
        self.assertIs(payload["log_stream_restarted"], True)
        self.assertNotEqual(payload["log_stream_pid"], stranger.pid)

    def test_an_unrelated_pid_survives_a_reinstall(self) -> None:
        self.start_streaming_session()
        stranger = self.sleeper()
        self.record_pid(stranger.pid)
        code, payload = self.ios("session", "clear", BUNDLE, "--strategy", "reinstall")
        self.assertEqual(code, 0, payload)
        time.sleep(0.5)
        self.assertIsNone(stranger.poll(), "an unrelated process was signalled")
        self.assertEqual(payload["previous_log_stream"],
                         {"pid": stranger.pid, "result": "pid_reused"})
        self.assertIs(payload["log_stream_restarted"], True)

    def test_a_registry_row_whose_pid_was_reused_is_not_signalled(self) -> None:
        self.start_streaming_session()
        stranger = self.sleeper()
        record = self.record_pid(stranger.pid)
        processes.register("log_stream", stranger.pid, owner=record["session_id"],
                           artifacts_dir=record["artifacts_dir"],
                           signature=str(Path(record["artifacts_dir"]) / "logs/stream.ndjson"))
        code, payload = self.ios("session", "clear", BUNDLE, "--strategy", "reinstall")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["previous_log_stream"]["result"], "pid_reused")
        self.assertIsNone(stranger.poll())
        self.assertNotIn(stranger.pid, [row["pid"] for row in processes.entries()])

    def test_a_reinstall_stops_the_verified_writer(self) -> None:
        self.start_streaming_session()
        writer = self.writer_like(session.load_current())
        self.record_pid(writer.pid)
        code, payload = self.ios("session", "clear", BUNDLE, "--strategy", "reinstall")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["previous_log_stream"],
                         {"pid": writer.pid, "result": "terminated"})
        self.assertTrue(_wait_until(lambda: writer.poll() is not None))
        self.assertIs(payload["log_stream_restarted"], True)

    def test_ensure_log_stream_restarts_past_a_reused_pid(self) -> None:
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=BUNDLE)
        stranger = self.sleeper()
        record["background"]["log_stream_pid"] = stranger.pid
        target = Target("ios", UDID, str(FAKE_SIMCTL), {"udid": UDID})
        writer = self.sleeper()
        with mock.patch.object(ios_simctl, "start_log_stream", return_value=writer.pid), \
                mock.patch.object(ios_simctl, "app_image", return_value=("Runner", None)):
            result = logs.ensure_log_stream(target, record)
            self.assertEqual((result["restarted"], result["pid"], result["previous_state"]),
                             (True, writer.pid, "pid_reused"))
            # the real writer is alive: nothing to do
            ours = self.writer_like(record)
            record["background"]["log_stream_pid"] = ours.pid
            self.assertEqual(logs.ensure_log_stream(target, record)["reason"], "alive")
            # alive, but ps cannot say: no second writer is started
            with mock.patch.object(processes, "command_of", return_value=None):
                self.assertEqual(logs.ensure_log_stream(target, record)["reason"],
                                 "unverified")
        self.assertIsNone(stranger.poll())

    def test_an_unverifiable_writer_is_skipped_with_a_warning(self) -> None:
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=BUNDLE)
        live = self.sleeper()
        record["background"]["log_stream_pid"] = live.pid
        session.save(record)
        target = Target("ios", UDID, str(FAKE_SIMCTL), {"udid": UDID})
        with mock.patch.object(processes, "command_of", return_value=None), \
                mock.patch.object(ios_simctl, "start_log_stream") as start:
            result = simulator.restart_session_log_stream(target, cause="reinstall",
                                                          stop_previous=True)
        start.assert_not_called()
        self.assertEqual(result["previous_log_stream"]["result"], "unverified_skipped")
        self.assertEqual([w["code"] for w in result["warnings"]], ["unverified_skipped"])
        self.assertIsNone(live.poll())

    def test_another_simulators_session_is_left_alone(self) -> None:
        session.start_session(str(FAKE_SIMCTL), platform="ios", target_id="OTHER-UDID",
                              app_id=BUNDLE)
        target = Target("ios", UDID, str(FAKE_SIMCTL), {"udid": UDID})
        with mock.patch.object(logs, "ensure_log_stream") as ensure:
            self.assertEqual(simulator.restart_session_log_stream(target), {})
        ensure.assert_not_called()


# --- 5. session start reports what it did ---------------------------------------


class SessionStartOutcomeTests(_Cli):
    def test_android_install_and_launch_are_reported(self) -> None:
        apk = self.root / "app.apk"
        apk.write_bytes(b"apk")
        code, payload = self.android("session", "start", "--app-id", BUNDLE,
                                     "--install", str(apk), "--launch")
        self.assertEqual(code, 0, payload)
        self.assertIs(payload["installed"], True)
        launched = payload["launched"]
        self.assertEqual((launched["app_id"], launched["mode"], launched["component"]),
                         (BUNDLE, "resume", f"{BUNDLE}/.MainActivity"))
        self.assertEqual(launched["launch"]["launch_state"], "cold")
        self.assertFalse(any("monkey" in call for call in self.calls("adb")))

    def test_ios_launch_names_the_pid(self) -> None:
        code, payload = self.ios("session", "start", "--app-id", BUNDLE, "--launch")
        self.assertEqual(code, 0, payload)
        self.assertIs(payload["installed"], False)
        self.assertEqual(payload["launched"], {"app_id": BUNDLE, "pid": 4242})

    def test_nothing_asked_nothing_done(self) -> None:
        code, payload = self.android("session", "start", "--app-id", BUNDLE)
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["installed"], payload["launched"]), (False, None))


# --- ui-core wiring, and H: `ui find --all` in --index order ----------------------


def _add_screen(*frames: tuple[int, int, int, int]) -> str:
    elements: list[dict[str, Any]] = [
        {"AXLabel": "Yarn Shop", "type": "Application",
         "frame": {"x": 0, "y": 0, "width": 440, "height": 956}}]
    for x, y, width, height in frames:
        elements.append({"AXLabel": "Add", "type": "Button",
                         "frame": {"x": x, "y": y, "width": width, "height": height}})
    return json.dumps(elements)


# the reviewer's screen: a 0x0 "Add" first (n1), then two on screen (n2, n3)
REVIEWER_SCREEN = _add_screen((0, 0, 0, 0), (16, 200, 100, 44), (16, 600, 100, 44))


def _sweep_screens() -> list[tuple[str, list[dict[str, Any]]]]:
    catalog = json.loads(IOS_CATALOG.read_text(encoding="utf-8"))
    scrolled = json.loads(json.dumps(catalog))
    irinas = [e for e in scrolled if e["AXLabel"] == "Irina Weaver"]
    irinas[0]["frame"] = {"x": 0, "y": 0, "width": 0, "height": 0}
    irinas[1]["frame"] = {"x": 16, "y": 640, "width": 200, "height": 44}
    screens = [
        ("ios catalog", ui.annotate_parents(ui_ios.parse_all(json.dumps(catalog)))),
        ("ios catalog scrolled", ui.annotate_parents(ui_ios.parse_all(json.dumps(scrolled)))),
        ("reviewer", ui.annotate_parents(ui_ios.parse_all(REVIEWER_SCREEN))),
        ("above then one", ui.annotate_parents(ui_ios.parse_all(
            _add_screen((16, -300, 200, 44), (16, 100, 200, 44))))),
        ("all off screen", ui.annotate_parents(ui_ios.parse_all(
            _add_screen((0, 0, 0, 0), (0, 0, 0, 0))))),
    ]
    for name in ("android_email_focused.xml", "android_long_press.xml", "android_sign_in.xml"):
        screens.append((name, ui.annotate_parents(ui_android.parse_all(
            (WOOLBOX / name).read_text(encoding="utf-8")))))
    return screens


class FindAllOrderTests(unittest.TestCase):
    """H: `ui find --all` position i is what `--index i` selects."""

    def selectors(self, nodes: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
        chosen: list[tuple[dict[str, Any], str]] = []
        for node in nodes:
            for field in ("desc", "text", "resource_id"):
                if node.get(field):
                    chosen.append(({field: node[field]}, "exact"))
        chosen += [({"desc": "a"}, "contains"), ({"text": "e"}, "contains"),
                   ({"desc": "Add"}, "exact"), ({"desc": "Weaver"}, "contains")]
        return chosen

    def test_every_position_round_trips_through_index(self) -> None:
        checked = 0
        for name, nodes in _sweep_screens():
            for fields, mode in self.selectors(nodes):
                listed = ui.select_for_find(nodes, fields, mode=mode, all_matches=True)
                for position, match in enumerate(listed):
                    with self.subTest(screen=name, fields=fields, position=position):
                        try:
                            (picked,) = ui.select_for_find(nodes, fields, mode=mode,
                                                           index=position)
                        except errors.AutonomError as exc:
                            self.assertEqual(exc.code, errors.SELECTOR_INDEX_OUT_OF_RANGE)
                            self.assertIsNone(match["index"])
                            continue
                        self.assertEqual(picked["ref"], match["ref"])
                        self.assertEqual(match["index"], position)
                        checked += 1
        self.assertGreater(checked, 50)

    def test_on_screen_first_then_off_screen_in_tree_order(self) -> None:
        nodes = ui.annotate_parents(ui_ios.parse_all(REVIEWER_SCREEN))
        listed = ui.select_for_find(nodes, {"desc": "Add"}, mode="exact", all_matches=True)
        self.assertEqual([(m["ref"], m["index"], m.get("visible", True)) for m in listed],
                         [("n2", 0, True), ("n3", 1, True), ("n1", None, False)])
        # nothing on screen: tree order, and every index reaches its node
        nodes = ui.annotate_parents(ui_ios.parse_all(_add_screen((0, 0, 0, 0), (0, 0, 0, 0))))
        listed = ui.select_for_find(nodes, {"desc": "Add"}, mode="exact", all_matches=True)
        self.assertEqual([(m["ref"], m["index"]) for m in listed], [("n1", 0), ("n2", 1)])

    def test_the_tree_itself_is_not_touched(self) -> None:
        nodes = ui.annotate_parents(ui_ios.parse_all(REVIEWER_SCREEN))
        ui.select_for_find(nodes, {"desc": "Add"}, mode="exact", all_matches=True)
        self.assertFalse(any("index" in node for node in nodes))


class UiWiringCliTests(_Cli):
    def use_screen(self, payload: str) -> None:
        screen = self.root / "screen.json"
        screen.write_text(payload, encoding="utf-8")
        self.write_state(idb_describe_all=str(screen))

    def test_find_all_indexes_round_trip_through_ui_tap(self) -> None:
        self.use_screen(REVIEWER_SCREEN)
        code, found = self.ios("ui", "find", "--desc", "Add", "--mode", "exact", "--all")
        self.assertEqual(code, 0, found)
        self.assertEqual([(m["ref"], m["index"]) for m in found["matches"]],
                         [("n2", 0), ("n3", 1), ("n1", None)])
        for match in found["matches"]:
            if match["index"] is None:
                continue
            with self.subTest(ref=match["ref"]):
                code, tapped = self.ios("ui", "tap", "--desc", "Add", "--mode", "exact",
                                        "--index", str(match["index"]))
                self.assertEqual(code, 0, tapped)
                self.assertEqual(tapped["ref"], match["ref"])
        taps = [call for call in self.calls("idb") if call[:2] == ["ui", "tap"]]
        self.assertEqual([call[2:4] for call in taps], [["66", "222"], ["66", "622"]])

    def test_ui_tap_index_1_taps_n3(self) -> None:
        self.use_screen(REVIEWER_SCREEN)
        code, tapped = self.ios("ui", "tap", "--desc", "Add", "--mode", "exact",
                                "--index", "1")
        self.assertEqual((code, tapped["ref"], tapped["x"], tapped["y"]), (0, "n3", 66, 622))

    def test_an_ios_dump_tree_marks_offscreen_nodes(self) -> None:
        code, tree = self.run_cli("ui", "tree", "--dump", str(IOS_CATALOG))
        self.assertEqual(code, 0, tree)
        whisper = [n for n in tree["nodes"] if n.get("desc") == 'Cardigan "Whisper"']
        self.assertIs(whisper[0]["visible"], False)
        radiance = [n for n in tree["nodes"] if n.get("desc") == 'Tank top "Radiance"']
        self.assertNotIn("visible", radiance[0])

    def test_find_all_from_a_dump_marks_and_orders(self) -> None:
        code, found = self.run_cli("ui", "find", "--dump", str(IOS_CATALOG), "--desc",
                                   "Irina Weaver", "--mode", "exact", "--all")
        self.assertEqual(code, 0, found)
        self.assertEqual([(m.get("visible", True), m["index"]) for m in found["matches"]],
                         [(True, 0), (False, None)])


class IosNodeParityTests(unittest.TestCase):
    """A: iOS nodes carry the Android schema's long_clickable/checkable."""

    ELEMENTS = [
        {"type": "Application", "AXLabel": "App", "frame": {"x": 0, "y": 0, "width": 440,
                                                            "height": 956}},
        {"type": "Switch", "AXLabel": "Wi-Fi", "AXValue": "1",
         "frame": {"x": 0, "y": 100, "width": 60, "height": 30}},
        {"type": "Other", "role": "AXCheckBox", "AXLabel": "Accept",
         "frame": {"x": 0, "y": 200, "width": 30, "height": 30}},
        {"type": "Button", "AXLabel": "Save", "frame": {"x": 0, "y": 300, "width": 90,
                                                        "height": 44}},
    ]

    def test_key_sets_match_and_toggles_are_checkable(self) -> None:
        nodes = ui_ios.parse_all(json.dumps(self.ELEMENTS))
        android = ui_android.parse_all(UI_DUMP.read_text(encoding="utf-8"))
        self.assertEqual(set(nodes[0]), set(android[0]))
        by_label = {node["desc"]: node for node in nodes}
        self.assertEqual({label: by_label[label]["checkable"] for label in by_label},
                         {"App": False, "Wi-Fi": True, "Accept": True, "Save": False})
        self.assertTrue(all(node["long_clickable"] is False for node in nodes))
        self.assertIs(by_label["Wi-Fi"]["checked"], True)


# --- iOS logs: one resolved matching path (ios-dev + logs-proc, E) ---------------


class IosLogWiringTests(_Cli):
    def test_follow_spawns_a_stream_filtered_to_the_installed_bundle(self) -> None:
        """Before: `--predicate` came from the unresolved subsystem/leaf form,
        which for `com.example.app` never named the Flutter `Runner`."""
        self.write_state(**self.ios_app_state(),
                         ios_log=[self.mine, "Filtering the log data using x"])
        code, lines = self.ios_stream("logs", "follow", "--source", "device", "--package",
                                      BUNDLE, "--max-seconds", "10")
        self.assertEqual(code, 0, lines)
        self.assertEqual([json.loads(line["text"])["eventMessage"]
                          for line in lines if line.get("kind") == "line"], ["mine"])
        stream = next(call for call in self.calls("simctl") if "stream" in call)
        predicate = stream[stream.index("--predicate") + 1]
        self.assertIn(f'processImagePath BEGINSWITH "{self.app_bundle}/"', predicate)
        self.assertFalse(hasattr(logs, "_ios_predicate"), "one resolved path only")

    def test_follow_of_the_live_session_file_keeps_only_the_app(self) -> None:
        self.write_state(**self.ios_app_state())
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=BUNDLE)
        writer = self.sleeper()
        record["background"]["log_stream_pid"] = writer.pid
        session.save(record)
        stream = Path(record["artifacts_dir"]) / "logs/stream.ndjson"
        stream.write_text(f"{self.theirs}\n{self.mine}\n", encoding="utf-8")
        code, lines = self.ios_stream("logs", "follow", "--source", "device", "--package",
                                      BUNDLE, "--from-start", "--max-seconds", "2")
        self.assertEqual(code, 0, lines)
        emitted = [line for line in lines if line.get("kind") == "line"]
        self.assertEqual([json.loads(line["text"])["eventMessage"] for line in emitted],
                         ["mine"])

    def test_past_session_replay_matches_the_recorded_executable(self) -> None:
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=BUNDLE)
        record["background"]["log_stream_executable"] = "Runner"
        session.save(record)
        stream = Path(record["artifacts_dir"]) / "logs/stream.ndjson"
        other = _ios_line("/x/Other.app/Other", "other")
        stream.write_text(f"{other}\n{self.mine}\n", encoding="utf-8")
        session.stop_session(reap=False)
        code, lines = self.ios_stream("logs", "follow", "--source", "device",
                                      "--session-id", record["session_id"],
                                      "--package", BUNDLE, "--max-seconds", "2")
        self.assertEqual(code, 0, lines)
        self.assertEqual([json.loads(line["text"])["eventMessage"]
                          for line in lines if line.get("kind") == "line"], ["mine"])

    def test_tail_names_the_executable_and_the_bundle_predicate(self) -> None:
        self.write_state(**self.ios_app_state(), ios_log=[self.mine])
        code, payload = self.ios("logs", "tail", "--package", BUNDLE)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["executable"], "Runner")
        show = next(call for call in self.calls("simctl") if "show" in call)
        self.assertIn(f'processImagePath BEGINSWITH "{self.app_bundle}/"',
                      show[show.index("--predicate") + 1])


# --- Android logs (logs-proc 3, 5) -----------------------------------------------


AM = "1000  1100 I ActivityManager"
LOGCAT = [
    "09-26 12:00:00.000  4242  4242 I flutter : [Network] GET /catalog",
    f"09-26 12:00:01.000  {AM}: Start proc 4242:com.example.app/u0a123 for top-activity",
    "09-26 12:00:02.000  5555  5555 I chatty  : another app talking",
]
UID_STATE = {
    "getprop": {"ro.build.version.sdk": "34"},
    "packages": {BUNDLE: 10123, "com.other": 10200},
    "pid_uids": {"4242": 10123, "5555": 10200, "1000": 1000},
    "pidof": {},
    "logcat": LOGCAT,
}


class AndroidLogWiringTests(_Cli):
    def test_follow_runs_the_uid_plan_and_names_its_filter(self) -> None:
        """Before: `--pid=` of a running process only; a stopped app followed
        every line on the device."""
        self.write_state(**UID_STATE)
        code, lines = self.run_stream("--adb", str(FAKE_ADB), "--serial", SERIAL, "logs",
                                      "follow", "--source", "device", "--package", BUNDLE,
                                      "--max-seconds", "10")
        self.assertEqual(code, 0, lines)
        emitted = [line["text"] for line in lines if line.get("kind") == "line"]
        self.assertNotIn(LOGCAT[2], emitted)
        self.assertIn(LOGCAT[0], emitted)
        self.assertEqual(lines[-1]["kind"], "eof")
        self.assertEqual(lines[-1]["filter"], "uid")
        self.assertTrue(any("--uid=10123" in call for call in self.calls("adb")))

    def test_follow_of_an_unknown_package_is_app_not_installed(self) -> None:
        self.write_state(**UID_STATE)
        code, payload = self.android("logs", "follow", "--source", "device", "--package",
                                     "com.typo.app", "--max-seconds", "5")
        self.assertEqual((code, payload["error_code"]), (2, errors.APP_NOT_INSTALLED))

    def test_tail_reports_its_filter(self) -> None:
        self.write_state(**UID_STATE)
        code, payload = self.android("logs", "tail", "--package", BUNDLE, "--since", "0")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["filter"], "uid")
        self.assertNotIn(LOGCAT[2], [entry["line"] for entry in payload["lines"]])


# --- session lifecycle wiring (logs-proc 2, 7; round 2) ---------------------------


COMPANION = "#!{python}\nimport time\ntime.sleep(120)\n"


class SessionTeardownTests(_Cli):
    def ios_session(self, udid: str) -> dict:
        self.write_state(simctl_devices=_booted(udid))
        return session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=udid,
                                     app_id=BUNDLE)

    def test_stop_reaps_owned_processes_and_names_foreign_companions(self) -> None:
        udid = f"FAKE-{uuid.uuid4().hex[:8].upper()}-0000-0000-0000-000000000000"
        record = self.ios_session(udid)
        writer = self.sleeper(sys.executable, "-c", "import time; time.sleep(120)",
                              str(Path(record["artifacts_dir"]) / "logs/stream.ndjson"))
        processes.register("log_stream", writer.pid, owner=record["session_id"],
                           session_id=record["session_id"],
                           artifacts_dir=record["artifacts_dir"], target_id=udid,
                           signature=str(Path(record["artifacts_dir"]) / "logs/stream.ndjson"))
        companion = self.root / "idb_companion"
        companion.write_text(COMPANION.format(python=sys.executable), encoding="utf-8")
        companion.chmod(0o755)
        foreign = self.sleeper(str(companion), "--udid", udid)
        self.assertTrue(_wait_until(lambda: any(
            item["pid"] == foreign.pid for item in processes.discover_companions(udid))))
        code, payload = self.run_cli("--simctl", str(FAKE_SIMCTL), "--udid", udid,
                                     "session", "stop")
        self.assertEqual(code, 0, payload)
        (reaped,) = [item for item in payload["teardown"]
                     if item["action"] == "session_processes"]
        self.assertEqual([item["pid"] for item in reaped["detail"]["terminated"]],
                         [writer.pid])
        self.assertTrue(_wait_until(lambda: writer.poll() is not None))
        self.assertEqual([t["pid"] for t in payload["session"]["process_teardown"]["terminated"]],
                         [writer.pid])
        (warning,) = [w for w in payload["warnings"] if w["code"] == "companion_left_running"]
        self.assertEqual([item["pid"] for item in warning["companions"]], [foreign.pid])
        self.assertIsNone(foreign.poll(), "a companion Autonom did not start was killed")

    def test_stop_never_signals_unrelated_recorded_pids(self) -> None:
        """Review round 1: `session stop` sent SIGTERM/SIGKILL to whatever
        now held the recorded log-stream and recorder pids."""
        code, started = self.android("session", "start", "--app-id", BUNDLE)
        self.assertEqual(code, 0, started)
        writer, recorder = self.sleeper(), self.sleeper()
        record = session.load_current()
        record["background"].update({"log_stream_pid": writer.pid,
                                     "recorder_pid": recorder.pid,
                                     "recorder_path": str(self.root / "rec.mp4")})
        session.save(record)
        code, payload = self.android("session", "stop")
        self.assertEqual(code, 0, payload)
        time.sleep(0.5)
        self.assertIsNone(writer.poll(), "an unrelated process was signalled")
        self.assertIsNone(recorder.poll(), "an unrelated process was signalled")
        details = {item["action"]: item["detail"] for item in payload["teardown"]}
        self.assertEqual((details["log_stream"], details["recorder"]), (False, False))
        self.assertEqual(payload["teardown_verification"], {
            "log_stream": {"pid": writer.pid, "result": "pid_reused"},
            "recorder": {"pid": recorder.pid, "result": "pid_reused"}})

    def test_stop_stops_a_verified_recorder_on_either_platform(self) -> None:
        cases = {
            "ios": ["simctl", "io", UDID, "recordVideo", "--codec", "h264", "--force",
                    "{path}"],
            "android": ["-s", SERIAL, "shell", "screenrecord",
                        "/sdcard/autonom-recording.mp4"],
        }
        for platform, tail in cases.items():
            with self.subTest(platform=platform):
                target_id = UDID if platform == "ios" else SERIAL
                record = session.start_session(str(FAKE_ADB), platform=platform,
                                               target_id=target_id, app_id=BUNDLE)
                path = str(Path(record["artifacts_dir"]) / "recordings/demo.mp4")
                recorder = self.sleeper(sys.executable, "-c",
                                        "import time; time.sleep(120)",
                                        *[part.format(path=path) for part in tail])
                record["background"].update({"recorder_pid": recorder.pid,
                                             "recorder_path": path})
                session.save(record)
                tools = (("--simctl", str(FAKE_SIMCTL), "--udid", UDID) if platform == "ios"
                         else ("--adb", str(FAKE_ADB), "--serial", SERIAL))
                code, payload = self.run_cli(*tools, "session", "stop")
                self.assertEqual(code, 0, payload)
                details = {item["action"]: item["detail"] for item in payload["teardown"]}
                self.assertIs(details["recorder"], True)
                self.assertTrue(_wait_until(lambda: recorder.poll() is not None))
                self.assertNotIn("teardown_verification", payload)

    def test_an_unverifiable_recorder_is_skipped_with_a_warning(self) -> None:
        cli = _load_cli()
        live = self.sleeper()
        record = {"target_id": SERIAL, "background": {"recorder_pid": live.pid}}
        with mock.patch.object(processes, "command_of", return_value=None):
            self.assertEqual(cli._stop_recorder(record),  # noqa: SLF001
                             {"pid": live.pid, "result": "unverified_skipped"})
        self.assertIsNone(live.poll())

    def test_record_stop_does_not_interrupt_a_reused_pid(self) -> None:
        self.android("session", "start", "--app-id", BUNDLE)
        stranger = self.sleeper()
        record = session.load_current()
        record["background"].update({"recorder_pid": stranger.pid,
                                     "recorder_path": str(self.root / "rec.mp4")})
        session.save(record)
        code, payload = self.android("record", "stop")
        self.assertEqual(code, 0, payload)
        time.sleep(0.5)
        self.assertIsNone(stranger.poll(), "SIGINT reached an unrelated process")
        self.assertEqual(payload["recorder"], {"pid": stranger.pid, "result": "pid_reused"})
        self.assertFalse(payload["was_recording"])
        # nor does it block a new recording as "already in progress"
        record = session.load_current()
        record["background"]["recorder_pid"] = stranger.pid
        session.save(record)
        cli = _load_cli()
        self.assertEqual(cli._recorder_state(record, stranger.pid), "pid_reused")  # noqa: SLF001

    def test_android_resume_launch_reports_the_component_and_report(self) -> None:
        code, payload = self.android("session", "launch", BUNDLE)
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["mode"], payload["component"]),
                         ("resume", f"{BUNDLE}/.MainActivity"))
        self.assertEqual(payload["launch"]["total_time_ms"], 412)


def _load_cli():
    import importlib.util

    spec = importlib.util.spec_from_file_location("autonom_cli_under_test", CLI)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


LEADER_EXITS = textwrap.dedent("""\
    import pathlib, subprocess, sys
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    pathlib.Path(sys.argv[1]).write_text(str(worker.pid))
    worker.returncode = 0  # leave it running: no "still running" warning at exit
""")


class GroupRemnantTests(_Cli):
    """Round 2 + K: a group whose recorded leader is gone is reported by
    `cleanup` and `doctor`, never signalled."""

    def remnant(self) -> tuple[int, int]:
        pidfile = self.root / "worker.pid"
        leader = subprocess.Popen([sys.executable, "-c", LEADER_EXITS, str(pidfile)],
                                  start_new_session=True)
        self.assertEqual(leader.wait(timeout=30), 0)
        self.assertTrue(_wait_until(lambda: pidfile.exists() and pidfile.read_text()))
        worker = int(pidfile.read_text())
        self.addCleanup(_kill_quietly, worker)
        processes.register("canvas_child", leader.pid, process_group=leader.pid,
                           parent_pid=999_999, signature="-c")
        return leader.pid, worker

    def test_cleanup_warns_and_leaves_it_running(self) -> None:
        pgid, worker = self.remnant()
        code, payload = self.run_cli("cleanup", "--dry-run")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["skipped_group_remnants"], 1)
        (warning,) = [w for w in payload["warnings"] if w["code"] == "group_remnant_left_running"]
        self.assertIn("kill -- -<pgid>", warning["hint"])
        self.assertFalse(_gone(worker))

    def test_doctor_names_each_remnant_with_its_hint(self) -> None:
        pgid, worker = self.remnant()
        code, payload = self.run_cli("doctor")
        self.assertEqual(code, 0, payload)
        (warning,) = [w for w in payload["warnings"] if w["code"] == "group_remnant_left_running"]
        self.assertIn(f"kill -- -{pgid}", warning["hint"])
        self.assertIn(worker, warning["group_remnant"]["members"])
        self.assertFalse(_gone(worker))

    def test_doctor_library_level(self) -> None:
        remnant = {"kind": "canvas_child", "pid": 4321, "members": [4322],
                   "hint": "Inspect them with 'ps -Ao pid,pgid,command' (pgid 4321)"}
        scanned = {"live": [], "orphans": [], "stale_entries": [], "harness": [],
                   "background": [], "group_remnants": [remnant]}
        with mock.patch.object(processes, "scan", return_value=scanned):
            _network, _orphans, warnings = doctor._runtime_state(None)  # noqa: SLF001
        (warning,) = [w for w in warnings if w["code"] == "group_remnant_left_running"]
        self.assertEqual(warning["hint"], remnant["hint"])


# --- canvas serve is supervised (logs-proc 8) --------------------------------------


FAKE_NODE = textwrap.dedent("""\
    #!{python}
    import json, os, pathlib, sys
    registry = pathlib.Path(os.environ["AUTONOM_HOME"]) / "processes" / "processes.json"
    rows = json.loads(registry.read_text())["processes"] if registry.exists() else []
    pathlib.Path(os.environ["FAKE_NODE_OUT"]).write_text(json.dumps(
        {{"argv": sys.argv[1:], "rows": rows}}))
""")


class CanvasSupervisedTests(_Cli):
    def test_the_bridge_runs_registered_and_owned_by_the_session(self) -> None:
        """Before: `subprocess.run(node …)` — invisible to `processes`,
        `cleanup --all` and `session stop`."""
        node = self.bin_dir / "node"
        node.write_text(FAKE_NODE.format(python=sys.executable), encoding="utf-8")
        node.chmod(0o755)
        out = self.root / "node.json"
        self.env["FAKE_NODE_OUT"] = str(out)
        code, started = self.android("session", "start", "--app-id", BUNDLE)
        self.assertEqual(code, 0, started)
        completed = self.run_raw("--adb", str(FAKE_ADB), "--serial", SERIAL, "canvas",
                                 "serve", "--token", "s3cret-token-value", "--port", "3999")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        seen = json.loads(out.read_text(encoding="utf-8"))
        self.assertIn("--token", seen["argv"])
        kinds = {row["kind"]: row for row in seen["rows"]}
        self.assertEqual(set(kinds), {"canvas", "canvas_child"})
        self.assertEqual(kinds["canvas"]["owner"], started["session"]["session_id"])
        self.assertEqual(kinds["canvas_child"]["port"], 3999)
        self.assertNotIn("s3cret-token-value", json.dumps(seen["rows"]))
        self.assertEqual(processes.entries(), [], "both rows are removed when it ends")


# --- metrics wiring ------------------------------------------------------------------


ZERO_FRAMES = ("Stats since: 1\nTotal frames rendered: 0\nJanky frames: 0 (0.00%)\n"
               "50th percentile: 4950ms\n90th percentile: 4950ms\n"
               "95th percentile: 4950ms\n99th percentile: 4950ms\n")


class MetricsWiringTests(_Cli):
    def test_ios_frames_hint_does_not_send_the_simulator_to_hitches(self) -> None:
        code, payload = self.ios("metrics", "frames", "reset", "--app-id", BUNDLE)
        self.assertEqual((code, payload["error_code"]), (2, errors.UNSUPPORTED_ON_PLATFORM))
        self.assertEqual(payload["hint"], metrics_frames.IOS_FRAMES_HINT)
        self.assertIn("physical device", payload["hint"])

    def test_zero_frames_warn_at_the_top_level(self) -> None:
        self.write_state(dumpsys_gfxinfo=ZERO_FRAMES)
        code, payload = self.android("metrics", "frames", "capture", "--app-id", BUNDLE)
        self.assertEqual(code, 0, payload)
        self.assertEqual([w["code"] for w in payload["warnings"]], ["no_frames"])
        self.assertNotIn("percentile_50_ms", payload["summary"])


# --- flows wiring ----------------------------------------------------------------------


class FlowWiringTests(_Cli):
    def test_flow_list_and_check_infer_the_platforms(self) -> None:
        self.write_flow("flows/back.yaml", "- back\n")
        self.write_flow("flows/any.yaml", SAVE_VISIBLE)
        code, listed = self.run_cli("flow", "list", str(self.root / "flows"))
        self.assertEqual(code, 0, listed)
        self.assertEqual({Path(f["file"]).name: f["platforms"] for f in listed["flows"]},
                         {"back.yaml": ["android"], "any.yaml": ["android", "ios"]})
        code, checked = self.run_cli("flow", "check", str(self.root / "flows/back.yaml"))
        self.assertEqual(checked["flows"][0]["platforms"], ["android"])

    def test_report_suite_last_counts_flows_not_runs(self) -> None:
        """Before: `--last 2` after a re-run showed the same flow twice."""
        first = self.write_flow("flows/a.yaml", SAVE_VISIBLE)
        second = self.write_flow("flows/b.yaml", SAVE_VISIBLE)
        self.android("session", "start", "--app-id", BUNDLE)
        for flow in (first, second, first, first):
            code, ran = self.android("flow", "run", str(flow))
            self.assertEqual(code, 0, ran)
        code, suite = self.run_cli("report", "suite", "--last", "2")
        self.assertEqual(code, 0, suite)
        self.assertEqual(suite["flows"], 2)
        junit = Path(suite["junit"]).read_text(encoding="utf-8")
        self.assertIn('name="a"', junit)
        self.assertIn('name="b"', junit)
        code, history = self.run_cli("report", "history", "--last", "2")
        self.assertEqual(code, 0, history)


class FailureClassTests(unittest.TestCase):
    def test_element_offscreen_is_a_test_failure(self) -> None:
        """F: an off-screen node is the app's state, never infrastructure."""
        self.assertEqual(flow_schema.failure_class(errors.ELEMENT_OFFSCREEN),
                         flow_schema.TEST_FAILURE)


# --- B: clipboard get without xcrun ---------------------------------------------------------


class ClipboardToolMissingTests(_Cli):
    def test_a_missing_xcrun_is_simctl_not_found(self) -> None:
        """Before: backend_failed, 'could not read the pasteboard'."""
        code, payload = self.run_cli("--simctl", "/nonexistent/xcrun", "--udid", UDID,
                                     "simulator", "clipboard", "get")
        self.assertEqual((code, payload["error_code"]), (2, errors.SIMCTL_NOT_FOUND))
        code, pinned = self.run_cli("--simctl", "/nonexistent/xcrun", "--udid", UDID,
                                    "simulator", "status-bar", "pin")
        self.assertEqual(pinned["error_code"], payload["error_code"])

    def test_a_readable_pasteboard_still_reads(self) -> None:
        self.write_state(pasteboard="hello")
        code, payload = self.ios("simulator", "clipboard", "get")
        self.assertEqual((code, payload["text"], payload["verified"]), (0, "hello", True))

    def test_push_takes_the_app_from_the_session(self) -> None:
        self.ios("session", "start", "--app-id", BUNDLE)
        code, payload = self.ios("simulator", "push", "send", "--json",
                                 '{"payload": {"aps": {"alert": "hi"}}}')
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["app_id"], payload["app_id_source"]), (BUNDLE, "session"))


# --- C: idb calls track the companion they start ---------------------------------------------


class IdbCompanionTrackingTests(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox_home()
        self.set_env(AUTONOM_IDB_COMPANION=None, AUTONOM_FAKE_STATE=None,
                     AUTONOM_FAKE_LOG=None)

    def test_a_local_call_is_tracked_a_remote_one_is_not(self) -> None:
        with mock.patch.object(processes, "track_idb_companions",
                               side_effect=lambda udid: contextlib.nullcontext()) as tracker:
            ios_idb.run_idb(str(FAKE_IDB), ["list-targets"], udid=UDID)
            tracker.assert_called_once_with(UDID)
            ios_idb.run_idb(str(FAKE_IDB), ["list-targets"], udid=UDID,
                            companion="mac-farm.local:10882")
            ios_idb.run_idb(str(FAKE_IDB), ["list-targets"])
            self.assertEqual(tracker.call_count, 1)

    def test_a_missing_idb_is_still_idb_required(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            ios_idb.run_idb("/nonexistent/idb", ["list-targets"], udid=UDID)
        self.assertEqual(caught.exception.code, errors.IDB_REQUIRED)


# --- I: linear redaction, bounded discovery -------------------------------------------------------


class RedactionTimingTests(unittest.TestCase):
    SHAPES = {
        "long letter run": "a" * 400_000,
        "scheme then a long host": "a://" + "x" * 400_000,
        "run then scheme": "a" * 400_000 + "://u:p@h",
        "mixed": ("ab://c" + "d" * 997 + ":") * 400,
        "many at signs": "http://" + "u:p@" * 100_000,
        "digit-letter run": "1a" * 200_000 + "://x@h",
        "dashes": "-" * 400_000 + "a://u:p@h",
    }

    def test_every_shape_is_linear(self) -> None:
        """Before: 400 KB took minutes (quadratic backtracking)."""
        for name, text in self.SHAPES.items():
            with self.subTest(shape=name):
                started = time.perf_counter()
                processes.redact_command(text)
                self.assertLess(time.perf_counter() - started, 1.0)

    def test_the_userinfo_is_still_redacted(self) -> None:
        cases = {
            "mitmdump --mode upstream:http://u:p@ss@proxy:8080/a@b":
                "mitmdump --mode upstream:http://<redacted>@proxy:8080/a@b",
            "curl -xhttp://u:p@proxy x": "curl -xhttp://<redacted>@proxy x",
            "git+https://u:p@host/r": "git+https://<redacted>@host/r",
            "plain http://host/a@b": "plain http://host/a@b",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(processes.redact_command(raw), expected)

    def test_discovery_bounds_what_it_redacts_and_keeps(self) -> None:
        huge = "idb_companion --udid X " + "a" * 400_000 + " --token SECRET"
        with mock.patch.object(processes, "_running_processes", return_value=[(4242, huge)]):
            started = time.perf_counter()
            (found,) = processes.discover_companions("X")
            self.assertLess(time.perf_counter() - started, 1.0)
        self.assertLessEqual(len(found["command"]), 400)


# --- J: the supervised group is signalled while its leader pins the id ----------------------------


GRANDPARENT = textwrap.dedent("""\
    import pathlib, subprocess, sys
    grand = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    pathlib.Path(sys.argv[1]).write_text(str(grand.pid))
    grand.returncode = 0  # exit now; the grandchild stays in the group
""")


def _leader_state(pid: int) -> str:
    completed = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                               capture_output=True, text=True, check=False)
    return completed.stdout.strip()


class SupervisedKillOrderTests(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.home = self.sandbox_home()
        patcher = mock.patch.object(processes, "discover_proxies", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_group_is_signalled_before_the_leader_is_reaped(self) -> None:
        pidfile = self.home / "grandchild.pid"
        real_killpg = os.killpg
        seen: list[tuple[int, str]] = []
        leader: list[int] = []

        def recording_killpg(pgid: int, signum: int) -> None:
            if signum != 0:
                leader.append(pgid)
                seen.append((signum, _leader_state(pgid)))
            real_killpg(pgid, signum)

        with mock.patch.object(os, "killpg", recording_killpg):
            code = processes.run_supervised(
                [sys.executable, "-c", GRANDPARENT, str(pidfile)], kind="canvas")
        self.assertEqual(code, 0)
        grandchild = int(pidfile.read_text(encoding="utf-8"))
        self.addCleanup(_kill_quietly, grandchild)
        self.assertTrue(seen, "the leftover group was never signalled")
        signum, state = seen[0]
        self.assertEqual(signum, signal.SIGTERM)
        # a zombie leader still holds its pid, and with it the group id
        self.assertTrue(state.startswith("Z"), f"leader state at killpg: {state!r}")
        self.assertTrue(_wait_until(lambda: _gone(grandchild)), "the group outlived the run")
        with self.assertRaises(ChildProcessError):
            os.waitpid(leader[0], os.WNOHANG)  # reaped: no zombie left behind
        self.assertEqual(processes.entries(), [])

    def test_the_child_exit_code_is_returned(self) -> None:
        code = processes.run_supervised([sys.executable, "-c", "raise SystemExit(3)"],
                                        kind="canvas")
        self.assertEqual(code, 3)
        self.assertEqual(processes.entries(), [])


# --- review round 1: companion signatures, the Android setting read, settle text, find index


class CompanionSignatureTests(_Cli):
    COMPANION = "#!{python}\nimport time\ntime.sleep(120)\n"

    def companion(self, udid: str) -> subprocess.Popen:
        script = self.root / "bin-companion" / "idb_companion"
        script.parent.mkdir(exist_ok=True)
        if not script.exists():
            script.write_text(self.COMPANION.format(python=sys.executable), encoding="utf-8")
            script.chmod(0o755)
        process = self.sleeper(str(script), "--udid", udid)
        self.assertTrue(_wait_until(lambda: any(
            item["pid"] == process.pid for item in processes.discover_companions(udid))))
        return process

    def test_a_tracked_companion_row_names_its_simulator(self) -> None:
        udid = f"FAKE-{uuid.uuid4().hex[:8].upper()}-0000-0000-0000-000000000000"
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=udid,
                                       app_id=BUNDLE)
        with processes.track_idb_companions(udid, record=record):
            started = self.companion(udid)
        (row,) = [e for e in processes.entries() if e["kind"] == "idb_companion"]
        self.assertEqual((row["pid"], row["signature"]),
                         (started.pid, [processes.COMPANION_MARKER, udid]))
        self.assertEqual(processes.terminate_entry(row), "terminated")

    def test_another_simulators_companion_is_never_taken_for_it(self) -> None:
        """Before: the signature was just 'idb_companion', which any
        simulator's companion carries."""
        mine = f"FAKE-{uuid.uuid4().hex[:8].upper()}-0000-0000-0000-000000000000"
        other = f"FAKE-{uuid.uuid4().hex[:8].upper()}-1111-1111-1111-111111111111"
        foreign = self.companion(other)
        row = {"kind": "idb_companion", "pid": foreign.pid, "udid": mine,
               "signature": [processes.COMPANION_MARKER, mine]}
        self.assertEqual(processes.terminate_entry(row), "pid_reused")
        self.assertIsNone(foreign.poll())
        self.assertTrue(processes.signature_matches("x", "a x b"))
        self.assertFalse(processes.signature_matches(["a", "z"], "a x b"))
        self.assertFalse(processes.signature_matches([], "a x b"))


class AndroidProxySettingTests(_Cli):
    TARGET = Target("android", SERIAL, str(FAKE_ADB), {"serial": SERIAL})

    def test_a_failed_read_is_an_error_not_a_value(self) -> None:
        """Before: adb's error text came back as the setting, `attach` saved it
        as the previous proxy, and `detach` wrote it back to the device."""
        from autonom_lib.network import device_proxy_android

        self.write_state(fail={f"-s {SERIAL} shell settings get global http_proxy":
                               [1, "error: device offline"]})
        with self.assertRaises(errors.AutonomError) as caught:
            device_proxy_android._get_setting(self.TARGET)  # noqa: SLF001
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        record = session.start_session(str(FAKE_ADB), serial=SERIAL, app_id=BUNDLE)
        # consent is not what this pins: grant it without any terminal (the
        # TTY guard pass runs this suite with a stdin that claims to be one)
        with mock.patch.object(device_proxy_android.consent, "require",
                               return_value={"kind": "device_proxy"}), \
                self.assertRaises(errors.AutonomError) as caught:
            device_proxy_android.attach(self.TARGET, record, port=8080, acknowledged=True,
                                        network_cycle=False)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertFalse(any("put" in call for call in self.calls("adb")),
                         "nothing may be written after a failed read")
        self.assertIsNone(record["network"].get("previous_http_proxy"))

    def test_empty_and_null_mean_no_proxy(self) -> None:
        from autonom_lib.network import device_proxy_android

        for value, expected in (("", None), ("null", None), ("10.0.2.2:8080", "10.0.2.2:8080")):
            with self.subTest(value=value):
                self.write_state(settings={"http_proxy": value})
                self.assertEqual(device_proxy_android._get_setting(self.TARGET),  # noqa: SLF001
                                 expected)


class SettleHonestyTests(_Cli):
    @staticmethod
    def settle(cost_s: float, changing: bool, **kwargs: Any) -> dict[str, Any]:
        now = [0.0]
        count = [0]

        def take() -> list[dict[str, Any]]:
            now[0] += cost_s
            count[0] += 1
            return [{"ref": "n0", "role": "text",
                     "text": str(count[0]) if changing else "same"}]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        return ui.settle(None, snapshot_fn=take, clock=lambda: now[0], sleep=sleep, **kwargs)

    def test_one_slow_dump_is_not_called_still_changing(self) -> None:
        """Live: 'the tree was still changing after 3000 ms (0 change(s) over
        1 snapshots)' while one ~2 s UI Automator dump fit in the budget."""
        result = self.settle(2.0, False, timeout_ms=3000, quiet_ms=500)
        self.assertEqual((result["settled"], result["snapshots"], result["changes"],
                          result["dump_ms"]), (False, 1, 0, 2000))
        error, hint = ui.unsettled_message(result, 3000, 500)
        self.assertEqual(error, "could not confirm the screen settled: only 1 snapshot fit "
                                "in 3000 ms (a dump takes ~2000 ms); raise --timeout-ms")
        self.assertNotIn("still changing", error)
        self.assertIn("~4500 ms", hint)

    def test_a_real_change_is_still_reported_as_one(self) -> None:
        result = self.settle(0.1, True, timeout_ms=1000, quiet_ms=500)
        error, _hint = ui.unsettled_message(result, 1000, 500)
        self.assertTrue(error.startswith("the tree was still changing after 1000 ms"), error)

    def test_identical_snapshots_short_of_the_quiet_window(self) -> None:
        error, _ = ui.unsettled_message({"snapshots": 2, "changes": 0, "dump_ms": 900,
                                         "stable_ms": 900}, 2000, 1500,
                                        timeout_name="timeoutMs")
        self.assertIn("2 identical snapshots held still for 900 ms, less than the 1500 ms "
                      "quiet window", error)
        self.assertTrue(error.endswith("raise timeoutMs"))

    def test_a_change_then_a_short_hold_is_said_precisely(self) -> None:
        """Review round 2: a change early and identical snapshots after it,
        short of the quiet window, is not "still changing"."""
        now = [0.0]
        frames = iter(["a", "b", "b", "b", "b", "b", "b", "b", "b", "b"])

        def take() -> list[dict[str, Any]]:
            now[0] += 0.2
            return [{"ref": "n0", "role": "text", "text": next(frames, "b")}]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        result = ui.settle(None, snapshot_fn=take, clock=lambda: now[0], sleep=sleep,
                           timeout_ms=1000, quiet_ms=800, interval_ms=100)
        self.assertEqual((result["settled"], result["changes"]), (False, 1))
        self.assertGreater(result["stable_ms"], 0)
        self.assertLess(result["stable_ms"], 800)
        error, hint = ui.unsettled_message(result, 1000, 800)
        self.assertTrue(error.startswith(
            f"the tree changed 1 time(s), then held still for {result['stable_ms']} ms, "
            "less than the 800 ms quiet window"), error)
        self.assertNotIn("still changing", error)
        self.assertEqual(hint.count("raise"), 1)

    def test_one_hint_per_case(self) -> None:
        cases = [
            {"snapshots": 6, "changes": 3, "dump_ms": 100, "stable_ms": 0},
            {"snapshots": 6, "changes": 2, "dump_ms": 100, "stable_ms": 300},
            {"snapshots": 1, "changes": 0, "dump_ms": 2000, "stable_ms": 0},
            {"snapshots": 3, "changes": 0, "dump_ms": 100, "stable_ms": 250},
        ]
        for result in cases:
            with self.subTest(result=result):
                for kwargs in ({}, {"timeout_name": "timeoutMs",
                                    "node_wait": "wait for the node the next step "
                                                 "needs with waitUntil"}):
                    _error, hint = ui.unsettled_message(result, 3000, 500, **kwargs)
                    self.assertEqual(hint.count("raise"), 1, hint)
                    self.assertLessEqual(hint.lower().count("wait for"), 1, hint)

    def test_the_cli_says_it_could_not_confirm(self) -> None:
        code, payload = self.android("ui", "wait", "--settled", "--timeout-ms", "0")
        self.assertEqual((code, payload["ok"], payload["settled"]), (1, False, False))
        (warning,) = payload["warnings"]
        self.assertEqual(warning["code"], "screen_not_settled")
        self.assertTrue(warning["error"].startswith(
            "could not confirm the screen settled: only 1 snapshot fit in 0 ms"), warning)
        self.assertIn("dump_ms", payload)

    def test_a_flow_warning_gives_one_hint(self) -> None:
        flow = self.write_flow("flows/settle.yaml", "- waitForSettled:\n    timeoutMs: 0\n")
        self.android("session", "start", "--app-id", BUNDLE)
        code, payload = self.android("flow", "run", str(flow))
        (warning,) = [w for w in payload["warnings"] if w["code"] == "screen_not_settled"]
        self.assertEqual(warning["hint"].count("raise"), 1, warning)
        self.assertNotIn("Or wait", warning["hint"])

    def test_a_flow_wait_says_so_too(self) -> None:
        flow = self.write_flow("flows/settle.yaml", "- waitForSettled:\n    timeoutMs: 0\n")
        self.android("session", "start", "--app-id", BUNDLE)
        code, payload = self.android("flow", "run", str(flow))
        self.assertEqual(code, 0, payload)
        (warning,) = [w for w in payload["warnings"] if w["code"] == "screen_not_settled"]
        self.assertIn("could not confirm the screen settled: only 1 snapshot", warning["message"])
        self.assertNotIn("still changing", warning["message"])


class OwnershipPrecisionTests(_Cli):
    """Review round 2: ownership checks match whole arguments, and an empty
    signature verifies nothing."""

    def test_a_prefix_colliding_serial_is_not_our_recorder(self) -> None:
        cli = _load_cli()
        record = {"target_id": "R58M123", "background": {}}
        for serial, expected in (("R58M123ABCD", "pid_reused"), ("XR58M123", "pid_reused"),
                                 ("R58M123", "ours")):
            with self.subTest(serial=serial):
                recorder = self.sleeper(sys.executable, "-c", "import time; time.sleep(120)",
                                        "-s", serial, "shell", "screenrecord",
                                        "/sdcard/autonom-recording.mp4")
                self.assertTrue(_wait_until(
                    lambda: processes.command_of(recorder.pid) is not None))
                self.assertEqual(cli._recorder_state(record, recorder.pid),  # noqa: SLF001
                                 expected)
        # the serial must follow -s: named elsewhere it proves nothing
        elsewhere = self.sleeper(sys.executable, "-c", "import time; time.sleep(120)",
                                 "-s", "emulator-5556", "shell", "screenrecord",
                                 "/sdcard/autonom-recording.mp4", "R58M123")
        self.assertTrue(_wait_until(lambda: processes.command_of(elsewhere.pid) is not None))
        self.assertEqual(cli._recorder_state(record, elsewhere.pid), "pid_reused")  # noqa: SLF001

    def test_an_ios_recording_path_must_be_a_whole_argument(self) -> None:
        cli = _load_cli()
        path = str(self.root / "recordings" / "demo.mp4")
        record = {"target_id": UDID, "background": {"recorder_path": path}}
        for argv_path, expected in ((path + ".bak", "pid_reused"), (path, "ours")):
            with self.subTest(path=argv_path):
                recorder = self.sleeper(sys.executable, "-c", "import time; time.sleep(120)",
                                        "simctl", "io", UDID, "recordVideo", "--force",
                                        argv_path)
                self.assertTrue(_wait_until(
                    lambda: processes.command_of(recorder.pid) is not None))
                self.assertEqual(cli._recorder_state(record, recorder.pid),  # noqa: SLF001
                                 expected)

    def test_only_the_real_writer_is_the_log_writer(self) -> None:
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=BUNDLE)
        stream = logs.stream_destination(record)
        stream.parent.mkdir(parents=True, exist_ok=True)
        stream.write_text("", encoding="utf-8")
        tail = self.sleeper("tail", "-f", str(stream))
        backup = _bounded_writer(self, Path(str(stream) + ".bak"))
        writer = _bounded_writer(self, stream)
        self.assertTrue(_wait_until(lambda: processes.command_of(tail.pid) is not None))
        expected = {tail.pid: "pid_reused", backup.pid: "pid_reused", writer.pid: "ours"}
        signature = logs.writer_signature(stream)
        for pid, state in expected.items():
            with self.subTest(pid=pid, expected=state):
                self.assertEqual(logs.log_writer_state(record, pid), state)
                self.assertEqual(
                    processes.signature_matches(signature, processes.command_of(pid)),
                    state == "ours")
        # and the stop honours it: tail and the .bak writer are never signalled
        for pid in (tail.pid, backup.pid):
            self.assertEqual(logs.stop_log_writer(record, pid)["result"], "pid_reused")
        self.assertIsNone(tail.poll())
        self.assertIsNone(backup.poll())
        self.assertEqual(logs.stop_log_writer(record, writer.pid)["result"], "terminated")
        self.assertTrue(_wait_until(lambda: writer.poll() is not None))

    def test_the_registered_writer_row_carries_the_strict_signature(self) -> None:
        record = session.start_session(str(FAKE_SIMCTL), platform="ios", target_id=UDID,
                                       app_id=BUNDLE)
        target = Target("ios", UDID, str(FAKE_SIMCTL), {"udid": UDID})
        started: list[subprocess.Popen] = []

        def fake_start(_xcrun, _udid, destination, **_kwargs):
            started.append(_bounded_writer(self, destination))
            return started[-1].pid

        with mock.patch.object(ios_simctl, "start_log_stream", fake_start), \
                mock.patch.object(ios_simctl, "app_image", return_value=("Runner", None)):
            pid = logs.start_session_log_stream(target, record)
        (row,) = [e for e in processes.entries() if e["pid"] == pid]
        self.assertEqual(row["signature"], logs.writer_signature(logs.stream_destination(record)))
        self.assertEqual(processes.terminate_entry(row), "terminated")

    def test_an_empty_signature_verifies_nothing(self) -> None:
        """Before: `if signature:` skipped the check, so `[]` or `""` killed
        whatever held the pid."""
        for signature in ("", [], [""], ["idb_companion", ""], None):
            with self.subTest(signature=signature):
                stranger = self.sleeper()
                row = {"kind": "log_stream", "pid": stranger.pid, "signature": signature}
                self.assertEqual(processes.terminate_entry(row), "unverified_skipped")
                self.assertIsNone(stranger.poll(), "an unverifiable row was signalled")
                self.assertFalse(processes.usable_signature(signature))
        # a row with no signature key at all keeps the legacy behaviour
        legacy = self.sleeper()
        self.assertEqual(processes.terminate_entry({"kind": "proxy", "pid": legacy.pid}),
                         "terminated")
        self.assertTrue(_wait_until(lambda: legacy.poll() is not None))


class FindIndexTests(_Cli):
    """Every `ui find` match names the `--index` that selects it."""

    def dump(self, payload: str) -> str:
        path = self.root / "screen.json"
        path.write_text(payload, encoding="utf-8")
        return str(path)

    def test_a_single_result_carries_its_index(self) -> None:
        screen = self.dump(REVIEWER_SCREEN)
        code, found = self.run_cli("ui", "find", "--dump", screen, "--desc", "Add",
                                   "--mode", "exact", "--index", "1")
        self.assertEqual(code, 0, found)
        self.assertEqual([(m["ref"], m["index"]) for m in found["matches"]], [("n3", 1)])
        code, found = self.run_cli("ui", "find", "--dump", screen, "--desc", "Add",
                                   "--mode", "exact", "--index", "-1")
        self.assertEqual([(m["ref"], m["index"]) for m in found["matches"]], [("n3", 1)])
        code, found = self.run_cli("ui", "find", "--dump", str(IOS_CATALOG), "--desc",
                                   "Irina Weaver", "--mode", "exact")
        self.assertEqual([m["index"] for m in found["matches"]], [0])
        # only off-screen matches: every match is counted, so the index is real
        code, found = self.run_cli("ui", "find", "--dump", str(IOS_CATALOG), "--desc",
                                   'Cardigan "Whisper"', "--mode", "exact")
        self.assertEqual([(m["index"], m["visible"]) for m in found["matches"]], [(0, False)])

    def test_the_index_round_trips_on_every_sweep_screen(self) -> None:
        for name, nodes in _sweep_screens():
            for field in ("desc", "text"):
                for node in nodes:
                    if not node.get(field):
                        continue
                    fields = {field: node[field]}
                    try:
                        (found,) = ui.select_for_find(nodes, fields, mode="exact")
                    except errors.AutonomError:
                        continue  # ambiguous without --index: nothing to name
                    with self.subTest(screen=name, fields=fields):
                        (again,) = ui.select_for_find(nodes, fields, mode="exact",
                                                      index=found["index"])
                        self.assertEqual(again["ref"], found["ref"])


if __name__ == "__main__":
    unittest.main()
