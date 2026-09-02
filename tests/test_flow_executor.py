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
# The focused variant: these flows tap the search field and then type, and a
# real device reports that field focused. `inputText` refuses to type into
# a tree with no focus (flow_no_focused_field), which the plain dump would trip.
UI_DUMP = ROOT / "tests/fixtures/ui_dump_focused.xml"
IOS_TREE = ROOT / "tests/fixtures/idb_describe_all_sample.json"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"

sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import errors  # noqa: E402
from autonom_lib.flow import conditions as flow_conditions  # noqa: E402
from autonom_lib.flow import executor as flow_executor  # noqa: E402
from autonom_lib.flow import parser as flow_parser  # noqa: E402
from autonom_lib.flow import schema as flow_schema  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

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

    def test_prefix_replay_stops_at_step_and_preserves_state(self) -> None:
        flow = self._flow(
            "- launchApp\n"
            "- tapOn:\n"
            "    selector:\n"
            "      description: Open settings\n"
            "      match: exact\n"
            "- pressKey: KEYCODE_ENTER\n",
            header=("schema: autonom.dev/flow/v1\nappId: com.example.app\n"
                    "name: replay\nid: replay-001\n"
                    "onFlowComplete:\n  - back\n---\n"),
        )
        result = self._cli(
            "flow", "run", str(flow), "--until-step", "2",
            "--evidence", "always", "--collect", "screenshot",
            "--collect", "hierarchy",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "replayed")
        self.assertEqual([step["command"] for step in summary["steps"]],
                         ["launchApp", "tapOn"])
        self.assertEqual(summary["replay_target"]["step_index"], 2)
        self.assertNotIn("onFlowComplete", json.dumps(summary))

        manifest = json.loads(
            (Path(summary["events"]).parent / "manifest.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 3)
        self.assertEqual(manifest["status"], "replayed")
        self.assertEqual(manifest["replay"]["portable_restore"],
                         "replay-from-flow-start")
        self.assertTrue(manifest["replay"]["cleanup_hooks_skipped"])
        kinds = {entry["kind"] for entry in manifest["artifact_steps"]}
        self.assertIn("screenshot-before", kinds)
        self.assertIn("screenshot-after", kinds)
        self.assertIn("hierarchy-before", kinds)
        self.assertIn("hierarchy-after", kinds)

    def test_checkpoint_is_an_evidence_boundary_and_replay_target(self) -> None:
        flow = self._flow(
            "- launchApp\n- checkpoint: ready\n- pressKey: KEYCODE_ENTER\n",
            header=("schema: autonom.dev/flow/v1\nappId: com.example.app\n"
                    "name: checkpoint\nid: checkpoint-001\n---\n"),
        )
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        manifest = json.loads(
            (Path(summary["events"]).parent / "manifest.json")
            .read_text(encoding="utf-8"))
        checkpoint = manifest["checkpoints"][0]
        self.assertEqual(checkpoint["step_index"], 2)
        self.assertEqual(checkpoint["name"], "ready")
        evidence = [entry for entry in manifest["artifact_steps"]
                    if entry["step_index"] == 2]
        self.assertEqual({entry["kind"] for entry in evidence},
                         {"screenshot-after", "hierarchy-after"})

    def test_prefix_replay_can_target_a_reproduced_failed_assertion(self) -> None:
        flow = self._flow(
            "- assertVisible:\n"
            "    selector:\n"
            "      id: never-present\n"
            "    timeoutMs: 200\n"
            "- pressKey: KEYCODE_ENTER\n",
        )
        result = self._cli("flow", "run", str(flow), "--until-step", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "replayed")
        self.assertEqual(summary["steps"][0]["status"], "failed")
        self.assertEqual(summary["replay_target"]["status"], "failed")
        self.assertEqual(summary["replay_target"]["error_code"],
                         "flow_assertion_timeout")
        self.assertEqual(len(summary["steps"]), 1)

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


class Slice0281Tests(_AndroidRunBase):
    """0.28.1 engine commands: variables, repeat, scroll, inline runFlow."""

    SETTINGS = "com.example.app:id/settings"

    def _typed(self) -> list[list[str]]:
        return [argv for argv in self._adb_calls()
                if argv[2:5] == ["shell", "input", "text"]]

    def _swipes(self) -> list[list[str]]:
        return [argv for argv in self._adb_calls()
                if argv[2:5] == ["shell", "input", "swipe"]]

    def test_copy_then_paste_types_the_node_text(self) -> None:
        path = self._flow(
            f"- copyTextFrom:\n    selector:\n      id: {self.SETTINGS}\n"
            "- pasteText\n")
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        typed = self._typed()
        self.assertEqual(len(typed), 1)
        self.assertIn("Settings", " ".join(typed[0]))

    def test_runtime_variable_interpolates(self) -> None:
        path = self._flow(
            f"- copyTextFrom:\n    selector:\n      id: {self.SETTINGS}\n"
            "    into: LABEL\n"
            "- inputText: got-${LABEL}\n")
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("got-Settings", " ".join(self._typed()[0]))

    def test_use_before_definition_refuses_statically(self) -> None:
        path = self._flow(
            "- inputText: ${LATER}\n"
            f"- copyTextFrom:\n    selector:\n      id: {self.SETTINGS}\n"
            "    into: LATER\n")
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("flow_var_undefined", result.stderr)
        self.assertEqual(self._typed(), [], "no device action before refusal")

    def test_paste_without_copy_refuses_statically(self) -> None:
        result = self._cli("flow", "run", str(self._flow("- pasteText\n")))
        self.assertEqual(result.returncode, 2)
        self.assertIn("flow_var_undefined", result.stderr)

    def test_variable_conflict_with_env_refuses(self) -> None:
        header = _HEAD.replace("---\n", "env:\n  LABEL: fixed\n---\n")
        path = self._flow(
            f"- copyTextFrom:\n    selector:\n      id: {self.SETTINGS}\n"
            "    into: LABEL\n", header=header)
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 2)
        self.assertIn("flow_var_conflict", result.stderr)

    def test_sensitive_copy_marks_run_and_steps(self) -> None:
        path = self._flow(
            f"- copyTextFrom:\n    selector:\n      id: {self.SETTINGS}\n"
            "    sensitive: true\n"
            "- pasteText\n")
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertTrue(summary["sensitive"])
        # The copied VALUE never lands in the summary or journal. (Screen
        # fingerprints still carry on-screen labels — that text is on the
        # screen regardless of the copy, and the run is marked sensitive.)
        self.assertNotIn("Settings", json.dumps(summary))
        events = [json.loads(line) for line in
                  Path(summary["events"]).read_text(encoding="utf-8").splitlines()]
        for event in events:
            payload = event["payload"]
            if payload.get("command") in ("copyTextFrom", "pasteText") \
                    and event["kind"] == "flow.step.finished":
                self.assertTrue(event["sensitive"], payload)
        journal = "".join(
            j.read_text(encoding="utf-8")
            for j in (self.root / "home/sessions").rglob("journal.ndjson"))
        self.assertNotIn("Settings", journal)

    def test_repeat_runs_exactly_times(self) -> None:
        path = self._flow(
            "- repeat:\n    times: 3\n    commands:\n"
            f"      - tapOn:\n          selector:\n            id: {self.SETTINGS}\n")
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._taps()), 3)
        summary = json.loads(result.stdout)
        events = Path(summary["events"]).read_text(encoding="utf-8")
        block = next(json.loads(line) for line in events.splitlines()
                     if json.loads(line)["kind"] == "flow.step.finished"
                     and json.loads(line)["payload"].get("command") == "repeat")
        self.assertEqual(block["payload"]["iterations"], 3)

    def test_repeat_while_can_stop_before_first_iteration(self) -> None:
        path = self._flow(
            "- repeat:\n    times: 5\n"
            "    while:\n      notVisible:\n        text: Settings\n"
            "    commands:\n"
            f"      - tapOn:\n          selector:\n            id: {self.SETTINGS}\n")
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._taps(), [], "Settings is visible, so zero runs")

    def test_inline_runflow_sees_parent_env(self) -> None:
        header = _HEAD.replace("---\n", "env:\n  GREETING: hello\n---\n")
        path = self._flow(
            "- runFlow:\n    commands:\n      - inputText: say-${GREETING}\n",
            header=header)
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("say-hello", " ".join(self._typed()[0]))

    def test_scroll_is_one_upward_swipe(self) -> None:
        result = self._cli("flow", "run", str(self._flow("- scroll\n")))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._swipes()), 1)

    def test_tap_repeat_taps_n_times(self) -> None:
        path = self._flow(
            f"- tapOn:\n    selector:\n      id: {self.SETTINGS}\n"
            "    repeat: 3\n    delayMs: 0\n")
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._taps()), 3)

    def test_assertions_honor_relational_selectors(self) -> None:
        path = self._flow(
            "- assertVisible:\n    selector:\n      role: framelayout\n"
            "      containsChild:\n        text: Search input\n")
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self._flow(
            f"- assertVisible:\n    selector:\n      id: {self.SETTINGS}\n"
            "      containsDescendants:\n        text: Nope\n"
            "    timeoutMs: 300\n")
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 1,
                         "a relation must not be silently dropped")
        self.assertIn("flow_assertion_timeout", result.stdout)

    def test_center_element_failures_are_honest(self) -> None:
        path = self._flow(
            "- scrollUntilVisible:\n    selector:\n"
            "      id: com.example.app:id/list\n"
            "      containsDescendants:\n        text: Nope\n"
            "    centerElement: true\n    maxSwipes: 1\n")
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("flow_assertion_timeout", result.stdout)
        path = self._flow(
            "- scrollUntilVisible:\n    selector:\n      text: Settings\n"
            "    centerElement: true\n")
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn("ambiguous_selector", result.stdout)

    def test_group_nested_runflow_works(self) -> None:
        (self.root / "sub.yaml").write_text(
            "schema: autonom.dev/flow/v1\nname: s\n---\n- back\n",
            encoding="utf-8")
        path = self._flow(
            "- group:\n    label: g\n    commands:\n"
            "      - runFlow: sub.yaml\n")
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_inline_env_cannot_shadow_a_secret(self) -> None:
        path = self._flow(
            "- runFlow:\n    env:\n      TOKEN: fake\n"
            "    commands:\n      - inputText: use-${TOKEN}\n")
        result = self._cli("flow", "run", str(path), "--secret", "TOKEN",
                           extra_env={"TOKEN": "realvalue"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("use-realvalue", " ".join(self._typed()[0]))

    def test_paste_falls_back_to_declared_env(self) -> None:
        header = _HEAD.replace("---\n", "env:\n  COPIED_TEXT: fromenv\n---\n")
        path = self._flow("- pasteText\n", header=header)
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fromenv", " ".join(self._typed()[0]))

    def test_conflict_with_a_subflow_env_is_static(self) -> None:
        (self.root / "sub.yaml").write_text(
            "schema: autonom.dev/flow/v1\nname: s\nenv:\n  LABEL: theirs\n"
            "---\n- back\n", encoding="utf-8")
        path = self._flow(
            f"- copyTextFrom:\n    selector:\n      id: {self.SETTINGS}\n"
            "    into: LABEL\n"
            "- runFlow: sub.yaml\n")
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 2)
        self.assertIn("flow_var_conflict", result.stderr)

    def test_memo_hit_still_contributes_definitions(self) -> None:
        (self.root / "sub.yaml").write_text(
            "schema: autonom.dev/flow/v1\nname: s\n---\n"
            "- copyTextFrom:\n    selector:\n"
            "      id: com.example.app:id/settings\n    into: GRABBED\n",
            encoding="utf-8")
        path = self._flow(
            "- runFlow:\n    file: sub.yaml\n"
            "    when:\n      platform: android\n"
            "- runFlow: sub.yaml\n"
            "- inputText: v-${GRABBED}\n")
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("v-Settings", " ".join(self._typed()[0]))

    def test_sensitive_nested_in_group_marks_the_run(self) -> None:
        path = self._flow(
            "- group:\n    label: g\n    commands:\n"
            "      - setClipboard:\n          value: sekret\n"
            "          sensitive: true\n")
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["sensitive"])

    def test_swipe_from_anchors_at_the_node(self) -> None:
        path = self._flow(
            "- swipe:\n    direction: up\n"
            "    from:\n      id: com.example.app:id/list\n")
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        swipe = self._swipes()[0]
        # list node bounds [0,700][1080,2200] -> center (540, 1450)
        self.assertEqual(swipe[5:7], ["540", "1450"])


