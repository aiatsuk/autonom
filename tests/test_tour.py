"""`autonom tour` — the guided first run, proven against the fakes.

The walk is an ordinary flow run with evidence mode `always`, so the tour's
promise — a screenshot, a hierarchy and a log per step, an HTML report, and
a written account — is checked here by pointing the tour at a flow the
fixture tree can satisfy and reading the files it says it wrote.
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
FAKE_EMULATOR = ROOT / "tests/fakes/fake_emulator.py"
UI_FIXTURE = ROOT / "tests/fixtures/ui_dump.xml"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, tour  # noqa: E402
from autonom_lib.flow import validator as flow_validator  # noqa: E402

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


class TourBase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.state = root / "state.json"
        self.state.write_text("{}", encoding="utf-8")
        self.set_env(AUTONOM_FAKE_STATE=str(self.state), AUTONOM_FAKE_LOG=str(root / "log.jsonl"),
                     AUTONOM_HOME=str(self.home), AUTONOM_EMULATOR=str(FAKE_EMULATOR),
                     AUTONOM_ADB=None, AUTONOM_SIMCTL=None, AUTONOM_IDB=None)
        self.env = dict(os.environ)
        self.flow = root / "walk.yaml"
        self.flow.write_text(FIXTURE_FLOW, encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

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


if __name__ == "__main__":
    unittest.main()
