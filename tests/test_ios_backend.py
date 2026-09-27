"""iOS backend hardening: HID failures, the AXe route, the remote companion,
the single-fire retry, and the bounded log stream (IOS-002..IOS-005, IOS-007).

Everything runs against the fakes in ``tests/fakes``. The CLI subprocesses
get a PATH that holds only a ``python3`` link, the fakes this test chose, and
the system directories — never ``/opt/homebrew/bin`` — so a real ``axe`` or
``idb`` on the developer's machine cannot be reached by accident.
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
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_IDB = ROOT / "tests/fakes/fake_idb.py"
FAKE_AXE = ROOT / "tests/fakes/fake_axe.py"
IOS_FIXTURE = ROOT / "tests/fixtures/idb_describe_all_sample.json"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"
SIMULATORKIT_STDERR = (
    "SimulatorKit is required for HID interactions: Error Domain=com.facebook.FBControlCore "
    "Code=0 \"Attempting to load a file at path '/Applications/Xcode.app/Contents/Developer/"
    "Library/PrivateFrameworks/SimulatorKit.framework', but it does not exist\""
)
BOOTED = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
    {"udid": UDID, "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}]}}

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, ios_idb, ios_simctl, providers, ui_ios  # noqa: E402
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


class IosBackendBase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.state = root / "state.json"
        self.log = root / "log.jsonl"
        self.bin = root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.state.write_text("{}", encoding="utf-8")
        self.set_env(
            AUTONOM_FAKE_STATE=str(self.state),
            AUTONOM_FAKE_LOG=str(self.log),
            AUTONOM_HOME=str(self.home),
            AUTONOM_ADB=None, AUTONOM_SIMCTL=None, AUTONOM_IDB=None,
            AUTONOM_AXE=None, AUTONOM_IOS_HID=None, AUTONOM_IDB_COMPANION=None,
            AUTONOM_IDB_STATE_FILE=str(root / "idb-state.json"),
            AUTONOM_IOS_LOG_MAX_MB=None,
        )
        self.env = dict(os.environ)
        self.env["PATH"] = os.pathsep.join([str(self.bin), "/usr/bin", "/bin"])
        ui_ios.reset_hid_probe()
        self.addCleanup(ui_ios.reset_hid_probe)

    def put_axe_on_path(self) -> None:
        (self.bin / "axe").symlink_to(FAKE_AXE)

    def set_state(self, **kwargs) -> None:
        kwargs.setdefault("simctl_devices", BOOTED)
        kwargs.setdefault("idb_describe_all", str(IOS_FIXTURE))
        self.state.write_text(json.dumps(kwargs), encoding="utf-8")

    def entries(self, tool: str) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if json.loads(line)["tool"] == tool]

    def argv_log(self, tool: str) -> list[list[str]]:
        return [entry["argv"] for entry in self.entries(tool)]

    def idb_verbs(self) -> list[list[str]]:
        return [ios_idb_verb(argv) for argv in self.argv_log("idb")]

    def ios(self, *argv: str, **env: str) -> tuple[int, dict]:
        run_env = {**self.env, **env}
        completed = subprocess.run(
            [sys.executable, str(CLI), "--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB),
             "--udid", UDID, *argv],
            capture_output=True, text=True, env=run_env, timeout=120, cwd=self.tmp.name,
        )
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.returncode in (0, 1) else completed.stderr
        return completed.returncode, json.loads(stream)


def ios_idb_verb(argv: list[str]) -> list[str]:
    """The verb of a logged idb argv, with top-level options stripped."""
    rest = list(argv)
    while rest and rest[0] in ("--companion", "--companion-path", "--log", "--compression"):
        rest = rest[2:]
    return rest[:2]


class HidFrameworkMissingTests(IosBackendBase):
    """IOS-002 — the Xcode 27 SimulatorKit failure has its own code."""

    def test_tap_surfaces_ios_hid_framework_missing(self) -> None:
        self.set_state(idb_fail={"ui tap": [1, SIMULATORKIT_STDERR]})
        code, payload = self.ios("ui", "tap", "--x", "10", "--y", "10")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.IOS_HID_FRAMEWORK_MISSING)
        self.assertIn("brew upgrade idb-companion", payload["hint"])
        self.assertIn("pipx upgrade fb-idb", payload["fix"])
        # A framework the companion cannot load is not retried.
        self.assertEqual(self.idb_verbs().count(["ui", "tap"]), 1)
        self.assertNotIn(["list-targets"], [verb[:1] for verb in self.idb_verbs()])

    def test_pinned_idb_backend_never_falls_back_to_axe(self) -> None:
        self.put_axe_on_path()
        self.set_state(idb_fail={"ui tap": [1, SIMULATORKIT_STDERR]})
        code, payload = self.ios("ui", "tap", "--x", "10", "--y", "10", AUTONOM_IOS_HID="idb")
        self.assertEqual(payload["error_code"], errors.IOS_HID_FRAMEWORK_MISSING)
        self.assertEqual(self.argv_log("axe"), [])

    def test_tree_keeps_working_while_hid_is_broken(self) -> None:
        self.set_state(idb_fail={"ui tap": [1, SIMULATORKIT_STDERR]})
        code, payload = self.ios("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertGreaterEqual(payload["count"], 3)


class AxeBackendTests(IosBackendBase):
    """IOS-003 — HID through AXe; the tree stays on idb."""

    def test_auto_routes_a_broken_idb_tap_through_axe(self) -> None:
        self.put_axe_on_path()
        self.set_state(idb_fail={"ui tap": [1, SIMULATORKIT_STDERR]})
        code, payload = self.ios("ui", "tap", "--desc", "General")
        self.assertEqual(code, 0, payload)
        # General's bounds are [16, 380, 386, 432]; the centre is in points.
        self.assertEqual(self.argv_log("axe"),
                         [["tap", "-x", "201", "-y", "406", "--udid", UDID]])
        self.assertEqual(self.idb_verbs().count(["ui", "tap"]), 1)
        self.assertIn(["ui", "describe-all"], self.idb_verbs(), "the tree stays on idb")

    def test_forced_axe_carries_every_hid_verb(self) -> None:
        self.set_state()
        env = {"AUTONOM_IOS_HID": "axe", "AUTONOM_AXE": str(FAKE_AXE)}
        self.assertEqual(self.ios("ui", "tap", "--x", "20", "--y", "30", **env)[0], 0)
        self.assertEqual(self.ios("ui", "swipe", "--from", "100,600", "--to", "100,200",
                                  "--duration", "0.4", **env)[0], 0)
        self.assertEqual(self.ios("ui", "type", "hello world", **env)[0], 0)
        self.assertEqual(self.ios("ui", "key", "HOME", **env)[0], 0)
        self.assertEqual(self.ios("ui", "key", "SIDE_BUTTON", **env)[0], 0)
        self.assertEqual(self.ios("ui", "key", "40", **env)[0], 0)
        self.assertEqual(self.argv_log("axe"), [
            ["tap", "-x", "20", "-y", "30", "--udid", UDID],
            ["swipe", "--start-x", "100", "--start-y", "600", "--end-x", "100",
             "--end-y", "200", "--duration", "0.4", "--udid", UDID],
            ["type", "--stdin", "--udid", UDID],
            ["button", "home", "--udid", UDID],
            ["button", "side-button", "--udid", UDID],
            ["key", "40", "--udid", UDID],
        ])
        typed = next(entry for entry in self.entries("axe") if entry["argv"][0] == "type")
        self.assertEqual(typed["stdin"], "hello world")
        hid = {("ui", verb) for verb in ("tap", "swipe", "text", "button", "key")}
        self.assertFalse(hid & {tuple(verb) for verb in self.idb_verbs()},
                         "no HID verb may reach idb when AXe is forced")

    def test_forced_axe_without_a_binary_is_a_typed_error(self) -> None:
        self.set_state()
        code, payload = self.ios("ui", "tap", "--x", "20", "--y", "30", AUTONOM_IOS_HID="axe")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.INVALID_VALUE)
        self.assertIn("brew install cameroncooke/axe/axe", payload["hint"])

    def test_healthy_idb_stays_on_idb_even_with_axe_present(self) -> None:
        self.put_axe_on_path()
        self.set_state()
        self.assertEqual(self.ios("ui", "tap", "--x", "20", "--y", "30")[0], 0)
        self.assertEqual(self.argv_log("axe"), [])
        self.assertEqual(self.idb_verbs().count(["ui", "tap"]), 1)


class AxeLibraryTests(EnvSandboxMixin, unittest.TestCase):
    """The library answers which backend carried each verb (additive)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log = Path(tmp.name) / "log.jsonl"
        self.set_env(AUTONOM_FAKE_LOG=str(self.log), AUTONOM_FAKE_STATE=None,
                     AUTONOM_AXE=str(FAKE_AXE), AUTONOM_IOS_HID=None,
                     AUTONOM_IDB=str(FAKE_IDB), AUTONOM_IDB_COMPANION=None)
        self.target = Target(IOS, UDID, str(FAKE_SIMCTL), {"udid": UDID})
        ui_ios.reset_hid_probe()
        self.addCleanup(ui_ios.reset_hid_probe)

    def axe_argv(self) -> list[list[str]]:
        return [json.loads(line)["argv"] for line in self.log.read_text().splitlines()
                if json.loads(line)["tool"] == "axe"]

    def test_results_name_the_backend(self) -> None:
        self.assertEqual(ui_ios.tap(self.target, 5, 6), "idb")
        self.assertEqual(ui_ios.last_backend(), "idb")
        self.set_env(AUTONOM_IOS_HID="axe")
        self.assertEqual(ui_ios.tap(self.target, 5, 6), "axe")
        self.assertEqual(ui_ios.last_backend(), "axe")

    def test_text_goes_through_stdin_so_a_leading_dash_is_not_a_flag(self) -> None:
        self.set_env(AUTONOM_IOS_HID="axe")
        self.assertEqual(ui_ios.type_text(self.target, "--udid evil"), "axe")
        entry = json.loads(self.log.read_text().splitlines()[-1])
        self.assertEqual(entry["argv"], ["type", "--stdin", "--udid", UDID])
        self.assertEqual(entry["stdin"], "--udid evil")

    def test_long_press_through_axe_holds_for_the_duration(self) -> None:
        self.set_env(AUTONOM_IOS_HID="axe")
        self.assertEqual(ui_ios.tap(self.target, 7, 8, duration=0.8), "axe")
        self.assertEqual(self.axe_argv()[-1], [
            "touch", "-x", "7", "-y", "8", "--down", "--up", "--delay", "0.8", "--udid", UDID])

    def test_axe_path_can_ride_on_the_target(self) -> None:
        self.set_env(AUTONOM_AXE=None, AUTONOM_IOS_HID=None)
        target = Target(IOS, UDID, str(FAKE_SIMCTL),
                        {"udid": UDID, "axe": str(FAKE_AXE), "ios_hid": "axe"})
        self.assertEqual(ui_ios.swipe(target, 1, 2, 3, 4, 0.5), "axe")

    def test_auto_probe_runs_once_per_process(self) -> None:
        self.set_env(AUTONOM_IDB=None)  # PATH-resolved idb: the probe speaks for it
        broken = {"ready": False, "reason": "stale companion"}
        with mock.patch.object(ios_idb, "hid_status", return_value=broken) as probe:
            self.assertEqual(ui_ios.hid_backend(self.target), "axe")
            self.assertEqual(ui_ios.hid_backend(self.target), "axe")
        self.assertEqual(probe.call_count, 1)

    def test_pinned_idb_is_not_second_guessed_by_the_probe(self) -> None:
        with mock.patch.object(ios_idb, "hid_status") as probe:
            self.assertEqual(ui_ios.hid_backend(self.target), "idb")
        probe.assert_not_called()

    def test_unknown_mode_is_refused(self) -> None:
        self.set_env(AUTONOM_IOS_HID="xcuitest")
        with self.assertRaises(errors.AutonomError) as caught:
            ui_ios.hid_backend(self.target)
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)

    def test_invalid_key_is_rejected_before_any_dispatch(self) -> None:
        self.set_env(AUTONOM_IOS_HID="axe")
        with self.assertRaises(errors.AutonomError) as caught:
            ui_ios.press_key(self.target, "KEYCODE_BACK")
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_KEY_FOR_PLATFORM)
        self.assertFalse(self.log.exists() and self.axe_argv())


