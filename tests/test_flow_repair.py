"""The `repair` block a failed `flow run` carries.

A bare `failure` says what broke; the brief says what to do next — and does
it in the CLI's own vocabulary, so an agent can run the commands verbatim.
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
UI_FIXTURE = ROOT / "tests/fixtures/ui_dump.xml"
FAIL_FLOW = ROOT / "tests/fixtures/flows/contract_fail.yaml"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors  # noqa: E402
from autonom_lib.flow import repair  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


def _failure(index: int = 3, code: str = errors.NO_MATCHING_NODE, **extra) -> dict:
    return {"step_index": index, "command": "tapOn", "line": 12,
            "error_code": code, "failure_class": "test_failure",
            "error": "no node matched", **extra}


class RepairBriefTests(unittest.TestCase):
    def test_nothing_to_repair_without_a_step(self) -> None:
        self.assertIsNone(repair.repair_brief("f.yaml", None))
        self.assertIsNone(repair.repair_brief("f.yaml", {"error_code": "x"}))

    def test_infrastructure_failures_get_no_brief(self) -> None:
        failure = _failure(failure_class="infrastructure", code=errors.BACKEND_FAILED)
        self.assertIsNone(repair.repair_brief("f.yaml", failure))

    def test_state_is_reconstructed_up_to_the_step_before(self) -> None:
        brief = repair.repair_brief("flows/login.yaml", _failure(index=3))
        self.assertEqual(brief["commands"][0], "autonom flow run flows/login.yaml --until-step 2")
        self.assertIn("autonom ui tree", brief["commands"])
        self.assertEqual(brief["commands"][-1], "autonom flow run flows/login.yaml")
        self.assertIn("autonom flow check flows/login.yaml", brief["commands"])

    def test_first_step_has_no_prefix_to_replay(self) -> None:
        brief = repair.repair_brief("f.yaml", _failure(index=1))
        self.assertFalse(any("--until-step" in c for c in brief["commands"]))

    def test_selector_becomes_a_widened_ui_find(self) -> None:
        steps = [{"index": 3, "selector": {"description": "Log In", "match": "exact"}}]
        brief = repair.repair_brief("f.yaml", _failure(index=3), steps)
        self.assertIn("autonom ui find --desc 'Log In' --mode contains --all", brief["commands"])
        self.assertEqual(brief["selector"], {"description": "Log In", "match": "exact"})

    def test_every_flow_field_maps_to_its_cli_flag(self) -> None:
        flags = repair.selector_flags({"id": "login_btn", "text": "Go", "role": "button"})
        self.assertEqual(flags, ["--resource-id", "login_btn", "--text", "Go",
                                 "--role", "button", "--mode", "contains", "--all"])
        self.assertEqual(repair.selector_flags({"match": "exact"}), [])
        self.assertEqual(repair.selector_flags(None), [])

    def test_paths_with_spaces_are_quoted(self) -> None:
        brief = repair.repair_brief("my flows/a.yaml", _failure(index=1))
        self.assertIn("autonom flow run 'my flows/a.yaml'", brief["commands"])

    def test_advice_follows_the_error_code(self) -> None:
        timeout = repair.repair_brief("f.yaml", _failure(code=errors.FLOW_ASSERTION_TIMEOUT))
        ambiguous = repair.repair_brief("f.yaml", _failure(code=errors.AMBIGUOUS_SELECTOR))
        other = repair.repair_brief("f.yaml", _failure(code="something_new"))
        self.assertIn("timeoutMs", timeout["advice"])
        self.assertIn("index", ambiguous["advice"])
        self.assertIn("unchanged flow proves nothing", other["advice"])
        self.assertIn("reviewed edit", other["note"])

    def test_events_path_is_named_as_the_evidence(self) -> None:
        brief = repair.repair_brief("f.yaml", _failure(), events_path="/x/events.ndjson")
        self.assertEqual(brief["evidence"], "/x/events.ndjson")


class RepairInFlowRunTests(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        state = root / "state.json"
        state.write_text(json.dumps({
            "devices": [["emulator-5554", "device", "product:sdk_gphone64_arm64"]],
            "ui_dump": str(UI_FIXTURE),
        }), encoding="utf-8")
        self.set_env(AUTONOM_FAKE_STATE=str(state), AUTONOM_HOME=str(root / "home"),
                     AUTONOM_FAKE_LOG=None)
        self.env = dict(os.environ)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _cli(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), "--adb", str(FAKE_ADB), "--serial", "emulator-5554", *argv],
            capture_output=True, text=True, env=self.env, timeout=120, cwd=self.tmp.name,
        )

    def test_a_test_failure_carries_a_runnable_brief(self) -> None:
        started = self._cli("session", "start", "--app-id", "com.example.app")
        self.assertEqual(started.returncode, 0, started.stderr)
        run = self._cli("flow", "run", str(FAIL_FLOW))
        self.assertEqual(run.returncode, 1, run.stderr)
        payload = json.loads(run.stdout)
        self.assertEqual(payload["failure"]["failure_class"], "test_failure")
        brief = payload["repair"]
        self.assertEqual(brief["step_index"], payload["failure"]["step_index"])
        self.assertEqual(brief["selector"]["id"], "does_not_exist")
        self.assertIn("autonom ui find --resource-id does_not_exist --mode contains --all",
                      brief["commands"])
        self.assertEqual(brief["evidence"], payload["events"])
        self.assertTrue(Path(brief["evidence"]).exists())


if __name__ == "__main__":
    unittest.main()
