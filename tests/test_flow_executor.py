"""Flow v1 executor: the research-doc Phase-1 exit criteria, against fakes.

What is proven here, with the fake backends' argv logs as the oracle:

- one login-shaped flow passes on fake Android **and** fake iOS (selectors via
  id/description — the iOS `text` caveat is demonstrated, not just documented);
- a duplicate selector performs **no** tap (single-fire mutations);
- an assertion timeout exits 1 as a *test failure* while a dead backend exits
  2 as *infrastructure* — the failure-class split is observable, not claimed;
- secrets never appear in events, journal, or the summary;
- the polling engine's cadence is exact (injectable clock).
"""
from __future__ import annotations

import json
import os
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
UI_DUMP = ROOT / "tests/fixtures/ui_dump.xml"
IOS_TREE = ROOT / "tests/fixtures/idb_describe_all_sample.json"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"

sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import errors  # noqa: E402
from autonom_lib.flow import executor as flow_executor  # noqa: E402
from autonom_lib.flow import parser as flow_parser  # noqa: E402
from autonom_lib.flow import schema as flow_schema  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402

_HEAD = "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: t\n---\n"


class _AndroidRunBase(unittest.TestCase):
    """CLI-level runs against fake adb, fully sandboxed."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.state.write_text(json.dumps({"ui_dump": str(UI_DUMP)}),
                              encoding="utf-8")
        self.env = dict(os.environ)
        self.env.update({
            "AUTONOM_HOME": str(self.root / "home"),
            "AUTONOM_FAKE_STATE": str(self.state),
            "AUTONOM_FAKE_LOG": str(self.log),
        })
        result = self._cli("session", "start", "--app-id", "com.example.app")
        assert result.returncode == 0, result.stderr

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _cli(self, *args: str, extra_env: dict | None = None):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(CLI), "--platform", "android",
             "--serial", "emulator-5554", "--adb", str(FAKE_ADB), *args],
            cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120,
        )

    def _flow(self, body: str, header: str = _HEAD) -> Path:
        path = self.root / "flow.yaml"
        path.write_text(header + body, encoding="utf-8")
        return path

    def _adb_calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"]
                for line in self.log.read_text(encoding="utf-8").splitlines()]

    def _taps(self) -> list[list[str]]:
        return [argv for argv in self._adb_calls()
                if argv[2:5] == ["shell", "input", "tap"]]

    def _artifacts_blob(self, summary: dict) -> str:
        blob = Path(summary["events"]).read_text(encoding="utf-8")
        blob += json.dumps(summary)
        for journal in (self.root / "home/sessions").rglob("journal.ndjson"):
            blob += journal.read_text(encoding="utf-8")
        return blob


class AndroidLoginFlowTests(_AndroidRunBase):
    def test_login_shaped_flow_passes(self) -> None:
        flow = self._flow(
            "- launchApp\n"
            "- tapOn:\n"
            "    selector:\n"
            "      description: Open settings\n"
            "      match: exact\n"
            "- inputText:\n"
            "    value: ${PASSWORD}\n"
            "    sensitive: true\n"
            "- assertVisible:\n"
            "    selector:\n"
            "      id: com.example.app:id/search\n"
            "- takeScreenshot\n"
        )
        result = self._cli("flow", "run", str(flow), "--secret", "PASSWORD",
                           extra_env={"PASSWORD": "hunter2"})
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "passed")
        self.assertEqual([s["status"] for s in summary["steps"]], ["passed"] * 5)
        self.assertTrue(summary["sensitive"])
        self.assertEqual(len(self._taps()), 1, "the tap must fire exactly once")

        blob = self._artifacts_blob(summary)
        self.assertNotIn("hunter2", blob, "secret leaked into an artifact")
        kinds = [json.loads(line)["kind"]
                 for line in Path(summary["events"]).read_text().splitlines()]
        self.assertEqual(kinds[0], "flow.run.started")
        self.assertEqual(kinds[-1], "flow.run.finished")
        self.assertEqual(kinds.count("flow.step.finished"), 5)

    def test_duplicate_selector_taps_nothing(self) -> None:
        flow = self._flow(
            "- tapOn:\n    selector:\n      text: Settings\n      match: exact\n")
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 1, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["failure"]["error_code"], "ambiguous_selector")
        self.assertEqual(summary["failure"]["failure_class"], "test_failure")
        self.assertEqual(self._taps(), [], "an ambiguous selector must not tap")

    def test_assertion_timeout_is_a_test_failure_exit_1(self) -> None:
        flow = self._flow(
            "- assertVisible:\n    selector:\n      id: nope\n    timeoutMs: 600\n")
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 1, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["failure"]["error_code"], "flow_assertion_timeout")
        self.assertEqual(summary["failure"]["failure_class"], "test_failure")
        self.assertGreaterEqual(summary["steps"][0]["attempts"], 2)

    def test_dead_backend_is_infrastructure_exit_2(self) -> None:
        state = json.loads(self.state.read_text())
        state["fail"] = {"-s emulator-5554 exec-out uiautomator dump": [1, "adb gone"]}
        self.state.write_text(json.dumps(state), encoding="utf-8")
        flow = self._flow(
            "- assertVisible:\n    selector:\n      id: anything\n    timeoutMs: 5000\n")
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 2, result.stdout)
        envelope = json.loads(result.stderr)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["failure_class"], "infrastructure")

    def test_optional_tap_is_skipped_and_the_run_continues(self) -> None:
        flow = self._flow(
            "- tapOn:\n"
            "    selector:\n"
            "      text: Not now\n"
            "      match: exact\n"
            "    optional: true\n"
            "    reason: external dialog may not appear\n"
            "    timeoutMs: 400\n"
            "- assertVisible:\n"
            "    selector:\n"
            "      id: com.example.app:id/search\n"
        )
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["steps"][0]["status"], "skipped")
        self.assertEqual(summary["steps"][0]["skip_reason"],
                         "external dialog may not appear")
        self.assertEqual(summary["steps"][1]["status"], "passed")

    def test_dry_run_touches_no_device(self) -> None:
        before = len(self._adb_calls())
        flow = self._flow("- launchApp\n- tapOn: Settings\n")
        result = self._cli("flow", "run", str(flow), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._adb_calls()), before,
                         "dry-run must not dispatch anything")

    def test_start_hook_runs_before_the_steps(self) -> None:
        flow = self._flow("- launchApp\n",
                          header=("schema: autonom.dev/flow/v1\n"
                                  "appId: com.example.app\nname: t\n"
                                  "onFlowStart:\n  - back\n---\n"))
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["steps"][0]["command"], "back")
        self.assertEqual(summary["steps"][0]["hook"], "onFlowStart")
        self.assertEqual(summary["steps"][1]["command"], "launchApp")

    def test_missing_secret_is_infrastructure(self) -> None:
        flow = self._flow("- launchApp\n")
        result = self._cli("flow", "run", str(flow), "--secret", "NO_SUCH_VAR")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error_code"],
                         "flow_secret_undefined")

    def test_undefined_variable_fails_preflight(self) -> None:
        before = len(self._adb_calls())
        flow = self._flow("- inputText:\n    value: ${UNDEFINED_VAR}\n")
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error_code"],
                         "flow_var_undefined")
        self.assertEqual(len(self._adb_calls()), before)

    def test_events_mode_streams_ndjson(self) -> None:
        flow = self._flow("- assertVisible:\n    selector:\n      text: Settings\n"
                          "      match: contains\n")
        result = self._cli("flow", "run", str(flow), "--events")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertGreaterEqual(len(lines), 3)
        self.assertEqual(lines[0]["kind"], "flow.run.started")
        self.assertEqual(lines[-1]["kind"], "flow.run.finished")
        self.assertEqual(lines[0]["schema_version"], 1)


class CompositionTests(_AndroidRunBase):
    """0.20.2: runFlow, hooks, conditions, tags, evidence policy."""

    def test_runflow_executes_the_child_with_inherited_app_id(self) -> None:
        sub = self.root / "sub.yaml"
        sub.write_text("schema: autonom.dev/flow/v1\nname: child\n---\n"
                       "- launchApp\n- back\n", encoding="utf-8")
        flow = self._flow("- runFlow: sub.yaml\n"
                          "- assertVisible:\n    selector:\n"
                          "      id: com.example.app:id/search\n")
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        commands = [(s["command"], s.get("flow", "")) for s in summary["steps"]]
        # child steps run inline, then the runFlow step reports, then the rest
        self.assertEqual([c for c, _ in commands],
                         ["launchApp", "back", "runFlow", "assertVisible"])
        self.assertIn("sub.yaml", commands[0][1])
        monkey = [a for a in self._adb_calls() if "monkey" in " ".join(a)]
        self.assertEqual(len(monkey), 1, "child launchApp must use the root appId")

    def test_runflow_when_condition_skips_with_reason(self) -> None:
        sub = self.root / "sub.yaml"
        sub.write_text("schema: autonom.dev/flow/v1\nname: child\n---\n- back\n",
                       encoding="utf-8")
        flow = self._flow(
            "- runFlow:\n"
            "    file: sub.yaml\n"
            "    when:\n"
            "      platform: ios\n"
            "- assertVisible:\n    selector:\n"
            "      id: com.example.app:id/search\n")
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["steps"][0]["command"], "runFlow")
        self.assertEqual(summary["steps"][0]["status"], "skipped")
        self.assertIn("platform", summary["steps"][0]["skip_reason"])
        keyevents = [a for a in self._adb_calls() if "keyevent" in " ".join(a)]
        self.assertEqual(keyevents, [], "a skipped child must not execute")

    def test_cleanup_hook_runs_after_failure_without_masking_it(self) -> None:
        flow = self._flow(
            "- assertVisible:\n    selector:\n      id: nope\n    timeoutMs: 400\n",
            header=("schema: autonom.dev/flow/v1\nappId: com.example.app\n"
                    "name: t\nonFlowComplete:\n  - back\n---\n"))
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 1, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["failure"]["error_code"], "flow_assertion_timeout")
        keyevents = [a for a in self._adb_calls() if "keyevent" in " ".join(a)]
        self.assertEqual(len(keyevents), 1, "cleanup must still run after failure")
        events = [json.loads(line)
                  for line in Path(summary["events"]).read_text().splitlines()]
        self.assertIn("flow.hook.finished", [e["kind"] for e in events])
        # evidence is captured before cleanup runs
        kinds = [e["kind"] for e in events]
        self.assertLess(kinds.index("flow.evidence.captured"),
                        kinds.index("flow.hook.finished"))

    def test_cleanup_failure_is_recorded_but_never_masks_a_pass(self) -> None:
        flow = self._flow(
            "- assertVisible:\n    selector:\n"
            "      id: com.example.app:id/search\n",
            header=("schema: autonom.dev/flow/v1\nappId: com.example.app\n"
                    "name: t\nonFlowComplete:\n"
                    "  - tapOn:\n"
                    "      selector:\n"
                    "        id: gone\n"
                    "      timeoutMs: 300\n---\n"))
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "passed")
        self.assertEqual(summary["hook_failures"][0]["error_code"],
                         "flow_assertion_timeout")

    def test_evidence_mode_always_captures_per_step(self) -> None:
        flow = self._flow(
            "- back\n- takeScreenshot\n",
            header=("schema: autonom.dev/flow/v1\nappId: com.example.app\n"
                    "name: t\nevidence:\n  mode: always\n---\n"))
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        events = [json.loads(line)
                  for line in Path(summary["events"]).read_text().splitlines()]
        captured = [e for e in events if e["kind"] == "flow.evidence.captured"]
        self.assertEqual(len(captured), 2, "one capture per executed step")

    def test_scroll_until_visible_bounded_and_failing(self) -> None:
        flow = self._flow(
            "- scrollUntilVisible:\n"
            "    selector:\n      id: nowhere\n"
            "    direction: down\n"
            "    maxSwipes: 2\n")
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 1, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["failure"]["error_code"], "flow_assertion_timeout")
        swipes = [a for a in self._adb_calls() if "swipe" in " ".join(a)]
        self.assertEqual(len(swipes), 2, "exactly maxSwipes swipes, never more")

    def test_directory_run_with_tag_filters(self) -> None:
        suite = self.root / "suite"
        suite.mkdir()
        (suite / "a.yaml").write_text(
            "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: a\n"
            "tags: [smoke]\n---\n- back\n", encoding="utf-8")
        (suite / "b.yaml").write_text(
            "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: b\n"
            "tags: [flaky]\n---\n- back\n", encoding="utf-8")
        result = self._cli("flow", "run", str(suite),
                           "--include-tag", "smoke", "--exclude-tag", "flaky")
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "passed")
        # single selected flow collapses to a single-run summary
        self.assertEqual(summary["name"], "a")
        result = self._cli("flow", "run", str(suite), "--include-tag", "nope")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error_code"],
                         "flow_no_flows_found")


class IosLoginFlowTests(unittest.TestCase):
    """The same shape on fake iOS — selectors via id/description, proving the
    platform caveat: the visible label lives in `description`, not `text`."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.state.write_text(json.dumps({
            "idb_describe_all": str(IOS_TREE),
            "installed": ["com.apple.Preferences"],
        }), encoding="utf-8")
        self.env = dict(os.environ)
        self.env.update({
            "AUTONOM_HOME": str(self.root / "home"),
            "AUTONOM_FAKE_STATE": str(self.state),
            "AUTONOM_FAKE_LOG": str(self.log),
        })
        result = self._cli("session", "start", "--app-id", "com.apple.Preferences")
        assert result.returncode == 0, result.stderr

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _cli(self, *args: str):
        return subprocess.run(
            [sys.executable, str(CLI), "--platform", "ios", "--target", UDID,
             "--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB), *args],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120,
        )

    def test_login_shaped_flow_passes_via_id_and_description(self) -> None:
        flow = self.root / "flow.yaml"
        flow.write_text(
            "schema: autonom.dev/flow/v1\n"
            "appId: com.apple.Preferences\n"
            "name: ios\n"
            "---\n"
            "- launchApp\n"
            "- assertNotVisible:\n"
            "    selector:\n"
            "      text: General\n"           # the caveat: text misses on iOS
            "      match: exact\n"
            "- tapOn:\n"
            "    selector:\n"
            "      id: com.apple.settings.general\n"
            "- assertVisible:\n"
            "    selector:\n"
            "      description: General\n"
            "      match: exact\n"
            "- waitUntil:\n"
            "    visible:\n"
            "      id: com.apple.settings.search\n"
            "    timeoutMs: 5000\n",
            encoding="utf-8",
        )
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "passed")
        taps = [json.loads(line)["argv"]
                for line in self.log.read_text().splitlines()
                if json.loads(line)["tool"] == "idb"
                and json.loads(line)["argv"][:2] == ["ui", "tap"]]
        self.assertEqual(len(taps), 1)


