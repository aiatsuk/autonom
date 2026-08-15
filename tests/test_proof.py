"""PR Proof (§11): diff → smallest sufficient suite → honest verdict."""
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
UI_DUMP = ROOT / "tests/fixtures/ui_dump.xml"

FLOW_OK = """schema: autonom.dev/flow/v1
appId: com.example.app
name: Settings covered
tags: [smoke]
properties:
  covers: "src/settings/*, src/shared/*"
---
- assertVisible:
    selector:
      description: Open settings
      match: exact
"""

FLOW_PR = """schema: autonom.dev/flow/v1
appId: com.example.app
name: Always on PRs
tags: [pull-request]
---
- assertVisible:
    selector:
      id: com.example.app:id/search
"""

FLOW_FAILING = """schema: autonom.dev/flow/v1
appId: com.example.app
name: Broken coverage
properties:
  covers: "src/payments/*"
---
- assertVisible:
    selector:
      id: does_not_exist
    timeoutMs: 300
"""


class ProofTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / ".autonom/flows").mkdir(parents=True)
        (self.repo / "src/settings").mkdir(parents=True)
        (self.repo / "src/payments").mkdir(parents=True)

        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=self.repo, check=True,
                           capture_output=True, text=True)

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (self.repo / "src/settings/screen.py").write_text("v1\n")
        (self.repo / "src/payments/pay.py").write_text("v1\n")
        (self.repo / ".autonom/flows/settings.yaml").write_text(FLOW_OK)
        (self.repo / ".autonom/flows/pr.yaml").write_text(FLOW_PR)
        git("add", "-A")
        git("commit", "-qm", "base")

        state = Path(self.tmp.name) / "state.json"
        state.write_text(json.dumps({"ui_dump": str(UI_DUMP)}), encoding="utf-8")
        self.env = dict(os.environ)
        self.env.update({
            "AUTONOM_HOME": str(Path(self.tmp.name) / "home"),
            "AUTONOM_FAKE_STATE": str(state),
            "AUTONOM_FAKE_LOG": str(Path(self.tmp.name) / "log.jsonl"),
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
            cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            timeout=180,
        )

    def _proof(self, *extra: str):
        return self._cli("proof", "--base", "HEAD", "--repo", str(self.repo),
                         *extra)

    def test_covered_change_selects_runs_and_passes(self) -> None:
        (self.repo / "src/settings/screen.py").write_text("v2\n")
        out = self.repo / "build/proof"
        result = self._proof("--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "pass")
        flows_run = {Path(r["flow"]).name for r in payload["runs"]}
        self.assertEqual(flows_run, {"settings.yaml", "pr.yaml"},
                         "covers-glob plus pull-request tag select the suite")
        self.assertIn("src/settings/screen.py",
                      " ".join(payload["selected"][0]["reasons"]) +
                      " ".join(payload["selected"][-1]["reasons"]))
        markdown = (out / "proof.md").read_text(encoding="utf-8")
        self.assertIn("PASS", markdown)
        self.assertIn("Changed areas", markdown)
        self.assertTrue((out / "proof.json").is_file())

    def test_failing_covering_flow_fails_the_proof(self) -> None:
        (self.repo / ".autonom/flows/payments.yaml").write_text(FLOW_FAILING)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", "add flow"], cwd=self.repo,
                       check=True, capture_output=True)
        (self.repo / "src/payments/pay.py").write_text("v2\n")
        result = self._proof()
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "fail")
        failing = [r for r in payload["runs"] if r["status"] == "failed"]
        self.assertEqual(failing[0]["failure"]["error_code"],
                         "flow_assertion_timeout")

    def test_uncovered_change_is_not_covered_never_pass(self) -> None:
        # remove the always-on flow so nothing selects
        (self.repo / ".autonom/flows/pr.yaml").unlink()
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", "drop pr flow"],
                       cwd=self.repo, check=True, capture_output=True)
        (self.repo / "src/payments/pay.py").write_text("v2\n")
        result = self._proof()
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "not_covered")
        self.assertIn("src/payments/pay.py", payload["uncovered_files"])

    def test_no_session_is_blocked_not_pass(self) -> None:
        stop = self._cli("session", "stop")
        assert stop.returncode == 0, stop.stderr
        (self.repo / "src/settings/screen.py").write_text("v2\n")
        result = self._proof()
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("session", payload["blocked_reason"])


if __name__ == "__main__":
    unittest.main()