class ConvertedRunTests(_AndroidRunBase):
    """`flow run` on a Maestro file: converted on the fly, marked as such."""

    def test_run_converts_and_marks_summary_and_event(self) -> None:
        path = self.root / "maestro.yaml"
        path.write_text("appId: com.example.app\n---\n- back\n",
                        encoding="utf-8")
        result = self._cli("flow", "run", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["converted_from"], "maestro")
        events = [json.loads(line) for line in
                  Path(summary["events"]).read_text(encoding="utf-8").splitlines()]
        started = next(e for e in events if e["kind"] == "flow.run.started")
        self.assertEqual(started["payload"]["converted_from"], "maestro")


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
        launches = [a for a in self._adb_calls() if "0x10008000" in a]
        self.assertEqual(len(launches), 1, "child launchApp must launch once")
        component = launches[0][launches[0].index("-n") + 1]
        self.assertTrue(component.startswith("com.example.app/"),
                        "child launchApp must use the root appId")

    def test_prefix_replay_can_stop_after_a_runflow_block(self) -> None:
        sub = self.root / "sub.yaml"
        sub.write_text("schema: autonom.dev/flow/v1\nname: child\n---\n"
                       "- launchApp\n- back\n", encoding="utf-8")
        flow = self._flow("- runFlow: sub.yaml\n"
                          "- pressKey: KEYCODE_ENTER\n")
        result = self._cli("flow", "run", str(flow), "--until-step", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "replayed")
        self.assertEqual(summary["replay_target"]["command"], "runFlow")
        self.assertEqual(summary["replay_target"]["step_index"], 1)
        self.assertEqual([step["command"] for step in summary["steps"]],
                         ["launchApp", "back", "runFlow"])
        runflow = summary["steps"][-1]
        self.assertTrue(runflow["step_id"].startswith("src_"))
        self.assertIsInstance(runflow["finished_at_ms"], int)

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
        self.assertEqual(len(captured), 4,
                         "always mode captures before and after each step")

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


