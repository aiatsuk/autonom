"""CLI wiring and envelopes (CLI-001/002/004/005/006/009, FLOW-008/009, IOS-004, SEC-002).

Every case drives the real CLI — as a subprocess against the fakes in
`tests/fakes/`, or in process with the one library call that would reach a
tool patched out — with a private AUTONOM_HOME and a PATH that holds nothing
but `python3`. Nothing here can reach adb, simctl, idb, AXe or an emulator.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_IDB = ROOT / "tests/fakes/fake_idb.py"
FAKE_AXE = ROOT / "tests/fakes/fake_axe.py"
UI_DUMP = ROOT / "tests/fixtures/ui_dump.xml"
IOS_DUMP = ROOT / "tests/fixtures/idb_describe_all_sample.json"
SERIAL = "emulator-5554"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"
BOOTED = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
    {"udid": UDID, "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}]}}

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import journal as journal_mod  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402
from autonom_lib import ui as ui_mod  # noqa: E402
from autonom_lib import ui_ios  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


_REAL_STDIN = None


def setUpModule() -> None:
    # The consent gate prompts when stdin is a TTY; the in-process cases must
    # never wait on one.
    global _REAL_STDIN
    _REAL_STDIN = sys.stdin
    sys.stdin = io.StringIO()


def tearDownModule() -> None:
    if _REAL_STDIN is not None:
        sys.stdin = _REAL_STDIN


def load_cli():
    spec = importlib.util.spec_from_file_location("autonom_cli_hardening", CLI)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


PASS_FLOW = """schema: autonom.dev/flow/v1
appId: com.example.app
name: pass
---
- tapOn:
    selector:
      description: Flutter Save Button
      match: exact
"""

CHILD_FLOW = """schema: autonom.dev/flow/v1
appId: com.example.app
name: child
---
- tapOn:
    selector:
      description: Flutter Save Button
      match: exact
- back
"""

PARENT_FLOW = """schema: autonom.dev/flow/v1
id: flow_parent
appId: com.example.app
name: parent
---
- runFlow: sub/child.yaml
- assertVisible:
    selector:
      id: com.example.app:id/search
"""


class CliCase(EnvSandboxMixin, unittest.TestCase):
    """A private home, a fake state/log, and a PATH with nothing real on it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / ".autonom").mkdir()  # a flow workspace root
        self.home = self.root / "home"
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.write_state()
        self.set_env(
            AUTONOM_HOME=str(self.home), AUTONOM_FAKE_STATE=str(self.state),
            AUTONOM_FAKE_LOG=str(self.log),
            AUTONOM_ADB=None, AUTONOM_SIMCTL=None, AUTONOM_IDB=None,
            AUTONOM_AXE=None, AUTONOM_IOS_HID=None, AUTONOM_IDB_COMPANION=None,
            AUTONOM_IDB_STATE_FILE=str(self.root / "idb-state.json"),
            AUTONOM_IOS_LOG_MAX_MB=None, DEVELOPER_DIR=None,
        )
        self.env = dict(os.environ)
        self.env["PATH"] = str(self.bin)
        ui_ios.reset_hid_probe()
        self.addCleanup(ui_ios.reset_hid_probe)

    def write_state(self, **extra) -> None:
        state = {"devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"]],
                 "ui_dump": str(UI_DUMP), "simctl_devices": BOOTED,
                 "idb_describe_all": str(IOS_DUMP)}
        state.update(extra)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def raw(self, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
        completed = subprocess.run(
            [sys.executable, str(CLI), *argv], cwd=self.root, env={**self.env, **(env or {})},
            text=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=120)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        return completed

    @staticmethod
    def parse(completed: subprocess.CompletedProcess) -> dict:
        stream = completed.stdout if completed.returncode in (0, 1) else completed.stderr
        return json.loads(stream)

    def android(self, *argv: str, **env) -> tuple[int, dict]:
        completed = self.raw("--adb", str(FAKE_ADB), "--serial", SERIAL, *argv, env=env)
        return completed.returncode, self.parse(completed)

    def ios(self, *argv: str, **env) -> tuple[int, dict]:
        completed = self.raw("--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB),
                             "--udid", UDID, *argv, env=env)
        return completed.returncode, self.parse(completed)

    def calls(self, tool: str) -> list[list[str]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        return [row["argv"] for row in rows if row.get("tool") == tool]

    def start_android(self, *extra: str) -> dict:
        code, payload = self.android("session", "start", "--app-id", "com.example.app", *extra)
        self.assertEqual(code, 0, payload)
        return payload

    def readonly_dir(self) -> Path:
        path = self.root / "readonly"
        path.mkdir()
        path.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(path.chmod, stat.S_IRWXU)
        if os.access(path, os.W_OK):  # running as root: permissions are moot
            self.skipTest("the test user can write to a read-only directory")
        return path

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


# --- CLI-001: envelopes, never tracebacks ------------------------------------


class EnvelopeTests(CliCase):
    def test_python_guard_names_the_minimum(self) -> None:
        cli = load_cli()
        payload = cli._python_too_old((3, 10, 9))  # noqa: SLF001
        self.assertEqual(payload["error_code"], "tool_missing")
        self.assertIn("3.11", payload["error"])
        self.assertIn("3.10.9", payload["error"])
        self.assertIsNone(cli._python_too_old((3, 11, 0)))  # noqa: SLF001
        self.assertIsNone(cli._python_too_old(tuple(sys.version_info)))  # noqa: SLF001

    def test_logs_tail_bad_grep_is_invalid_value(self) -> None:
        code, payload = self.android("logs", "tail", "--grep", "(")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], "invalid_value")

    def test_ui_find_bad_regex_is_invalid_value_not_no_match(self) -> None:
        completed = self.raw("ui", "find", "--dump", str(UI_DUMP), "--mode", "regex",
                             "--text", "[")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(self.parse(completed)["error_code"], "invalid_value")

    def test_bad_regex_on_ui_tap_is_refused_before_the_device(self) -> None:
        code, payload = self.android("ui", "tap", "--mode", "regex", "--desc", "(")
        self.assertEqual((code, payload["error_code"]), (2, "invalid_value"))
        self.assertEqual(self.calls("adb"), [])

    def test_missing_dump_carries_an_error_code(self) -> None:
        completed = self.raw("ui", "tree", "--dump", str(self.root / "nope.xml"))
        self.assertEqual(completed.returncode, 2)
        payload = self.parse(completed)
        self.assertEqual(payload["error_code"], "invalid_value")
        self.assertIn("nope.xml", payload["error"])

    def test_screenshot_into_readonly_dir_is_output_not_writable(self) -> None:
        target = self.readonly_dir() / "shot.png"
        code, payload = self.android("screenshot", "--out", str(target))
        self.assertEqual((code, payload["error_code"]), (2, "output_not_writable"))
        self.assertEqual(payload["path"], str(target))
        # checked before the capture: nothing reached the device
        self.assertFalse(any("screencap" in argv for argv in self.calls("adb")))

    def test_screenshot_out_dev_null_is_still_allowed(self) -> None:
        code, payload = self.android("screenshot", "--out", os.devnull)
        self.assertEqual(code, 0, payload)

    def test_network_export_into_readonly_dir(self) -> None:
        self.start_android()
        target = self.readonly_dir() / "flows.har"
        code, payload = self.android("network", "export", "--har", str(target))
        self.assertEqual((code, payload["error_code"]), (2, "output_not_writable"))

    def test_file_pull_onto_a_directory(self) -> None:
        code, payload = self.android("file", "pull", "files/a.txt", "--app-id",
                                     "com.example.app", "--out", str(self.root))
        self.assertEqual((code, payload["error_code"]), (2, "output_not_writable"))

    def test_report_export_into_readonly_dir(self) -> None:
        self.start_android()
        self.write("f.yaml", PASS_FLOW)
        self.assertEqual(self.android("flow", "run", "f.yaml")[0], 0)
        target = self.readonly_dir() / "report.html"
        code, payload = self.android("report", "export", "--out", str(target))
        self.assertEqual((code, payload["error_code"]), (2, "output_not_writable"))

    def test_unknown_flag_shows_the_leaf_usage(self) -> None:
        completed = self.raw("ui", "tap", "--text", "x", "--bogus")
        self.assertEqual(completed.returncode, 2)
        payload = self.parse(completed)
        self.assertEqual(payload["error_code"], "usage_error")
        self.assertIn("--bogus", payload["error"])
        self.assertTrue(payload["hint"].startswith("usage: autonom ui tap"), payload["hint"])