class RemoteCompanionTests(IosBackendBase):
    """IOS-004 — a configured companion reaches every idb invocation."""

    def test_env_endpoint_is_passed_to_every_idb_call(self) -> None:
        self.set_state()
        env = {"AUTONOM_IDB_COMPANION": "localhost:10999"}
        self.assertEqual(self.ios("ui", "tree", **env)[0], 0)
        self.assertEqual(self.ios("ui", "tap", "--x", "20", "--y", "30", **env)[0], 0)
        self.assertEqual(self.ios("screenshot", **env)[0], 0)
        calls = self.argv_log("idb")
        self.assertTrue(calls)
        for argv in calls:
            with self.subTest(argv=argv):
                # A top-level idb option: it must precede the verb.
                self.assertEqual(argv[:2], ["--companion", "localhost:10999"])

    def test_host_and_port_flags_become_the_endpoint(self) -> None:
        self.set_state()
        code, payload = self.ios("--idb-host", "10.0.0.5", "--idb-port", "10999", "ui", "tree")
        self.assertEqual(code, 0, payload)
        for argv in self.argv_log("idb"):
            self.assertEqual(argv[:2], ["--companion", "10.0.0.5:10999"])

    def test_no_endpoint_means_no_companion_flag(self) -> None:
        self.set_state()
        self.assertEqual(self.ios("ui", "tree")[0], 0)
        self.assertFalse(any("--companion" in argv for argv in self.argv_log("idb")))

    def test_target_alias_wins_over_the_environment(self) -> None:
        self.set_env(AUTONOM_IDB=str(FAKE_IDB), AUTONOM_IDB_COMPANION="env-host:1")
        target = Target(IOS, UDID, str(FAKE_SIMCTL),
                        {"udid": UDID, "idb_companion": "alias-host:2"})
        ios_idb.tap(target, 1, 1)
        self.assertEqual(self.argv_log("idb")[-1][:2], ["--companion", "alias-host:2"])


