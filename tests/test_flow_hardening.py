"""Flow runtime hardening: the failure that ended a run, broken runs in JUnit,
static checks that used to wait for the device, suite discovery, and --env
values that feed sensitive slots.

Every test is hermetic: the executor runs in-process with ``ui``/screenshot
calls mocked, or the CLI runs against ``tests/fakes/fake_adb.py`` with
``AUTONOM_HOME`` in a temp dir. No real device is contacted.
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
UI_FIXTURE = ROOT / "tests/fixtures/ui_dump.xml"

sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import errors  # noqa: E402
from autonom_lib.flow import executor as flow_executor  # noqa: E402
from autonom_lib.flow import parser as flow_parser  # noqa: E402
from autonom_lib.flow import repair as flow_repair  # noqa: E402
from autonom_lib.flow import report as flow_report  # noqa: E402
from autonom_lib.flow import schema as flow_schema  # noqa: E402
from autonom_lib.flow import validator as flow_validator  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

_HEAD = "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: t\n---\n"
_FOCUSED = {"ref": "n1", "role": "textfield", "text": "", "resource_id": "field",
            "focused": True, "bounds": [0, 0, 100, 40], "class": "android.widget.EditText"}
_SETTINGS = {"ref": "n2", "role": "text", "text": "Settings", "bounds": [0, 50, 100, 90]}


class _InProcess(EnvSandboxMixin, unittest.TestCase):
    """Executor in-process: injectable clock, no device I/O."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.sandbox_home()
        # failure evidence would shell out to (fake) adb; keep it in memory
        no_shot = errors.AutonomError(errors.BACKEND_FAILED, "no screenshot in tests")
        for patcher in (
                mock.patch("autonom_lib.screenshot.capture_evidence", side_effect=no_shot),
                mock.patch("autonom_lib.logs.tail", return_value=([], []))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _executor(self, **config):
        target = Target("android", "emulator-5554", str(FAKE_ADB),
                        {"serial": "emulator-5554"})
        session = {"session_id": "s_test", "artifacts_dir": str(self.root / "art")}
        clock = [0.0]
        return flow_executor.Executor(
            target, session,
            flow_executor.RunConfig(**{"default_timeout_ms": 500,
                                       "interval_ms": 500, **config}),
            clock=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            screen=(1080, 1920),
        )

    def _write(self, name: str, body: str, head: str = _HEAD) -> Path:
        path = self.root / name
        path.write_text(head + body, encoding="utf-8")
        return path

    def _build(self, body: str):
        return flow_schema.build_flow(
            flow_parser.parse_document(_HEAD + body, str(self.root / "t.yaml")))

    @staticmethod
    def _steps(result) -> list[dict]:
        return [{k: v for k, v in vars(step).items() if v is not None}
                for step in result.steps]

    @staticmethod
    def _manifest(result) -> dict:
        return json.loads((Path(result.events_path).parent / "manifest.json")
                          .read_text(encoding="utf-8"))


class TerminalFailureTests(_InProcess):
    """FLOW-001(c): `failure` names the step that ended the run."""

    def test_a_recovered_retry_attempt_is_not_the_failure(self) -> None:
        flow = self._build(
            "- retry:\n"
            "    maxAttempts: 2\n"
            "    commands:\n"
            "      - assertVisible:\n"
            "          selector:\n"
            "            text: Settings\n"
            "- assertVisible:\n"
            "    selector:\n"
            "      text: Nope\n")
        calls = [0]

        def snapshot(_target):
            calls[0] += 1
            return [] if calls[0] <= 2 else [_SETTINGS]  # attempt 1 misses

        with mock.patch("autonom_lib.ui.snapshot", side_effect=snapshot), \
             mock.patch("autonom_lib.ui.tree", return_value=([_SETTINGS], [])):
            result = self._executor().run(flow)
        self.assertEqual(result.status, "failed")
        failed = [s for s in result.steps if s.status == "failed"]
        self.assertEqual(len(failed), 2, "attempt 1 and the final assertion")
        # before the fix: the recovered attempt (the first failed step, line 8)
        self.assertEqual(result.failure["step_index"], failed[-1].index)
        self.assertNotEqual(result.failure["step_index"], failed[0].index)
        self.assertEqual(result.failure["line"], 11)
        brief = flow_repair.repair_brief(flow.path, result.failure, self._steps(result),
                                         events_path=result.events_path)
        self.assertEqual(brief["step_index"], failed[-1].index)
        self.assertEqual(brief["until_step"], 3, "the passing second attempt")

    def test_a_failing_cleanup_hook_never_becomes_the_failure(self) -> None:
        flow = flow_schema.build_flow(flow_parser.parse_document(
            "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: t\n"
            "onFlowComplete:\n  - assertVisible:\n      selector:\n        id: gone\n"
            "---\n- assertVisible:\n    selector:\n      text: Nope\n",
            str(self.root / "t.yaml")))
        with mock.patch("autonom_lib.ui.snapshot", return_value=[]), \
             mock.patch("autonom_lib.ui.tree", return_value=([], [])):
            result = self._executor().run(flow)
        self.assertEqual(result.failure["line"], 9)
        self.assertEqual(result.failure["step_index"], 1)
        self.assertEqual(len(result.hook_failures), 1)


class RunFlowReplayTests(_InProcess):
    """FLOW-001-S01: the brief's --until-step for a first-child failure replays."""

    def test_until_step_from_the_brief_replays(self) -> None:
        self._write("child.yaml",
                    "- assertVisible:\n    selector:\n      visibleText: Nope\n"
                    "    timeoutMs: 200\n",
                    head="schema: autonom.dev/flow/v1\nname: child\n---\n")
        parent = self._write(
            "parent.yaml",
            "- assertVisible:\n    selector:\n      text: Settings\n"
            "- runFlow: child.yaml\n")
        flow = flow_validator.validate_tree(parent)
        with mock.patch("autonom_lib.ui.snapshot", return_value=[_SETTINGS]), \
             mock.patch("autonom_lib.ui.tree", return_value=([_SETTINGS], [])):
            result = self._executor().run(flow)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure["step_index"], 3)
        brief = flow_repair.repair_brief(flow.path, result.failure, self._steps(result),
                                         events_path=result.events_path)
        # the child's line in the child's file; the root is what replays
        self.assertEqual(brief["flow"], str((self.root / "child.yaml").resolve()))
        self.assertEqual(brief["root_flow"], flow.path)
        # before the fix: index - 1 == 2, the runFlow block itself
        self.assertEqual(brief["until_step"], 1)
        self.assertIn("autonom ui find --text Nope --mode contains --all", brief["commands"])
        self.assertIn("autonom ui find --desc Nope --mode contains --all", brief["commands"])
        with mock.patch("autonom_lib.ui.snapshot", return_value=[_SETTINGS]):
            replay = self._executor(stop_after_step=brief["until_step"]).run(flow)
        self.assertEqual(replay.status, "replayed")

        # the index the old brief printed (the runFlow block) is only reached
        # after its children — so the replay runs into the failure instead
        with mock.patch("autonom_lib.ui.snapshot", return_value=[_SETTINGS]), \
             mock.patch("autonom_lib.ui.tree", return_value=([], [])):
            old = self._executor(stop_after_step=2).run(flow)
        self.assertEqual(old.status, "failed")

    def test_the_brief_ranks_the_captured_hierarchy(self) -> None:
        flow = self._build("- assertVisible:\n    selector:\n      text: Sign In\n")
        screen = [{"ref": "n1", "role": "button", "text": "Sign in"}, _SETTINGS]
        with mock.patch("autonom_lib.ui.snapshot", return_value=screen), \
             mock.patch("autonom_lib.ui.tree", return_value=(screen, [])):
            result = self._executor().run(flow)
        brief = flow_repair.repair_brief(flow.path, result.failure, self._steps(result),
                                         events_path=result.events_path)
        self.assertEqual(brief["candidates"][0]["selector"]["text"], "Sign in")


