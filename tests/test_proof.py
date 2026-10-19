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
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import proof  # noqa: E402

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

    def test_malformed_env_is_refused_like_flow_run(self) -> None:
        (self.repo / "src/settings/screen.py").write_text("v2\n")
        result = self._proof("--env", "NOEQUALS")
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stderr)
        self.assertEqual(payload["error_code"], "flow_command_invalid",
                         "proof must not silently drop a malformed --env")

    def test_pr_flow_for_ios_on_android_target_is_warned(self) -> None:
        """A.10: the proof passes the target platform to the suite selection
        and surfaces its platform warning in the payload and proof.md."""
        (self.repo / ".autonom/flows/ios_only.yaml").write_text(
            "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: iPhone login\n"
            "tags: [pull-request]\nrequires:\n  platform:\n    - ios\n---\n"
            "- assertVisible:\n    selector:\n      id: com.example.app:id/search\n")
        (self.repo / "src/settings/screen.py").write_text("v2\n")
        out = self.repo / "build/proof"
        result = self._proof("--out", str(out))
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn(".autonom/flows/ios_only.yaml",
                      " ".join(item["flow"] for item in payload["selected"]))
        self.assertEqual(payload["flow_warnings"], [{
            "code": "flow_platform_mismatch",
            "message": "flow iPhone login is for ios",
            "flow": ".autonom/flows/ios_only.yaml",
            "platforms": ["ios"],
            "session_platform": "android",
        }])
        self.assertIn("flow iPhone login is for ios", payload["warnings"])
        written = json.loads((out / "proof.json").read_text(encoding="utf-8"))
        self.assertEqual(written["flow_warnings"], payload["flow_warnings"])
        self.assertIn("> **Warning:** flow iPhone login is for ios",
                      (out / "proof.md").read_text(encoding="utf-8"))

    def test_proof_platform_comes_from_flags_or_the_session(self) -> None:
        import argparse
        import importlib.util

        spec = importlib.util.spec_from_file_location("autonom_cli_for_proof", CLI)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        ns = argparse.Namespace
        self.assertEqual(cli._proof_platform(ns(platform="ios", serial=None, udid=None)),
                         "ios")
        self.assertEqual(cli._proof_platform(ns(platform=None, serial="e-1", udid=None)),
                         "android")
        self.assertEqual(cli._proof_platform(ns(platform=None, serial=None, udid="U")),
                         "ios")
        from unittest import mock as _mock

        with _mock.patch.object(cli.session_mod, "load_current",
                                return_value={"platform": "ios", "target_id": "U"}):
            self.assertEqual(cli._proof_platform(ns()), "ios")
        with _mock.patch.object(cli.session_mod, "load_current", return_value=None):
            self.assertIsNone(cli._proof_platform(ns()))
        with _mock.patch.object(cli.session_mod, "load_current",
                                side_effect=ValueError("torn")):
            self.assertIsNone(cli._proof_platform(ns()))

    def test_no_session_is_blocked_not_pass(self) -> None:
        stop = self._cli("session", "stop")
        assert stop.returncode == 0, stop.stderr
        (self.repo / "src/settings/screen.py").write_text("v2\n")
        result = self._proof()
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("session", payload["blocked_reason"])


def _pr_flow(name: str, requires: str = "", body: str = "- assertVisible:\n"
             "    selector:\n      id: x\n") -> str:
    return ("schema: autonom.dev/flow/v1\nappId: com.example.app\n"
            f"name: {name}\ntags: [pull-request]\n{requires}---\n{body}")


ANDROID_ONLY = "requires:\n  platform:\n    - android\n"
LATIN1 = ("schema: autonom.dev/flow/v1\nname: caf\u00e9\n---\n- back\n"
          .encode("latin-1"))


