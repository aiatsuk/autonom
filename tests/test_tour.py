"""`autonom tour` — the guided first run, proven against the fakes.

The walk is an ordinary flow run with evidence mode `always`, so the tour's
promise — a screenshot, a hierarchy and a log per step, an HTML report, and
a written account — is checked here by pointing the tour at a flow the
fixture tree can satisfy and reading the files it says it wrote.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
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
FAKE_EMULATOR = ROOT / "tests/fakes/fake_emulator.py"
FAKE_AXE = ROOT / "tests/fakes/fake_axe.py"
UI_FIXTURE = ROOT / "tests/fixtures/ui_dump.xml"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, session as session_mod, tour  # noqa: E402
from autonom_lib.flow import executor as flow_executor  # noqa: E402
from autonom_lib.flow import selectors as flow_selectors  # noqa: E402
from autonom_lib.flow import validator as flow_validator  # noqa: E402
from autonom_lib.platform import IOS, Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

FIXTURE_FLOW = (
    "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: Fixture walk\n---\n"
    "- launchApp:\n    label: Launch the app\n"
    "- assertVisible:\n    selector:\n      text: Settings\n    label: The home screen is up\n"
    "- tapOn:\n    selector:\n      description: Flutter Save Button\n    label: Tap Save\n"
)


# Host tools a tour test must never reach. Real emulators and simulators may
# be attached to the machine running the suite; a test that resolved `xcrun`
# or `adb` from PATH would drive them (and fail whenever one is booted).
HOST_TOOLS = ("adb", "xcrun", "idb", "idb_companion", "emulator", "axe")


class TourBase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.state = root / "state.json"
        self.state.write_text("{}", encoding="utf-8")
        # Every tool resolves to a fake or to a path that does not exist, so
        # nothing falls through to PATH. Tests hand the fakes in explicitly.
        absent = root / "absent"
        self.set_env(AUTONOM_FAKE_STATE=str(self.state), AUTONOM_FAKE_LOG=str(root / "log.jsonl"),
                     AUTONOM_HOME=str(self.home), AUTONOM_EMULATOR=str(FAKE_EMULATOR),
                     AUTONOM_ADB=str(absent / "adb"), AUTONOM_SIMCTL=str(absent / "xcrun"),
                     AUTONOM_IDB=str(absent / "idb"))
        # Sentinels ahead of the real PATH: if anything still looked a host
        # tool up on PATH it would find one of these and leave a trace.
        shims = root / "host-tools"
        shims.mkdir()
        self.host_calls = root / "host-calls.log"
        for name in HOST_TOOLS:
            shim = shims / name
            shim.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.host_calls}"\nexit 1\n',
                            encoding="utf-8")
            shim.chmod(0o755)
        self.set_env(PATH=f"{shims}{os.pathsep}{os.environ.get('PATH', '')}")
        self.env = dict(os.environ)
        self.flow = root / "walk.yaml"
        self.flow.write_text(FIXTURE_FLOW, encoding="utf-8")

    def tearDown(self) -> None:
        calls = self.host_calls.read_text(encoding="utf-8") if self.host_calls.exists() else ""
        self.tmp.cleanup()
        self.assertEqual(calls, "", "a tour test reached a host tool on PATH")

    def fake_calls(self) -> list[dict]:
        log = Path(self.tmp.name) / "log.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def device_rows(self) -> list[list[str]]:
        return json.loads(self.state.read_text(encoding="utf-8")).get("devices", [])

    def set_state(self, **kwargs) -> None:
        self.state.write_text(json.dumps(kwargs), encoding="utf-8")

    def run_raw(self, *argv: str) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run([sys.executable, str(CLI), *argv], capture_output=True,
                                   text=True, env=self.env, timeout=180, cwd=self.tmp.name,
                                   stdin=subprocess.DEVNULL)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        return completed

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        completed = self.run_raw(*argv)
        stream = completed.stdout if completed.returncode in (0, 1) else completed.stderr
        return completed.returncode, json.loads(stream)


class BuiltInFlowTests(unittest.TestCase):
    def test_built_in_tours_validate_and_carry_labels(self) -> None:
        for platform, spec in tour.BUILT_IN.items():
            with self.subTest(platform=platform):
                flow = flow_validator.validate_tree(spec["flow"])
                self.assertEqual(flow.app_id, spec["app_id"])
                self.assertTrue(all(step.label for step in flow.steps),
                                "every tour step must explain itself")
                self.assertNotIn(
                    "takeScreenshot", [step.command for step in flow.steps],
                    "automatic per-step evidence must not create screenshot-only phases")
                self.assertGreaterEqual(
                    sum(1 for step in flow.steps if step.command == "tapOn"), 2)

    def test_every_navigation_step_is_followed_by_an_assertion(self) -> None:
        """TOUR-004: an exit code of 0 does not prove a tap or a back moved
        anything, so the step after each one looks at the screen — and the
        checks after it must contradict the screen just left: something
        asserted visible there is asserted gone, or something asserted gone
        there is asserted visible. A check that also held before the step
        would pass even if the step did nothing."""
        asserts = ("assertVisible", "assertNotVisible")

        def key(step) -> tuple:
            selector = step.args["selector"]
            return tuple(sorted(selector.fields.items())), selector.match

        def run_after(steps, start: int) -> list:
            run = []
            for step in steps[start:]:
                if step.command not in asserts:
                    break
                run.append(step)
            return run

        for platform, spec in tour.BUILT_IN.items():
            steps = flow_validator.validate_tree(spec["flow"]).steps
            previous: list = []
            for index, step in enumerate(steps):
                if step.command not in ("launchApp", "tapOn", "back"):
                    continue
                after = run_after(steps, index + 1)
                with self.subTest(platform=platform, step=step.label):
                    self.assertTrue(after, f"{step.label!r} is not followed by an assertion")
                    self.assertEqual(after[0].command, "assertVisible")
                    if step.command != "launchApp":
                        was_visible = {key(s) for s in previous if s.command == "assertVisible"}
                        was_gone = {key(s) for s in previous if s.command == "assertNotVisible"}
                        contradicts = [
                            s for s in after
                            if (s.command == "assertNotVisible" and key(s) in was_visible)
                            or (s.command == "assertVisible" and key(s) in was_gone)]
                        self.assertTrue(contradicts, f"nothing after {step.label!r} would fail "
                                                     "if the step left the screen unchanged")
                previous = after

    def test_ios_tour_selects_by_description(self) -> None:
        steps = flow_validator.validate_tree(tour.BUILT_IN["ios"]["flow"]).steps
        for step in steps:
            selector = step.args.get("selector")
            if selector is not None:
                fields = set(selector.source_fields or selector.fields)
                self.assertIn("description", fields, step.label)
                self.assertNotIn("text", fields, step.label)

    # Trimmed from live hierarchies (TOUR-006): Android 16 (API 36) Settings,
    # where a sub-screen's title is the collapsing toolbar's description, not
    # a text row; and the iOS 26.5 Simulator Settings, whose home has no
    # Wi-Fi or Bluetooth rows. Each tour's navigation steps walk the screens
    # in the WALKS order.
    LIVE_SCREENS = {
        "android": [
            [{"text": "Search Settings",
              "resource_id": "com.android.settings:id/search_bar_title"},
             {"text": "Network & internet", "resource_id": "android:id/title"},
             {"text": "Mobile, Wi‑Fi, hotspot", "resource_id": "android:id/summary"},
             {"text": "Connected devices", "resource_id": "android:id/title"},
             {"text": "Apps", "resource_id": "android:id/title"}],
            [{"desc": "Network & internet",
              "resource_id": "com.android.settings:id/collapsing_toolbar"},
             {"desc": "Navigate up", "role": "button"},
             {"text": "Internet", "resource_id": "android:id/title"},
             {"text": "AndroidWifi", "resource_id": "android:id/summary"},
             {"text": "SIMs", "resource_id": "android:id/title"},
             {"text": "Airplane mode", "resource_id": "android:id/title"}],
            [{"desc": "Internet", "resource_id": "com.android.settings:id/collapsing_toolbar"},
             {"desc": "Navigate up", "role": "button"},
             {"text": "Wi-Fi", "resource_id": "android:id/title"},
             {"text": "AndroidWifi", "resource_id": "android:id/title"}],
        ],
        "ios": [
            [{"desc": "Settings", "role": "heading"},
             {"desc": "General", "resource_id": "com.apple.settings.general", "role": "button"},
             {"desc": "Accessibility", "resource_id": "com.apple.settings.accessibility",
              "role": "button"},
             {"desc": "Camera", "resource_id": "com.apple.settings.camera", "role": "button"}],
            [{"desc": "Settings", "resource_id": "BackButton", "role": "button"},
             {"desc": "General", "role": "heading"},
             {"desc": "About", "resource_id": "About", "role": "button"},
             {"desc": "Keyboard", "resource_id": "Keyboard", "role": "button"}],
            [{"desc": "General", "resource_id": "BackButton", "role": "button"},
             {"desc": "About", "role": "heading"},
             {"desc": "Model Name, iPhone 17 Pro Max", "resource_id": "ProductModelName",
              "role": "text"},
             {"desc": "iOS Version, 26.5", "resource_id": "SW_VERSION_SPECIFIER",
              "role": "button"}],
        ],
    }
    WALKS = {"android": [0, 1, 2, 1, 0], "ios": [0, 1, 2, 1, 0]}

    def test_built_in_tours_pass_on_the_live_screens(self) -> None:
        """TOUR-006: the tours failed live — Android at the sub-screen title
        (asserted as text, shown as the toolbar description) and iOS at the
        home check (Wi-Fi/Bluetooth rows the 26.5 Simulator does not show).
        Every step must hold against the screens the targets really show."""
        for platform, spec in tour.BUILT_IN.items():
            steps = flow_validator.validate_tree(spec["flow"]).steps
            walk = iter(self.WALKS[platform])
            screen: list = []
            for step in steps:
                with self.subTest(platform=platform, step=step.label):
                    if step.command == "launchApp":
                        screen = self.LIVE_SCREENS[platform][next(walk)]
                        continue
                    selector = step.args.get("selector")
                    found = flow_selectors.select_all(screen, selector) if selector else []
                    if step.command == "assertVisible":
                        self.assertTrue(found, "asserted visible but absent")
                    elif step.command == "assertNotVisible":
                        self.assertFalse(found, "asserted gone but present")
                    elif step.command == "tapOn":
                        self.assertEqual(len(found), 1, "a tap needs exactly one node")
                    if step.command in ("tapOn", "back"):
                        screen = self.LIVE_SCREENS[platform][next(walk)]
            self.assertIsNone(next(walk, None), f"{platform} walk left screens unvisited")


class OverviewTests(TourBase):
    def test_bare_host_still_gets_the_overview(self) -> None:
        self.env["PATH"] = self.tmp.name
        code, payload = self.run_cli("tour")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["mode"], "overview")
        self.assertTrue(payload["overview"] and payload["how_to"])
        self.assertFalse(payload["proposal"]["available"])
        self.assertTrue(payload["proposal"]["reasons"])

    def test_running_emulator_is_offered_first(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]], avds=["Pixel_9"],
                       avd_names={"emulator-5554": "Pixel_9"})
        code, payload = self.run_cli("tour", "--adb", str(FAKE_ADB))
        self.assertEqual(code, 0, payload)
        prop = payload["proposal"]
        self.assertTrue(prop["available"])
        self.assertEqual(prop["device"], {"platform": "android", "target_id": "emulator-5554",
                                          "name": "Pixel_9", "boot_needed": False})
        self.assertEqual(prop["app_id"], "com.android.settings")
        self.assertTrue(all(step["label"] for step in prop["steps"]))
        self.assertIn("--target emulator-5554", prop["run_command"])

    def test_an_avd_is_offered_when_nothing_runs(self) -> None:
        self.set_state(devices=[], avds=["Pixel_9", "Tablet"])
        code, payload = self.run_cli("tour", "--adb", str(FAKE_ADB))
        prop = payload["proposal"]
        self.assertTrue(prop["available"])
        self.assertEqual(prop["device"]["avd"], "Pixel_9")
        self.assertTrue(prop["device"]["boot_needed"])
        self.assertIn("--avd Pixel_9", prop["run_command"])

    def test_ios_needs_idb_to_be_offered(self) -> None:
        # Hide adb through its override rather than PATH: the fakes need
        # python3 on PATH to run at all.
        self.env["AUTONOM_ADB"] = str(Path(self.tmp.name) / "no-adb")
        self.env["AUTONOM_IDB"] = str(Path(self.tmp.name) / "no-idb")  # this Mac has a real idb
        self.set_state(avds=[])  # and the fake emulator would otherwise offer Pixel_9
        code, payload = self.run_cli("tour", "--simctl", str(FAKE_SIMCTL))
        self.assertFalse(payload["proposal"]["available"])
        self.assertTrue(any("idb" in r for r in payload["proposal"]["reasons"]))
        code, payload = self.run_cli("tour", "--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB))
        prop = payload["proposal"]
        self.assertTrue(prop["available"])
        self.assertEqual(prop["platform"], "ios")
        self.assertIn("iPhone", prop["device"]["name"])
        self.assertTrue(prop["device"]["boot_needed"])

    def test_human_prints_markdown(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]])
        completed = self.run_raw("tour", "--adb", str(FAKE_ADB), "--human")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(completed.stdout.startswith("# Autonom"))
        self.assertIn("## The offer", completed.stdout)
        self.assertIn("autonom tour --run", completed.stdout)


class WalkTests(TourBase):
    def test_the_walk_leaves_evidence_a_report_and_an_account(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]], ui_dump=str(UI_FIXTURE),
                       pidof={"com.example.app": "4242"},
                       logcat=["01-01 00:00:00.000  4242  4242 I Example: hello from the app"])
        code, payload = self.run_cli("tour", "--run", "--adb", str(FAKE_ADB),
                                     "--flow", str(self.flow))
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["mode"], "run")
        run = payload["run"]
        self.assertEqual(run["status"], "passed")
        self.assertEqual(run["title"], "Fixture walk")
        self.assertEqual([s["label"] for s in run["steps"]],
                         ["Launch the app", "The home screen is up", "Tap Save"])
        for step in run["steps"]:
            self.assertTrue(Path(step["screenshot"]).exists(), step)
            self.assertTrue(Path(step["hierarchy"]).exists(), step)
        tap = next(step for step in run["steps"] if step["command"] == "tapOn")
        self.assertTrue(Path(tap["screenshot_before"]).exists(), tap)
        self.assertTrue(Path(tap["screenshot"]).exists(), tap)
        self.assertTrue(Path(tap["hierarchy"]).exists(), tap)
        self.assertTrue(Path(tap["logs"]).exists(), tap)
        logs = [step for step in run["steps"] if step.get("logs")]
        self.assertTrue(logs, "the fake logcat lines must reach at least one step's log")
        self.assertTrue(all(Path(step["logs"]).exists() for step in logs))
        for key in ("report_html", "report_junit", "tour_md", "journal"):
            self.assertTrue(Path(run[key]).exists(), key)
        self.assertTrue(Path(run["report_bundle"]).is_dir())
        account = Path(run["tour_md"]).read_text(encoding="utf-8")
        self.assertIn("## What was done", account)
        self.assertIn("Tap Save", account)
        self.assertIn(run["artifacts_dir"], account)
        self.assertTrue(run["session_stopped"])
        self.assertFalse(run["booted_by_tour"])
        code, shown = self.run_cli("session", "show")
        self.assertEqual(shown["error_code"], errors.NO_ACTIVE_SESSION)

    def test_a_failing_walk_is_reported_with_a_repair_brief(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]], ui_dump=str(UI_FIXTURE))
        broken = Path(self.tmp.name) / "broken.yaml"
        broken.write_text(FIXTURE_FLOW.replace("text: Settings", "text: Nowhere")
                          .replace("label: The home screen is up",
                                   "timeoutMs: 300\n    label: The home screen is up"),
                          encoding="utf-8")
        code, payload = self.run_cli("tour", "--run", "--adb", str(FAKE_ADB), "--flow", str(broken))
        self.assertEqual(code, 0, payload)  # the tour itself succeeded in reporting
        run = payload["run"]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["failure"]["error_code"], errors.FLOW_ASSERTION_TIMEOUT)
        self.assertTrue(run["repair"]["commands"])
        self.assertIn("## The step that failed", run["narrative"])

    def test_an_infrastructure_failure_still_closes_the_session(self) -> None:
        """The first real run hit a hung `uiautomator dump`; the tour must not
        leave its own session dangling behind the error envelope."""
        self.set_state(devices=[["emulator-5554", "device", ""]], ui_dump=str(UI_FIXTURE),
                       fail={"-s emulator-5554 exec-out uiautomator": [1, "hung"]})
        code, payload = self.run_cli("tour", "--run", "--adb", str(FAKE_ADB),
                                     "--flow", str(self.flow))
        self.assertEqual(code, 2)
        self.assertTrue(payload.get("session_id"))
        self.assertTrue(payload.get("artifacts_dir"))
        code, shown = self.run_cli("session", "show")
        self.assertEqual(shown["error_code"], errors.NO_ACTIVE_SESSION)

    def test_boots_an_avd_and_can_shut_it_down_after(self) -> None:
        self.set_state(devices=[], avds=["Pixel_9"], ui_dump=str(UI_FIXTURE))
        code, payload = self.run_cli("tour", "--run", "--adb", str(FAKE_ADB),
                                     "--flow", str(self.flow), "--shutdown")
        self.assertEqual(code, 0, payload)
        run = payload["run"]
        self.assertTrue(run["booted_by_tour"])
        self.assertEqual(run["avd"], "Pixel_9")
        self.assertEqual(run["target_id"], "emulator-5556")
        self.assertTrue(run["shutdown"])
        self.assertIn("booted by the tour", run["narrative"])

    def test_run_with_nothing_to_run_on_fails_by_name(self) -> None:
        self.env["PATH"] = self.tmp.name
        code, payload = self.run_cli("tour", "--run")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.NO_TARGET)
        self.assertTrue(payload["reasons"])

    def test_explicit_unknown_target_is_refused(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]])
        code, payload = self.run_cli("tour", "--adb", str(FAKE_ADB), "--target", "emulator-9999")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.NO_TARGET)


class SessionSafetyTests(TourBase):
    """TOUR-001: the tour owns a session of its own and must never replace
    one the user already has (with its proxy, log streams and recorder)."""

    def test_run_refuses_while_a_session_is_active(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]], ui_dump=str(UI_FIXTURE))
        code, started = self.run_cli("session", "start", "--serial", "emulator-5554",
                                     "--adb", str(FAKE_ADB))
        self.assertEqual(code, 0, started)
        code, payload = self.run_cli("tour", "--run", "--adb", str(FAKE_ADB),
                                     "--flow", str(self.flow))
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.SESSION_ALREADY_ACTIVE)
        self.assertIn("autonom session stop", payload["hint"])
        session_id = started["session"]["session_id"]
        self.assertEqual(payload["session_id"], session_id)
        code, shown = self.run_cli("session", "show")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown["session"]["session_id"], session_id)

    def test_refusal_happens_before_anything_boots(self) -> None:
        self.set_state(devices=[], avds=["Pixel_9"], ui_dump=str(UI_FIXTURE))
        session_mod.start_session(str(FAKE_ADB), serial="emulator-5554")
        choice = {"platform": "android", "avd": "Pixel_9", "name": "Pixel_9",
                  "boot_needed": True}
        with self.assertRaises(errors.AutonomError) as caught:
            tour.run(choice, argparse.Namespace(adb=str(FAKE_ADB)), flow_override=self.flow)
        self.assertEqual(caught.exception.code, errors.SESSION_ALREADY_ACTIVE)
        self.assertFalse([c for c in self.fake_calls() if c["tool"] == "emulator"
                          and c["argv"][:1] == ["-avd"]])


class TargetSelectionTests(TourBase):
    """TOUR-002: the walk owns a device, so it never guesses which one."""

    def test_unknown_avd_is_refused_with_the_list(self) -> None:
        self.set_state(devices=[], avds=["Pixel_9", "Tablet"])
        for argv in (("tour",), ("tour", "--run")):
            with self.subTest(argv=argv):
                code, payload = self.run_cli(*argv, "--adb", str(FAKE_ADB), "--avd", "Nope")
                self.assertEqual(code, 2, payload)
                self.assertEqual(payload["error_code"], errors.AVD_NOT_FOUND)
                self.assertEqual(payload["avds"], ["Pixel_9", "Tablet"])
                self.assertIn("Pixel_9", payload["hint"])

    def test_avd_is_honoured_while_another_emulator_runs(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]], avds=["Pixel_9", "Tablet"],
                       avd_names={"emulator-5554": "Pixel_9"})
        code, payload = self.run_cli("tour", "--adb", str(FAKE_ADB), "--avd", "Tablet")
        self.assertEqual(code, 0, payload)
        device = payload["proposal"]["device"]
        self.assertEqual(device["avd"], "Tablet")
        self.assertTrue(device["boot_needed"])
        self.assertIn("--avd Tablet", payload["proposal"]["run_command"])

    def test_avd_that_already_runs_is_used_in_place(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]], avds=["Pixel_9", "Tablet"],
                       avd_names={"emulator-5554": "Pixel_9"})
        code, payload = self.run_cli("tour", "--adb", str(FAKE_ADB), "--avd", "Pixel_9")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["proposal"]["device"],
                         {"platform": "android", "target_id": "emulator-5554",
                          "name": "Pixel_9", "boot_needed": False})

    def test_several_running_targets_are_ambiguous_for_run(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""], ["emulator-5556", "device", ""]],
                       ui_dump=str(UI_FIXTURE))
        code, payload = self.run_cli("tour", "--run", "--adb", str(FAKE_ADB),
                                     "--flow", str(self.flow))
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.AMBIGUOUS_TARGET)
        self.assertEqual(sorted(c["target_id"] for c in payload["candidates"]),
                         ["emulator-5554", "emulator-5556"])
        self.assertIn("emulator-5554", payload["hint"])
        self.assertIn("emulator-5556", payload["hint"])
        code, shown = self.run_cli("session", "show")
        self.assertEqual(shown["error_code"], errors.NO_ACTIVE_SESSION)
        # The overview may still offer one — it runs nothing.
        code, payload = self.run_cli("tour", "--adb", str(FAKE_ADB))
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["proposal"]["available"])

    def test_several_avds_to_boot_are_ambiguous_for_run(self) -> None:
        self.set_state(devices=[], avds=["Pixel_9", "Tablet"])
        code, payload = self.run_cli("tour", "--run", "--adb", str(FAKE_ADB))
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.AMBIGUOUS_TARGET)
        self.assertEqual([c["avd"] for c in payload["candidates"]], ["Pixel_9", "Tablet"])
        self.assertFalse([c for c in self.fake_calls() if c["tool"] == "emulator"
                          and c["argv"][:1] == ["-avd"]], "nothing may boot on ambiguity")

    def test_platform_and_target_mismatch_is_refused(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]])
        code, payload = self.run_cli("tour", "--run", "--adb", str(FAKE_ADB),
                                     "--platform", "ios", "--target", "emulator-5554")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.CONFLICTING_TARGET_FLAGS)

    def test_avd_with_ios_platform_is_refused(self) -> None:
        self.set_state(devices=[], avds=["Pixel_9"])
        code, payload = self.run_cli("tour", "--adb", str(FAKE_ADB),
                                     "--platform", "ios", "--avd", "Pixel_9")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.CONFLICTING_TARGET_FLAGS)

    def test_offline_explicit_target_is_an_envelope_not_a_traceback(self) -> None:
        self.set_state(devices=[["emulator-5554", "offline", ""]], avds=["Pixel_9"])
        for argv in (("tour",), ("tour", "--run")):
            with self.subTest(argv=argv):
                code, payload = self.run_cli(*argv, "--adb", str(FAKE_ADB),
                                             "--target", "emulator-5554")
                self.assertEqual(code, 2, payload)
                self.assertEqual(payload["error_code"], errors.NO_TARGET)
                self.assertEqual(payload["state"], "offline")


class CleanupTests(TourBase):
    """TOUR-003: whatever ends the walk, the tour's session is closed and a
    device it booted goes back down when --shutdown was asked for."""

    def test_infrastructure_failure_after_boot_shuts_down_and_reports(self) -> None:
        self.set_state(devices=[], avds=["Pixel_9"], ui_dump=str(UI_FIXTURE),
                       fail={"-s emulator-5556 exec-out uiautomator": [1, "hung"]})
        code, payload = self.run_cli("tour", "--run", "--adb", str(FAKE_ADB),
                                     "--flow", str(self.flow), "--shutdown")
        self.assertEqual(code, 2, payload)
        self.assertNotIn("emulator-5556", [row[0] for row in self.device_rows()],
                         "the emulator the tour booted must be powered off")
        code, shown = self.run_cli("session", "show")
        self.assertEqual(shown["error_code"], errors.NO_ACTIVE_SESSION)
        # The failure still leaves an account and a report behind.
        account = Path(payload["tour_md"])
        self.assertTrue(account.is_file())
        self.assertIn("## The step that failed", account.read_text(encoding="utf-8"))
        self.assertTrue(Path(payload["report_html"]).is_file())
        self.assertEqual(payload["run_id"], Path(payload["run_dir"]).name)

    def test_a_failing_walk_from_a_shut_down_target_ends_shut_down(self) -> None:
        self.set_state(devices=[], avds=["Pixel_9"], ui_dump=str(UI_FIXTURE))
        broken = Path(self.tmp.name) / "broken.yaml"
        broken.write_text(FIXTURE_FLOW.replace("text: Settings", "text: Nowhere")
                          .replace("label: The home screen is up",
                                   "timeoutMs: 300\n    label: The home screen is up"),
                          encoding="utf-8")
        code, payload = self.run_cli("tour", "--run", "--adb", str(FAKE_ADB),
                                     "--flow", str(broken), "--shutdown")
        self.assertEqual(code, 0, payload)
        run = payload["run"]
        self.assertEqual(run["status"], "failed")
        self.assertTrue(run["shutdown"])
        self.assertTrue(run["session_stopped"])
        self.assertNotIn("emulator-5556", [row[0] for row in self.device_rows()])
        code, shown = self.run_cli("session", "show")
        self.assertEqual(shown["error_code"], errors.NO_ACTIVE_SESSION)

    def test_an_interrupt_mid_walk_stops_the_session_and_the_device(self) -> None:
        self.set_state(devices=[], avds=["Pixel_9"], ui_dump=str(UI_FIXTURE))
        choice = {"platform": "android", "avd": "Pixel_9", "name": "Pixel_9",
                  "boot_needed": True}
        with mock.patch.object(flow_executor.Executor, "run", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                tour.run(choice, argparse.Namespace(adb=str(FAKE_ADB)),
                         flow_override=self.flow, shutdown=True)
        self.assertIsNone(session_mod.load_current())
        self.assertNotIn("emulator-5556", [row[0] for row in self.device_rows()])

    def test_an_unexpected_exception_still_stops_the_session(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]], ui_dump=str(UI_FIXTURE))
        choice = {"platform": "android", "target_id": "emulator-5554", "name": "x",
                  "boot_needed": False}
        with mock.patch.object(flow_executor.Executor, "run", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                tour.run(choice, argparse.Namespace(adb=str(FAKE_ADB)),
                         flow_override=self.flow, shutdown=True)
        self.assertIsNone(session_mod.load_current())
        # Not booted by the tour: --shutdown leaves a device the user had up alone.
        self.assertIn("emulator-5554", [row[0] for row in self.device_rows()])

    def test_ios_without_idb_is_refused_before_booting(self) -> None:
        for idb in (None, str(Path(self.tmp.name) / "no-idb")):
            with self.subTest(idb=idb):
                argv = ["tour", "--run", "--simctl", str(FAKE_SIMCTL), "--target", UDID]
                if idb:
                    argv += ["--idb", idb]  # a missing path used to escape as FileNotFoundError
                code, payload = self.run_cli(*argv)
                self.assertEqual(code, 2, payload)
                self.assertEqual(payload["error_code"], errors.IDB_REQUIRED)
                self.assertFalse([c for c in self.fake_calls() if c["tool"] == "simctl"
                                  and c["argv"][1:2] == ["bootstatus"]],
                                 "the simulator must not be booted for a walk that cannot tap")


BROKEN_IDB_HID = {"state": "ready", "version": "1.1.7",
                  "hid": {"ready": False, "reason": "SimulatorKit moved",
                          "fix": "brew update && brew upgrade idb-companion"}}


class IosHidReadinessTests(TourBase):
    """The tour's HID preflight must agree with the backend `ui_ios` picks."""

    def setUp(self) -> None:
        super().setUp()
        # An empty PATH: the host-tools sentinel `axe` would otherwise count
        # as an installed AXe for every readiness answer below.
        empty = Path(self.tmp.name) / "empty-path"
        empty.mkdir()
        self.set_env(AUTONOM_AXE=None, AUTONOM_IOS_HID=None, AUTONOM_IDB_COMPANION=None,
                     PATH=str(empty))

    def test_forced_idb_with_axe_installed_says_axe_is_not_selected(self) -> None:
        """Before the fix the refusal claimed AXe was not installed while the
        only thing wrong was AUTONOM_IOS_HID=idb overriding it."""
        self.set_env(AUTONOM_AXE=str(FAKE_AXE), AUTONOM_IOS_HID="idb")
        hid = tour.ios_hid_readiness(BROKEN_IDB_HID)
        self.assertEqual((hid["ready"], hid["backend"]), (False, None))
        with self.assertRaises(errors.AutonomError) as caught:
            tour._refuse_unready_ios(BROKEN_IDB_HID, hid)
        self.assertEqual(caught.exception.code, errors.IOS_HID_FRAMEWORK_MISSING)
        self.assertNotIn("AXe is not installed", caught.exception.message)
        self.assertIn("installed but not selected", caught.exception.message)
        self.assertIn("AUTONOM_IOS_HID=axe", caught.exception.hint)

    def test_forced_idb_without_axe_still_says_it_is_missing(self) -> None:
        self.set_env(AUTONOM_IOS_HID="idb")
        hid = tour.ios_hid_readiness(BROKEN_IDB_HID)
        self.assertIsNone(hid["axe"])
        with self.assertRaises(errors.AutonomError) as caught:
            tour._refuse_unready_ios(BROKEN_IDB_HID, hid)
        self.assertIn("AXe is not installed", caught.exception.message)

    def test_configured_companion_is_idb_hid_ready(self) -> None:
        """`ui_ios` never probes a remote companion's HID (the local probe
        says nothing about it), so the tour must not refuse on it either."""
        self.set_env(AUTONOM_IDB_COMPANION="build-mac.local:10882")
        hid = tour.ios_hid_readiness(BROKEN_IDB_HID)
        self.assertEqual((hid["ready"], hid["backend"], hid["idb_hid_ready"]),
                         (True, "idb", True))

    def test_companion_target_alias_is_idb_hid_ready(self) -> None:
        target = Target(IOS, UDID, "xcrun", {"idb_companion": "build-mac.local:10882"})
        hid = tour.ios_hid_readiness(BROKEN_IDB_HID, target)
        self.assertEqual((hid["ready"], hid["backend"]), (True, "idb"))
        # without the alias the same probe is refused
        self.assertFalse(tour.ios_hid_readiness(BROKEN_IDB_HID)["ready"])

    def test_companion_does_not_stand_in_for_a_missing_tree(self) -> None:
        self.set_env(AUTONOM_IDB_COMPANION="build-mac.local:10882")
        hid = tour.ios_hid_readiness({"state": "missing", "error": "idb not found"})
        self.assertFalse(hid["ready"])