class SingleFireRetryTests(IosBackendBase):
    """IOS-005 — the stale-companion retry never repeats an action."""

    RESET = "Failed to connect to companion: Connection reset by peer"
    REFUSED = ("Failed to connect to companion at address DomainSocketAddress(...): "
               "[Errno 61] Connection refused")

    def state_file(self, entries: list[dict]) -> Path:
        path = Path(self.env["AUTONOM_IDB_STATE_FILE"])
        path.write_text(json.dumps(entries), encoding="utf-8")
        return path

    def test_connection_reset_on_type_is_sent_exactly_once(self) -> None:
        # Even with a prunable registration present: a reset may arrive after
        # the text was typed, so it is never re-sent.
        self.state_file([{"udid": UDID, "pid": _dead_pid(), "is_local": True}])
        self.set_state(idb_fail={"ui text": [1, self.RESET]})
        code, payload = self.ios("ui", "type", "hello")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.IDB_COMPANION_UNAVAILABLE)
        self.assertEqual(self.idb_verbs().count(["ui", "text"]), 1)
        self.assertNotIn(["list-targets"], [verb[:1] for verb in self.idb_verbs()])

    def test_refused_with_nothing_to_prune_is_not_retried(self) -> None:
        self.state_file([{"udid": UDID, "pid": os.getpid(), "is_local": True}])
        self.set_state(idb_fail={"ui text": [1, self.REFUSED]})
        code, payload = self.ios("ui", "type", "hello")
        self.assertEqual(payload["error_code"], errors.IDB_COMPANION_UNAVAILABLE)
        self.assertEqual(self.idb_verbs().count(["ui", "text"]), 1)

    def test_refused_with_an_unknown_registry_is_retried_once(self) -> None:
        # No readable state file: whether anything was pruned is unknown, and
        # a refused connection never reached the companion, so one retry is safe.
        self.set_state(idb_fail={"ui text": [1, self.REFUSED]})
        code, payload = self.ios("ui", "type", "hello")
        self.assertEqual(payload["error_code"], errors.IDB_COMPANION_UNAVAILABLE)
        self.assertEqual(self.idb_verbs().count(["ui", "text"]), 2, "exactly one retry")
        self.assertIn(["list-targets"], [verb[:1] for verb in self.idb_verbs()])

    def test_pinned_idb_never_reads_the_default_registry(self) -> None:
        self.set_env(AUTONOM_IDB_STATE_FILE=None, AUTONOM_IDB=str(FAKE_IDB))
        self.assertIsNone(ios_idb._state_path())  # noqa: SLF001
        self.set_env(AUTONOM_IDB=None)
        self.assertEqual(str(ios_idb._state_path()), ios_idb.IDB_STATE_FILE)  # noqa: SLF001

    def test_refused_and_actually_pruned_is_retried_once(self) -> None:
        path = self.state_file([{"udid": UDID, "pid": _dead_pid(), "is_local": True}])
        self.set_state(idb_fail_until_pruned={"ui text": [1, self.REFUSED]},
                       idb_describe_all=str(IOS_FIXTURE))
        code, payload = self.ios("ui", "type", "hello")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.idb_verbs().count(["ui", "text"]), 2)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [])

    def test_prune_reports_false_when_nothing_disappeared(self) -> None:
        self.state_file([{"udid": UDID, "pid": os.getpid(), "is_local": True}])
        self.assertFalse(ios_idb._prune_companions(str(FAKE_IDB)))  # noqa: SLF001
        self.state_file([{"udid": UDID, "pid": _dead_pid(), "is_local": True}])
        self.assertTrue(ios_idb._prune_companions(str(FAKE_IDB)))  # noqa: SLF001