class RequireFocusTests(_InProcess):
    def test_timeout_zero_checks_once(self) -> None:
        flow = self._build("- inputText:\n    value: hi\n    timeoutMs: 0\n")
        with mock.patch("autonom_lib.ui.snapshot", return_value=[]) as snap, \
             mock.patch("autonom_lib.ui.tree", return_value=([], [])), \
             mock.patch("autonom_lib.ui.type_text") as typed:
            result = self._executor(default_timeout_ms=2000).run(flow)
        self.assertEqual(result.failure["error_code"], errors.FLOW_NO_FOCUSED_FIELD)
        # before the fix `0 or default` polled for the default 2000 ms (5 dumps)
        self.assertEqual(snap.call_count, 1)
        typed.assert_not_called()

    def test_an_incomplete_dump_polls_again(self) -> None:
        flow = self._build("- inputText: hi\n")
        dumps = [errors.AutonomError(errors.BACKEND_FAILED,
                                     "UIAutomator returned an incomplete hierarchy"),
                 [_FOCUSED]]

        def snapshot(_target):
            item = dumps.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch("autonom_lib.ui.snapshot", side_effect=snapshot), \
             mock.patch("autonom_lib.ui.type_text") as typed:
            result = self._executor().run(flow)
        self.assertEqual(result.status, "passed")
        typed.assert_called_once()

    def test_a_dump_that_never_completes_is_still_infrastructure(self) -> None:
        flow = self._build("- inputText: hi\n")
        boom = errors.AutonomError(errors.BACKEND_FAILED, "incomplete hierarchy")
        with mock.patch("autonom_lib.ui.snapshot", side_effect=boom), \
             mock.patch("autonom_lib.ui.tree", return_value=([], [])):
            with self.assertRaises(errors.AutonomError) as caught:
                self._executor().run(flow)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)


