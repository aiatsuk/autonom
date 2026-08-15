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
from autonom_lib.flow import maestro, parser, schema  # noqa: E402

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
        self.assertEqual(tap.fields["text"], "Username")
        wait = flow.steps[4].args["visible"]  # Welcome.* — a real pattern
        self.assertEqual(wait.match, "regex")
        self.assertEqual(wait.fields["text"], "^(?:Welcome.*)$",
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