# --- CLI-002: session start ----------------------------------------------------


class SessionStartTests(CliCase):
    def test_second_start_is_refused_with_the_live_session(self) -> None:
        first = self.start_android()["session"]
        code, payload = self.android("session", "start")
        self.assertEqual((code, payload["error_code"]), (2, "session_already_active"))
        self.assertEqual(payload["session_id"], first["session_id"])
        self.assertEqual(payload["target_id"], SERIAL)
        self.assertIn("session stop", payload["hint"])
        self.assertEqual(session_mod.load_current()["session_id"], first["session_id"])

    def test_missing_install_path_creates_no_session(self) -> None:
        code, payload = self.android("session", "start", "--install", str(self.root / "a.apk"))
        self.assertEqual((code, payload["error_code"]), (2, "install_path_not_found"))
        self.assertIsNone(session_mod.load_current())
        sessions = self.home / "sessions"
        self.assertFalse(sessions.exists() and any(p.is_dir() for p in sessions.iterdir()))

    def test_failed_launch_rolls_the_session_back(self) -> None:
        self.write_state(fail={f"-s {SERIAL} shell am start": [1, "Error: Activity not started"]})
        code, payload = self.android("session", "start", "--app-id", "com.example.app",
                                     "--launch")
        self.assertEqual(code, 2, payload)
        self.assertIn("session_rolled_back", payload)
        self.assertIsNone(session_mod.load_current())
        record = json.loads((self.home / "sessions" / payload["session_rolled_back"]
                             / "session.json").read_text(encoding="utf-8"))
        self.assertTrue(record.get("stopped_at"))
        # and the next start is not refused by a half-built one
        self.write_state()
        self.start_android()

    def test_failed_install_rolls_the_session_back(self) -> None:
        apk = self.write("app.apk", "not really an apk")
        self.write_state(fail={f"-s {SERIAL} install": [1, "adb: failed to install"]})
        code, payload = self.android("session", "start", "--install", str(apk))
        self.assertEqual(code, 2, payload)
        self.assertIsNone(session_mod.load_current())

    def test_log_stream_on_android_is_a_warning(self) -> None:
        payload = self.start_android("--log-stream")
        codes = [w["code"] for w in payload.get("warnings", [])]
        self.assertIn("flag_ignored_on_platform", codes)
        self.assertIsNone(payload["session"]["background"].get("log_stream_pid"))

    def test_ios_log_stream_uses_the_bounded_writer(self) -> None:
        code, payload = self.ios("session", "start", "--app-id", "com.example.app",
                                 "--log-stream")
        self.assertEqual(code, 0, payload)
        pid = payload["session"]["background"]["log_stream_pid"]
        self.assertIsInstance(pid, int)
        self.addCleanup(session_mod.terminate_pid, pid)
        streams = {s["id"]: s for s in payload["session"].get("streams", [])}
        self.assertEqual(streams["log_stream"]["kind"], "device_log")
        self.assertEqual(self.ios("session", "stop")[0], 0)
        spawned = [argv for argv in self.calls("simctl") if "log" in argv and "stream" in argv]
        self.assertTrue(spawned)
        self.assertIn("--predicate", spawned[0])


