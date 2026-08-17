"""Maestro Core Profile import/export (§15): faithful or refused, never fuzzy."""
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
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import errors  # noqa: E402
from autonom_lib.flow import maestro, parser, schema, validator  # noqa: E402

MAESTRO_FIXTURE = ROOT / "tests/fixtures/maestro/login.yaml"

_HEAD = "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: t\n---\n"


class ImportTests(unittest.TestCase):
    def _import(self, text: str) -> schema.Flow:
        canonical = maestro.import_flow(text, "maestro.yaml")
        return schema.build_flow(parser.parse_document(canonical, "imported.yaml"))

    def test_core_profile_fixture_imports_and_validates(self) -> None:
        flow = self._import(MAESTRO_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(flow.app_id, "com.example.app")
        self.assertEqual(flow.tags, ["smoke"])
        self.assertEqual(flow.env, {"USERNAME": "user@example.com"})
        commands = [step.command for step in flow.steps]
        self.assertEqual(commands, ["launchApp", "tapOn", "inputText", "tapOn",
                                    "waitUntil", "assertVisible", "swipe",
                                    "takeScreenshot", "runFlow"])
        self.assertTrue(flow.steps[0].args["clearState"])

    def test_plain_text_becomes_exact_and_regex_is_anchored(self) -> None:
        flow = self._import(MAESTRO_FIXTURE.read_text(encoding="utf-8"))
        tap = flow.steps[1].selector      # tapOn: Username — no metacharacters
        self.assertEqual(tap.match, "exact")
        self.assertEqual(tap.fields["visible_text"], "Username",
                         "Maestro text is the label union, not the strict text attr")
        wait = flow.steps[4].args["visible"]  # Welcome.* — a real pattern
        self.assertEqual(wait.match, "regex")
        self.assertEqual(wait.fields["visible_text"], "^(?:Welcome.*)$",
                         "Maestro full-match must anchor for our search regex")

    def test_unsupported_commands_refuse_with_position(self) -> None:
        text = "appId: a.b\n---\n- runScript: hack.js\n"
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.import_flow(text, "m.yaml")
        exc = caught.exception
        self.assertEqual(exc.code, errors.UNSUPPORTED_FLOW_COMMAND)
        self.assertEqual(exc.extra["line"], 3)
        self.assertIn("subflow", exc.hint)

    def test_js_interpolation_refuses(self) -> None:
        text = "appId: a.b\n---\n- inputText: ${output.user.name}\n"
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.import_flow(text, "m.yaml")
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_FLOW_COMMAND)

    def test_point_swipes_refuse(self) -> None:
        text = "appId: a.b\n---\n- swipe:\n    start: 10,10\n    end: 10,400\n"
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.import_flow(text, "m.yaml")
        self.assertIn("points do not import", caught.exception.hint)