class ReviewRegressionTests(_AndroidRunBase):
    """Fixes from the adversarial review of the flow commits — each of these
    was a reproduced defect, so each gets a pinned regression."""

    def test_runflow_env_variables_are_preflighted(self) -> None:
        sub = self.root / "sub.yaml"
        sub.write_text("schema: autonom.dev/flow/v1\nname: child\n---\n- back\n",
                       encoding="utf-8")
        before = len(self._adb_calls())
        flow = self._flow("- runFlow:\n    file: sub.yaml\n"
                          "    env:\n      TOKEN: ${UNDEFINED}\n")
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 2, result.stdout)
        envelope = json.loads(result.stderr)
        self.assertEqual(envelope["error_code"], "flow_var_undefined")
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(len(self._adb_calls()), before,
                         "pre-flight must fail with zero device side effects")

    def test_second_inclusion_with_a_different_frame_is_preflighted(self) -> None:
        sub = self.root / "sub.yaml"
        sub.write_text("schema: autonom.dev/flow/v1\nname: child\n---\n"
                       "- note: hello ${USER}\n", encoding="utf-8")
        before = len(self._adb_calls())
        flow = self._flow(
            "- runFlow:\n    file: sub.yaml\n    env:\n      USER: alice\n"
            "- runFlow: sub.yaml\n")
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(json.loads(result.stderr)["error_code"],
                         "flow_var_undefined")
        self.assertEqual(len(self._adb_calls()), before)

    def test_cleanup_hook_composes_runflow_with_env_and_when(self) -> None:
        inner = self.root / "inner.yaml"
        inner.write_text("schema: autonom.dev/flow/v1\nname: inner\n---\n- back\n",
                         encoding="utf-8")
        cleanup = self.root / "cleanup.yaml"
        cleanup.write_text(
            "schema: autonom.dev/flow/v1\nname: cleanup\n---\n"
            "- note: cleanup for ${ACCOUNT}\n"
            "- runFlow: inner.yaml\n", encoding="utf-8")
        flow = self._flow(
            "- assertVisible:\n    selector:\n"
            "      id: com.example.app:id/search\n",
            header=("schema: autonom.dev/flow/v1\nappId: com.example.app\n"
                    "name: t\nonFlowComplete:\n"
                    "  - runFlow:\n"
                    "      file: cleanup.yaml\n"
                    "      env:\n        ACCOUNT: tester\n"
                    "  - runFlow:\n"
                    "      file: inner.yaml\n"
                    "      when:\n        platform: ios\n---\n"))
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "passed")
        self.assertNotIn("hook_failures", summary,
                         "a composed cleanup hook must succeed")
        keyevents = [a for a in self._adb_calls() if "keyevent" in " ".join(a)]
        self.assertEqual(len(keyevents), 1,
                         "nested runFlow ran once; the ios-only hook skipped")

    def test_env_equals_skip_reason_redacts_secrets(self) -> None:
        sub = self.root / "sub.yaml"
        sub.write_text("schema: autonom.dev/flow/v1\nname: child\n---\n- back\n",
                       encoding="utf-8")
        flow = self._flow(
            "- runFlow:\n"
            "    file: sub.yaml\n"
            "    when:\n"
            "      envEquals:\n"
            "        PASSWORD: wrong\n")
        result = self._cli("flow", "run", str(flow), "--secret", "PASSWORD",
                           extra_env={"PASSWORD": "hunter2"})
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["steps"][0]["status"], "skipped")
        reason = summary["steps"][0]["skip_reason"]
        self.assertIn("PASSWORD", reason)
        self.assertNotIn("hunter2", reason)
        self.assertNotIn("wrong", reason)
        blob = self._artifacts_blob(summary)
        for manifest in (self.root / "home/sessions").rglob("manifest.json"):
            blob += manifest.read_text(encoding="utf-8")
        built = self._cli("report", "build")
        self.assertEqual(built.returncode, 0, built.stderr)
        payload = json.loads(built.stdout)
        blob += Path(payload["html"]).read_text(encoding="utf-8")
        blob += Path(payload["junit"]).read_text(encoding="utf-8")
        self.assertNotIn("hunter2", blob, "secret leaked into an artifact")

    def test_env_equals_redacts_interpolated_secret_expected(self) -> None:
        sub = self.root / "sub.yaml"
        sub.write_text("schema: autonom.dev/flow/v1\nname: child\n---\n- back\n",
                       encoding="utf-8")
        flow = self._flow(
            "- runFlow:\n"
            "    file: sub.yaml\n"
            "    when:\n"
            "      envEquals:\n"
            "        MODE: ${PASSWORD}\n",
            header=("schema: autonom.dev/flow/v1\nappId: com.example.app\n"
                    "name: t\nenv:\n  MODE: fast\n---\n"))
        result = self._cli("flow", "run", str(flow), "--secret", "PASSWORD",
                           extra_env={"PASSWORD": "hunter2"})
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        reason = summary["steps"][0]["skip_reason"]
        self.assertIn("MODE", reason)
        self.assertNotIn("hunter2", reason)

    def test_missing_network_capture_fails_preflight(self) -> None:
        flow = self._flow(
            "- back\n",
            header=("schema: autonom.dev/flow/v1\nappId: com.example.app\n"
                    "name: t\nrequires:\n  capabilities:\n"
                    "    - network.capture\n---\n"))
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 2, result.stdout)
        envelope = json.loads(result.stderr)
        self.assertEqual(envelope["error_code"], "flow_requirements_unmet")
        self.assertIn("network.capture", envelope["error"])
        self.assertEqual(len(self._adb_calls()), 0,
                         "unmet requires must not touch the device")

    def test_declared_local_capabilities_pass_on_fake_android(self) -> None:
        flow = self._flow(
            "- back\n",
            header=("schema: autonom.dev/flow/v1\nappId: com.example.app\n"
                    "name: t\nrequires:\n  capabilities:\n"
                    "    - ui.accessibility\n    - screenshots\n"
                    "    - logs\n---\n"))
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_when_clause_interpolates_variables(self) -> None:
        sub = self.root / "sub.yaml"
        sub.write_text("schema: autonom.dev/flow/v1\nname: child\n---\n- back\n",
                       encoding="utf-8")
        flow = self._flow(
            "- runFlow:\n"
            "    file: sub.yaml\n"
            "    when:\n"
            "      visible:\n"
            "        text: ${ROW}\n"
            "        match: exact\n"
            "      envEquals:\n"
            "        MODE: ${WANTED}\n",
            header=("schema: autonom.dev/flow/v1\nappId: com.example.app\n"
                    "name: t\nenv:\n  ROW: Settings\n  MODE: fast\n"
                    "  WANTED: fast\n---\n"))
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        statuses = {s["command"]: s["status"] for s in summary["steps"]}
        self.assertEqual(statuses["back"], "passed",
                         "an interpolated when: must evaluate, not compare "
                         "the literal ${...} text")

    def test_assertions_honor_selector_index(self) -> None:
        # the fixture has two 'Settings' nodes: index 1 exists, index 5 does not
        flow = self._flow(
            "- assertVisible:\n    selector:\n      text: Settings\n"
            "      match: exact\n      index: 1\n")
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 0, result.stderr)

        flow = self._flow(
            "- assertVisible:\n    selector:\n      text: Settings\n"
            "      match: exact\n      index: 5\n    timeoutMs: 400\n")
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["failure"]["error_code"],
                         "flow_assertion_timeout")

    def test_malformed_number_is_a_positioned_definition_error(self) -> None:
        flow = self._flow("- assertVisible:\n    selector:\n      id: a\n"
                          "    timeoutMs: --5\n")
        result = self._cli("flow", "check", str(flow))
        self.assertEqual(result.returncode, 2)
        envelope = json.loads(result.stderr)
        self.assertEqual(envelope["error_code"], "flow_command_invalid")
        self.assertIn("line", envelope)


