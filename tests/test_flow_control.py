"""Flows under the host-wide control lease (contract 5.5).

The executor admits every mutation against the target's lease immediately
before each device actuator call, not only at step start. When the lease
moves (a human takes the device, a holder's generation goes stale, a lease
expires) the run ends `interrupted`: the refused step is recorded as
skipped/`control_lost`, no later step and no cleanup hook runs, and the
evidence (events, manifest) says so.

Everything runs in process with `ui` calls mocked, or the CLI against
`tests/fakes/fake_adb.py`, with a private AUTONOM_HOME (the lease files live
there). No real device is contacted.
"""
from __future__ import annotations

import ast
import contextlib
import io
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
UI_DUMP = ROOT / "tests/fixtures/ui_dump.xml"
EXECUTOR = ROOT / "scripts/autonom_lib/flow/executor.py"
SERIAL = "emulator-5590"

sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import contracts  # noqa: E402
from autonom_lib import control as control_mod  # noqa: E402
from autonom_lib import errors  # noqa: E402
from autonom_lib import proof as proof_mod  # noqa: E402
from autonom_lib.flow import executor as flow_executor  # noqa: E402
from autonom_lib.flow import parser as flow_parser  # noqa: E402
from autonom_lib.flow import report as flow_report  # noqa: E402
from autonom_lib.flow import schema as flow_schema  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

_HEAD = "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: t\n---\n"
_BUTTON = {"ref": "n1", "role": "button", "text": "Go", "desc": "Go",
           "bounds": [100, 300, 500, 420], "clickable": True, "enabled": True}
_TAP_GO = "- tapOn:\n    selector:\n      text: Go\n"
HUMAN = "canvas:3277:4242"
AGENT = "agent:flow-1"


def take_human_lease() -> dict:
    """What a person's first Canvas input does (DEC-013): a human lease."""
    return control_mod.acquire("android", SERIAL, holder_id=HUMAN, kind="human",
                               label="Canvas 3277", ttl_s=3600)