class SelectSuiteTests(unittest.TestCase):
    """In-process selection: non-UTF-8 files (B.6), platform warning (A.10)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.flows = self.repo / ".autonom/flows"
        self.flows.mkdir(parents=True)

    def _write(self, name: str, text: str) -> Path:
        path = self.flows / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_non_utf8_flow_is_invalid_with_reason_and_others_select(self) -> None:
        (self.flows / "bad.yaml").write_bytes(LATIN1)
        self._write("pr.yaml", _pr_flow("pr"))
        suite = proof.select_suite(self.flows, self.repo, [])
        self.assertEqual([Path(e["path"]).name for e in suite["selected"]],
                         ["pr.yaml"])
        self.assertEqual(len(suite["invalid"]), 1)
        entry = suite["invalid"][0]
        self.assertEqual(entry["flow"], ".autonom/flows/bad.yaml")
        self.assertEqual(entry["reason"], "not UTF-8 text")
        self.assertEqual(entry["error_code"], "flow_parse_error")
        self.assertEqual(suite["warnings"], [])

    def test_other_invalid_flows_keep_their_own_reason(self) -> None:
        self._write("bad.yaml", "nope\n")
        suite = proof.select_suite(self.flows, self.repo, [])
        self.assertEqual(suite["invalid"][0]["error_code"], "flow_parse_error")
        self.assertEqual(suite["invalid"][0]["reason"], "missing_separator")

    def test_non_utf8_subflow_keeps_the_parent_selected(self) -> None:
        (self.flows / "sub.yaml").write_bytes(LATIN1)
        self._write("pr.yaml", _pr_flow("pr", body="- runFlow: sub.yaml\n"))
        suite = proof.select_suite(self.flows, self.repo, [])
        self.assertEqual([Path(e["path"]).name for e in suite["selected"]],
                         ["pr.yaml"])
        by_flow = {item["flow"]: item for item in suite["invalid"]}
        self.assertEqual(by_flow[".autonom/flows/pr.yaml"]["subflow"],
                         ".autonom/flows/sub.yaml")
        self.assertEqual(by_flow[".autonom/flows/pr.yaml"]["reason"],
                         "not UTF-8 text")
        self.assertEqual(by_flow[".autonom/flows/sub.yaml"]["reason"],
                         "not UTF-8 text")

    def test_pr_tagged_flow_for_other_platform_is_selected_with_warning(self) -> None:
        self._write("pixel.yaml", _pr_flow("Pixel login", ANDROID_ONLY))
        suite = proof.select_suite(self.flows, self.repo, [], platform="ios")
        self.assertEqual([Path(e["path"]).name for e in suite["selected"]],
                         ["pixel.yaml"])
        self.assertEqual(suite["warnings"], [{
            "code": "flow_platform_mismatch",
            "message": "flow Pixel login is for android",
            "flow": ".autonom/flows/pixel.yaml",
            "platforms": ["android"],
            "session_platform": "ios",
        }])

    def test_android_only_steps_count_as_android(self) -> None:
        # no requires header, but `back` cannot run on iOS
        self._write("back.yaml", _pr_flow("back", body="- back\n"))
        suite = proof.select_suite(self.flows, self.repo, [], platform="ios")
        self.assertEqual(suite["warnings"][0]["message"], "flow back is for android")

    def test_no_warning_on_matching_platform_or_without_platform(self) -> None:
        self._write("pixel.yaml", _pr_flow("pixel", ANDROID_ONLY))
        self._write("both.yaml", _pr_flow("both"))
        for platform in ("android", None):
            suite = proof.select_suite(self.flows, self.repo, [], platform=platform)
            self.assertEqual(len(suite["selected"]), 2)
            self.assertEqual(suite["warnings"], [], platform)
        suite = proof.select_suite(self.flows, self.repo, [], platform="ios")
        self.assertEqual([w["flow"] for w in suite["warnings"]],
                         [".autonom/flows/pixel.yaml"],
                         "a flow for both platforms is never warned about")

    def test_untagged_covers_selection_is_not_warned(self) -> None:
        self._write("cov.yaml", (
            "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: cov\n"
            "properties:\n  covers: \"src/*\"\n" + ANDROID_ONLY +
            "---\n- back\n"))
        suite = proof.select_suite(self.flows, self.repo, ["src/a.py"],
                                   platform="ios")
        self.assertEqual(len(suite["selected"]), 1)
        self.assertEqual(suite["warnings"], [])

    def test_select_flows_keeps_the_two_value_shape(self) -> None:
        self._write("pr.yaml", _pr_flow("pr"))
        selected, covered = proof.select_flows(self.flows, self.repo, [])
        self.assertEqual(len(selected), 1)
        self.assertEqual(covered, [])


if __name__ == "__main__":
    unittest.main()