class StaleSessionTests(CliCase):
    """CLI-011: a session whose emulator was killed (or simulator deleted)
    must still be clearable, and the refusal to start another one must say
    which target holds it and that `session stop` clears it anyway."""

    def test_stop_with_the_android_target_gone_succeeds_with_a_warning(self) -> None:
        first = self.start_android()["session"]
        self.write_state(devices=[])
        code, payload = self.android("session", "stop")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["stale_target"])
        warning = payload["warnings"][0]
        self.assertEqual(warning["code"], "stale_target")
        self.assertIn(SERIAL, warning["error"])
        self.assertEqual(payload["session"]["session_id"], first["session_id"])
        self.assertIsNone(session_mod.load_current())
        # and a new session starts once a target is back
        self.write_state()
        self.start_android()

    def test_stop_skips_the_device_proxy_restore_on_a_gone_device(self) -> None:
        self.start_android()
        record = session_mod.load_current()
        record["network"].update({"attached": True, "previous_http_proxy": None})
        session_mod.save(record)
        self.write_state(devices=[])
        self.log.unlink(missing_ok=True)
        code, payload = self.android("session", "stop")
        self.assertEqual(code, 0, payload)
        detach = next(item for item in payload["teardown"] if item["action"] == "network_detach")
        self.assertTrue(detach["ok"], detach)
        self.assertEqual(detach["detail"]["skipped"], "stale_target")
        self.assertFalse(any("settings" in argv for argv in self.calls("adb")))

    def test_stop_with_the_simulator_gone_succeeds_with_a_warning(self) -> None:
        code, payload = self.ios("session", "start", "--app-id", "com.example.app")
        self.assertEqual(code, 0, payload)
        self.write_state(simctl_devices={"devices": {}})
        code, payload = self.ios("session", "stop")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["warnings"][0]["code"], "stale_target")
        self.assertIsNone(session_mod.load_current())

    def test_a_present_target_stops_without_the_warning(self) -> None:
        self.start_android()
        code, payload = self.android("session", "stop")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("stale_target", payload)
        self.assertNotIn("warnings", payload)

    def test_the_refusal_names_the_gone_target_and_the_way_out(self) -> None:
        self.start_android()
        self.write_state(devices=[["emulator-5556", "device", "product:sdk_gphone64_arm64"]])
        completed = self.raw("--adb", str(FAKE_ADB), "--serial", "emulator-5556",
                             "session", "start")
        payload = self.parse(completed)
        self.assertEqual((completed.returncode, payload["error_code"]),
                         (2, "session_already_active"))
        self.assertTrue(payload["stale_target"])
        self.assertEqual(payload["target_id"], SERIAL)
        self.assertIn(SERIAL, payload["hint"])
        self.assertIn("autonom session stop", payload["hint"])
        self.assertIn("even if", payload["hint"])


# --- session verbs ------------------------------------------------------------


class SessionVerbTests(CliCase):
    def test_fresh_with_activity_is_a_cleared_task_start(self) -> None:
        code, payload = self.android("session", "launch", "com.example.app", "--fresh",
                                     "--activity", ".Main")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["mode"], "fresh")
        joined = [" ".join(argv) for argv in self.calls("adb")]
        starts = [call for call in joined if " am start " in f" {call} "]
        self.assertTrue(starts, joined)
        self.assertIn("-n com.example.app/.Main", starts[0])
        self.assertIn("-f 0x10008000", starts[0])
        self.assertFalse(any("monkey" in call for call in joined))

    def test_ios_only_launch_flags_warn_on_android(self) -> None:
        code, payload = self.android("session", "launch", "com.example.app",
                                     "--arg=-x", "--setenv", "A=1")
        self.assertEqual(code, 0, payload)
        flags = [w["error"].split()[0] for w in payload["warnings"]]
        self.assertEqual(flags, ["--arg", "--setenv"])

    def test_android_only_activity_warns_on_ios(self) -> None:
        code, payload = self.ios("session", "launch", "com.example.app", "--activity", ".Main")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["warnings"][0]["code"], "flag_ignored_on_platform")

    def test_android_uninstall_failure_is_not_ok(self) -> None:
        for code_value in (1, 0):  # adb exits 1, or prints Failure and exits 0
            with self.subTest(exit_code=code_value):
                self.write_state(fail={f"-s {SERIAL} uninstall": [
                    code_value, "Failure [DELETE_FAILED_INTERNAL_ERROR]"]})
                code, payload = self.android("session", "uninstall", "com.example.app")
                self.assertEqual(code, 2)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error_code"], "backend_failed")
                self.assertIn("DELETE_FAILED_INTERNAL_ERROR", payload["error"])

    def test_android_uninstall_success(self) -> None:
        code, payload = self.android("session", "uninstall", "com.example.app")
        self.assertEqual((code, payload["ok"]), (0, True))

    def test_ios_uninstall_failure_is_not_ok(self) -> None:
        self.write_state(simctl_fail={"simctl uninstall": [1, "No such app"]})
        code, payload = self.ios("session", "uninstall", "com.example.app")
        self.assertEqual((code, payload["error_code"]), (2, "backend_failed"))

    def test_clear_strategy_on_android_warns(self) -> None:
        code, payload = self.android("session", "clear", "com.example.app",
                                     "--strategy", "privacy")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["warnings"][0]["code"], "flag_ignored_on_platform")

    def test_clear_failure_carries_a_hint(self) -> None:
        self.write_state(fail={f"-s {SERIAL} shell pm clear": [1, "Error: unknown package"]})
        code, payload = self.android("session", "clear", "com.example.app")
        self.assertEqual(code, 2)
        self.assertIn("pm list packages", payload["hint"])


# --- ui verbs (CLI-004 / CLI-005) --------------------------------------------


