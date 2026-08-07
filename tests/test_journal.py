"""Session journal: every action and note is recorded, secrets never are.

Drives the real CLI against the fake adb so the assertions are about what
actually lands in `journal.ndjson`, not what a handler claims.
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

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import journal as journal_mod  # noqa: E402


class JournalBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = self.tmp.name
        self.state = Path(self.cwd) / "state.json"
        self.state.write_text(json.dumps({"devices": [["emulator-5554", "device", ""]]}),
                              encoding="utf-8")
        self.env = {
            **os.environ,
            "AUTONOM_FAKE_STATE": str(self.state),
            "AUTONOM_HOME": str(Path(self.cwd) / "home"),
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cli(self, *argv: str) -> dict:
        completed = subprocess.run(
            [sys.executable, str(CLI), *argv, "--adb", str(FAKE_ADB)],
            capture_output=True, text=True, env=self.env, cwd=self.cwd, timeout=60,
        )
        stream = completed.stdout if completed.returncode == 0 else completed.stderr
        return json.loads(stream)

    def start(self) -> None:
        self.run_cli("session", "start", "--serial", "emulator-5554",
                     "--app-id", "com.example.app")

    def journal_lines(self) -> list[dict]:
        # Sessions are global now, under AUTONOM_HOME/sessions.
        root = Path(self.cwd) / "home" / "sessions"
        found = list(root.glob("*/journal.ndjson"))
        if not found:
            return []
        return [json.loads(line) for line in found[0].read_text(encoding="utf-8").splitlines()]


class ActionJournalTests(JournalBase):
    def test_every_verb_lands_in_the_timeline_in_order(self) -> None:
        self.start()
        self.run_cli("open", "myapp://order/42")
        self.run_cli("ui", "tap", "--x", "10", "--y", "20")
        lines = self.journal_lines()
        verbs = [e["verb"] for e in lines if e["kind"] == "action"]
        self.assertEqual(verbs, ["session start", "open", "ui tap"])
        self.assertEqual([e["seq"] for e in lines], list(range(1, len(lines) + 1)))
        for entry in lines:
            self.assertIn("ts", entry)

    def test_a_failed_action_is_journaled_with_its_error_code(self) -> None:
        self.start()
        # `location clear` on an emulator is a coded refusal (unsupported), so
        # the timeline should carry both ok:false and the stable code.
        self.run_cli("location", "clear", "--serial", "emulator-5554")
        failed = [e for e in self.journal_lines() if e.get("ok") is False]
        self.assertTrue(failed)
        self.assertEqual(failed[0].get("error_code"), "unsupported_on_platform")

    def test_nothing_is_written_without_a_session(self) -> None:
        self.run_cli("open", "myapp://x")
        self.assertEqual(self.journal_lines(), [])


class SecretRedactionTests(JournalBase):
    def test_typed_text_is_reduced_to_its_length(self) -> None:
        self.start()
        self.run_cli("ui", "type", "hunter2-password")
        blob = json.dumps(self.journal_lines(), ensure_ascii=False)
        self.assertNotIn("hunter2-password", blob)
        self.assertIn("<16 chars>", blob)

    def test_mock_body_json_is_masked(self) -> None:
        self.start()
        self.run_cli("network", "mock", "add", "--match", "*/login",
                     "--json", '{"token":"SECRET-abc123"}')
        blob = json.dumps(self.journal_lines(), ensure_ascii=False)
        self.assertNotIn("SECRET-abc123", blob)


class NotesAndReadTests(JournalBase):
    def test_note_add_and_list_roundtrip(self) -> None:
        self.start()
        self.run_cli("note", "add", "login screen renders", "--task", "login", "--tag", "ui")
        listed = self.run_cli("note", "list")
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["notes"][0]["text"], "login screen renders")
        self.assertEqual(listed["notes"][0]["task"], "login")
        # A note is a note, never a double-counted action.
        actions = [e for e in self.journal_lines() if e["kind"] == "action"]
        self.assertNotIn("note add", [e["verb"] for e in actions])

    def test_journal_filters_by_kind_and_verb(self) -> None:
        self.start()
        self.run_cli("open", "myapp://x")
        self.run_cli("note", "add", "a thought")
        only_notes = self.run_cli("journal", "--kind", "note")
        self.assertEqual({e["kind"] for e in only_notes["entries"]}, {"note"})
        only_open = self.run_cli("journal", "--verb", "open")
        self.assertEqual([e["verb"] for e in only_open["entries"]], ["open"])

    def test_journal_without_a_session_is_a_soft_empty(self) -> None:
        result = self.run_cli("journal")
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], [])


class ScrubUnitTests(unittest.TestCase):
    def test_sensitive_value_flags_are_masked(self) -> None:
        argv = ["network", "mock", "add", "--json", '{"k":"v"}', "--header", "Authorization: Bearer x"]
        scrubbed = journal_mod.scrub_argv(argv)
        self.assertEqual(scrubbed[4], "***")
        self.assertEqual(scrubbed[6], "***")

    def test_non_sensitive_argv_passes_through(self) -> None:
        argv = ["ui", "tap", "--desc", "Continue"]
        self.assertEqual(journal_mod.scrub_argv(argv), argv)


if __name__ == "__main__":
    unittest.main()