class Slice021CommandTests(_AndroidRunBase):
    """0.21.0: relational selectors, long/double press, orientation, group."""

    def test_relational_tap_disambiguates_and_hits_the_left_twin(self) -> None:
        flow = self._flow(
            "- tapOn:\n"
            "    selector:\n"
            "      text: Settings\n"
            "      match: exact\n"
            "      leftOf:\n"
            "        id: com.example.app:id/settings_secondary\n")
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 0, result.stderr)
        taps = self._taps()
        self.assertEqual(len(taps), 1)
        # center of the left twin [40,100,440,220]
        self.assertEqual(taps[0][-2:], ["240", "160"])

    def test_long_press_is_a_zero_distance_swipe(self) -> None:
        flow = self._flow(
            "- longPressOn:\n"
            "    selector:\n"
            "      description: Flutter Save Button\n"
            "    durationMs: 800\n")
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 0, result.stderr)
        presses = [a for a in self._adb_calls()
                   if a[2:5] == ["shell", "input", "swipe"]]
        self.assertEqual(len(presses), 1)
        self.assertEqual(presses[0][5:], ["300", "360", "300", "360", "800"])

    def test_double_tap_dispatches_twice_exactly(self) -> None:
        flow = self._flow(
            "- doubleTapOn:\n"
            "    selector:\n"
            "      description: Flutter Save Button\n")
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._taps()), 2)

    def test_set_orientation_android_and_ios_refusal(self) -> None:
        flow = self._flow("- setOrientation: landscape\n")
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 0, result.stderr)
        rotations = [a for a in self._adb_calls()
                     if a[2:6] == ["shell", "settings", "put", "system"]]
        self.assertEqual([r[6:] for r in rotations],
                         [["accelerometer_rotation", "0"], ["user_rotation", "1"]])

    def test_group_wraps_steps_with_boundary_events(self) -> None:
        flow = self._flow(
            "- group:\n"
            "    label: sanity\n"
            "    commands:\n"
            "      - back\n"
            "      - assertVisible:\n"
            "          selector:\n"
            "            id: com.example.app:id/search\n")
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual([s["command"] for s in summary["steps"]],
                         ["back", "assertVisible"])
        kinds = [json.loads(line)["kind"]
                 for line in Path(summary["events"]).read_text().splitlines()]
        self.assertEqual(kinds.count("flow.step.started"), 3)  # group + 2 steps