class UiTreeTests(CliCase):
    def dump_count(self) -> int:
        completed = self.raw("ui", "tree", "--dump", str(UI_DUMP), "--max-nodes", "1000")
        return self.parse(completed)["count"]

    def test_truncated_is_exact_for_dumps(self) -> None:
        total = self.dump_count()
        exact = self.parse(self.raw("ui", "tree", "--dump", str(UI_DUMP),
                                    "--max-nodes", str(total)))
        self.assertEqual(exact["count"], total)
        self.assertNotIn("truncated", exact)
        cut = self.parse(self.raw("ui", "tree", "--dump", str(UI_DUMP),
                                  "--max-nodes", str(total - 1)))
        self.assertEqual(cut["count"], total - 1)
        self.assertTrue(cut["truncated"])

    def test_truncated_is_exact_for_a_live_tree(self) -> None:
        total = self.dump_count()
        code, exact = self.android("ui", "tree", "--max-nodes", str(total))
        self.assertEqual(code, 0, exact)
        self.assertNotIn("truncated", exact)
        code, cut = self.android("ui", "tree", "--max-nodes", str(total - 1))
        self.assertTrue(cut["truncated"])

    def test_outline_format_prints_one_line_per_node(self) -> None:
        payload = self.parse(self.raw("ui", "tree", "--dump", str(UI_DUMP),
                                      "--format", "outline"))
        self.assertNotIn("nodes", payload)
        lines = payload["outline"].splitlines()
        self.assertEqual(len(lines), payload["count"])
        self.assertTrue(lines[0].startswith("n1 "), lines[0])

    def test_interactable_keeps_only_actionable_nodes(self) -> None:
        full = self.parse(self.raw("ui", "tree", "--dump", str(UI_DUMP)))
        only = self.parse(self.raw("ui", "tree", "--dump", str(UI_DUMP), "--interactable"))
        self.assertLess(only["count"], full["count"])
        self.assertTrue(only["nodes"])
        self.assertTrue(all(ui_mod.is_interactable(node) for node in only["nodes"]))

    def test_sparse_live_android_tree_explains_missing_flutter_labels(self) -> None:
        sparse = self.root / "sparse.xml"
        sparse.write_text(
            '<hierarchy rotation="0"><node class="android.widget.FrameLayout" '
            'package="com.example.app" resource-id="android:id/content" '
            'bounds="[0,0][1080,2400]"/></hierarchy>', encoding="utf-8")
        self.write_state(ui_dump=str(sparse))

        code, payload = self.android("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["warnings"][0]["code"], "sparse_accessibility_tree")
        self.assertIn("before it starts", payload["warnings"][0]["hint"])
        # An offline fixture is parsed, not diagnosed as a running screen.
        offline = self.parse(self.raw("ui", "tree", "--dump", str(sparse)))
        self.assertNotIn("warnings", offline)

    def test_labeled_live_android_tree_has_no_sparse_warning(self) -> None:
        code, payload = self.android("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("warnings", payload)


class UiSelectTests(CliCase):
    def test_find_without_selector_is_selector_required(self) -> None:
        completed = self.raw("ui", "find", "--dump", str(UI_DUMP))
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(self.parse(completed)["error_code"], "selector_required")

    def test_ios_text_miss_hints_desc(self) -> None:
        completed = self.raw("ui", "find", "--dump", str(IOS_DUMP), "--text", "Settings",
                             "--mode", "exact")
        payload = self.parse(completed)
        self.assertEqual(payload["count"], 0)
        codes = {w["code"]: w for w in payload.get("warnings", [])}
        self.assertIn("label_is_in_desc", codes)
        self.assertIn("--desc", codes["label_is_in_desc"]["hint"])

    def test_ios_tap_miss_hints_desc(self) -> None:
        code, payload = self.ios("ui", "tap", "--text", "Settings", "--mode", "exact")
        self.assertEqual((code, payload["error_code"]), (2, "no_matching_node"))
        self.assertIn("--desc", payload["hint"])

    def test_boolean_flags_are_strict(self) -> None:
        for word, accepted in (("flase", False), ("maybe", False), ("yes", True), ("0", True)):
            with self.subTest(word=word):
                completed = self.raw("ui", "find", "--dump", str(UI_DUMP), "--text", "S",
                                     "--all", "--clickable", word)
                self.assertEqual(completed.returncode == 0, accepted, completed.stderr)
                if not accepted:
                    self.assertEqual(self.parse(completed)["error_code"], "usage_error")

    def test_tap_with_selector_and_coordinates_is_a_usage_error(self) -> None:
        code, payload = self.android("ui", "tap", "--text", "a", "--x", "1", "--y", "2")
        self.assertEqual((code, payload["error_code"]), (2, "usage_error"))
        code, payload = self.android("ui", "tap", "--x", "1")
        self.assertEqual((code, payload["error_code"]), (2, "usage_error"))
        self.assertEqual(self.calls("adb"), [])

    def test_swipe_with_one_number_is_invalid_value(self) -> None:
        code, payload = self.android("ui", "swipe", "--from", "100", "--to", "1,2")
        self.assertEqual((code, payload["error_code"]), (2, "invalid_value"))
        self.assertIn("--from", payload["error"])


class UiWaitTests(CliCase):
    def test_settled_on_a_still_screen(self) -> None:
        code, payload = self.android("ui", "wait", "--settled", "--timeout-ms", "5000",
                                     "--quiet-ms", "0")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["settled"])
        self.assertGreaterEqual(payload["snapshots"], 2)
        self.assertEqual(payload["changes"], 0)
        self.assertIn("elapsed_ms", payload)

    def test_timeout_reports_not_settled_with_exit_1(self) -> None:
        code, payload = self.android("ui", "wait", "--settled", "--timeout-ms", "0")
        self.assertEqual(code, 1, payload)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["settled"])
        self.assertEqual(payload["warnings"][0]["code"], "screen_not_settled")

    def test_wait_needs_a_condition(self) -> None:
        code, payload = self.android("ui", "wait")
        self.assertEqual((code, payload["error_code"]), (2, "usage_error"))

    def test_settles_after_the_tree_stops_changing(self) -> None:
        """CLI-005-S01, in process: a tree that changes twice then stays."""
        cli = load_cli()
        trees = [[{"role": "a"}], [{"role": "b"}], [{"role": "c"}], [{"role": "c"}]]
        calls = iter(trees + [trees[-1]] * 10)
        out = io.StringIO()
        target = cli.Target("android", SERIAL, str(FAKE_ADB), {"serial": SERIAL})
        with mock.patch.object(cli.ui_mod, "snapshot", lambda _t: next(calls)), \
                mock.patch.object(cli, "_target", lambda _a: target), \
                contextlib.redirect_stdout(out):
            code = cli.main(["ui", "wait", "--settled", "--timeout-ms", "5000",
                             "--quiet-ms", "0"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["settled"])
        self.assertEqual((payload["snapshots"], payload["changes"]), (4, 2))


class UiBackendTests(CliCase):
    def test_android_input_payloads_name_adb(self) -> None:
        for argv in (("ui", "tap", "--x", "10", "--y", "10"),
                     ("ui", "tap", "--x", "10", "--y", "10", "--duration", "500"),
                     ("ui", "swipe", "--from", "1,2", "--to", "3,4"),
                     ("ui", "type", "hello"),
                     ("ui", "key", "KEYCODE_BACK")):
            with self.subTest(argv=argv):
                code, payload = self.android(*argv)
                self.assertEqual(code, 0, payload)
                self.assertEqual(payload["backend"], "adb")

    def test_ios_hid_flags_route_input_through_axe(self) -> None:
        code, payload = self.ios("--ios-hid", "axe", "--axe", str(FAKE_AXE),
                                 "ui", "tap", "--x", "20", "--y", "30")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["backend"], "axe")
        axe = self.calls("axe")
        self.assertTrue(axe and axe[-1][0] == "tap", axe)
        self.assertFalse(any(argv[:2] == ["ui", "tap"] for argv in self.calls("idb")))

    def test_ios_hid_idb_keeps_idb(self) -> None:
        code, payload = self.ios("--ios-hid", "idb", "ui", "key", "HOME")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["backend"], "idb")

    def test_ios_hid_rejects_unknown_backends(self) -> None:
        completed = self.raw("--ios-hid", "xcuitest", "ui", "tree")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(self.parse(completed)["error_code"], "usage_error")

    def test_idb_host_and_port_reach_every_idb_call(self) -> None:
        """IOS-004 through the CLI flags."""
        code, payload = self.ios("--idb-host", "h.example", "--idb-port", "10999",
                                 "ui", "tree")
        self.assertEqual(code, 0, payload)
        calls = self.calls("idb")
        self.assertTrue(calls)
        for argv in calls:
            self.assertEqual(argv[:2], ["--companion", "h.example:10999"])