class SensitiveEnvTests(_InProcess):
    """SEC-004: --env feeding `sensitive: true` is redacted like a secret."""

    def test_env_value_in_a_sensitive_slot_never_persists(self) -> None:
        flow = self._build(
            "- inputText:\n    value: ${PASSWORD}\n    sensitive: true\n"
            "- assertVisible:\n    selector:\n      text: Nope\n")
        with mock.patch("autonom_lib.ui.snapshot", return_value=[_FOCUSED]), \
             mock.patch("autonom_lib.ui.tree", return_value=([_FOCUSED], [])), \
             mock.patch("autonom_lib.ui.type_text") as typed:
            result = self._executor(env={"PASSWORD": "plain-v4lue",
                                         "QUERY": "visible-q"}).run(flow)
        typed.assert_called_once()
        self.assertEqual(typed.call_args.args[1], "plain-v4lue")
        manifest = self._manifest(result)
        manifest_text = json.dumps(manifest)
        events_text = Path(result.events_path).read_text(encoding="utf-8")
        report_text = (flow_report.render_html(manifest, self.root / "art")
                       + flow_report.render_junit(manifest))
        for blob in (manifest_text, events_text, report_text):
            self.assertNotIn("plain-v4lue", blob)
        self.assertEqual(manifest["env"]["PASSWORD"], "<redacted>")
        # reproduced the way it should have been passed; replays require it
        self.assertIn("--secret PASSWORD", manifest["reproduction"])
        self.assertNotIn("--env PASSWORD", manifest["reproduction"])
        self.assertEqual(manifest["secret_names"], ["PASSWORD"])
        self.assertEqual(manifest["env_override_names"], ["QUERY"])
        # an unrelated --env keeps its value (it is not a secret)
        self.assertIn("--env QUERY=visible-q", manifest["reproduction"])
        names = [w.get("name") for w in result.warnings]
        self.assertEqual(names, ["PASSWORD"])
        self.assertIn("--secret PASSWORD", result.warnings[0]["hint"])
        self.assertEqual(manifest["warnings"], result.warnings)
        self.assertTrue(result.sensitive)
        started = json.loads(events_text.splitlines()[0])
        self.assertEqual(started["payload"]["warnings"][0]["name"], "PASSWORD")
        # the brief names the key as a placeholder only
        brief = flow_repair.repair_brief(flow.path, result.failure, self._steps(result),
                                         events_path=result.events_path)
        self.assertIn("--secret PASSWORD --env QUERY=<value>", brief["commands"][-1])
        self.assertNotIn("plain-v4lue", json.dumps(brief))

    def test_forwarding_through_runflow_env_is_followed(self) -> None:
        self._write("login.yaml",
                    "- inputText:\n    value: ${PW}\n    sensitive: true\n",
                    head="schema: autonom.dev/flow/v1\nname: login\n---\n")
        parent = self._write("parent.yaml",
                             "- runFlow:\n    file: login.yaml\n"
                             "    env:\n      PW: ${PASSWORD}\n")
        flow = flow_validator.validate_tree(parent)
        with mock.patch("autonom_lib.ui.snapshot", return_value=[_FOCUSED]), \
             mock.patch("autonom_lib.ui.type_text"):
            result = self._executor(env={"PASSWORD": "fwd-s3cret"}).run(flow)
        self.assertEqual(result.status, "passed")
        manifest_text = json.dumps(self._manifest(result))
        self.assertNotIn("fwd-s3cret", manifest_text)
        self.assertNotIn("fwd-s3cret", Path(result.events_path).read_text(encoding="utf-8"))
        self.assertEqual([w["name"] for w in result.warnings], ["PASSWORD"])

    def test_plain_env_gets_no_warning(self) -> None:
        flow = self._build("- inputText: ${QUERY}\n")
        with mock.patch("autonom_lib.ui.snapshot", return_value=[_FOCUSED]), \
             mock.patch("autonom_lib.ui.type_text"):
            result = self._executor(env={"QUERY": "bluetooth"}).run(flow)
        self.assertEqual(result.warnings, [])
        self.assertIn("--env QUERY=bluetooth", self._manifest(result)["reproduction"])


