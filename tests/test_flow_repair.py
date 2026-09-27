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
        timeline = [{"index": 1, "status": "passed"}, {"index": 2, "status": "passed"},
                    {"index": 3, "status": "failed"}]
        brief = repair.repair_brief("flows/login.yaml", _failure(index=3), timeline)
        self.assertEqual(brief["commands"][0], "autonom flow run flows/login.yaml --until-step 2")
        self.assertEqual(brief["until_step"], 2)
        self.assertIn("autonom ui tree", brief["commands"])
        self.assertEqual(brief["commands"][-1], "autonom flow run flows/login.yaml")
        self.assertIn("autonom flow check flows/login.yaml", brief["commands"])

    def test_first_step_has_no_prefix_to_replay(self) -> None:
        brief = repair.repair_brief("f.yaml", _failure(index=1),
                                    [{"index": 1, "status": "failed"}])
        self.assertFalse(any("--until-step" in c for c in brief["commands"]))
        self.assertIsNone(brief["until_step"])
        self.assertIn("first step", brief["until_step_reason"])

    def test_without_a_timeline_the_prefix_is_not_guessed(self) -> None:
        # index - 1 is only right for a flat flow; without the timeline the
        # brief says why it cannot name the prefix instead of guessing
        brief = repair.repair_brief("f.yaml", _failure(index=3))
        self.assertFalse(any("--until-step" in c for c in brief["commands"]))
        self.assertIn("timeline", brief["until_step_reason"])

    def test_first_child_of_a_runflow_replays_to_the_last_completed_step(self) -> None:
        # completion order: the runFlow block (3) finishes AFTER its child (4)
        timeline = [{"index": 1, "status": "passed", "command": "launchApp"},
                    {"index": 2, "status": "passed", "command": "assertVisible"},
                    {"index": 4, "status": "failed", "command": "assertVisible",
                     "parent_index": 3},
                    {"index": 3, "status": "failed", "command": "runFlow"}]
        brief = repair.repair_brief("parent.yaml", _failure(index=4), timeline)
        self.assertEqual(brief["until_step"], 2)
        self.assertIn("autonom flow run parent.yaml --until-step 2", brief["commands"])

    def test_a_recovered_attempt_is_not_the_replay_boundary(self) -> None:
        timeline = [{"index": 2, "status": "failed", "retry_attempt": 1},
                    {"index": 3, "status": "passed", "retry_attempt": 2},
                    {"index": 4, "status": "failed"}]
        self.assertEqual(repair.replay_prefix(timeline, 4), (3, None))
        timeline = [{"index": 2, "status": "failed", "retry_attempt": 1},
                    {"index": 4, "status": "failed"}]
        until, reason = repair.replay_prefix(timeline, 4)
        self.assertIsNone(until)
        self.assertTrue(reason)

    def test_the_brief_names_the_child_file_and_keeps_the_root_for_replay(self) -> None:
        failure = _failure(index=4, flow="flows/child.yaml")
        timeline = [{"index": 1, "status": "passed"},
                    {"index": 4, "status": "failed"}]
        brief = repair.repair_brief("flows/parent.yaml", failure, timeline)
        self.assertEqual(brief["flow"], "flows/child.yaml")
        self.assertEqual(brief["root_flow"], "flows/parent.yaml")
        self.assertIn("autonom flow run flows/parent.yaml --until-step 1", brief["commands"])
        self.assertIn("autonom flow check flows/parent.yaml", brief["commands"])

    def test_visible_text_is_queried_as_text_and_as_description(self) -> None:
        sets = repair.selector_flag_sets({"visibleText": "Nope", "match": "exact"})
        self.assertEqual(sets, [["--text", "Nope", "--mode", "contains", "--all"],
                                ["--desc", "Nope", "--mode", "contains", "--all"]])
        steps = [{"index": 3, "selector": {"visibleText": "Nope"}}]
        brief = repair.repair_brief("f.yaml", _failure(index=3), steps)
        self.assertIn("autonom ui find --text Nope --mode contains --all", brief["commands"])
        self.assertIn("autonom ui find --desc Nope --mode contains --all", brief["commands"])

    def test_regex_selectors_keep_regex_mode(self) -> None:
        flags = repair.selector_flags({"text": "Sett.*", "match": "regex"})
        self.assertEqual(flags, ["--text", "'Sett.*'", "--mode", "regex",
                                 "--case-sensitive", "--all"])

    def test_declared_secrets_and_env_become_placeholders(self) -> None:
        timeline = [{"index": 1, "status": "passed"}, {"index": 2, "status": "failed"}]
        brief = repair.repair_brief("f.yaml", _failure(index=2), timeline,
                                    secret_names=["PASSWORD"], env_names=["USER"])
        self.assertIn("autonom flow run f.yaml --until-step 1 --secret PASSWORD "
                      "--env USER=<value>", brief["commands"])
        self.assertEqual(brief["commands"][-1],
                         "autonom flow run f.yaml --secret PASSWORD --env USER=<value>")

    def test_placeholders_come_from_the_run_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "manifest.json").write_text(json.dumps({
                "secret_names": ["TOKEN"], "env_override_names": ["QUERY"],
                "env": {"QUERY": "bluetooth"}}), encoding="utf-8")
            brief = repair.repair_brief(
                "f.yaml", _failure(index=1), [{"index": 1, "status": "failed"}],
                events_path=str(run_dir / "events.ndjson"))
        self.assertEqual(brief["commands"][-1],
                         "autonom flow run f.yaml --secret TOKEN --env QUERY=<value>")
        self.assertNotIn("bluetooth", json.dumps(brief))

    def test_selector_becomes_a_widened_ui_find(self) -> None:
        steps = [{"index": 3, "selector": {"description": "Log In", "match": "exact"}}]
        brief = repair.repair_brief("f.yaml", _failure(index=3), steps)
        self.assertIn("autonom ui find --desc 'Log In' --mode contains --all", brief["commands"])
        self.assertEqual(brief["selector"], {"description": "Log In", "match": "exact"})

    def test_every_flow_field_maps_to_its_cli_flag(self) -> None:
        flags = repair.selector_flags({"id": "login_btn", "text": "Go", "role": "button"})
        self.assertEqual(flags, ["--resource-id", "login_btn", "--text", "Go",
                                 "--role", "button", "--mode", "contains", "--all"])
        flags = repair.selector_flags({"description": "Save", "enabled": True})
        self.assertEqual(flags, ["--desc", "Save", "--enabled", "true",
                                 "--mode", "contains", "--all"])
        # engine names are not Flow fields: a brief never sees them
        self.assertEqual(repair.selector_flags({"resource_id": "x", "desc": "y"}), [])
        self.assertEqual(repair.selector_flags({"match": "exact"}), [])
        self.assertEqual(repair.selector_flags(None), [])

    def test_paths_with_spaces_are_quoted(self) -> None:
        brief = repair.repair_brief("my flows/a.yaml", _failure(index=1))
        self.assertIn("autonom flow run 'my flows/a.yaml'", brief["commands"])

    def test_advice_matches_what_flows_actually_produce(self) -> None:
        # a missing element surfaces as a polling timeout on a selector
        steps = [{"index": 3, "selector": {"text": "Sign In"}}]
        miss = repair.repair_brief("f.yaml", _failure(code=errors.FLOW_ASSERTION_TIMEOUT),
                                   steps)
        self.assertIn("not on screen", miss["advice"])
        self.assertIn("candidates", miss["advice"])
        focus = repair.repair_brief("f.yaml", _failure(code=errors.FLOW_NO_FOCUSED_FIELD))
        self.assertIn("requireFocus", focus["advice"])
        self.assertIn("tapOn", focus["advice"])

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