class RetryExecutionTests(EnvSandboxMixin, unittest.TestCase):
    """In-process retry semantics with an injectable clock."""

    def setUp(self) -> None:
        import tempfile as _tempfile
        self._tmp = _tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sandbox_home()

    def _executor(self):
        target = Target("android", "emulator-5554", str(FAKE_ADB),
                        {"serial": "emulator-5554"})
        session = {"session_id": "s_test", "artifacts_dir": self._tmp.name}
        clock_value = [0.0]
        return flow_executor.Executor(
            target, session,
            flow_executor.RunConfig(default_timeout_ms=500, interval_ms=500),
            clock=lambda: clock_value[0],
            sleep=lambda s: clock_value.__setitem__(0, clock_value[0] + s),
            screen=(1080, 1920),
        )

    def _build(self, body: str):
        return flow_schema.build_flow(
            flow_parser.parse_document(_HEAD + body, "t.yaml"))

    def test_retry_succeeds_on_the_second_attempt(self) -> None:
        node = {"ref": "n1", "resource_id": "status", "bounds": [0, 0, 10, 10],
                "enabled": True}
        flow = self._build(
            "- retry:\n"
            "    maxAttempts: 2\n"
            "    onlyOn:\n      - flow_assertion_timeout\n"
            "    commands:\n"
            "      - assertVisible:\n"
            "          selector:\n"
            "            id: status\n")
        runner = self._executor()
        responses = [[], [], [node]]  # attempt 1 times out, attempt 2 sees it
        with mock.patch("autonom_lib.ui.snapshot",
                        side_effect=lambda _t: responses.pop(0)
                        if responses else [node]):
            result = runner.run(flow)
        self.assertEqual(result.status, "passed")
        statuses = [(s.command, s.status, s.error_code) for s in result.steps]
        self.assertEqual(statuses[0][1], "failed", statuses)
        self.assertEqual(statuses[1][1], "passed", statuses)

    def test_retry_does_not_catch_codes_outside_only_on(self) -> None:
        flow = self._build(
            "- retry:\n"
            "    maxAttempts: 3\n"
            "    onlyOn:\n      - no_matching_node\n"
            "    commands:\n"
            "      - assertVisible:\n"
            "          selector:\n"
            "            id: status\n")
        runner = self._executor()
        with mock.patch("autonom_lib.ui.snapshot", return_value=[]):
            result = runner.run(flow)
        self.assertEqual(result.status, "failed")
        failed = [s for s in result.steps if s.status == "failed"]
        self.assertEqual(len(failed), 1, "one attempt only — the code is not retryable")


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