class JUnitBrokenRunTests(_InProcess):
    """FLOW-003: an aborted run is an error in JUnit, never green."""

    def test_an_unreached_replay_step_exports_an_error(self) -> None:
        flow = self._build("- assertVisible:\n    selector:\n      text: Settings\n")
        runner = self._executor(stop_after_step=999)
        with mock.patch("autonom_lib.ui.snapshot", return_value=[_SETTINGS]):
            with self.assertRaises(errors.AutonomError) as caught:
                runner.run(flow)
        self.assertEqual(caught.exception.code, errors.FLOW_REPLAY_STEP_NOT_REACHED)
        run_dirs = list((self.root / "art" / "flows").iterdir())
        manifest = json.loads((run_dirs[0] / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["execution_status"], "broken")
        xml = flow_report.render_junit(manifest)
        # before the fix: tests="1" failures="0" errors="0" — green in CI
        self.assertIn('errors="1"', xml)
        self.assertIn('tests="2"', xml)
        self.assertIn(f'type="{errors.FLOW_REPLAY_STEP_NOT_REACHED}"', xml)
        self.assertIn("<error ", xml)

    def test_a_run_with_no_steps_is_one_error_case(self) -> None:
        manifest = {"flow_name": "broken", "status": "failed",
                    "execution_status": "broken", "steps": [],
                    "primary_error": {"error_code": "backend_failed",
                                      "failure_class": "infrastructure",
                                      "error": "adb died", "command": "setLocation",
                                      "line": 5}}
        xml = flow_report.render_junit(manifest)
        self.assertIn('tests="1" failures="0" skipped="0" errors="1"', xml)
        self.assertIn('<error message="adb died" type="backend_failed">', xml)
        self.assertIn("setLocation line 5", xml)

    def test_a_definition_error_step_is_an_error_not_a_failure(self) -> None:
        manifest = {"flow_name": "f", "status": "failed", "execution_status": "broken",
                    "steps": [{"index": 1, "command": "runFlow", "status": "failed",
                               "error_code": "flow_var_undefined",
                               "failure_class": "flow_definition", "error": "x"}],
                    "primary_error": {"error_code": "flow_var_undefined",
                                      "failure_class": "flow_definition"}}
        xml = flow_report.render_junit(manifest)
        self.assertIn('tests="1" failures="0" skipped="0" errors="1"', xml)

    def test_suite_rolls_up_errors_so_it_agrees_with_the_suite_json(self) -> None:
        failed = {"flow_name": "a", "status": "failed", "execution_status": "failed",
                  "steps": [{"index": 1, "command": "assertVisible", "status": "failed",
                             "error_code": "flow_assertion_timeout",
                             "failure_class": "test_failure"}],
                  "primary_error": {"failure_class": "test_failure"}}
        broken = {"flow_name": "b", "status": "failed", "execution_status": "broken",
                  "steps": [], "primary_error": {"error_code": "backend_failed",
                                                 "failure_class": "infrastructure"}}
        passed = {"flow_name": "c", "status": "passed", "execution_status": "passed",
                  "steps": [{"index": 1, "command": "note", "status": "passed"}]}
        xml = flow_report.render_suite_junit([failed, broken, passed])
        head = xml.split("<testsuite ", 1)[0]
        self.assertIn('tests="3" failures="1" errors="1"', head)
        failed_flows = sum(1 for m in (failed, broken, passed) if m["status"] == "failed")
        self.assertEqual(failed_flows, 2)  # = failures + errors in the roll-up

    def test_a_replay_is_never_an_error(self) -> None:
        manifest = {"flow_name": "r", "status": "replayed", "execution_status": "passed",
                    "steps": [{"index": 1, "command": "note", "status": "passed"}]}
        self.assertIn('errors="0"', flow_report.render_junit(manifest))


class StaticCheckTests(unittest.TestCase):
    """FLOW-005: errors that used to wait for the device, caught by check."""

    def _error(self, body: str, head: str = _HEAD) -> errors.AutonomError:
        with self.assertRaises(errors.AutonomError) as caught:
            flow_schema.build_flow(flow_parser.parse_document(head + body, "f.yaml"))
        return caught.exception

    def test_negative_timeouts_are_refused_with_a_position(self) -> None:
        exc = self._error("- assertVisible:\n    selector:\n      id: x\n    timeoutMs: -5\n")
        self.assertEqual(exc.code, errors.FLOW_COMMAND_INVALID)
        self.assertEqual((exc.extra["line"], exc.extra["column"]), (8, 16))
        self.assertIn("negative", exc.message)
        for body in ("- tapOn:\n    selector:\n      id: x\n    timeoutMs: -1\n",
                     "- longPressOn:\n    selector:\n      id: x\n    durationMs: -1\n",
                     "- eraseText:\n    chars: -3\n",
                     "- inputText:\n    value: a\n    timeoutMs: -1\n"):
            self.assertEqual(self._error(body).code, errors.FLOW_COMMAND_INVALID, body)

    def test_zero_timeout_is_legal(self) -> None:
        flow = flow_schema.build_flow(flow_parser.parse_document(
            _HEAD + "- inputText:\n    value: a\n    timeoutMs: 0\n", "f.yaml"))
        self.assertEqual(flow.steps[0].args["timeoutMs"], 0)

    def test_coordinates_out_of_range_are_refused(self) -> None:
        exc = self._error("- setLocation:\n    latitude: 200\n    longitude: 0\n")
        self.assertEqual(exc.code, errors.FLOW_COMMAND_INVALID)
        self.assertEqual(exc.extra["line"], 6)
        self.assertIn("-90..90", exc.message)
        exc = self._error("- setLocation:\n    latitude: 10\n    longitude: -180.5\n")
        self.assertIn("-180..180", exc.message)
        ok = flow_schema.build_flow(flow_parser.parse_document(
            _HEAD + "- setLocation:\n    latitude: -90\n    longitude: 180\n", "f.yaml"))
        self.assertEqual(ok.steps[0].args["latitude"], -90.0)

    def test_setup_location_is_range_checked(self) -> None:
        head = ("schema: autonom.dev/flow/v1\nappId: a\nname: t\n"
                "setup:\n  location:\n    latitude: 91\n    longitude: 0\n---\n")
        exc = self._error("- back\n", head=head)
        self.assertEqual(exc.code, errors.FLOW_HEADER_INVALID)
        head = "schema: autonom.dev/flow/v1\nappId: a\nname: t\nsetup:\n  location: here\n---\n"
        self.assertEqual(self._error("- back\n", head=head).code, errors.FLOW_HEADER_INVALID)

    def test_empty_selector_strings_are_refused(self) -> None:
        exc = self._error('- tapOn: ""\n')
        self.assertEqual(exc.code, errors.FLOW_SELECTOR_INVALID)
        exc = self._error('- assertVisible:\n    selector:\n      text: ""\n')
        self.assertEqual(exc.code, errors.FLOW_SELECTOR_INVALID)
        self.assertEqual(exc.extra["line"], 7)

    def test_invalid_regex_is_refused_at_check_time(self) -> None:
        exc = self._error('- tapOn:\n    selector:\n      text: "(unclosed"\n'
                          '      match: regex\n')
        self.assertEqual(exc.code, errors.FLOW_SELECTOR_INVALID)
        self.assertIn("regular expression", exc.message)
        self.assertEqual(exc.extra["line"], 7)
        # an interpolated pattern is only known at run time
        flow_schema.build_flow(flow_parser.parse_document(
            _HEAD + '- tapOn:\n    selector:\n      text: "${Q}(x"\n      match: regex\n',
            "f.yaml"))
        # a literal "(" is fine outside regex mode
        flow_schema.build_flow(flow_parser.parse_document(
            _HEAD + '- tapOn:\n    selector:\n      text: "(unclosed"\n', "f.yaml"))

    def test_did_you_mean_for_commands_arguments_fields_and_modes(self) -> None:
        exc = self._error("- tapOnn:\n    selector:\n      id: x\n")
        self.assertEqual(exc.code, errors.FLOW_UNKNOWN_COMMAND)
        self.assertTrue(exc.hint.startswith("Did you mean 'tapOn'? Commands: "), exc.hint)
        self.assertIn("assertVisible", exc.hint)  # the full list still follows
        exc = self._error("- tapOn:\n    selector:\n      id: x\n    timeout: 5\n")
        self.assertIn("Did you mean 'timeoutMs'?", exc.hint)
        exc = self._error("- tapOn:\n    selector:\n      txet: x\n")
        self.assertIn("Did you mean 'text'?", exc.hint)
        exc = self._error("- tapOn:\n    selector:\n      id: x\n      match: exakt\n")
        self.assertIn("Did you mean 'exact'?", exc.hint)
        exc = self._error("- back\n", head="schema: autonom.dev/flow/v1\nappid: a\nname: t\n---\n")
        self.assertIn("Did you mean 'appId'?", exc.hint)
        exc = self._error("- zzzzqq\n")
        self.assertTrue(exc.hint.startswith("Commands: "), exc.hint)

    def test_optional_without_reason_says_what_to_add(self) -> None:
        exc = self._error("- tapOn:\n    selector:\n      id: x\n    optional: true\n")
        self.assertEqual(exc.code, errors.FLOW_OPTIONAL_ASSERTION_FORBIDDEN)
        self.assertIn("no reason:", exc.message)
        self.assertIn("reason:", exc.hint)

    def test_utf8_bom_is_accepted(self) -> None:
        document = flow_parser.parse_document("﻿" + _HEAD + "- back\n", "f.yaml")
        self.assertEqual(flow_schema.build_flow(document).name, "t")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bom.yaml"
            path.write_bytes(b"\xef\xbb\xbf" + (_HEAD + "- back\n").encode("utf-8"))
            flow = flow_validator.load_flow(path)
        self.assertEqual(flow.name, "t")
        self.assertIsNone(flow.converted_from, "a BOM must not look like Maestro")

    def test_runflow_cycle_names_the_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.yaml").write_text(_HEAD + "- note: a\n- runFlow: b.yaml\n",
                                         encoding="utf-8")
            (root / "b.yaml").write_text(_HEAD + "- runFlow: a.yaml\n", encoding="utf-8")
            (root / "self.yaml").write_text(_HEAD + "- runFlow:\n    file: self.yaml\n",
                                            encoding="utf-8")
            with self.assertRaises(errors.AutonomError) as caught:
                flow_validator.validate_tree(root / "a.yaml")
            exc = caught.exception
            self.assertEqual(exc.code, errors.FLOW_CYCLE_DETECTED)
            self.assertEqual(exc.extra["file"], str((root / "b.yaml").resolve()))
            self.assertEqual(exc.extra["line"], 5)
            self.assertEqual(len(exc.extra["chain"]), 3)
            with self.assertRaises(errors.AutonomError) as caught:
                flow_validator.validate_tree(root / "self.yaml")
            self.assertEqual(caught.exception.extra["line"], 5)
            self.assertIn(":5:", caught.exception.message)


