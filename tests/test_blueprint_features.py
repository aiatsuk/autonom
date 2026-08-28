"""Contracts introduced by the complete Autonom product blueprint."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import actions, campaign, contracts, errors, gates, journal  # noqa: E402
from autonom_lib import report_bundle, report_export, report_model, teach  # noqa: E402
from autonom_lib.flow import canonical, parser, schema  # noqa: E402
from autonom_lib.flow.events import EventWriter  # noqa: E402


def sample_manifest(root: Path, run_id: str = "fr_example",
                    status: str = "passed") -> dict:
    (root / "login.yaml").write_text(
        "schema: autonom.dev/flow/v1\nappId: com.example\nname: Login\nid: login-v1\n---\n- launchApp\n",
        encoding="utf-8")
    run_dir = root / "flows" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events.ndjson").write_text("{}\n", encoding="utf-8")
    (run_dir / "step-1-logs.txt").write_text("ready\n", encoding="utf-8")
    failure = None if status == "passed" else {
        "step_index": 1, "failure_class": "test_failure",
        "error_code": "flow_assertion_timeout", "error": "not visible",
    }
    return {
        "schema_version": 3, "session_id": "s_example", "run_id": run_id,
        "attempt_id": f"attempt_{run_id}", "flow_id": "login-v1",
        "flow_name": "Login", "flow_path": str(root / "login.yaml"),
        "app_id": "com.example", "platform": "android",
        "target_id": "emulator-5554", "status": status,
        "execution_status": status, "proof_verdict": "pass" if status == "passed" else "fail",
        "primary_error": failure, "started_at_ms": 1000, "finished_at_ms": 1100,
        "properties": {"tenant": "demo", "password": "must-not-be-history"},
        "history_parameters": ["tenant"], "tags": ["smoke"],
        "steps": [{
            "index": 1, "step_id": "src_step1:1", "source_id": "src_step1",
            "command": "assertVisible", "status": status,
            "started_at_ms": 1000, "finished_at_ms": 1100, "duration_ms": 100,
            **({"failure_class": "test_failure", "error_code": "flow_assertion_timeout",
                "error": "not visible"} if failure else {}),
        }],
        "artifacts": [f"flows/{run_id}/events.ndjson",
                      f"flows/{run_id}/step-1-logs.txt"],
        "artifact_steps": [{"path": f"flows/{run_id}/step-1-logs.txt",
                            "kind": "logs-after", "step_index": 1,
                            "step_id": "src_step1:1"}],
        "environment": {"platform": "android"},
        "setup": {"available": ["profile"], "selected": [{"kind": "profile"}]},
        "reproduction": f"autonom flow run {root / 'login.yaml'}",
    }


class ReportModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_model_has_two_status_axes_stable_identity_and_complete_delta(self) -> None:
        model = report_model.compile_manifest(sample_manifest(self.root))
        self.assertEqual(model["schema"], contracts.REPORT_SCHEMA)
        self.assertEqual(model["attempt"]["status"], "passed")
        self.assertEqual(model["attempt"]["proof_verdict"], "pass")
        self.assertTrue(model["test_case"]["case_id"].startswith("case_"))
        self.assertTrue(model["test_case"]["history_id"].startswith("hist_"))
        self.assertEqual(model["test_case"]["parameters"], {"tenant": "demo"})
        self.assertEqual(len(model["action_attempts"]), 1)
        self.assertEqual(model["action_attempts"][0]["step_id"],
                         model["steps"][0]["step_id"])
        delta = model["steps"][0]["delta"]
        self.assertTrue(delta["complete"])
        self.assertEqual(delta["logs"]["count"], 1)
        self.assertEqual(delta["ui"]["availability"], "unavailable")
        self.assertIsNone(delta["ui"]["changed"])
        self.assertEqual(delta["requests"]["availability"], "unavailable")

    def test_finalized_bundle_exporters_integrity_and_idempotence(self) -> None:
        manifest = sample_manifest(self.root)
        bundle = self.root / "bundle"
        first = report_bundle.build(manifest, artifacts_root=self.root, out=bundle)
        self.assertFalse(first["idempotent"])
        second = report_bundle.build(manifest, artifacts_root=self.root, out=bundle)
        self.assertTrue(second["idempotent"])
        self.assertTrue(report_bundle.verify(bundle)["ok"])
        self.assertTrue((bundle / "flow/login.yaml").is_file())
        portable_run = json.loads((bundle / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(portable_run["flow_path"], "flow/login.yaml")
        model = report_model.load(bundle / "model/report.json")
        for name, output in (
            ("agent", self.root / "agent.json"),
            ("csv", self.root / "steps.csv"),
            ("metrics", self.root / "metrics.json"),
            ("allure", self.root / "allure-results"),
        ):
            report_export.export(model, name, output, bundle_root=bundle)
        result_file = next((self.root / "allure-results").glob("*-result.json"))
        allure = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(allure["historyId"], model["test_case"]["history_id"])
        finalized = json.loads((bundle / "finalized.json").read_text(encoding="utf-8"))
        finalized["model_sha256"] = "0" * 64
        (bundle / "finalized.json").write_text(json.dumps(finalized), encoding="utf-8")
        with self.assertRaises(errors.AutonomError):
            report_bundle.verify(bundle)

    def test_campaign_merge_is_idempotent_and_missing_shards_are_explicit(self) -> None:
        manifest = sample_manifest(self.root)
        bundle = self.root / "bundle"
        report_bundle.build(manifest, artifacts_root=self.root, out=bundle)
        packed = self.root / "shard.zip"
        campaign.pack(bundle, packed, shard_id="a")
        merged = campaign.merge([packed, packed], self.root / "campaign",
                                expected_shards=2)
        self.assertEqual(merged["received_shards"], 1)
        self.assertEqual(merged["missing_shards"], 1)
        final = campaign.finalize(self.root / "campaign")
        self.assertEqual(final["status"], "failed")

    def test_gate_and_retry_history_keep_latest_and_prior_attempts(self) -> None:
        failed = report_model.compile_manifest(
            sample_manifest(self.root, "fr_first", "failed"))
        passed = report_model.compile_manifest(
            sample_manifest(self.root, "fr_second", "passed"))
        self.assertFalse(gates.evaluate(failed)["passed"])
        history = gates.history([failed, passed])
        self.assertEqual(history["cases"][0]["attempts"], 2)
        self.assertTrue(history["cases"][0]["retried"])
        self.assertTrue(history["cases"][0]["flaky"])


class FlowAndEventContractTests(unittest.TestCase):
    def test_setup_side_effects_and_semantic_capabilities_round_trip(self) -> None:
        text = (
            "schema: autonom.dev/flow/v1\nname: setup demo\nid: setup-demo\n"
            "sideEffects:\n  - app-data\n  - network\n"
            "setup:\n  profile: logged-in\n  fixtures:\n    - account\n"
            "  location:\n    latitude: 52.37\n    longitude: 4.89\n"
            "requires:\n  capabilities:\n    - checkpoint.restore\n"
            "---\n- launchApp\n")
        flow = schema.build_flow(parser.parse_document(text, "flow.yaml"))
        emitted = canonical.emit_flow(flow)
        reparsed = schema.build_flow(parser.parse_document(emitted, "flow.yaml"))
        self.assertEqual(flow.setup, reparsed.setup)
        self.assertEqual(flow.side_effects, reparsed.side_effects)
        self.assertEqual(flow.requires_capabilities, ["checkpoint.restore"])

    def test_mutating_command_postcondition_round_trip(self) -> None:
        text = (
            "schema: autonom.dev/flow/v1\nname: postcondition\nid: postcondition\n"
            "---\n- tapOn:\n    selector:\n      text: Continue\n"
            "    postcondition:\n      id: dashboard\n")
        flow = schema.build_flow(parser.parse_document(text, "flow.yaml"))
        self.assertEqual(flow.steps[0].args["postcondition"].source_fields["id"],
                         "dashboard")
        emitted = canonical.emit_flow(flow)
        reparsed = schema.build_flow(parser.parse_document(emitted, "flow.yaml"))
        self.assertEqual(
            reparsed.steps[0].args["postcondition"].source_fields["id"],
            "dashboard")

    def test_event_envelope_has_ordered_monotonic_and_redaction_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = {"session_id": "s1", "artifacts_dir": directory}
            writer = EventWriter(record, "run1", "flow1", "android", "emulator-1")
            first = writer.emit("flow.started", {})
            second = writer.emit("flow.step.finished", {"step_id": "step1"}, sensitive=True)
            self.assertEqual(first["schema"], contracts.EVENT_SCHEMA)
            self.assertEqual((first["sequence"], second["sequence"]), (1, 2))
            self.assertLessEqual(first["monotonic_ns"], second["monotonic_ns"])
            self.assertEqual(second["step_id"], "step1")
            self.assertEqual(second["redaction"]["policy"], "pre-persistence")


class TeachTests(unittest.TestCase):
    def test_marker_range_compiles_and_three_clean_runs_approve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = {"session_id": "s1", "artifacts_dir": str(root),
                      "app_id": "com.example"}
            started = teach.start(record, "login")
            detail = actions.record_detail(record, "tap", {
                "kind": "tap", "coordinate": False,
                "selector": {"desc": "Open settings", "mode": "exact",
                             "case_sensitive": True, "index": None},
                "node": {"desc": "Open settings"}, "nodes": [],
            })
            journal.append(record, {"kind": "action", "verb": "ui tap", "ok": True,
                                    "argv": ["ui", "tap"], "result": {"detail": detail}})
            actions.record_detail(record, "find", {
                "kind": "find", "selector": {"desc": "Open settings", "mode": "exact",
                                               "case_sensitive": True, "index": None},
                "count": 1,
            })
            teach.mark(record, "after-tap")
            teach.stop(record)
            flow_path = root / "login.yaml"
            compiled = teach.compile_recording(record, out=flow_path,
                                                recording_id=started["recording_id"])
            self.assertTrue(compiled["flow_sha256"])
            flow = schema.build_flow(parser.parse_document(
                flow_path.read_text(encoding="utf-8"), str(flow_path)))
            for index in range(3):
                run = root / "flows" / f"run-{index}"
                run.mkdir(parents=True)
                (run / "manifest.json").write_text(json.dumps({
                    "run_id": f"run-{index}", "flow_id": flow.flow_id,
                    "status": "passed"}), encoding="utf-8")
            receipt = teach.approve(record, flow_path)
            self.assertEqual(len(receipt["clean_replays"]), 3)


class BlueprintCliEndToEndTests(unittest.TestCase):
    def test_bundle_agent_replay_and_supervised_ci(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            state.write_text(json.dumps({
                "ui_dump": str(ROOT / "tests/fixtures/ui_dump.xml")}),
                encoding="utf-8")
            environment = dict(os.environ)
            environment.update({
                "AUTONOM_HOME": str(root / "home"),
                "AUTONOM_FAKE_STATE": str(state),
                "AUTONOM_FAKE_LOG": str(root / "adb.jsonl"),
            })
            cli = ROOT / "scripts/autonom.py"
            prefix = [sys.executable, str(cli), "--platform", "android",
                      "--serial", "emulator-5554", "--adb",
                      str(ROOT / "tests/fakes/fake_adb.py")]

            def run(*arguments: str, expected: int = 0) -> dict:
                completed = subprocess.run(
                    [*prefix, *arguments], cwd=ROOT, env=environment,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=60, check=False)
                self.assertEqual(completed.returncode, expected, completed.stderr)
                return json.loads(completed.stdout)

            run("session", "start", "--app-id", "com.example")
            flow = root / "flow.yaml"
            flow.write_text(
                "schema: autonom.dev/flow/v1\nappId: com.example\n"
                "name: Blueprint smoke\nid: blueprint-smoke\n"
                "sideEffects:\n  - app-data\n---\n- launchApp:\n"
                "    postcondition:\n"
                "      description: Flutter Save Button\n",
                encoding="utf-8")
            executed = run("flow", "run", str(flow))
            built = run("report", "build", "--run", executed["run_id"])
            bundle = Path(built["bundle"])
            self.assertTrue((bundle / "finalized.json").is_file())
            agent_path = root / "agent.json"
            run("agent", "export", "--bundle", str(bundle), "--out", str(agent_path))
            inspected = run("agent", "inspect", "--bundle", str(bundle), "--step", "1")
            self.assertEqual(inspected["step"]["command"], "launchApp")
            replayed = run("replay", "--bundle", str(bundle), "--to-step", "1")
            self.assertEqual(replayed["status"], "replayed")
            ci = run("ci", "run", str(flow), "--out", str(root / "ci"))
            self.assertEqual(ci["status"], "passed")


if __name__ == "__main__":
    unittest.main()