class PollCadenceTests(EnvSandboxMixin, unittest.TestCase):
    """Injectable clock: the engine's timing is exact, not approximate.

    Runs the executor in-process, so the machine stores must be redirected —
    failure evidence reads the mock registry, which would otherwise mkdir
    the operator's real ~/.local/state/autonom.
    """

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
        self.sandbox_home()

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

    def test_stale_screen_cache_is_refreshed_on_guard_refusal(self) -> None:
        """Mid-flow rotation: the coordinate guard fires pre-dispatch, the
        executor refreshes the cached size once and retries the guard —
        never the tap itself."""
        node = {"ref": "n1", "text": "Login", "bounds": [1400, 100, 1500, 160],
                "enabled": True}
        runner, _sleeps = self._executor()
        flow = self._build("- tapOn:\n    selector:\n      text: Login\n")
        boom = errors.AutonomError(errors.COORDINATE_SPACE_MISMATCH,
                                   "outside 1080x1920")
        with mock.patch("autonom_lib.ui.snapshot", return_value=[node]), \
             mock.patch("autonom_lib.ui.tap",
                        side_effect=[boom, None]) as tap, \
             mock.patch("autonom_lib.ui.screen_size",
                        return_value=(1920, 1080)) as size:
            result = runner.run(flow)
        self.assertEqual(result.status, "passed")
        self.assertEqual(tap.call_count, 2)
        self.assertEqual(tap.call_args.kwargs.get("screen"), (1920, 1080))
        size.assert_called_once()

    def test_backend_death_mid_poll_aborts_as_infrastructure(self) -> None:
        runner, _sleeps = self._executor()
        flow = self._build("- assertVisible:\n    selector:\n      id: x\n")
        boom = errors.AutonomError(errors.BACKEND_FAILED, "adb died")
        with mock.patch("autonom_lib.ui.snapshot", side_effect=boom):
            with self.assertRaises(errors.AutonomError) as caught:
                runner.run(flow)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertEqual(caught.exception.extra["failure_class"], "infrastructure")