class PollCadenceTests(unittest.TestCase):
    """Injectable clock: the engine's timing is exact, not approximate."""

    def _executor(self, **config):
        target = Target("android", "emulator-5554", str(FAKE_ADB),
                        {"serial": "emulator-5554"})
        session = {"session_id": "s_test", "artifacts_dir": self.tmp.name}
        clock_value = [0.0]
        sleeps: list[float] = []

        def clock() -> float:
            return clock_value[0]

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock_value[0] += seconds

        runner = flow_executor.Executor(
            target, session, flow_executor.RunConfig(**config),
            clock=clock, sleep=sleep, screen=(1080, 1920),
        )
        return runner, sleeps

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp
        self.addCleanup(self._tmp.cleanup)

    def _build(self, body: str):
        return flow_schema.build_flow(
            flow_parser.parse_document(_HEAD + body, "t.yaml"))

    def test_timeout_cadence_and_attempt_count(self) -> None:
        runner, sleeps = self._executor(default_timeout_ms=2000, interval_ms=500)
        flow = self._build("- assertVisible:\n    selector:\n      id: nope\n")
        with mock.patch("autonom_lib.ui.snapshot", return_value=[]) as snap:
            result = runner.run(flow)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure["error_code"], "flow_assertion_timeout")
        # t=0, .5, 1.0, 1.5 poll and sleep; the t=2.0 attempt hits the deadline
        self.assertEqual(snap.call_count, 5)
        self.assertEqual(sleeps, [0.5] * 4)

    def test_tap_polls_only_while_zero_matches_then_fires_once(self) -> None:
        node = {"ref": "n1", "text": "Login", "bounds": [0, 0, 100, 50],
                "enabled": True}
        responses = [[], [], [node]]
        runner, _sleeps = self._executor()
        flow = self._build("- tapOn:\n    selector:\n      text: Login\n")
        with mock.patch("autonom_lib.ui.snapshot",
                        side_effect=lambda _t: responses.pop(0)) as snap, \
             mock.patch("autonom_lib.ui.tap") as tap:
            result = runner.run(flow)
        self.assertEqual(result.status, "passed")
        self.assertEqual(snap.call_count, 3)
        tap.assert_called_once()
        self.assertEqual(tap.call_args.kwargs.get("screen"), (1080, 1920))

    def test_backend_death_mid_poll_aborts_as_infrastructure(self) -> None:
        runner, _sleeps = self._executor()
        flow = self._build("- assertVisible:\n    selector:\n      id: x\n")
        boom = errors.AutonomError(errors.BACKEND_FAILED, "adb died")
        with mock.patch("autonom_lib.ui.snapshot", side_effect=boom):
            with self.assertRaises(errors.AutonomError) as caught:
                runner.run(flow)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertEqual(caught.exception.extra["failure_class"], "infrastructure")


if __name__ == "__main__":
    unittest.main()