# --- simulator (F) -----------------------------------------------------------


class SimulatorTests(CliCase):
    def test_animations_control_is_wired(self) -> None:
        code, payload = self.android("simulator", "animations", "pin")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["control"], "animations")
        self.assertTrue(payload["verified"], payload)
        puts = [argv for argv in self.calls("adb") if "put" in argv]
        self.assertEqual(len(puts), 3)

    def test_help_lists_each_controls_actions(self) -> None:
        completed = self.raw("simulator", "--help")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("pin, reset, show", completed.stdout)
        self.assertIn("animations", completed.stdout)
        battery = self.raw("simulator", "battery", "--help")
        self.assertIn("set, reset", battery.stdout)


# --- journal (SEC-002) --------------------------------------------------------


class JournalRedactionTests(CliCase):
    def test_markers_never_reach_the_journal(self) -> None:
        session = self.start_android()["session"]
        self.write("f.yaml", PASS_FLOW)
        runs = [
            ("--adb", str(FAKE_ADB), "--ser", SERIAL, "ui", "type", "LEAKA"),
            ("--adb", str(FAKE_ADB), "--serial", SERIAL, "simulator", "clipboard", "set",
             "--v", "text=LEAKB"),
            ("--adb", str(FAKE_ADB), "network", "mock", "add", "--url",
             "https://x.example/?access_token=LEAKC", "--j", "{}"),
            ("--adb", str(FAKE_ADB), "--serial", SERIAL, "flow", "run", "f.yaml",
             "--env", "password=LEAKD"),
        ]
        for argv in runs:
            completed = self.raw(*argv)
            self.assertIn(completed.returncode, (0, 1), completed.stderr)
        journal = Path(session["artifacts_dir"]) / journal_mod.JOURNAL_FILE
        text = journal.read_text(encoding="utf-8")
        verbs = [json.loads(line).get("verb") for line in text.splitlines() if line.strip()]
        for verb in ("ui type", "simulator clipboard", "network mock", "flow run"):
            self.assertIn(verb, verbs)  # journaled, so the check is not vacuous
        for marker in ("LEAKA", "LEAKB", "LEAKC", "LEAKD"):
            self.assertNotIn(marker, text)

    def test_env_values_are_redacted_whatever_the_key(self) -> None:
        """SEC-004: a key does not say whether its value is secret."""
        session = self.start_android()["session"]
        self.write("f.yaml", PASS_FLOW)
        self.write("s.yaml", PASS_FLOW + "- inputText:\n    value: ${CODE}\n"
                                         "    sensitive: true\n")
        runs = [
            ("s.yaml", "--env", "CODE=LEAKX"),
            ("f.yaml", "--env", "secret_token=LEAKA"),
            ("f.yaml", "--env", "auth_token=LEAKD"),
            ("f.yaml", "--env", "user_pin=LEAKE"),
            ("f.yaml", "--env=PLAIN=LEAKF"),
            ("f.yaml", "--en", "ABBR=LEAKG"),
        ]
        for argv in runs:
            completed = self.raw("--adb", str(FAKE_ADB), "--serial", SERIAL, "flow", "run",
                                 *argv)
            self.assertIn(completed.returncode, (0, 1), completed.stderr)
        journal = Path(session["artifacts_dir"]) / journal_mod.JOURNAL_FILE
        entries = [json.loads(line) for line in
                   journal.read_text(encoding="utf-8").splitlines() if line.strip()]
        flow_runs = [e for e in entries if e.get("verb") == "flow run"
                     and e.get("origin") == "agent" and "argv" in e]
        self.assertEqual(len(flow_runs), len(runs))
        text = journal.read_text(encoding="utf-8")
        for marker in ("LEAKX", "LEAKA", "LEAKD", "LEAKE", "LEAKF", "LEAKG"):
            self.assertNotIn(marker, text)
        for entry, argv in zip(flow_runs, runs):
            # 1:1 with what was typed, and the key stays readable
            self.assertEqual(len(entry["argv"]), len(argv) + 6, entry["argv"])
        self.assertIn("CODE=<redacted>", flow_runs[0]["argv"])
        self.assertIn("--env=PLAIN=<redacted>", flow_runs[4]["argv"])
        self.assertIn("ABBR=<redacted>", flow_runs[5]["argv"])

    def test_session_launch_setenv_is_redacted(self) -> None:
        session = self.ios("session", "start")[1]["session"]
        code, _ = self.ios("session", "launch", "com.example.app", "--setenv", "TOKENISH=LEAKS")
        self.assertEqual(code, 0)
        text = (Path(session["artifacts_dir"]) / journal_mod.JOURNAL_FILE).read_text(
            encoding="utf-8")
        self.assertIn("session launch", text)
        self.assertNotIn("LEAKS", text)

    def test_the_root_parser_and_args_reach_the_journal(self) -> None:
        cli = load_cli()
        seen: dict = {}
        with mock.patch.object(cli.session_mod, "load_current",
                               lambda *a, **k: {"session_id": "s_x"}), \
                mock.patch.object(cli.journal_mod, "record_action",
                                  lambda session, **kwargs: seen.update(kwargs)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["ui", "tree", "--dump", str(UI_DUMP)]), 0)
        self.assertIsInstance(seen["parser"], cli.argparse.ArgumentParser)
        self.assertEqual(seen["args"].command, "ui")
        self.assertEqual(seen["verb"], "ui tree")