class WhenEnvEqualsRedactionTests(unittest.TestCase):
    def test_secret_name_is_redacted_and_plain_values_are_not(self) -> None:
        secret = flow_schema.WhenClause(env_equals={"PASSWORD": "wrong"})
        met, reason = flow_conditions.evaluate(
            secret, "android", {"PASSWORD": "hunter2"}, lambda: [],
            secret_names={"PASSWORD"}, secret_literals={"hunter2"})
        self.assertFalse(met)
        self.assertEqual(reason, "envEquals: PASSWORD does not match")
        self.assertNotIn("hunter2", reason)
        self.assertNotIn("wrong", reason)

        plain = flow_schema.WhenClause(env_equals={"MODE": "slow"})
        met, reason = flow_conditions.evaluate(
            plain, "android", {"MODE": "fast"}, lambda: [])
        self.assertFalse(met)
        self.assertIn("fast", reason)
        self.assertIn("slow", reason)

        matched = flow_schema.WhenClause(env_equals={"MODE": "fast"})
        met, reason = flow_conditions.evaluate(
            matched, "android", {"MODE": "fast"}, lambda: [])
        self.assertTrue(met)
        self.assertIsNone(reason)

    def test_session_capabilities_need_an_attached_proxy_for_capture(self) -> None:
        target = Target("android", "emulator-5554", str(FAKE_ADB),
                        {"serial": "emulator-5554"})
        bare = flow_executor.session_capabilities(target, {})
        self.assertTrue(bare["ui.accessibility"])
        self.assertTrue(bare["screenshots"])
        self.assertTrue(bare["logs"])
        self.assertFalse(bare["network.capture"])
        attached = flow_executor.session_capabilities(
            target, {"network": {"attached": True}})
        self.assertTrue(attached["network.capture"])


if __name__ == "__main__":
    unittest.main()