class ImportV2Tests(unittest.TestCase):
    """0.28.0 import courtesy: flow mappings, aliases, extras, hooks."""

    def _import(self, text: str) -> schema.Flow:
        canonical = maestro.import_flow(text, "maestro.yaml")
        return schema.build_flow(parser.parse_document(canonical, "imported.yaml"))

    def test_flow_mapping_selector_imports(self) -> None:
        flow = self._import("appId: a.b\n---\n- tapOn: {text: OK, index: 1}\n")
        step = flow.steps[0]
        self.assertEqual(step.command, "tapOn")
        self.assertEqual(step.selector.fields["visible_text"], "OK")
        self.assertEqual(step.selector.match, "exact")
        self.assertEqual(step.selector.index, 1)

    def test_label_and_optional_move_to_the_command(self) -> None:
        flow = self._import(
            "appId: a.b\n---\n"
            "- tapOn: {text: Later, label: Dismiss, optional: true}\n")
        step = flow.steps[0]
        self.assertEqual(step.args["label"], "Dismiss")
        self.assertTrue(step.args["optional"])
        self.assertEqual(step.args["reason"], "optional in the Maestro source")

    def test_optional_assertion_refuses(self) -> None:
        text = "appId: a.b\n---\n- assertVisible: {text: X, optional: true}\n"
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.import_flow(text, "m.yaml")
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_FLOW_COMMAND)
        self.assertIn("optional assertion", caught.exception.hint)

    def test_header_hooks_and_properties_import(self) -> None:
        flow = self._import(
            "appId: a.b\n"
            "properties:\n  owner: qa\n"
            "onFlowStart:\n  - launchApp\n"
            "onFlowComplete:\n  - back\n"
            "---\n- back\n")
        self.assertEqual(flow.properties, {"owner": "qa"})
        self.assertEqual([s.command for s in flow.on_flow_start], ["launchApp"])
        self.assertEqual([s.command for s in flow.on_flow_complete], ["back"])

    def test_url_header_refuses(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.import_flow("url: https://x.test\n---\n- back\n", "m.yaml")
        self.assertIn("web", caught.exception.hint)

    def test_input_text_and_erase_text_map_forms(self) -> None:
        flow = self._import(
            "appId: a.b\n---\n"
            "- inputText:\n    text: hello\n    label: Type\n"
            "- eraseText:\n    charactersToErase: 5\n")
        self.assertEqual(flow.steps[0].args,
                         {"value": "hello", "label": "Type"})
        self.assertEqual(flow.steps[1].args, {"chars": 5})

    def test_open_link_map_imports_and_browser_refuses(self) -> None:
        flow = self._import(
            "appId: a.b\n---\n- openLink:\n    link: https://x.test/a\n")
        self.assertEqual(flow.steps[0].args["url"], "https://x.test/a")
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.import_flow(
                "appId: a.b\n---\n"
                "- openLink:\n    link: https://x\n    browser: true\n",
                "m.yaml")
        self.assertIn("browser", caught.exception.message)

    def test_scroll_until_visible_imports(self) -> None:
        flow = self._import(
            "appId: a.b\n---\n"
            "- scrollUntilVisible:\n    element:\n      id: cell_42\n"
            "    direction: DOWN\n")
        step = flow.steps[0]
        self.assertEqual(step.command, "scrollUntilVisible")
        self.assertEqual(step.args["direction"], "down")
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.import_flow(
                "appId: a.b\n---\n"
                "- scrollUntilVisible:\n    element:\n      id: x\n"
                "    speed: 40\n", "m.yaml")
        self.assertIn("maxSwipes", caught.exception.hint)

    def test_retry_imports_with_explicit_mutation_intent(self) -> None:
        flow = self._import(
            "appId: a.b\n---\n"
            "- retry:\n    maxRetries: 1\n    commands:\n"
            "      - tapOn: Refresh\n")
        step = flow.steps[0]
        self.assertEqual(step.command, "retry")
        self.assertEqual(step.args["maxAttempts"], 2)
        self.assertTrue(step.args["allowMutations"],
                        "Maestro retries mutations; the intent must be visible")

    def test_retry_beyond_the_attempt_cap_refuses(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.import_flow(
                "appId: a.b\n---\n"
                "- retry:\n    maxRetries: 3\n    commands:\n      - back\n",
                "m.yaml")
        self.assertIn("3 attempts", caught.exception.hint)

    def test_boolean_spellings_normalize(self) -> None:
        flow = self._import(
            "appId: a.b\n---\n"
            "- tapOn: {text: X, optional: True}\n"
            "- tapOn: {text: Y, enabled: on}\n"
            "- launchApp: {clearState: yes}\n")
        self.assertTrue(flow.steps[0].args["optional"])
        self.assertTrue(flow.steps[1].selector.fields["enabled"])
        self.assertTrue(flow.steps[2].args["clearState"])
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.import_flow(
                "appId: a.b\n---\n- tapOn: {text: X, enabled: nope}\n",
                "m.yaml")
        self.assertIn("boolean", caught.exception.hint)

    def test_malformed_value_shapes_refuse_not_crash(self) -> None:
        cases = [
            "properties: oops\n---\n- back\n",
            "properties:\n  - a\n---\n- back\n",
            "env: oops\n---\n- back\n",
            "tags: oops\n---\n- back\n",
            "---\n- retry:\n    - x\n",
            "---\n- scrollUntilVisible:\n    - x\n",
            "---\n- extendedWaitUntil: x\n",
            "---\n- swipe:\n    - x\n",
            "---\n- launchApp:\n    - x\n",
            "---\n- tapOn:\n    - x\n",
        ]
        for body in cases:
            with self.subTest(body=body.split("\n")[0] or body):
                with self.assertRaises(errors.AutonomError) as caught:
                    maestro.import_flow("appId: a.b\n" + body, "m.yaml")
                self.assertEqual(caught.exception.code,
                                 errors.UNSUPPORTED_FLOW_COMMAND)

    def test_source_side_validation_positions(self) -> None:
        # Each construct converts syntactically but is invalid Flow v1; the
        # refusal must carry positions in the SOURCE file, not the canonical
        # rebuild (which has different coordinates).
        nested = ("appId: a.b\n---\n"
                  "- retry:\n    commands:\n"
                  "      - retry:\n          commands:\n            - back\n")
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.import_flow(nested, "m.yaml")
        self.assertEqual(caught.exception.extra["line"], 5)
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.import_flow(
                "appId: a.b\n---\n"
                "- retry:\n    maxRetries: -1\n    commands:\n      - back\n",
                "m.yaml")
        self.assertIn("negative", caught.exception.hint)
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.import_flow(
                "appId: a.b\nenv:\n  MY-VAR: x\n---\n- back\n", "m.yaml")
        self.assertEqual(caught.exception.extra["line"], 3)
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.import_flow(
                "appId: a.b\n---\n- tapOn: {enabled: true}\n", "m.yaml")
        self.assertIn("text or id", caught.exception.hint)
        self.assertEqual(caught.exception.extra["line"], 3)

    def test_empty_retry_commands_refuse(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.import_flow(
                "appId: a.b\n---\n- retry:\n    commands: []\n", "m.yaml")
        self.assertIn("Inline the retried commands", caught.exception.hint)

    def test_non_integer_refuses_with_position(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.import_flow(
                "appId: a.b\n---\n- swipe:\n    direction: up\n"
                "    duration: fast\n", "m.yaml")
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_FLOW_COMMAND)
        self.assertIn("integer", caught.exception.hint)

    def test_labels_import_on_more_commands(self) -> None:
        flow = self._import(
            "appId: a.b\n---\n"
            "- swipe:\n    direction: up\n    label: Nudge\n"
            "- extendedWaitUntil:\n    visible: Done\n    label: Settle\n"
            "- runFlow:\n    file: sub.yaml\n    label: Auth\n")
        self.assertEqual([s.args["label"] for s in flow.steps],
                         ["Nudge", "Settle", "Auth"])

    def test_apostrophe_in_plain_mapping_value(self) -> None:
        flow = self._import("appId: a.b\n---\n- tapOn: {text: Don't allow}\n")
        self.assertEqual(flow.steps[0].selector.fields["visible_text"], "Don't allow")

    def test_clipboard_trio_imports(self) -> None:
        flow = self._import(
            "appId: a.b\n---\n"
            "- copyTextFrom: Price\n"
            "- setClipboard: hello\n"
            "- pasteText\n"
            "- scroll\n")
        self.assertEqual([s.command for s in flow.steps],
                         ["copyTextFrom", "setClipboard", "pasteText", "scroll"])
        self.assertEqual(flow.steps[0].selector.fields["visible_text"], "Price")
        self.assertEqual(flow.steps[1].args["value"], "hello")

    def test_repeat_imports_bounded(self) -> None:
        flow = self._import(
            "appId: a.b\n---\n"
            "- repeat:\n    times: 4\n"
            "    while:\n      notVisible: Done\n"
            "    commands:\n      - back\n")
        step = flow.steps[0]
        self.assertEqual(step.args["times"], 4)
        self.assertIsNotNone(step.args["while"].not_visible)
        for body, hint_bit in (
                ("- repeat:\n    commands:\n      - back\n", "finite times"),
                ("- repeat:\n    times: 30\n    commands:\n      - back\n",
                 "25"),
                ("- repeat:\n    times: 2\n"
                 "    while:\n      true: ${output.i < 3}\n"
                 "    commands:\n      - back\n", "Autonom equivalent")):
            with self.subTest(hint=hint_bit):
                with self.assertRaises(errors.AutonomError) as caught:
                    maestro.import_flow("appId: a.b\n---\n" + body, "m.yaml")
                self.assertIn(hint_bit,
                              (caught.exception.hint or "")
                              + caught.exception.message)

    def test_inline_runflow_and_swipe_from_import(self) -> None:
        flow = self._import(
            "appId: a.b\n---\n"
            "- runFlow:\n    commands:\n      - back\n"
            "- swipe:\n    direction: UP\n    from: Cart\n")
        self.assertEqual(flow.steps[0].args["commands"][0].command, "back")
        self.assertEqual(flow.steps[1].args["from"].fields["visible_text"], "Cart")

    def test_tap_repeat_and_delay_import(self) -> None:
        flow = self._import(
            "appId: a.b\n---\n"
            "- tapOn: {text: Plus, repeat: 3, delay: 50}\n")
        self.assertEqual(flow.steps[0].args["repeat"], 3)
        self.assertEqual(flow.steps[0].args["delayMs"], 50)


class AutoDetectTests(unittest.TestCase):
    """A file without ``schema:`` is a Maestro document (decision D6)."""

    def test_is_maestro_document(self) -> None:
        self.assertTrue(maestro.is_maestro_document("appId: a.b\n---\n- back\n"))
        self.assertFalse(maestro.is_maestro_document(_HEAD + "- back\n"))
        self.assertFalse(maestro.is_maestro_document("no separator at all\n"))

    def test_load_flow_converts_and_marks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.yaml"
            path.write_text("appId: a.b\n---\n- tapOn: {text: OK}\n",
                            encoding="utf-8")
            flow = validator.load_flow(path)
            self.assertEqual(flow.converted_from, "maestro")
            self.assertEqual(flow.steps[0].command, "tapOn")

    def test_native_flow_is_not_marked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.yaml"
            path.write_text(_HEAD + "- back\n", encoding="utf-8")
            self.assertIsNone(validator.load_flow(path).converted_from)

    def test_maestro_tree_with_maestro_subflow_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "sub.yaml"
            sub.write_text("appId: a.b\n---\n- back\n", encoding="utf-8")
            root = Path(tmp) / "root.yaml"
            root.write_text("appId: a.b\n---\n- runFlow: sub.yaml\n",
                            encoding="utf-8")
            flow = validator.validate_tree(root)
            self.assertEqual(flow.converted_from, "maestro")


class ExportTests(unittest.TestCase):
    def _flow(self, body: str, header: str = _HEAD) -> schema.Flow:
        return schema.build_flow(parser.parse_document(header + body, "t.yaml"))

    def test_exact_text_is_regex_escaped(self) -> None:
        flow = self._flow("- tapOn:\n    selector:\n      text: Save (draft)\n")
        out = maestro.export_flow(flow, "t.yaml")
        self.assertIn(r"Save\ \(draft\)", out.replace("\\ ", r"\ "))
        self.assertIn("- tapOn:", out)

    def test_wait_until_becomes_extended_wait(self) -> None:
        flow = self._flow("- waitUntil:\n    visible:\n      id: done\n"
                          "    timeoutMs: 9000\n")
        out = maestro.export_flow(flow, "t.yaml")
        self.assertIn("- extendedWaitUntil:", out)
        self.assertIn("timeout: 9000", out)

    def test_checkpoints_become_comments(self) -> None:
        flow = self._flow("- checkpoint:\n    name: logged-in\n")
        out = maestro.export_flow(flow, "t.yaml")
        self.assertIn("# autonom checkpoint: logged-in", out)

    def test_autonom_only_commands_refuse(self) -> None:
        flow = self._flow("- setOrientation: landscape\n")
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.export_flow(flow, "t.yaml")
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_FLOW_COMMAND)

    def test_new_command_arguments_refuse_instead_of_dropping(self) -> None:
        bodies = [
            "- tapOn:\n    selector:\n      text: X\n    repeat: 3\n"
            "    delayMs: 10\n",
            "- swipe:\n    direction: up\n    from:\n      text: Cart\n",
            "- runFlow:\n    commands:\n      - back\n",
        ]
        for body in bodies:
            with self.subTest(command=body.split(":")[0].strip("- ")):
                flow = self._flow(body)
                with self.assertRaises(errors.AutonomError) as caught:
                    maestro.export_flow(flow, "t.yaml")
                self.assertEqual(caught.exception.code,
                                 errors.UNSUPPORTED_FLOW_COMMAND)

    def test_visible_text_exports_as_maestro_text(self) -> None:
        """Round trip: Maestro `text` is our `visibleText`, both ways."""
        flow = self._flow("- tapOn:\n    selector:\n      visibleText: Profile\n")
        out = maestro.export_flow(flow, "t.yaml")
        self.assertIn("text: Profile", out)
        self.assertNotIn("visibleText", out)

    def test_relational_selectors_refuse(self) -> None:
        flow = self._flow("- tapOn:\n    selector:\n      text: a\n"
                          "      childOf:\n        id: list\n")
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.export_flow(flow, "t.yaml")
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_FLOW_COMMAND)


class CliTests(unittest.TestCase):
    def _run(self, *args: str):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ)
            env["AUTONOM_HOME"] = home
            return subprocess.run(
                [sys.executable, str(CLI), *args],
                cwd=ROOT, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                timeout=120,
            )

    def test_import_writes_a_checkable_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "imported.yaml"
            sub = Path(tmp) / "sub"
            sub.mkdir()
            (sub / "cleanup.yaml").write_text(
                "schema: autonom.dev/flow/v1\nname: c\n---\n- back\n",
                encoding="utf-8")
            source = Path(tmp) / "maestro.yaml"
            source.write_text(MAESTRO_FIXTURE.read_text(encoding="utf-8"),
                              encoding="utf-8")
            result = self._run("flow", "import", str(source), "--out", str(out))
            self.assertEqual(result.returncode, 0, result.stderr)
            check = self._run("flow", "check", str(out))
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_check_and_fmt_accept_a_maestro_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "maestro.yaml"
            path.write_text("appId: a.b\n---\n- tapOn: {text: OK}\n",
                            encoding="utf-8")
            check = self._run("flow", "check", str(path))
            self.assertEqual(check.returncode, 0, check.stderr)
            fmt = self._run("flow", "fmt", str(path))
            self.assertEqual(fmt.returncode, 0, fmt.stderr)
            canonical = json.loads(fmt.stdout)["files"][0]["canonical"]
            self.assertIn("schema: autonom.dev/flow/v1", canonical)
            self.assertIn("match: exact", canonical)

    def test_fmt_write_never_rewrites_a_maestro_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "maestro.yaml"
            original = "appId: a.b\n# keep me\n---\n- tapOn: {text: OK}\n"
            path.write_text(original, encoding="utf-8")
            result = self._run("flow", "fmt", "--write", str(path))
            self.assertEqual(result.returncode, 0, result.stderr)
            entry = json.loads(result.stdout)["files"][0]
            self.assertEqual(entry["converted_from"], "maestro")
            self.assertIn("flow import", entry["write_skipped"])
            self.assertNotIn("written", entry)
            self.assertEqual(path.read_text(encoding="utf-8"), original,
                             "the Maestro source must stay byte-identical")

    def test_export_round_trips_through_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "flow.yaml"
            source.write_text(
                _HEAD.replace("name: t", "name: Round trip")
                + "- launchApp\n- tapOn: Sign in\n"
                  "- inputText:\n    value: ${EMAIL}\n"
                  "- assertVisible:\n    selector:\n      id: home\n",
                encoding="utf-8")
            exported = Path(tmp) / "maestro.yaml"
            result = self._run("flow", "export", str(source), "--out", str(exported))
            self.assertEqual(result.returncode, 0, result.stderr)
            back = self._run("flow", "import", str(exported))
            self.assertEqual(back.returncode, 0, back.stderr)
            canonical = json.loads(back.stdout)["canonical"]
            self.assertIn("tapOn", canonical)
            self.assertIn("match: exact", canonical)


if __name__ == "__main__":
    unittest.main()