class EvidenceTruthTests(TourBase):
    """TOUR-004: reports are private, hints work after the tour, and the
    prompt reads English answers only."""

    def test_reports_are_private_and_the_next_hints_work(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]], ui_dump=str(UI_FIXTURE))
        code, payload = self.run_cli("tour", "--run", "--adb", str(FAKE_ADB),
                                     "--flow", str(self.flow))
        self.assertEqual(code, 0, payload)
        run = payload["run"]
        for key in ("report_html", "report_junit", "tour_md"):
            mode = stat.S_IMODE(Path(run[key]).stat().st_mode)
            self.assertEqual(mode, 0o600, f"{key} is {oct(mode)}")
        self.assertNotIn("journal --session-id", run["narrative"])
        hint = f"autonom report open --session {run['session_id']} --run {run['run_id']}"
        self.assertIn(hint, run["narrative"])
        self.assertIn(run["journal"], run["narrative"])
        self.assertTrue(Path(run["journal"]).is_file())
        # The session is stopped; the hinted lookup (same resolver as
        # `report open`, without launching a browser) must still find the run.
        code, built = self.run_cli("report", "build", "--session", run["session_id"],
                                   "--run", run["run_id"])
        self.assertEqual(code, 0, built)
        self.assertEqual(built["run_id"], run["run_id"])

    def test_the_prompt_takes_english_answers_only(self) -> None:
        class Tty(io.StringIO):
            def isatty(self) -> bool:
                return True

        self.set_state(devices=[["emulator-5554", "device", ""]])
        args = argparse.Namespace(adb=str(FAKE_ADB), run=False, flow=None, target=None,
                                  serial=None, udid=None, platform=None, avd=None,
                                  shutdown=False)
        answers = {"\u0434\n": False, "\u0434\u0430\n": False, "n\n": False,
                   "y\n": True, "yes\n": True}
        for answer, runs in answers.items():
            with self.subTest(answer=answer.strip()):
                with mock.patch.object(sys, "stdin", Tty(answer)), \
                        mock.patch.object(tour, "run", return_value={"narrative": "ran"}) as walk, \
                        contextlib.redirect_stderr(io.StringIO()):
                    payload, _text = tour.command(args)
                self.assertEqual(walk.called, runs)
                self.assertEqual(payload["mode"], "run" if runs else "overview")


if __name__ == "__main__":
    unittest.main()