# --- network (H) ---------------------------------------------------------------


class NetworkTests(CliCase):
    def test_malformed_header_is_refused(self) -> None:
        completed = self.raw("network", "mock", "add", "--match", "*/x", "--header", "nocolon")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(self.parse(completed)["error_code"], "invalid_value")

    def test_mock_without_target_is_selector_required(self) -> None:
        completed = self.raw("network", "mock", "add", "--json", "{}")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(self.parse(completed)["error_code"], "selector_required")

    def test_mock_status_out_of_range(self) -> None:
        completed = self.raw("network", "mock", "add", "--match", "*/x", "--status", "700")
        self.assertEqual(self.parse(completed)["error_code"], "invalid_value")

    def test_start_keeps_library_warnings_and_reports_actual_capture(self) -> None:
        cli = load_cli()
        record = session_mod.start_session("fake-adb", serial=SERIAL, platform="android",
                                           target_id=SERIAL)
        already = {"code": "proxy_already_running", "error": "kept", "hint": "restart"}
        state = {"running": True, "proxy_host": "127.0.0.1", "port": 8080,
                 "capture_bodies": False, "already_running": True, "warnings": [already]}
        out = io.StringIO()
        with mock.patch.object(cli.proxy_mod, "start", lambda *a, **k: dict(state)), \
                mock.patch.object(cli.proxy_mod, "ca_certificate", lambda *_a: None), \
                contextlib.redirect_stdout(out):
            code = cli.main(["network", "start", "--i-understand-mitm", "--capture-bodies"])
        self.assertEqual(code, 0)
        codes = [w["code"] for w in json.loads(out.getvalue())["warnings"]]
        self.assertIn("proxy_already_running", codes)
        # the running proxy does not capture bodies, whatever was asked
        self.assertNotIn("full_body_capture_enabled", codes)
        self.assertTrue(record["session_id"])

    def test_status_adds_attach_state_and_keeps_attached_types(self) -> None:
        self.start_android()
        code, payload = self.android("network", "status")
        self.assertEqual(code, 0, payload)
        self.assertIs(payload["attached"], False)
        self.assertEqual(payload["attach_state"], "not_attached")


# --- flow (FLOW-008 / FLOW-009) ----------------------------------------------


class FlowRunTests(CliCase):
    def test_dry_run_indexes_equal_runtime_indexes(self) -> None:
        """FLOW-009-S01: runFlow then assertVisible."""
        self.write("sub/child.yaml", CHILD_FLOW)
        self.write("parent.yaml", PARENT_FLOW)
        self.start_android()
        code, planned = self.android("flow", "run", "parent.yaml", "--dry-run")
        self.assertEqual(code, 0, planned)
        self.assertEqual(planned["status"], "planned")
        plan = [(entry["index"], entry["command"]) for entry in planned["planned"]]
        self.assertEqual(plan, [(1, "runFlow"), (2, "tapOn"), (3, "back"),
                                (4, "assertVisible")])
        code, ran = self.android("flow", "run", "parent.yaml")
        self.assertEqual(code, 0, ran)
        runtime = {(step["index"], step["command"]) for step in ran["steps"]}
        for entry in plan:
            if entry[1] != "runFlow":  # composites are blocks, not steps
                self.assertIn(entry, runtime)

    def test_dry_run_until_step_is_applied(self) -> None:
        self.write("sub/child.yaml", CHILD_FLOW)
        self.write("parent.yaml", PARENT_FLOW)
        self.start_android()
        code, payload = self.android("flow", "run", "parent.yaml", "--dry-run",
                                     "--until-step", "2")
        self.assertEqual(code, 0, payload)
        self.assertEqual([e["index"] for e in payload["planned"]], [1, 2])
        self.assertEqual(payload["until_step"], 2)

    def test_run_warnings_reach_the_summary(self) -> None:
        self.write("typed.yaml", PASS_FLOW + "- inputText:\n    value: ${password}\n"
                                             "    sensitive: true\n")
        self.start_android()
        code, payload = self.android("flow", "run", "typed.yaml", "--env", "password=x",
                                     "--dry-run")
        self.assertEqual(code, 0, payload)
        self.assertIn("flow_env_value_sensitive",
                      [w["code"] for w in payload.get("warnings", [])])

    def test_directory_verbs_warn_about_skipped_schemas(self) -> None:
        self.write("flows/good.yaml", PASS_FLOW)
        self.write("flows/typo.yaml", PASS_FLOW.replace("autonom.dev/flow/v1",
                                                        "autonom.dev/flows/v1"))
        self.start_android()
        for argv in (("flow", "list", "flows"), ("flow", "check", "flows"),
                     ("flow", "run", "flows", "--dry-run")):
            with self.subTest(argv=argv):
                code, payload = self.android(*argv)
                self.assertEqual(code, 0, payload)
                self.assertIn("flow_files_skipped",
                              [w["code"] for w in payload.get("warnings", [])])


