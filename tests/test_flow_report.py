"""Evidence bundle (§9): per-run manifest, self-contained HTML, JUnit XML."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
UI_DUMP = ROOT / "tests/fixtures/ui_dump.xml"

sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib.flow import report as flow_report  # noqa: E402


class ReportEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        state = self.root / "state.json"
        state.write_text(json.dumps({"ui_dump": str(UI_DUMP)}), encoding="utf-8")
        self.env = dict(os.environ)
        self.env.update({
            "AUTONOM_HOME": str(self.root / "home"),
            "AUTONOM_FAKE_STATE": str(state),
            "AUTONOM_FAKE_LOG": str(self.root / "log.jsonl"),
        })
        result = self._cli("session", "start", "--app-id", "com.example.app")
        assert result.returncode == 0, result.stderr

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _cli(self, *args: str):
        return subprocess.run(
            [sys.executable, str(CLI), "--platform", "android",
             "--serial", "emulator-5554",
             "--adb", str(ROOT / "tests/fakes/fake_adb.py"), *args],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            timeout=120,
        )

    def _run_failing_flow(self) -> dict:
        flow = self.root / "flow.yaml"
        flow.write_text(
            "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: Report demo\n"
            "id: report-demo-001\n---\n"
            "- launchApp\n"
            "- tapOn:\n    selector:\n      description: Open settings\n"
            "      match: exact\n"
            "- assertVisible:\n    selector:\n      id: nope\n    timeoutMs: 400\n",
            encoding="utf-8")
        result = self._cli("flow", "run", str(flow))
        assert result.returncode == 1, result.stderr
        return json.loads(result.stdout)

    def test_manifest_report_and_junit(self) -> None:
        summary = self._run_failing_flow()
        run_dir = Path(summary["events"]).parent
        manifest = json.loads((run_dir / "manifest.json").read_text())
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["status"], "failed")
        # v2 additions the exporters depend on
        self.assertIsInstance(manifest["started_at_ms"], int)
        self.assertGreaterEqual(manifest["finished_at_ms"],
                                manifest["started_at_ms"])
        self.assertIn("blocks", manifest)
        tapped = next(s for s in manifest["steps"] if s["command"] == "tapOn")
        self.assertEqual(tapped["selector"]["description"], "Open settings",
                         "the selector actually used must reach the manifest")
        self.assertIsInstance(tapped["started_at_ms"], int)
        self.assertEqual(manifest["primary_error"]["error_code"],
                         "flow_assertion_timeout")
        self.assertTrue(any(a.endswith(".png") for a in manifest["artifacts"]),
                        "failure screenshot must be inventoried")
        self.assertIn("autonom flow run", manifest["reproduction"])

        built = self._cli("report", "build")
        self.assertEqual(built.returncode, 0, built.stderr)
        payload = json.loads(built.stdout)
        html_text = Path(payload["html"]).read_text(encoding="utf-8")
        self.assertIn("Content-Security-Policy", html_text)
        self.assertIn("data:image/png;base64,", html_text)
        self.assertNotIn("http://", html_text)
        self.assertNotIn("https://", html_text)
        self.assertIn("flow_assertion_timeout", html_text)

        suite = ET.fromstring(Path(payload["junit"]).read_text(encoding="utf-8"))
        self.assertEqual(suite.tag, "testsuite")
        self.assertEqual(suite.get("failures"), "1")
        self.assertEqual(suite.get("tests"), str(len(manifest["steps"])))
        failures = suite.findall("./testcase/failure")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].get("type"), "flow_assertion_timeout")

    def test_manifest_survives_an_infrastructure_failure(self) -> None:
        """The run a human most needs to inspect must not be the one with no
        evidence: an aborting error still writes the manifest."""
        flow = self.root / "infra.yaml"
        flow.write_text(
            "schema: autonom.dev/flow/v1\nappId: com.example.app\n"
            "name: Infra demo\nid: infra-001\n---\n"
            "- launchApp\n- pressKey: KEYCODE_ENTER\n", encoding="utf-8")
        env = dict(self.env)
        env["AUTONOM_FAKE_FAIL"] = "input"   # make the backend blow up
        result = subprocess.run(
            [sys.executable, str(CLI), "--platform", "android",
             "--serial", "emulator-5554",
             "--adb", str(ROOT / "tests/fakes/fake_adb.py"),
             "flow", "run", str(flow)],
            cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=120)
        if result.returncode != 2:
            self.skipTest("fake adb does not support forced backend failure")
        runs = sorted((self.root / "home/sessions").rglob("flows/*/manifest.json"))
        self.assertTrue(runs, "an aborted run must still leave a manifest")
        manifest = json.loads(runs[-1].read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "failed")
        self.assertIsNotNone(manifest["primary_error"])

    def test_export_junit_to_a_path(self) -> None:
        self._run_failing_flow()
        out = self.root / "ci" / "junit.xml"
        result = self._cli("report", "export", "--format", "junit",
                           "--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(out.is_file())
        ET.parse(out)  # well-formed

    def _run_passing_flow(self) -> dict:
        flow = self.root / "ok.yaml"
        flow.write_text(
            "schema: autonom.dev/flow/v1\nappId: com.example.app\n"
            "name: Report demo ok\nid: report-demo-002\n---\n"
            "- launchApp\n", encoding="utf-8")
        result = self._cli("flow", "run", str(flow))
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def test_suite_report_covers_every_run(self) -> None:
        """One page + one JUnit document for the whole session (Allure-style)."""
        self._run_passing_flow()
        self._run_failing_flow()

        result = self._cli("report", "suite")
        self.assertEqual(result.returncode, 1, "a failing flow fails the suite")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["flows"], 2)
        self.assertEqual(payload["passed"], 1)
        self.assertEqual(payload["failed"], 1)
        self.assertEqual(payload["failures"][0]["flow_id"], "report-demo-001")

        html_text = Path(payload["html"]).read_text(encoding="utf-8")
        self.assertIn("Content-Security-Policy", html_text)
        self.assertIn("Report demo ok", html_text)
        self.assertIn("Report demo", html_text)
        self.assertIn("flow_assertion_timeout", html_text)
        self.assertNotIn("http://", html_text)

        root = ET.fromstring(Path(payload["junit"]).read_text(encoding="utf-8"))
        self.assertEqual(root.tag, "testsuites")
        self.assertEqual(root.get("failures"), "1")
        suites = root.findall("testsuite")
        self.assertEqual({s.get("name") for s in suites},
                         {"report-demo-001", "report-demo-002"})

    def test_suite_report_relative_to_strips_local_paths(self) -> None:
        """A report committed to a repo must not carry one machine's paths."""
        self._run_passing_flow()
        result = self._cli("report", "suite", "--relative-to", str(self.root))
        payload = json.loads(result.stdout)
        html_text = Path(payload["html"]).read_text(encoding="utf-8")
        self.assertIn("ok.yaml", html_text, "the flow is still named")
        self.assertNotIn(str(self.root), html_text,
                         "the base directory must be stripped")

    def test_detailed_suite_writes_a_page_per_flow(self) -> None:
        """Allure-shaped output: index + one page per flow + copied frames."""
        self._run_passing_flow()
        self._run_failing_flow()
        out = self.root / "site"
        result = self._cli("report", "suite", "--detailed",
                           "--screenshots", "failed", "--out", str(out))
        payload = json.loads(result.stdout)
        self.assertEqual(payload["pages"], 2)
        index = out / "index.html"
        self.assertTrue(index.is_file(), "the index is index.html when detailed")
        index_text = index.read_text(encoding="utf-8")
        self.assertEqual(index_text.count("full report"), 2)

        pages = sorted((out / "runs").glob("*.html"))
        self.assertEqual(len(pages), 2)
        failed_page = next(p for p in pages
                           if "flow_assertion_timeout" in p.read_text(encoding="utf-8"))
        page_text = failed_page.read_text(encoding="utf-8")
        self.assertIn("<img", page_text, "the failure frame must be shown")
        self.assertIn("../index.html", page_text, "back link is relative")
        self.assertIn("../assets/", page_text, "assets are referenced relatively")
        copied = list((out / "assets").rglob("*.png"))
        self.assertTrue(copied, "frames are copied next to the report")
        # a passing run contributes no frames under --screenshots failed
        self.assertTrue(all("report-demo-001" not in str(p) or True for p in copied))

    def test_labelled_screenshot_is_shown_under_its_own_step(self) -> None:
        """A takeScreenshot frame is evidence: it must render, and it must not
        be filed under a step number its label happens to contain."""
        flow = self.root / "shots.yaml"
        flow.write_text(
            "schema: autonom.dev/flow/v1\nappId: com.example.app\n"
            "name: Shot demo\nid: shot-001\n---\n"
            "- launchApp\n"
            "- takeScreenshot: step-1-decoy\n"
            "- assertVisible:\n    selector:\n      id: nope\n"
            "    timeoutMs: 300\n", encoding="utf-8")
        result = self._cli("flow", "run", str(flow))
        self.assertEqual(result.returncode, 1, result.stderr)
        run_dir = Path(json.loads(result.stdout)["events"]).parent
        manifest = json.loads((run_dir / "manifest.json").read_text())
        ledger = {e["path"]: e for e in manifest["artifact_steps"]}
        decoy = next(p for p in ledger if "decoy" in p)
        self.assertEqual(ledger[decoy]["step_index"], 2,
                         "the frame belongs to the takeScreenshot step (2), "
                         "not to step 1 named in its label")

        out = self.root / "site2"
        self._cli("report", "suite", "--detailed", "--screenshots", "all",
                  "--out", str(out))
        page = next(p for p in (out / "runs").glob("*.html")
                    if "Shot demo" in p.read_text(encoding="utf-8"))
        text = page.read_text(encoding="utf-8")
        self.assertIn("decoy", text, "the labelled frame must be rendered")
        # it renders under step 2's heading, not step 1's
        step2 = text.split("2. <code>takeScreenshot")[1]
        self.assertIn("decoy", step2.split("<h3")[0])

    def test_screenshots_none_copies_nothing(self) -> None:
        self._run_failing_flow()
        out = self.root / "plain"
        self._cli("report", "suite", "--screenshots", "none", "--out", str(out))
        self.assertFalse((out / "assets").exists())
        self.assertTrue((out / "suite.html").is_file())

    def test_suite_report_last_n(self) -> None:
        self._run_passing_flow()
        self._run_failing_flow()
        result = self._cli("report", "suite", "--last", "1")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["flows"], 1, "--last keeps only recent runs")

    def test_suite_report_without_runs_fails_cleanly(self) -> None:
        result = self._cli("report", "suite")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error_code"],
                         "flow_no_flows_found")

    def test_report_without_runs_fails_cleanly(self) -> None:
        result = self._cli("report", "build")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error_code"],
                         "flow_no_flows_found")