class RepairCandidateTests(unittest.TestCase):
    """FLOW-002: what was on screen, ranked — suggestions, never applied."""

    NODES = [
        {"ref": "n1", "role": "button", "text": "Sign in", "bounds": [0, 0, 10, 10]},
        {"ref": "n2", "role": "text", "text": "Signing you in", "bounds": [0, 20, 10, 30]},
        {"ref": "n3", "role": "button", "desc": "Settings", "bounds": [0, 40, 10, 50]},
        {"ref": "n4", "role": "image", "resource_id": "com.x:id/logo"},
        {"ref": "n5", "role": "text", "text": "Completely unrelated words"},
    ]

    def _brief(self, selector, nodes=None, code=errors.FLOW_ASSERTION_TIMEOUT):
        steps = [{"index": 3, "selector": selector, "status": "failed"}]
        return repair.repair_brief("f.yaml", _failure(index=3, code=code), steps,
                                   hierarchy=self.NODES if nodes is None else nodes)

    def test_the_closest_label_ranks_first_with_a_runnable_query(self) -> None:
        brief = self._brief({"text": "Sign In", "match": "exact"})
        first = brief["candidates"][0]
        self.assertEqual(first["selector"]["text"], "Sign in")
        self.assertEqual(first["command"],
                         "autonom ui find --text 'Sign in' --mode exact --case-sensitive")
        self.assertGreaterEqual(first["score"], 0.5)
        texts = [c["selector"].get("text") for c in brief["candidates"]]
        self.assertNotIn("Completely unrelated words", texts)
        # the flow itself is untouched: the brief still reports the old selector
        self.assertEqual(brief["selector"], {"text": "Sign In", "match": "exact"})

    def test_a_label_in_another_field_becomes_that_field(self) -> None:
        brief = self._brief({"text": "Setting"})
        first = brief["candidates"][0]
        self.assertEqual(first["selector"], {"description": "Settings"})
        self.assertIn("--desc Settings", first["command"])

    def test_role_agreement_breaks_ties(self) -> None:
        nodes = [{"ref": "a", "role": "text", "text": "Save"},
                 {"ref": "b", "role": "button", "text": "Save"}]
        brief = self._brief({"text": "Save it", "role": "button"}, nodes)
        first = brief["candidates"][0]
        self.assertEqual(first["selector"], {"text": "Save", "role": "button"})
        self.assertGreater(first["score"], brief["candidates"][1]["score"])

    def test_at_most_five_candidates(self) -> None:
        nodes = [{"ref": f"n{i}", "text": f"Item {i}"} for i in range(12)]
        brief = self._brief({"text": "Item"}, nodes)
        self.assertEqual(len(brief["candidates"]), 5)

    def test_duplicate_labels_get_the_index_that_picks_the_node(self) -> None:
        nodes = [{"ref": "a", "text": "OK"}, {"ref": "b", "text": "OK"}]
        brief = self._brief({"text": "Ok!"}, nodes)
        selectors = [c["selector"] for c in brief["candidates"]]
        self.assertEqual(selectors, [{"text": "OK", "index": 0}, {"text": "OK", "index": 1}])
        self.assertIn("--index 1", brief["candidates"][1]["command"])

    def test_ambiguous_matches_are_listed_with_distinguishing_fields(self) -> None:
        nodes = [{"ref": "a", "role": "button", "text": "Delete", "resource_id": "row1"},
                 {"ref": "b", "role": "button", "text": "Delete", "resource_id": "row2"},
                 {"ref": "c", "role": "button", "text": "Delete"}]
        brief = self._brief({"text": "Delete", "match": "exact"}, nodes,
                            code=errors.AMBIGUOUS_SELECTOR)
        candidates = brief["candidates"]
        self.assertEqual(len(candidates), 3)
        self.assertEqual(candidates[0]["distinguishing"], {"resource_id": "row1"})
        self.assertEqual(candidates[0]["selector"],
                         {"text": "Delete", "match": "exact", "id": "row1"})
        self.assertEqual(candidates[2]["selector"],
                         {"text": "Delete", "match": "exact", "index": 2})

    def test_no_hierarchy_means_no_candidates_key(self) -> None:
        steps = [{"index": 3, "selector": {"text": "x"}, "status": "failed"}]
        brief = repair.repair_brief("f.yaml", _failure(index=3), steps)
        self.assertNotIn("candidates", brief)

    def test_hierarchy_is_read_from_the_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "failure-step-3-hierarchy.json").write_text(
                json.dumps({"nodes": self.NODES}), encoding="utf-8")
            steps = [{"index": 3, "selector": {"text": "Sign In"}, "status": "failed"}]
            brief = repair.repair_brief("f.yaml", _failure(index=3), steps,
                                        events_path=str(run_dir / "events.ndjson"))
        self.assertEqual(brief["candidates"][0]["selector"]["text"], "Sign in")


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
        # the failure hierarchy was captured, so candidates are present
        self.assertIn("candidates", brief)
        self.assertEqual(brief["root_flow"], payload["flow"])


if __name__ == "__main__":
    unittest.main()
