"""Session → Flow compiler (§8): record with the CLI, compile, replay.

The research doc's Phase-2 exit criteria, verified end to end against the
fakes: a manual login-shaped session becomes a validated flow; secrets never
appear in the output; the generated flow replays green on the same backend;
coordinate taps are flagged, not approximated.
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
# The focused variant: these flows tap the search field and then type, and a
# real device reports that field focused. `inputText` refuses to type into
# a tree with no focus (flow_no_focused_field), which the plain dump would trip.
UI_DUMP = ROOT / "tests/fixtures/ui_dump_focused.xml"

sys.path.insert(0, str(ROOT / "scripts"))


class CompilerEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.state.write_text(json.dumps({"ui_dump": str(UI_DUMP)}),
                              encoding="utf-8")
        self.env = dict(os.environ)
        self.env.update({
            "AUTONOM_HOME": str(self.root / "home"),
            "AUTONOM_FAKE_STATE": str(self.state),
            "AUTONOM_FAKE_LOG": str(self.root / "log.jsonl"),
        })

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

    def _record_session(self) -> None:
        steps = [
            ("session", "start", "--app-id", "com.example.app"),
            ("session", "launch", "com.example.app"),
            ("ui", "tap", "--desc", "Open settings", "--mode", "exact"),
            ("ui", "type", "shoes size 42"),
            ("ui", "tap", "--resource-id", "com.example.app:id/search",
             "--mode", "exact"),
            ("ui", "type", "hunter2", "--sensitive"),
            ("ui", "key", "KEYCODE_BACK"),
            ("screenshot", "--label", "after-search"),
            ("note", "add", "search works"),
            ("ui", "find", "--desc", "Flutter Save Button", "--mode", "exact"),
        ]
        for step in steps:
            result = self._cli(*step)
            assert result.returncode == 0, (step, result.stderr)

    def test_recorded_session_compiles_and_replays(self) -> None:
        self._record_session()
        out = self.root / "recorded.yaml"
        result = self._cli("flow", "create", "--from-session", "current",
                           "--task", "search", "--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        text = out.read_text(encoding="utf-8")

        # structure: launch, taps with the proven selectors, typed text,
        # a secret placeholder, back, screenshot, note, closing assertion
        self.assertIn("- launchApp", text)
        self.assertIn("description: Open settings", text)
        self.assertIn("value: shoes size 42", text)
        self.assertIn("${SECRET_1}", text)
        self.assertNotIn("hunter2", text, "the secret leaked into the flow")
        self.assertIn("- back", text)
        self.assertIn("- takeScreenshot", text)
        self.assertIn("- assertVisible:", text)
        self.assertEqual(payload["secrets_required"], ["SECRET_1"])
        self.assertEqual(payload["quality"]["secrets"], 1)

        check = self._cli("flow", "check", str(out))
        self.assertEqual(check.returncode, 0, check.stderr)

        # replay on the same fakes — the §17 Phase-2 criterion
        replay_env = dict(self.env)
        replay_env["SECRET_1"] = "hunter2"
        replay = subprocess.run(
            [sys.executable, str(CLI), "--platform", "android",
             "--serial", "emulator-5554",
             "--adb", str(ROOT / "tests/fakes/fake_adb.py"),
             "flow", "run", str(out), "--secret", "SECRET_1"],
            cwd=ROOT, env=replay_env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            timeout=120,
        )
        self.assertEqual(replay.returncode, 0, replay.stderr)
        summary = json.loads(replay.stdout)
        self.assertEqual(summary["status"], "passed")
        blob = Path(summary["events"]).read_text(encoding="utf-8")
        self.assertNotIn("hunter2", blob)

    def test_coordinate_taps_are_flagged_not_approximated(self) -> None:
        for step in [("session", "start", "--app-id", "com.example.app"),
                     ("session", "launch", "com.example.app"),
                     ("ui", "tap", "--x", "100", "--y", "200"),
                     ("ui", "find", "--desc", "Flutter Save Button",
                      "--mode", "exact")]:
            result = self._cli(*step)
            assert result.returncode == 0, (step, result.stderr)
        result = self._cli("flow", "create", "--from-session", "current")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        codes = {w["code"] for w in payload["warnings"]}
        self.assertIn("coordinate_tap_not_compilable", codes)
        self.assertNotIn("--x", payload["canonical"])

    def test_empty_session_refuses(self) -> None:
        result = self._cli("session", "start")
        assert result.returncode == 0, result.stderr
        result = self._cli("flow", "create", "--from-session", "current")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error_code"],
                         "flow_check_failed")

    def test_unknown_session_id(self) -> None:
        result = self._cli("flow", "create", "--from-session", "s_nope")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error_code"],
                         "session_not_found")


if __name__ == "__main__":
    unittest.main()