class CompanionOutputTests(unittest.TestCase):
    """The companion may print its environment; none of it may leak."""

    def companion(self, stdout: str) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "idb_companion"
        path.write_text("#!/bin/sh\ncat <<'EOF'\n" + stdout + "\nEOF\n", encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def test_only_version_fields_are_kept(self) -> None:
        path = self.companion('GITHUB_TOKEN=ghp_secret123\nAWS_SECRET=abc\n'
                              '{"build_time":"08:41:50","build_date":"Aug 12 2022"}')
        info = ios_idb.companion_info(path)
        self.assertEqual(info, {"version": None, "build_date": "Aug 12 2022",
                                "build_time": "08:41:50"})
        self.assertNotIn("secret", json.dumps(info).lower())
        self.assertEqual(ios_idb.companion_version(path), "built Aug 12 2022")

    def test_predates_fix_by_version_or_date(self) -> None:
        cases = [
            ({"build_date": "Aug 12 2022"}, True),
            ({"build_date": "Jul  1 2026"}, False),
            ({"version": "1.6.1"}, True),
            ({"version": "1.6.2", "build_date": "Aug 12 2022"}, False),
            ({}, None),
        ]
        for info, expected in cases:
            with self.subTest(info=info):
                self.assertEqual(ios_idb.companion_predates_xcode27(info), expected)


class BoundedLogStreamTests(EnvSandboxMixin, unittest.TestCase):
    """IOS-007 — the iOS log stream is filtered and capped on disk."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.set_env(AUTONOM_FAKE_STATE=str(self.state), AUTONOM_FAKE_LOG=str(self.log),
                     AUTONOM_IOS_LOG_MAX_MB=None)

    def test_stream_stays_under_the_cap_and_rotates_once(self) -> None:
        line = json.dumps({"eventMessage": "x" * 90})
        self.state.write_text(json.dumps({"ios_log": [line] * 400}), encoding="utf-8")
        destination = self.root / "logs" / "stream.ndjson"
        pid = ios_simctl.start_log_stream(str(FAKE_SIMCTL), UDID, destination,
                                          bundle_id="com.example.app", max_bytes=4000)
        self.assertIsNotNone(pid)
        os.waitpid(pid, 0)
        self.assertLessEqual(destination.stat().st_size, 4000)
        rotated = destination.with_name("stream.ndjson.1")
        self.assertTrue(rotated.exists(), "the overflow is rotated, not dropped silently")
        self.assertLessEqual(rotated.stat().st_size, 4000)
        self.assertFalse(destination.with_name("stream.ndjson.2").exists())
        # Whole lines only: each file is valid ndjson.
        for path in (destination, rotated):
            for record in path.read_text(encoding="utf-8").splitlines():
                json.loads(record)
        argv = [json.loads(entry)["argv"] for entry in self.log.read_text().splitlines()]
        stream = next(item for item in argv if "stream" in item)
        predicate = stream[stream.index("--predicate") + 1]
        self.assertIn('subsystem == "com.example.app"', predicate)

    def test_no_bundle_means_no_predicate(self) -> None:
        argv = ios_simctl.log_stream_argv("xcrun", UDID)
        self.assertNotIn("--predicate", argv)
        self.assertEqual(argv[:6], ["xcrun", "simctl", "spawn", UDID, "log", "stream"])

    def test_cap_comes_from_the_environment(self) -> None:
        self.assertEqual(ios_simctl.log_max_bytes(), 50 * 1024 * 1024)
        self.set_env(AUTONOM_IOS_LOG_MAX_MB="0.5")
        self.assertEqual(ios_simctl.log_max_bytes(), 512 * 1024)
        self.set_env(AUTONOM_IOS_LOG_MAX_MB="nonsense")
        self.assertEqual(ios_simctl.log_max_bytes(), 50 * 1024 * 1024)

    def test_endless_stream_is_capped_and_stops_on_sigterm(self) -> None:
        producer = [sys.executable, "-c",
                    "import sys\nwhile True:\n    sys.stdout.write('y' * 99 + '\\n')\n"]
        destination = self.root / "endless.ndjson"
        writer = ios_simctl.spawn_bounded(producer, destination, max_bytes=10_000)
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not destination.with_name(
                    "endless.ndjson.1").exists():
                time.sleep(0.05)
            for _ in range(50):
                # Rotation swaps the file in atomically, so it never vanishes.
                self.assertLessEqual(destination.stat().st_size, 10_000)
                time.sleep(0.01)
        finally:
            writer.send_signal(signal.SIGTERM)
            writer.wait(timeout=10)
        self.assertTrue(destination.with_name("endless.ndjson.1").exists())
        self.assertLessEqual(destination.stat().st_size, 10_000)


class UninstallResultTests(unittest.TestCase):
    """CLI-003 support — a failed uninstall is reported, not swallowed."""

    def failing_xcrun(self) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "xcrun"
        path.write_text("#!/bin/sh\necho 'An error was encountered processing the command "
                        "(domain=IXUserPresentableErrorDomain, code=4)' >&2\nexit 4\n",
                        encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def test_failure_returns_false_and_raises_on_request(self) -> None:
        xcrun = self.failing_xcrun()
        self.assertFalse(ios_simctl.uninstall(xcrun, UDID, "com.example.app"))
        with self.assertRaises(errors.AutonomError) as caught:
            ios_simctl.uninstall(xcrun, UDID, "com.example.app", check=True)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertIn("IXUserPresentableErrorDomain", caught.exception.message)

    def test_success_returns_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AUTONOM_FAKE_STATE": str(Path(tmp) / "s.json"),
                                              "AUTONOM_FAKE_LOG": str(Path(tmp) / "l.jsonl")}):
                self.assertTrue(ios_simctl.uninstall(str(FAKE_SIMCTL), UDID, "com.example.app"))


class ProviderInputTests(EnvSandboxMixin, unittest.TestCase):
    """The capability snapshot separates the tree from input."""

    def snapshot(self, hid: dict) -> dict:
        target = Target(IOS, UDID, "xcrun", {"udid": UDID})
        record = {"tooling": {"idb": {"state": "ready", "hid": hid}}}
        snap = providers.open_session(target, record).capabilities()
        return {item.name: item for item in snap.capabilities}

    def test_broken_hid_without_axe_makes_input_unavailable(self) -> None:
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        self.set_env(AUTONOM_AXE=None, PATH=empty.name)
        caps = self.snapshot({"ready": False, "reason": "stale companion"})
        self.assertEqual(caps["ui.accessibility"].state, "available")
        self.assertEqual(caps["ui.input"].state, "unavailable")
        self.assertEqual(caps["ui.input"].reason, "stale companion")

    def test_broken_hid_with_axe_is_degraded_but_usable(self) -> None:
        self.set_env(AUTONOM_AXE=str(FAKE_AXE))
        caps = self.snapshot({"ready": False, "reason": "stale companion"})
        self.assertEqual(caps["ui.input"].state, "degraded")
        self.assertTrue(caps["ui.input"].available)

    def test_healthy_hid_is_available(self) -> None:
        self.assertEqual(self.snapshot({"ready": True})["ui.input"].state, "available")


if __name__ == "__main__":
    unittest.main()