class FlowFmtTests(CliCase):
    COMMENTED = PASS_FLOW.replace("---\n", "---\n# keep me: why this taps Save\n")

    def test_write_refuses_to_drop_comments(self) -> None:
        path = self.write("c.yaml", self.COMMENTED)
        completed = self.raw("flow", "fmt", str(path), "--write")
        self.assertEqual(completed.returncode, 2)
        payload = self.parse(completed)
        self.assertEqual(payload["error_code"], "comments_would_be_lost")
        self.assertEqual(path.read_text(encoding="utf-8"), self.COMMENTED)

    def test_drop_comments_rewrites(self) -> None:
        path = self.write("c.yaml", self.COMMENTED)
        completed = self.raw("flow", "fmt", str(path), "--write", "--drop-comments")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("keep me", path.read_text(encoding="utf-8"))

    def test_directory_refusal_writes_nothing(self) -> None:
        clean = self.write("d/a.yaml", PASS_FLOW.replace("name: pass", "name:   pass"))
        before = clean.read_text(encoding="utf-8")
        self.write("d/b.yaml", self.COMMENTED)
        completed = self.raw("flow", "fmt", str(self.root / "d"), "--write")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(clean.read_text(encoding="utf-8"), before)

    def test_preview_counts_lost_comments(self) -> None:
        path = self.write("c.yaml", self.COMMENTED)
        payload = self.parse(self.raw("flow", "fmt", str(path)))
        self.assertEqual(payload["files"][0]["comments_lost"], 1)


class ReportSuiteTests(CliCase):
    def test_suite_json_carries_broken_and_junit_counts(self) -> None:
        self.write("f.yaml", PASS_FLOW)
        self.start_android()
        self.assertEqual(self.android("flow", "run", "f.yaml")[0], 0)
        code, payload = self.android("report", "suite")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["broken"], 0)
        counts = payload["junit_counts"]
        xml = Path(payload["junit"]).read_text(encoding="utf-8")
        self.assertIn(f'<testsuites tests="{counts["tests"]}" '
                      f'failures="{counts["failures"]}" errors="{counts["errors"]}"', xml)


# --- teach approve (FLOW-009, J) ---------------------------------------------


class TeachApproveTests(CliCase):
    ENV_FLOW = """schema: autonom.dev/flow/v1
id: env_flow
appId: com.example.app
name: env flow
---
- assertVisible:
    selector:
      id: ${SEARCH_ID}
"""

    def test_zero_runs_is_refused_before_anything_runs(self) -> None:
        path = self.write("e.yaml", self.ENV_FLOW)
        self.start_android()
        before = len(self.calls("adb"))
        code, payload = self.android("teach", "approve", str(path), "--run",
                                     "--minimum-runs", "0")
        self.assertEqual((code, payload["error_code"]), (2, "usage_error"))
        self.assertEqual(len(self.calls("adb")), before)

    def test_env_reaches_the_replays(self) -> None:
        path = self.write("e.yaml", self.ENV_FLOW)
        self.start_android()
        code, payload = self.android("teach", "approve", str(path), "--run",
                                     "--minimum-runs", "1",
                                     "--env", "SEARCH_ID=com.example.app:id/search")
        self.assertEqual(code, 0, payload)
        self.assertEqual(len(payload["replays"]), 1)
        self.assertTrue(payload["replays"][0]["flow_sha256"])

    def test_missing_secret_is_refused(self) -> None:
        path = self.write("e.yaml", self.ENV_FLOW)
        self.start_android()
        code, payload = self.android("teach", "approve", str(path), "--run",
                                     "--minimum-runs", "1", "--secret", "NOT_SET_ANYWHERE")
        self.assertEqual((code, payload["error_code"]), (2, "flow_secret_undefined"))


# --- proof / atlas (K) -------------------------------------------------------