class SuiteDiscoveryTests(EnvSandboxMixin, unittest.TestCase):
    """FLOW-006: app overlay scaffolding is not a suite member."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        workspace = self.root / "w"
        (workspace / "flows").mkdir(parents=True)
        (workspace / "flows" / "real.yaml").write_text(_HEAD + "- back\n",
                                                        encoding="utf-8")
        (workspace / "flows" / "maestro.yaml").write_text(
            "appId: com.example.app\n---\n- back\n", encoding="utf-8")
        app = workspace / ".autonom" / "apps" / "com.example.app"
        (app / "subflows").mkdir(parents=True)
        (app / "selectors.yaml").write_text("schema: autonom.selectors/v1\nselectors: []\n",
                                            encoding="utf-8")
        (app / "fixtures.yaml").write_text("schema: autonom.fixtures/v1\nfixtures: []\n",
                                           encoding="utf-8")
        (app / "compatibility.yaml").write_text(
            "schema: autonom.compatibility/v1\nbuilds: []\n", encoding="utf-8")
        (app / "subflows" / "real.yaml").write_text(_HEAD + "- back\n", encoding="utf-8")
        self.workspace = workspace

    def test_overlay_and_foreign_schema_files_are_skipped(self) -> None:
        (self.workspace / "data.yaml").write_text("schema: autonom.fixtures/v1\nx: []\n",
                                                  encoding="utf-8")
        found = [p.relative_to(self.workspace).as_posix()
                 for p in flow_validator.discover(self.workspace)]
        self.assertEqual(found, ["flows/maestro.yaml", "flows/real.yaml"])

    def test_real_flows_are_never_skipped(self) -> None:
        bad = self.workspace / "flows"
        (bad / "v2.yaml").write_text("schema: autonom.dev/flow/v2\nname: x\n---\n- back\n",
                                     encoding="utf-8")
        (bad / "nosep.yaml").write_text("schema: autonom.dev/flow/v1\nname: x\n",
                                        encoding="utf-8")
        found = {p.name for p in flow_validator.discover(self.workspace)}
        self.assertTrue({"v2.yaml", "nosep.yaml", "real.yaml"} <= found)
        # pointing discovery AT an overlay still finds its flows
        overlay = self.workspace / ".autonom" / "apps" / "com.example.app" / "subflows"
        self.assertEqual([p.name for p in flow_validator.discover(overlay)], ["real.yaml"])

    def test_dry_run_of_a_workspace_with_a_promoted_skill(self) -> None:
        state = self.root / "state.json"
        state.write_text(json.dumps({
            "devices": [["emulator-5554", "device", "product:sdk_gphone64_arm64"]],
            "ui_dump": str(UI_FIXTURE)}), encoding="utf-8")
        self.set_env(AUTONOM_FAKE_STATE=str(state), AUTONOM_HOME=str(self.root / "home"),
                     AUTONOM_FAKE_LOG=None)
        env = dict(os.environ)

        def cli(*argv: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, str(CLI), "--adb", str(FAKE_ADB),
                 "--serial", "emulator-5554", *argv],
                capture_output=True, text=True, env=env, timeout=120,
                cwd=str(self.root))

        started = cli("session", "start", "--app-id", "com.example.app")
        self.assertEqual(started.returncode, 0, started.stderr)
        run = cli("flow", "run", str(self.workspace), "--dry-run")
        self.assertEqual(run.returncode, 0, run.stderr)
        payload = json.loads(run.stdout)
        self.assertNotIn("flow_parse_error", run.stdout + run.stderr)
        self.assertEqual(payload["flows"], 2)


if __name__ == "__main__":
    unittest.main()