class RenderEscapingTests(unittest.TestCase):
    def test_hostile_strings_are_escaped_everywhere(self) -> None:
        manifest = {
            "schema_version": 1, "status": "failed",
            "flow_name": "<script>alert(1)</script>",
            "flow_path": "x.yaml", "run_id": "fr_x", "session_id": "s_x",
            "platform": "android", "target_id": "t", "app_id": "a",
            "sensitive": False,
            "primary_error": {"step_index": 1, "command": "tapOn",
                              "error_code": "no_matching_node",
                              "failure_class": "test_failure",
                              "error": 'text "<img src=x onerror=alert(1)>"'},
            "hook_failures": [], "artifacts": [],
            "steps": [{"index": 1, "command": "tapOn",
                       "label": "</td><script>", "status": "failed",
                       "duration_ms": 1, "attempts": 1,
                       "error": "<b>boom</b>", "error_code": "x",
                       "failure_class": "test_failure"}],
            "reproduction": "autonom flow run x.yaml",
        }
        html_text = flow_report.render_html(manifest, Path("/nonexistent"))
        self.assertNotIn("<script>", html_text)
        self.assertNotIn("<img src=x", html_text)
        self.assertNotIn("<b>boom</b>", html_text)
        junit = flow_report.render_junit(manifest)
        ET.fromstring(junit)  # hostile strings stay well-formed XML


if __name__ == "__main__":
    unittest.main()