class ProofAtlasTests(CliCase):
    def test_proof_reports_invalid_flows(self) -> None:
        git = shutil.which("git")
        if not git:
            self.skipTest("git is not installed")
        (self.bin / "git").symlink_to(git)
        repo = self.root / "repo"
        repo.mkdir()
        run = {"cwd": repo, "check": True, "capture_output": True}
        subprocess.run([git, "init", "-q"], **run)
        subprocess.run([git, "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "base"], **run)
        (repo / "app.txt").write_text("changed\n", encoding="utf-8")
        flows = repo / ".autonom/flows"
        flows.mkdir(parents=True)
        (flows / "broken.yaml").write_text(
            "schema: autonom.dev/flow/v1\nappId: x\n---\n- notACommand\n", encoding="utf-8")
        completed = self.raw("proof", "--base", "HEAD", "--repo", str(repo),
                             "--out", str(self.root / "proof"))
        payload = self.parse(completed)
        self.assertEqual(payload["status"], "not_covered")
        self.assertEqual(len(payload["invalid_flows"]), 1)
        self.assertTrue(payload["warnings"])
        markdown = (self.root / "proof/proof.md").read_text(encoding="utf-8")
        self.assertIn("Invalid flows", markdown)
        self.assertIn(".autonom/flows/broken.yaml", markdown)

    def test_atlas_diff_refuses_a_malformed_snapshot(self) -> None:
        base = self.write("base.json", "[1, 2]")
        completed = self.raw("atlas", "diff", "--base", str(base), "--head", str(base))
        self.assertEqual(completed.returncode, 2)
        self.assertTrue(self.parse(completed)["error_code"])


# --- CLI-006 / CLI-009 -------------------------------------------------------


class SessionLookupTests(CliCase):
    def test_journal_session_id_without_follow(self) -> None:
        session = self.start_android()["session"]
        self.assertEqual(self.android("ui", "key", "KEYCODE_BACK")[0], 0)
        self.assertEqual(self.android("session", "stop")[0], 0)
        completed = self.raw("journal", "--session-id", session["session_id"])
        payload = self.parse(completed)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertGreaterEqual(payload["count"], 1)
        self.assertIn("ui key", [entry.get("verb") for entry in payload["entries"]])

    def test_shots_show_accepts_the_listed_relative_path(self) -> None:
        self.start_android()
        self.assertEqual(self.android("screenshot")[0], 0)
        listed = self.parse(self.raw("shots", "list"))
        relative = listed["shots"][-1]["file"]
        self.assertFalse(os.path.isabs(relative))
        code, payload = self.android("shots", "show", relative)
        self.assertEqual(code, 0, payload)
        self.assertTrue(Path(payload["path"]).is_file())

    def test_shots_list_max_bounds(self) -> None:
        self.start_android()
        self.assertEqual(self.android("screenshot")[0], 0)
        none = self.parse(self.raw("shots", "list", "--max", "0"))
        self.assertEqual((none["count"], none["total_matched"]), (0, 1))
        completed = self.raw("shots", "list", "--max", "-1")
        self.assertEqual(self.parse(completed)["error_code"], "invalid_value")

    def test_logs_follow_defaults_to_the_only_device_stream(self) -> None:
        record = session_mod.start_session("fake-xcrun", platform="ios", target_id=UDID)
        stream = Path(record["artifacts_dir"]) / "logs/stream.ndjson"
        stream.write_text('{"eventMessage": "hello from the stream"}\n', encoding="utf-8")
        session_mod.register_stream(record, stream_id="log_stream", kind="device_log",
                                    path="logs/stream.ndjson", label="ios log stream",
                                    pid=None)
        session_mod.save(record)
        completed = self.raw("logs", "follow", "--from-start", "--max-lines", "1",
                             "--max-seconds", "5")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("hello from the stream", completed.stdout)

    def test_android_device_follow_starts_now(self) -> None:
        """CLI-009-S01."""
        self.write_state(logcat=["01-01 00:00:00.000 I/x: old line"])
        completed = self.raw("--adb", str(FAKE_ADB), "--serial", SERIAL, "logs", "follow",
                             "--source", "device", "--max-lines", "1", "--max-seconds", "5")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        logcat = [argv for argv in self.calls("adb") if "logcat" in argv]
        self.assertIn("-T", logcat[-1])
        stamp = logcat[-1][logcat[-1].index("-T") + 1]
        self.assertRegex(stamp, r"^\d+\.000$")

    def test_android_device_follow_from_start_has_no_cutoff(self) -> None:
        completed = self.raw("--adb", str(FAKE_ADB), "--serial", SERIAL, "logs", "follow",
                             "--source", "device", "--from-start", "--max-lines", "1",
                             "--max-seconds", "5")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        logcat = [argv for argv in self.calls("adb") if "logcat" in argv]
        self.assertNotIn("-T", logcat[-1])



# --- CAPST-003: a live status-bar pin is re-asserted before each capture ------


SHOT_FLOW = """schema: autonom.dev/flow/v1
appId: com.example.app
name: shot
---
- takeScreenshot:
    label: pinned
"""


class RepinCaptureTests(CliCase):
    def events(self, payload: dict) -> list[dict]:
        run = payload if "events" in payload else payload["runs"][0]
        path = Path(run["events"])
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def adb_before_screencap(self) -> list[str]:
        calls = [" ".join(argv[2:]) for argv in self.calls("adb")]
        index = next(i for i, call in enumerate(calls) if call.startswith("exec-out screencap"))
        return calls[:index]

    def test_flow_take_screenshot_re_asserts_a_live_pin_first(self) -> None:
        self.start_android()
        self.assertEqual(self.android("simulator", "status-bar", "pin")[0], 0)
        self.write("f.yaml", SHOT_FLOW)
        self.log.unlink()
        code, payload = self.android("flow", "run", "f.yaml")
        self.assertEqual(code, 0, payload)
        before = self.adb_before_screencap()
        self.assertIn("emu gsm signal-profile 4", before)
        self.assertIn("emu gsm signal 31 0", before)
        self.assertIn("shell dumpsys battery set level 100", before)
        finished = [event for event in self.events(payload)
                    if event.get("kind") == "flow.step.finished"]
        self.assertTrue(finished[-1]["payload"]["repinned"], finished[-1])
        self.assertIn("mobile_level", finished[-1]["payload"]["repinned_controls"])

    def test_flow_take_screenshot_after_clear_sends_nothing(self) -> None:
        self.start_android()
        self.android("simulator", "status-bar", "pin")
        self.android("simulator", "status-bar", "clear")
        self.write("f.yaml", SHOT_FLOW)
        self.log.unlink()
        code, payload = self.android("flow", "run", "f.yaml")
        self.assertEqual(code, 0, payload)
        self.assertFalse(any(call.startswith(("emu gsm", "shell dumpsys battery"))
                             for call in self.adb_before_screencap()))
        finished = [event for event in self.events(payload)
                    if event.get("kind") == "flow.step.finished"]
        self.assertEqual(finished[-1]["payload"]["command"], "takeScreenshot")
        self.assertNotIn("repinned", finished[-1]["payload"])


# --- CLI-004 misc ------------------------------------------------------------


class InputValidationTests(CliCase):
    def test_record_name_cannot_escape(self) -> None:
        """CLI-004-S01."""
        self.start_android()
        for name in ("../evil", "a/b", "..", ""):
            with self.subTest(name=name):
                code, payload = self.android("record", "start", "--name", name)
                self.assertEqual((code, payload["error_code"]), (2, "invalid_value"))
        self.assertFalse(any("screenrecord" in " ".join(argv) for argv in self.calls("adb")))

    def test_canvas_values_are_validated_in_python(self) -> None:
        for argv in (("--port", "0"), ("--port", "70000"), ("--fps", "0"), ("--fps", "500")):
            with self.subTest(argv=argv):
                code, payload = self.android("canvas", "serve", *argv)
                self.assertEqual((code, payload["error_code"]), (2, "invalid_value"))

    def test_empty_note_is_refused(self) -> None:
        self.start_android()
        for text in ("", "   "):
            code, payload = self.android("note", "add", text)
            self.assertEqual((code, payload["error_code"]), (2, "invalid_value"))

    def test_metrics_series_needs_positive_count_and_interval(self) -> None:
        for argv in (("--count", "0"), ("--interval", "-1"), ("--count", "-3")):
            with self.subTest(argv=argv):
                code, payload = self.android("metrics", "series", "--app-id",
                                             "com.example.app", *argv)
                self.assertEqual((code, payload["error_code"]), (2, "invalid_value"))

    def test_doctor_strict_failure_is_ok_false(self) -> None:
        cli = load_cli()
        report = {"ok": True, "tools": {"adb": {"state": "missing"}, "xcrun": {"state": "ok"}}}
        out = io.StringIO()
        with mock.patch.object(cli.doctor_mod, "collect", lambda _args: dict(report)), \
                contextlib.redirect_stdout(out):
            code = cli.main(["doctor", "--strict"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["strict_failures"], ["adb"])
        # without --strict the diagnostic stays a success
        out = io.StringIO()
        with mock.patch.object(cli.doctor_mod, "collect", lambda _args: dict(report)), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["doctor"]), 0)
        self.assertNotIn("strict_failures", json.loads(out.getvalue()))


if __name__ == "__main__":
    unittest.main()