class _InProcess(EnvSandboxMixin, unittest.TestCase):
    """Executor in process: injectable clock, `ui` mocked, private AUTONOM_HOME."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.sandbox_home()
        self.set_env(AUTONOM_CONTROL_HOLDER=None, AUTONOM_CONTROL_GENERATION=None,
                     AUTONOM_TEST_MODE=None, AUTONOM_CONTROL_NOW_MS=None)
        control_mod.end_invocation()
        self.addCleanup(control_mod.end_invocation)
        no_shot = errors.AutonomError(errors.BACKEND_FAILED, "no screenshot in tests")
        for patcher in (
                mock.patch("autonom_lib.screenshot.capture_evidence", side_effect=no_shot),
                mock.patch("autonom_lib.logs.tail", return_value=([], [])),
                mock.patch("autonom_lib.ui.tree", return_value=([_BUTTON], []))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls: list[tuple] = []

    def executor(self, **config) -> flow_executor.Executor:
        target = Target("android", SERIAL, str(FAKE_ADB), {"serial": SERIAL})
        session = {"session_id": "s_flowctl", "artifacts_dir": str(self.root / "art")}
        clock = [0.0]
        return flow_executor.Executor(
            target, session,
            flow_executor.RunConfig(**{"default_timeout_ms": 2000, "interval_ms": 500,
                                       **config}),
            clock=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            screen=(1080, 1920),
        )

    def build(self, body: str, head: str = _HEAD):
        return flow_schema.build_flow(
            flow_parser.parse_document(head + body, str(self.root / "t.yaml")))

    def recorder(self, name: str, after: int | None = None, action=None):
        """A fake actuator that logs its calls; on call number `after` it runs
        `action` (by default: a person takes the device) after acting."""
        count = [0]

        def actuator(*args, **kwargs):
            count[0] += 1
            self.calls.append((name, args[1:]))
            if after is not None and count[0] == after:
                (action or take_human_lease)()
            return {"backend": "fake"}
        return actuator

    def count(self, name: str) -> int:
        return sum(1 for call in self.calls if call[0] == name)

    @staticmethod
    def events(result) -> list[dict]:
        lines = Path(result.events_path).read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    @staticmethod
    def manifest(result) -> dict:
        return json.loads((Path(result.events_path).parent / "manifest.json")
                          .read_text(encoding="utf-8"))


class InterruptedRunTests(_InProcess):

    def run_six_taps(self, head: str = _HEAD):
        flow = self.build(_TAP_GO * 6, head=head)
        with mock.patch("autonom_lib.ui.snapshot", return_value=[_BUTTON]), \
             mock.patch("autonom_lib.ui.tap", side_effect=self.recorder("tap", after=3)), \
             mock.patch("autonom_lib.ui.press_key", side_effect=self.recorder("key")):
            return self.executor().run(flow)

    def test_a_human_taking_the_device_after_the_third_tap_interrupts_the_run(self) -> None:
        head = ("schema: autonom.dev/flow/v1\nappId: com.example.app\nname: t\n"
                "onFlowComplete:\n  - back\n---\n")
        result = self.run_six_taps(head)
        self.assertEqual(result.status, "interrupted")
        self.assertEqual(self.count("tap"), 3, self.calls)
        self.assertEqual(self.count("key"), 0, "onFlowComplete must not run")
        self.assertEqual(result.hook_failures, [])
        self.assertEqual([step.status for step in result.steps],
                         ["passed", "passed", "passed", "skipped"])
        refused = result.steps[3]
        self.assertEqual(refused.index, 4)
        self.assertEqual(refused.skip_reason, "control_lost")
        self.assertEqual(refused.error_code, errors.CONTROL_HELD)
        self.assertEqual(refused.failure_class, flow_schema.CONTROL_LOST)
        failure = result.failure
        self.assertEqual(failure["step_index"], 4)
        self.assertEqual(failure["command"], "tapOn")
        self.assertEqual(failure["error_code"], errors.CONTROL_HELD)
        self.assertEqual(failure["failure_class"], "control_lost")
        self.assertEqual(failure["holder"]["id"], HUMAN)
        self.assertEqual(failure["holder"]["kind"], "human")
        self.assertEqual(failure["generation"], 1)
        for key in ("line", "flow", "error"):
            self.assertIn(key, failure)

        events = self.events(result)
        kinds = [event["kind"] for event in events]
        self.assertIn("flow.step.refused", kinds)
        refused_at = kinds.index("flow.step.refused")
        self.assertEqual(kinds[refused_at + 1:], ["flow.run.finished"],
                         "only flow.run.finished follows the refusal")
        payload = events[refused_at]["payload"]
        self.assertEqual(payload["step_index"], 4)
        self.assertEqual(payload["command"], "tapOn")
        self.assertEqual(payload["error_code"], errors.CONTROL_HELD)
        self.assertEqual(payload["holder"]["id"], HUMAN)
        self.assertEqual(payload["generation"], 1)
        self.assertEqual(events[-1]["payload"]["status"], "interrupted")
        self.assertNotIn("flow.hook.finished", kinds)

        manifest = self.manifest(result)
        self.assertEqual(manifest["status"], "interrupted")
        self.assertEqual(manifest["execution_status"], "broken")
        self.assertEqual(manifest["proof_verdict"], "inconclusive")
        self.assertEqual(manifest["primary_error"]["failure_class"], "control_lost")

    def test_without_a_lease_file_the_same_flow_passes_untouched(self) -> None:
        flow = self.build(_TAP_GO * 6)
        with mock.patch("autonom_lib.ui.snapshot", return_value=[_BUTTON]), \
             mock.patch("autonom_lib.ui.tap", side_effect=self.recorder("tap")):
            result = self.executor().run(flow)
        self.assertEqual(result.status, "passed")
        self.assertEqual(self.count("tap"), 6)
        self.assertIsNone(result.failure)
        kinds = [event["kind"] for event in self.events(result)]
        self.assertNotIn("flow.step.refused", kinds)
        self.assertFalse((self.home / "control").exists(), "admission created files")

    def test_a_takeover_inside_retry_ends_the_run(self) -> None:
        flow = self.build(
            "- retry:\n    maxAttempts: 3\n    allowMutations: true\n    commands:\n"
            "      - tapOn:\n          selector:\n            text: Go\n"
            "      - assertVisible:\n          selector:\n            text: Never\n")
        with mock.patch("autonom_lib.ui.snapshot", return_value=[_BUTTON]), \
             mock.patch("autonom_lib.ui.tap", side_effect=self.recorder("tap", after=1)):
            result = self.executor().run(flow)
        self.assertEqual(result.status, "interrupted")
        self.assertEqual(self.count("tap"), 1, "the retry must not tap again")
        self.assertEqual(result.failure["error_code"], errors.CONTROL_HELD)
        self.assertEqual(result.steps[-1].status, "skipped")
        self.assertEqual(result.steps[-1].skip_reason, "control_lost")

    def test_a_takeover_inside_retry_records_the_block_as_interrupted(self) -> None:
        # attempt 1 taps (then a person takes the device) and fails its
        # assertion; attempt 2 is refused before it taps
        flow = self.build(
            "- retry:\n    maxAttempts: 3\n    allowMutations: true\n    commands:\n"
            "      - tapOn:\n          selector:\n            text: Go\n"
            "      - assertVisible:\n          selector:\n            text: Never\n")
        with mock.patch("autonom_lib.ui.snapshot", return_value=[_BUTTON]), \
             mock.patch("autonom_lib.ui.tap", side_effect=self.recorder("tap", after=1)):
            result = self.executor().run(flow)
        self.assertEqual(result.status, "interrupted")
        self.assertEqual(self.count("tap"), 1)
        retry = [block for block in result.blocks if block["command"] == "retry"]
        self.assertEqual(len(retry), 1, result.blocks)
        block = retry[0]
        self.assertEqual(block["status"], "interrupted")
        self.assertEqual(block["index"], 1)
        self.assertEqual([attempt["status"] for attempt in block["attempts_detail"]],
                         ["failed", "interrupted"])
        refused = result.steps[-1]
        self.assertEqual(block["attempts_detail"][-1]["first_index"], refused.index)
        # like group and repeat: only flow.run.finished follows the refusal
        kinds = [event["kind"] for event in self.events(result)]
        refused_at = kinds.index("flow.step.refused")
        self.assertEqual(kinds[refused_at + 1:], ["flow.run.finished"])
        manifest_retry = [block for block in self.manifest(result)["blocks"]
                          if block["command"] == "retry"]
        self.assertEqual([block["status"] for block in manifest_retry], ["interrupted"])

    def test_scroll_until_visible_stops_between_attempts(self) -> None:
        flow = self.build("- scrollUntilVisible:\n    selector:\n      text: Far\n"
                          "    maxSwipes: 5\n")
        with mock.patch("autonom_lib.ui.snapshot", return_value=[_BUTTON]), \
             mock.patch("autonom_lib.ui.swipe", side_effect=self.recorder("swipe", after=1)):
            result = self.executor().run(flow)
        self.assertEqual(result.status, "interrupted")
        self.assertEqual(self.count("swipe"), 1)

    def test_a_long_press_whose_element_appears_after_the_lease_changed_never_presses(self) -> None:
        flow = self.build("- longPressOn:\n    selector:\n      text: Go\n")
        polls = [0]

        def snapshot(_target):
            polls[0] += 1
            if polls[0] == 2:
                take_human_lease()  # the lease moves while the step polls
            return [] if polls[0] < 3 else [_BUTTON]

        with mock.patch("autonom_lib.ui.snapshot", side_effect=snapshot), \
             mock.patch("autonom_lib.ui.long_press", side_effect=self.recorder("press")):
            result = self.executor().run(flow)
        self.assertGreaterEqual(polls[0], 3, "the element did appear")
        self.assertEqual(self.count("press"), 0)
        self.assertEqual(result.status, "interrupted")
        self.assertEqual(result.steps[-1].command, "longPressOn")

    def test_a_typed_text_waits_for_focus_then_is_refused(self) -> None:
        field = {**_BUTTON, "role": "textfield", "focused": True,
                 "class": "android.widget.EditText", "resource_id": "f"}
        flow = self.build("- inputText: hello\n")
        polls = [0]

        def snapshot(_target):
            polls[0] += 1
            if polls[0] == 1:
                take_human_lease()
            return [field]

        with mock.patch("autonom_lib.ui.snapshot", side_effect=snapshot), \
             mock.patch("autonom_lib.ui.type_text", side_effect=self.recorder("type")):
            # admitted at dispatch start (no lease yet); the lease moves during
            # the focus poll, so the type itself is refused
            result = self.executor().run(flow)
        self.assertEqual(polls[0], 1)
        self.assertEqual(self.count("type"), 0)
        self.assertEqual(result.status, "interrupted")
        self.assertEqual(result.failure["error_code"], errors.CONTROL_HELD)

    def test_a_setup_write_under_a_foreign_lease_is_refused(self) -> None:
        take_human_lease()
        head = ("schema: autonom.dev/flow/v1\nappId: com.example.app\nname: t\n"
                "setup:\n  orientation: landscape\n---\n")
        flow = self.build(_TAP_GO, head=head)
        with mock.patch("autonom_lib.device_state.set_orientation",
                        side_effect=self.recorder("orientation")), \
             mock.patch("autonom_lib.ui.snapshot", return_value=[_BUTTON]), \
             mock.patch("autonom_lib.ui.tap", side_effect=self.recorder("tap")):
            result = self.executor().run(flow)
        self.assertEqual(result.status, "interrupted")
        self.assertEqual(self.calls, [])
        self.assertEqual(result.failure["command"], "setup")
        self.assertEqual(result.steps, [])
        refused = [event for event in self.events(result) if event["kind"] == "flow.step.refused"]
        self.assertEqual(len(refused), 1)

    def test_a_lease_lost_during_cleanup_keeps_the_outcome_and_stops_cleanup(self) -> None:
        head = ("schema: autonom.dev/flow/v1\nappId: com.example.app\nname: t\n"
                "onFlowComplete:\n  - back\n  - back\n---\n")
        flow = self.build(_TAP_GO, head=head)
        with mock.patch("autonom_lib.ui.snapshot", return_value=[_BUTTON]), \
             mock.patch("autonom_lib.ui.tap", side_effect=self.recorder("tap", after=1)), \
             mock.patch("autonom_lib.ui.press_key", side_effect=self.recorder("key")):
            result = self.executor().run(flow)
        self.assertEqual(result.status, "passed")
        self.assertEqual(self.count("key"), 0)
        self.assertEqual(len(result.hook_failures), 1)
        self.assertEqual(result.hook_failures[0]["error_code"], errors.CONTROL_HELD)


class ClaimedRunTests(_InProcess):

    def test_the_holder_runs_while_it_holds_the_lease(self) -> None:
        grant = control_mod.acquire("android", SERIAL, holder_id=AGENT, kind="agent", ttl_s=3600)
        control_mod.begin_invocation(claim=control_mod.Claim(AGENT, grant["generation"]),
                                     operation_id=None, mode="flow")
        flow = self.build(_TAP_GO * 2)
        with mock.patch("autonom_lib.ui.snapshot", return_value=[_BUTTON]), \
             mock.patch("autonom_lib.ui.tap", side_effect=self.recorder("tap")):
            result = self.executor().run(flow)
        self.assertEqual(result.status, "passed")
        self.assertEqual(self.count("tap"), 2)

    def test_operation_ids_name_the_run_and_the_step(self) -> None:
        control_mod.acquire("android", SERIAL, holder_id=AGENT, kind="agent", ttl_s=3600)
        control_mod.begin_invocation(claim=control_mod.Claim(AGENT, None),
                                     operation_id=None, mode="flow")
        seen: list[str | None] = []
        real = control_mod.admit

        def spy(*args, **kwargs):
            seen.append(kwargs.get("operation_id"))
            return real(*args, **kwargs)

        flow = self.build(_TAP_GO * 2)
        with mock.patch("autonom_lib.ui.snapshot", return_value=[_BUTTON]), \
             mock.patch("autonom_lib.ui.tap", side_effect=self.recorder("tap")), \
             mock.patch.object(control_mod, "admit", side_effect=spy):
            result = self.executor().run(flow)
        self.assertEqual(result.status, "passed")
        self.assertTrue(seen)
        self.assertEqual({op.rsplit(":", 1)[1] for op in seen}, {"1", "2"})
        self.assertTrue(all(op.startswith(result.run_id + ":") for op in seen))

    def test_an_unclaimed_run_under_an_agent_lease_never_taps(self) -> None:
        control_mod.acquire("android", SERIAL, holder_id=AGENT, kind="agent", ttl_s=3600)
        flow = self.build(_TAP_GO * 2)
        with mock.patch("autonom_lib.ui.snapshot", return_value=[_BUTTON]), \
             mock.patch("autonom_lib.ui.tap", side_effect=self.recorder("tap")):
            result = self.executor().run(flow)
        self.assertEqual(result.status, "interrupted")
        self.assertEqual(self.count("tap"), 0)
        self.assertEqual(result.steps[0].status, "skipped")

    def test_a_stale_generation_is_refused_stale(self) -> None:
        control_mod.acquire("android", SERIAL, holder_id=AGENT, kind="agent", ttl_s=3600)
        control_mod.acquire("android", SERIAL, holder_id=HUMAN, kind="human",
                            takeover=True, ttl_s=3600)
        control_mod.begin_invocation(claim=control_mod.Claim(AGENT, 1),
                                     operation_id=None, mode="flow")
        flow = self.build(_TAP_GO)
        with mock.patch("autonom_lib.ui.snapshot", return_value=[_BUTTON]), \
             mock.patch("autonom_lib.ui.tap", side_effect=self.recorder("tap")):
            result = self.executor().run(flow)
        self.assertEqual(result.status, "interrupted")
        self.assertEqual(result.failure["error_code"], errors.CONTROL_GENERATION_STALE)
        self.assertEqual(result.failure["generation"], 2)


class SourceScanTests(unittest.TestCase):
    """Every actuator call of the executor is admitted immediately before it."""

    ACTUATORS = {
        ("session_mod", "clear_data"), ("session_mod", "launch_app"),
        ("session_mod", "launch_app_fresh"), ("session_mod", "force_stop"),
        ("ios_simctl", "terminate"), ("ios_simctl", "launch"),
        ("ios_simctl", "uninstall"), ("ios_simctl", "install"),
        ("device_state", "open_url"), ("device_state", "set_orientation"),
        ("device_state", "set_location"), ("device_state", "permissions"),
        ("device_state", "add_media"), ("ui_mod", "long_press"),
        ("ui_mod", "double_tap"), ("ui_mod", "type_text"), ("ui_mod", "press_key"),
        ("ui_mod", "tap"), ("ui_mod", "swipe"), ("simulator_mod", "apply"),
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = EXECUTOR.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    @staticmethod
    def _is_admit(statement: ast.stmt) -> bool:
        return (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Attribute)
                and statement.value.func.attr == "_admit"
                and isinstance(statement.value.func.value, ast.Name)
                and statement.value.func.value.id == "self")

    def _actuator_calls(self, statement: ast.stmt) -> list[ast.Call]:
        """Actuator calls inside one simple statement (not nested blocks)."""
        calls = []
        for node in ast.walk(statement):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and (node.func.value.id, node.func.attr) in self.ACTUATORS):
                calls.append(node)
        return calls

    def test_every_actuator_call_is_preceded_by_admit_in_its_block(self) -> None:
        checked = 0
        problems = []
        for node in ast.walk(self.tree):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                for position, statement in enumerate(block):
                    if isinstance(statement, (ast.If, ast.For, ast.While, ast.Try,
                                              ast.With, ast.FunctionDef, ast.ClassDef)):
                        continue  # their own blocks are visited on their own
                    calls = self._actuator_calls(statement)
                    if not calls:
                        continue
                    checked += len(calls)
                    previous = block[position - 1] if position else None
                    if previous is None or not self._is_admit(previous):
                        problems.append(f"line {statement.lineno}: "
                                        f"{ast.get_source_segment(self.source, calls[0])[:60]}")
        self.assertEqual(problems, [], "actuator calls without self._admit() right before them")
        self.assertGreaterEqual(checked, 25)

    def test_the_command_tables_cover_the_language(self) -> None:
        mutating = flow_executor.MUTATING_COMMANDS
        other = flow_executor.NON_MUTATING_COMMANDS
        self.assertEqual(mutating & other, set())
        self.assertEqual(mutating | other, set(flow_schema.REGISTRY))
        self.assertEqual(len(mutating), 19)
        self.assertIn("setClipboard", other)

    def test_control_lost_is_caught_by_no_step_handler(self) -> None:
        lost = flow_executor._ControlLost
        self.assertFalse(issubclass(lost, errors.AutonomError))
        self.assertFalse(issubclass(lost, flow_executor._Stop))

    def test_the_control_codes_are_their_own_failure_class(self) -> None:
        for code in (errors.CONTROL_HELD, errors.CONTROL_GENERATION_STALE,
                     errors.CONTROL_LEASE_EXPIRED, errors.CONTROL_NOT_HELD):
            self.assertEqual(flow_schema.failure_class(code), flow_schema.CONTROL_LOST)


class ConsumerTests(unittest.TestCase):
    """Consumers never read `interrupted` as a pass."""

    def test_execution_status_of_an_interrupted_run_is_broken(self) -> None:
        self.assertEqual(contracts.execution_status("interrupted"), "broken")
        self.assertEqual(contracts.execution_status("passed"), "passed")
        self.assertEqual(contracts.execution_status("nonsense"), "unknown")

    def test_proof_over_an_interrupted_run_is_inconclusive(self) -> None:
        selected = [{"path": "a.yaml", "reasons": ["x"]}]
        steps = [{"status": "passed"}, {"status": "skipped"}]
        self.assertEqual(proof_mod.verdict(selected, [{"status": "interrupted", "steps": steps}],
                                           [], None), "inconclusive")
        self.assertEqual(proof_mod.verdict(
            selected, [{"status": "interrupted", "steps": steps},
                       {"status": "failed", "steps": steps}], [], None), "fail")
        self.assertEqual(proof_mod.verdict(selected, [{"status": "passed", "steps": steps}],
                                           [], None), "pass")

    @staticmethod
    def manifest(status: str) -> dict:
        return {"run_id": f"fr_{status}", "flow_name": f"flow {status}", "status": status,
                "flow_path": "/x/f.yaml", "reproduction": "autonom flow run f.yaml",
                "primary_error": ({"error_code": "control_held"}
                                  if status == "interrupted" else None),
                "steps": [{"index": 1, "command": "tapOn", "status": "passed",
                           "duration_ms": 5}]}

    def test_the_suite_html_of_an_interrupted_run_is_not_marked_passed(self) -> None:
        page = flow_report.render_suite_html([self.manifest("interrupted")])
        self.assertIn('<span class="status">failed</span>', page)
        self.assertNotIn('<span class="status">passed</span>', page)
        self.assertIn("interrupted", page)
        self.assertIn(flow_report.INTERRUPTED_COLOR, page)

    def test_a_passing_suite_page_is_unchanged_by_the_new_status(self) -> None:
        page = flow_report.render_suite_html([self.manifest("passed")])
        self.assertIn('<span class="status">passed</span>', page)
        self.assertNotIn("interrupted", page)
        self.assertNotIn(flow_report.INTERRUPTED_COLOR, page)

    def test_the_run_page_colours_an_interrupted_run(self) -> None:
        page = flow_report.render_run_page(self.manifest("interrupted"), {})
        self.assertIn(f".status {{ color:{flow_report.INTERRUPTED_COLOR}; }}", page)


class FlowRunCliTests(EnvSandboxMixin, unittest.TestCase):
    """`flow run` exits 1 with `status: interrupted` (in process, fake adb)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / ".autonom").mkdir()
        self.home = self.root / "home"
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.state.write_text(json.dumps({
            "devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"]],
            "ui_dump": str(UI_DUMP)}), encoding="utf-8")
        self.set_env(AUTONOM_HOME=str(self.home), AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.log), AUTONOM_ADB=None, AUTONOM_SIMCTL=None,
                     AUTONOM_IDB=None, AUTONOM_CONTROL_HOLDER=None,
                     AUTONOM_CONTROL_GENERATION=None)
        control_mod.end_invocation()
        self.addCleanup(control_mod.end_invocation)
        from autonom_lib import session as session_mod  # noqa: PLC0415
        token = session_mod.select(None)
        self.addCleanup(session_mod._SELECTED.reset, token)
        completed = subprocess.run(
            [sys.executable, str(CLI), "--adb", str(FAKE_ADB), "--serial", SERIAL,
             "session", "start", "--app-id", "com.example.app"],
            cwd=self.root, env=dict(os.environ), text=True, capture_output=True,
            stdin=subprocess.DEVNULL, check=False, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.flow = self.root / ".autonom" / "taps.yaml"
        self.flow.write_text(
            _HEAD + "- tapOn:\n    selector:\n      description: Flutter Save Button\n"
                    "      match: exact\n" * 3, encoding="utf-8")

    @staticmethod
    def load_cli():
        import importlib.util  # noqa: PLC0415
        spec = importlib.util.spec_from_file_location("autonom_cli_flow_control", CLI)
        cli = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cli)
        return cli

    def call(self, cli, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--adb", str(FAKE_ADB), "--serial", SERIAL, *argv])
        control_mod.end_invocation()
        return code, out.getvalue(), err.getvalue()

    def device_taps(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text(encoding="utf-8").splitlines()
                if '"tap"' in line]

    def test_a_dry_run_under_a_foreign_lease_is_not_refused(self) -> None:
        cli = self.load_cli()
        take_human_lease()
        for path in (self.flow, self.flow.parent):  # file and directory forms
            code, out, err = self.call(cli, "flow", "run", "--dry-run", str(path))
            self.assertEqual(code, 0, err)
            summary = json.loads(out)
            self.assertEqual(summary["status"], "planned", summary)
            self.assertNotIn(errors.CONTROL_HELD, err)
        self.assertEqual(self.device_taps(), [])
        # a real run under the same lease is still refused
        code, out, err = self.call(cli, "flow", "run", str(self.flow))
        self.assertNotEqual(code, 0)
        self.assertIn(errors.CONTROL_HELD, out + err)
        self.assertEqual(self.device_taps(), [])

    def test_report_suite_counts_an_interrupted_run_as_a_failure(self) -> None:
        cli = self.load_cli()
        real_tap = cli.ui_mod.tap

        def tap(*args, **kwargs):
            answer = real_tap(*args, **kwargs)
            take_human_lease()
            return answer

        with mock.patch("autonom_lib.ui.tap", side_effect=tap):
            code, out, err = self.call(cli, "flow", "run", str(self.flow))
        self.assertEqual(code, 1, err)
        self.assertEqual(json.loads(out)["status"], "interrupted")

        code, out, err = self.call(cli, "report", "suite", "--out", str(self.root / "suite"))
        self.assertEqual(code, 1, "an interrupted run fails the suite")
        payload = json.loads(out)
        self.assertEqual(payload["flows"], 1)
        self.assertEqual(payload["passed"], 0)
        self.assertEqual(payload["failed"], 0)
        self.assertEqual(payload["interrupted"], 1)
        self.assertEqual(len(payload["failures"]), 1, payload)
        failure = payload["failures"][0]
        self.assertEqual(failure["status"], "interrupted")
        self.assertEqual(failure["error_code"], errors.CONTROL_HELD)
        html_text = Path(payload["html"]).read_text(encoding="utf-8")
        self.assertIn('<span class="status">failed</span>', html_text)

    def test_report_suite_without_a_lease_keeps_the_pre_lease_json_shape(self) -> None:
        """No lease, no interrupted run: the suite JSON has exactly the keys it
        had before the control lease existed (no `interrupted` count, no
        `status` on a failed entry)."""
        cli = self.load_cli()
        code, out, err = self.call(cli, "flow", "run", str(self.flow))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "passed")
        missing = self.root / ".autonom" / "missing.yaml"
        missing.write_text(
            _HEAD.replace("name: t", "name: missing")
            + "- tapOn:\n    selector:\n      text: Nowhere\n    timeoutMs: 0\n",
            encoding="utf-8")
        code, out, err = self.call(cli, "flow", "run", str(missing))
        self.assertEqual(code, 1, err)
        self.assertEqual(json.loads(out)["status"], "failed")

        code, out, err = self.call(cli, "report", "suite", "--out", str(self.root / "suite"))
        self.assertEqual(code, 1, "a failed run fails the suite, as before")
        payload = json.loads(out)
        self.assertEqual(set(payload), {
            "ok", "flows", "passed", "failed", "broken", "replayed", "junit_counts",
            "html", "junit", "sensitive", "failures"})
        self.assertNotIn("interrupted", payload)
        self.assertEqual((payload["flows"], payload["passed"], payload["failed"]), (2, 1, 1))
        self.assertEqual(len(payload["failures"]), 1, payload)
        self.assertEqual(set(payload["failures"][0]), {"flow", "flow_id", "error_code"})
        self.assertEqual(payload["failures"][0]["flow"], "missing")

    def test_flow_run_exits_1_with_status_interrupted(self) -> None:
        cli = self.load_cli()
        taps = [0]
        real_tap = cli.ui_mod.tap

        def tap(*args, **kwargs):
            taps[0] += 1
            answer = real_tap(*args, **kwargs)
            if taps[0] == 1:
                take_human_lease()
            return answer

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("autonom_lib.ui.tap", side_effect=tap), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--adb", str(FAKE_ADB), "--serial", SERIAL,
                             "flow", "run", str(self.flow)])
        self.assertEqual(code, 1, err.getvalue())
        summary = json.loads(out.getvalue())
        self.assertEqual(summary["status"], "interrupted")
        self.assertEqual(summary["failure"]["error_code"], errors.CONTROL_HELD)
        self.assertEqual(summary["failure"]["failure_class"], "control_lost")
        self.assertEqual(taps[0], 1)
        self.assertNotIn("repair", summary)
        taps_sent = [line for line in self.log.read_text(encoding="utf-8").splitlines()
                     if '"tap"' in line]
        self.assertEqual(len(taps_sent), 1, taps_sent)


if __name__ == "__main__":
    unittest.main()
