"""Regressions for what the 2026-09-02 device sweep found.

Every case here reproduces a behaviour observed on a real emulator or
simulator with the fakes, so the fix is pinned without the hardware: an
argparse failure that was prose, a biometric control that had never worked,
typing that landed nowhere and passed, a companion registration that took
every idb verb down, and a handful of answers that were technically true and
practically useless.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_IDB = ROOT / "tests/fakes/fake_idb.py"
UI_FIXTURE = ROOT / "tests/fixtures/ui_dump.xml"
UI_FOCUSED = ROOT / "tests/fixtures/ui_dump_focused.xml"
IOS_FIXTURE = ROOT / "tests/fixtures/idb_describe_all_sample.json"
IOS_TEXTFIELD = ROOT / "tests/fixtures/idb_describe_all_textfield.json"
PASS_FLOW = ROOT / "tests/fixtures/flows/contract_pass.yaml"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, processes  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


class SweepBase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.state = root / "state.json"
        self.log = root / "log.jsonl"
        self.state.write_text("{}", encoding="utf-8")
        self.set_env(
            AUTONOM_FAKE_STATE=str(self.state),
            AUTONOM_FAKE_LOG=str(self.log),
            AUTONOM_HOME=str(self.home),
            AUTONOM_ADB=None, AUTONOM_SIMCTL=None, AUTONOM_IDB=None,
        )
        self.env = dict(os.environ)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def set_state(self, **kwargs) -> None:
        self.state.write_text(json.dumps(kwargs), encoding="utf-8")

    def argv_log(self, tool: str) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"]
                for line in self.log.read_text(encoding="utf-8").splitlines()
                if json.loads(line)["tool"] == tool]

    def run_raw(self, *argv: str) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [sys.executable, str(CLI), *argv],
            capture_output=True, text=True, env=self.env, timeout=120, cwd=self.tmp.name,
        )
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        return completed

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        completed = self.run_raw(*argv)
        stream = completed.stdout if completed.returncode in (0, 1) else completed.stderr
        return completed.returncode, json.loads(stream)

    def android(self, *argv: str) -> tuple[int, dict]:
        return self.run_cli("--adb", str(FAKE_ADB), "--serial", "emulator-5554", *argv)

    def ios(self, *argv: str) -> tuple[int, dict]:
        return self.run_cli("--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB),
                            "--udid", UDID, *argv)


class UsageErrorTests(SweepBase):
    def test_unknown_verb_is_a_json_envelope(self) -> None:
        code, payload = self.run_cli("bogus")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.USAGE_ERROR)
        self.assertIn("invalid choice", payload["error"])
        self.assertIn("usage:", payload["hint"].lower())

    def test_a_flag_the_verb_lacks_is_a_json_envelope(self) -> None:
        code, payload = self.run_cli("ui", "tap", "--text", "x", "--all")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.USAGE_ERROR)
        self.assertIn("--all", payload["error"])

    def test_an_option_shaped_value_is_a_json_envelope(self) -> None:
        """`session launch app --arg --es` — the real mistake from the sweep."""
        code, payload = self.run_cli("session", "launch", "com.example.app", "--arg", "--es")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.USAGE_ERROR)
        self.assertIn("--arg", payload["error"])


class CapabilitiesWithoutSessionTests(SweepBase):
    def test_explicit_target_needs_no_session(self) -> None:
        code, payload = self.android("capabilities")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["target_id"], "emulator-5554")
        self.assertIsNone(payload["session_id"])
        names = {item["name"]: item["state"] for item in payload["capabilities"]}
        self.assertEqual(names["ui.accessibility"], "available")
        self.assertEqual(names["network.capture"], "unavailable")


class TapAndTypeTests(SweepBase):
    def test_tap_with_nothing_to_tap_is_refused_by_name(self) -> None:
        self.set_state(ui_dump=str(UI_FIXTURE))
        code, payload = self.android("ui", "tap")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.SELECTOR_REQUIRED)
        dumps = [argv for argv in self.argv_log("adb") if "uiautomator" in argv]
        self.assertEqual(dumps, [], "refused before touching the device")

    def test_type_warns_when_nothing_has_focus(self) -> None:
        self.set_state(ui_dump=str(UI_FIXTURE))
        code, payload = self.android("ui", "type", "hello")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["typed"], "hello")
        self.assertEqual([w["code"] for w in payload["warnings"]], ["no_focused_field"])
        typed = [argv for argv in self.argv_log("adb") if argv[-3:-1] == ["input", "text"]]
        self.assertEqual(len(typed), 1, "the CLI still types; it just says what it saw")

    def test_type_names_the_focused_field(self) -> None:
        self.set_state(ui_dump=str(UI_FOCUSED))
        code, payload = self.android("ui", "type", "hello")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("warnings", payload)
        self.assertEqual(payload["focused"]["resource_id"], "com.example.app:id/search")

    def test_ios_type_reports_focus_as_unverified_when_a_field_exists(self) -> None:
        """idb's dump has no focus attribute (checked on a Simulator), so iOS
        can only say whether a text field is on screen."""
        self.set_state(idb_describe_all=str(IOS_TEXTFIELD))
        code, payload = self.ios("ui", "type", "hello")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("warnings", payload)
        self.assertEqual(payload["focused"]["role"], "textfield")
        self.assertEqual(payload["focus"], "unverified")
        self.set_state(idb_describe_all=str(IOS_FIXTURE))
        code, payload = self.ios("ui", "type", "hello")
        self.assertEqual([w["code"] for w in payload["warnings"]], ["no_focused_field"])

    def test_off_screen_tap_hint_is_per_platform(self) -> None:
        code, android = self.android("ui", "tap", "--x", "99999", "--y", "5")
        self.assertEqual(android["error_code"], errors.COORDINATE_SPACE_MISMATCH)
        self.assertIn("Android coordinates are pixels", android["hint"])
        self.set_state(idb_describe_all=str(IOS_FIXTURE))
        code, ios = self.ios("ui", "tap", "--x", "99999", "--y", "5")
        self.assertEqual(ios["error_code"], errors.COORDINATE_SPACE_MISMATCH)
        self.assertIn("points, not pixels", ios["hint"])


class TreeTests(SweepBase):
    def test_max_nodes_says_it_truncated_and_names_the_screen(self) -> None:
        self.set_state(ui_dump=str(UI_FIXTURE))
        code, payload = self.android("ui", "tree", "--max-nodes", "3")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["count"], 3)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["screen"], [1080, 1920])
        code, whole = self.android("ui", "tree")
        self.assertNotIn("truncated", whole)


class FileAndLocationTests(SweepBase):
    def test_run_as_refusal_is_an_error_not_a_file(self) -> None:
        self.set_state(run_as_refused="run-as: package not an application: com.android.settings")
        code, payload = self.android("file", "ls", "--app-id", "com.android.settings")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.APP_NOT_DEBUGGABLE)
        self.assertIn("debuggable", payload["hint"])
        code, pull = self.android("file", "pull", "files/x", "--app-id", "com.android.settings",
                                  "--out", str(Path(self.tmp.name) / "x"))
        self.assertEqual(pull["error_code"], errors.APP_NOT_DEBUGGABLE)

    def test_debuggable_app_lists_normally(self) -> None:
        code, payload = self.android("file", "ls", "--app-id", "com.example.app")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["entries"], ["files", "shared_prefs"])

    def test_location_set_states_its_delivery(self) -> None:
        code, payload = self.android("location", "set", "55.751244,37.618423")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["delivery"], "on_subscription")
        self.assertIn("location get", payload["note"])


class LifecycleAnswerTests(SweepBase):
    def test_record_stop_with_nothing_recording_invents_no_path(self) -> None:
        self.android("session", "start", "--app-id", "com.example.app")
        code, payload = self.android("record", "stop")
        self.assertEqual(code, 0, payload)
        self.assertFalse(payload["was_recording"])
        self.assertIsNone(payload["path"])
        self.assertEqual(payload["bytes"], 0)

    def test_shutdown_of_a_stopped_simulator_says_so(self) -> None:
        code, payload = self.ios("devices", "shutdown")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["stopped"])
        self.assertTrue(payload["already_stopped"])
        shutdowns = [argv for argv in self.argv_log("simctl") if argv[1:2] == ["shutdown"]]
        self.assertEqual(shutdowns, [], "no shutdown sent to a device that is down")
        self.ios("devices", "boot")
        code, payload = self.ios("devices", "shutdown")
        self.assertFalse(payload["already_stopped"])

    def test_mocked_is_a_bare_flag(self) -> None:
        completed = self.run_raw("network", "requests", "list", "--mocked")
        payload = json.loads(completed.stderr)
        self.assertNotEqual(payload["error_code"], errors.USAGE_ERROR)


class BiometricTests(SweepBase):
    def test_ios_biometric_posts_darwin_notifications(self) -> None:
        for action, expected in (
            ("enroll", ["-s", "com.apple.BiometricKit.enrollmentChanged", "1",
                        "-p", "com.apple.BiometricKit.enrollmentChanged"]),
            ("match", ["-p", "com.apple.BiometricKit_Sim.fingerTouch.match"]),
            ("nonmatch", ["-p", "com.apple.BiometricKit_Sim.fingerTouch.nomatch"]),
        ):
            with self.subTest(action=action):
                code, payload = self.ios("simulator", "biometric", action)
                self.assertEqual(code, 0, payload)
                argv = self.argv_log("simctl")[-1]
                self.assertEqual(argv[:4], ["simctl", "spawn", UDID, "notifyutil"])
                self.assertEqual(argv[4:], expected)
        code, payload = self.ios("simulator", "biometric", "wink")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.FLOW_COMMAND_INVALID)


class IdbCompanionRetryTests(SweepBase):
    def test_stale_companion_is_pruned_and_the_verb_retried_once(self) -> None:
        self.set_state(
            idb_describe_all=str(IOS_FIXTURE),
            idb_fail_until_pruned={"ui describe-all": [
                1, "Failed to connect to companion at address DomainSocketAddress(...): "
                   "[Errno 61] Connection refused"]},
            simctl_devices={"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
                {"udid": UDID, "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}]}},
        )
        code, payload = self.ios("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertGreaterEqual(payload["count"], 3)
        idb = [argv[:2] for argv in self.argv_log("idb")]
        self.assertEqual(idb.count(["ui", "describe-all"]), 2)
        self.assertIn(["list-targets"], [argv[:1] for argv in self.argv_log("idb")])

    def test_a_companion_that_stays_down_fails_once_with_the_code(self) -> None:
        self.set_state(
            idb_fail={"ui describe-all": [1, "Failed to connect to companion: Connection refused"]},
            simctl_devices={"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
                {"udid": UDID, "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}]}},
        )
        code, payload = self.ios("ui", "tree")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.IDB_COMPANION_UNAVAILABLE)
        idb = [argv[:2] for argv in self.argv_log("idb")]
        self.assertEqual(idb.count(["ui", "describe-all"]), 2, "exactly one retry")


class DoctorForeignProxyTests(SweepBase):
    def test_emulator_routed_through_another_sessions_live_proxy_is_named(self) -> None:
        network = self.home / "sessions" / "s_old" / "network"
        network.mkdir(parents=True)
        (network / "proxy.json").write_text(json.dumps({"pid": os.getpid(), "port": 8080}),
                                            encoding="utf-8")
        processes.register("proxy", os.getpid(), port=8080, session_id="s_old",
                           artifacts_dir=str(network))
        self.set_state(devices=[["emulator-5554", "device", ""]],
                       settings={"http_proxy": "10.0.2.2:8080"})
        code, report = self.run_cli("doctor", "--adb", str(FAKE_ADB))
        self.assertEqual(code, 0)
        warning = next(w for w in report["warnings"]
                       if w["code"] == "device_attached_to_foreign_proxy")
        self.assertEqual(warning["target_id"], "emulator-5554")
        self.assertEqual(warning["session_id"], "s_old")
        self.assertIn("network detach", warning["hint"])

    def test_no_proxy_setting_means_no_warning(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]], settings={"http_proxy": ":0"})
        code, report = self.run_cli("doctor", "--adb", str(FAKE_ADB))
        self.assertNotIn("device_attached_to_foreign_proxy",
                         [w["code"] for w in report["warnings"]])


class FlowSweepTests(SweepBase):
    def _flow(self, name: str, body: str) -> Path:
        path = Path(self.tmp.name) / name
        path.write_text("schema: autonom.dev/flow/v1\nappId: com.example.app\n"
                        f"name: {name}\n---\n{body}", encoding="utf-8")
        return path

    def test_dry_run_lists_what_it_would_run(self) -> None:
        self.set_state(ui_dump=str(UI_FIXTURE))
        self.android("session", "start", "--app-id", "com.example.app")
        code, payload = self.android("flow", "run", str(PASS_FLOW), "--dry-run")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["steps"], [])
        self.assertTrue(payload["planned"])
        self.assertEqual(payload["planned"][0]["index"], 1)
        self.assertIn("command", payload["planned"][0])

    def test_input_text_refuses_to_type_into_nothing(self) -> None:
        self.set_state(ui_dump=str(UI_FIXTURE))
        self.android("session", "start", "--app-id", "com.example.app")
        flow = self._flow("type.yaml", "- inputText:\n    value: hello\n    timeoutMs: 300\n")
        code, payload = self.android("flow", "run", str(flow))
        self.assertEqual(code, 1, payload)
        self.assertEqual(payload["failure"]["error_code"], errors.FLOW_NO_FOCUSED_FIELD)
        self.assertEqual(payload["failure"]["failure_class"], "test_failure")
        typed = [argv for argv in self.argv_log("adb") if argv[-3:-1] == ["input", "text"]]
        self.assertEqual(typed, [], "nothing may be typed when nothing has focus")

    def test_input_text_types_once_a_field_has_focus(self) -> None:
        self.set_state(ui_dump=str(UI_FOCUSED))
        self.android("session", "start", "--app-id", "com.example.app")
        flow = self._flow("type.yaml", "- inputText:\n    value: hello\n")
        code, payload = self.android("flow", "run", str(flow))
        self.assertEqual(code, 0, payload)
        typed = [argv for argv in self.argv_log("adb") if argv[-3:-1] == ["input", "text"]]
        self.assertEqual(len(typed), 1)

    def test_ios_input_text_needs_a_field_on_screen(self) -> None:
        booted = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
            {"udid": UDID, "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}]}}
        self.set_state(idb_describe_all=str(IOS_FIXTURE), simctl_devices=booted)
        self.ios("session", "start", "--app-id", "com.example.app")
        flow = Path(self.tmp.name) / "ios_type.yaml"
        flow.write_text("schema: autonom.dev/flow/v1\nappId: com.example.app\nname: t\n---\n"
                        "- inputText:\n    value: hello\n    timeoutMs: 300\n", encoding="utf-8")
        code, payload = self.ios("flow", "run", str(flow))
        self.assertEqual(code, 1, payload)
        self.assertEqual(payload["failure"]["error_code"], errors.FLOW_NO_FOCUSED_FIELD)
        self.set_state(idb_describe_all=str(IOS_TEXTFIELD), simctl_devices=booted)
        code, payload = self.ios("flow", "run", str(flow))
        self.assertEqual(code, 0, payload)
        typed = [argv for argv in self.argv_log("idb") if argv[:2] == ["ui", "text"]]
        self.assertEqual(len(typed), 1)

    def test_require_focus_false_opts_out(self) -> None:
        self.set_state(ui_dump=str(UI_FIXTURE))
        self.android("session", "start", "--app-id", "com.example.app")
        flow = self._flow("type.yaml", "- inputText:\n    value: hello\n    requireFocus: false\n")
        code, payload = self.android("flow", "run", str(flow))
        self.assertEqual(code, 0, payload)

    def test_failure_messages_show_resolved_env_values(self) -> None:
        self.set_state(ui_dump=str(UI_FIXTURE))
        self.android("session", "start", "--app-id", "com.example.app")
        flow = Path(self.tmp.name) / "env.yaml"
        flow.write_text("schema: autonom.dev/flow/v1\nappId: com.example.app\nname: env\n"
                        "env:\n  QUERY: nope\n---\n- assertVisible:\n    selector:\n"
                        "      text: ${QUERY}\n    timeoutMs: 300\n", encoding="utf-8")
        code, payload = self.android("flow", "run", str(flow), "--env", "QUERY=bluetooth")
        self.assertEqual(code, 1, payload)
        self.assertIn("bluetooth", payload["failure"]["error"])
        self.assertNotIn("${QUERY}", payload["failure"]["error"])
        self.assertIn("autonom ui find --text bluetooth --mode contains --all",
                      payload["repair"]["commands"])

    def test_secrets_stay_unresolved_in_messages(self) -> None:
        self.set_state(ui_dump=str(UI_FIXTURE))
        self.android("session", "start", "--app-id", "com.example.app")
        flow = self._flow("secret.yaml", "- assertVisible:\n    selector:\n"
                          "      text: ${PIN}\n    timeoutMs: 300\n")
        self.env["PIN"] = "123456"
        code, payload = self.android("flow", "run", str(flow), "--secret", "PIN")
        self.assertEqual(code, 1, payload)
        self.assertNotIn("123456", json.dumps(payload))


class TraceHintTests(SweepBase):
    def test_simulator_only_instrument_names_the_real_fix(self) -> None:
        self.set_state(
            xctrace=True,
            xctrace_fail="Run issues were detected: * [Error] Hitches is not supported on this platform.",
            simctl_devices={"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
                {"udid": UDID, "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}]}},
        )
        code, payload = self.ios("metrics", "trace", "--preset", "hitches", "--duration", "1",
                                 "--app-id", "com.example.app",
                                 "--out", str(Path(self.tmp.name) / "trace"))
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.TRACE_FAILED)
        self.assertIn("physical device", payload["hint"])


if __name__ == "__main__":
    unittest.main()
