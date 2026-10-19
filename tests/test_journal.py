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
        # An explicit serial keeps target discovery on the fake adb; without it
        # the CLI also asks the host's real `simctl`, which can hang for minutes.
        self.run_cli("open", "myapp://x", "--serial", "emulator-5554")
        self.assertEqual(self.journal_lines(), [])


class SecretRedactionTests(JournalBase):
    def test_typed_text_is_reduced_to_its_length(self) -> None:
        self.start()
        self.run_cli("ui", "type", "hunter2-password")
        blob = json.dumps(self.journal_lines(), ensure_ascii=False)
        self.assertNotIn("hunter2-password", blob)
        self.assertIn("<16 chars>", blob)

    def test_mock_headers_keep_content_type_and_mask_credentials(self) -> None:
        """A.5 — only secret-looking header names lose their value."""
        self.start()
        self.run_cli("network", "mock", "add", "--match", "*/login",
                     "--header", "Content-Type: text/plain",
                     "--header", "Authorization: Bearer HDRSECRET1",
                     "--header=X-Session-Id: HDRSECRET2",
                     "--json", '{"ok":true}')
        entries = [e for e in self.journal_lines()
                   if e.get("verb") == "network mock" and "add" in e.get("argv", [])]
        self.assertEqual(len(entries), 1)
        self.assertIn("Content-Type: text/plain", entries[0]["argv"])
        blob = json.dumps(self.journal_lines(), ensure_ascii=False)
        self.assertNotIn("HDRSECRET1", blob)
        self.assertNotIn("HDRSECRET2", blob)

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


class HeaderMaskingTests(unittest.TestCase):
    """A.5 — `--header` values are masked by header name, not wholesale."""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib

        cls.parser = importlib.import_module("autonom").build_parser()

    def scrub(self, argv: list[str], *, parser: bool = True) -> list[str]:
        if not parser:
            return journal_mod.scrub_for_journal(argv)
        import contextlib
        import io

        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            args = self.parser.parse_args(argv)
        return journal_mod.scrub_for_journal(argv, args, self.parser)

    def test_secret_names(self) -> None:
        for name in ("Authorization", "cookie", "Set-Cookie", "X-API-Key",
                     "Proxy-Authorization", "X-Auth-Token", "X-Client-Secret", "Api-Key",
                     "X-Password", "Session", "X-Session-Id", "WWW-Authenticate"):
            with self.subTest(name=name):
                self.assertTrue(journal_mod.secret_header_name(name))
        for name in ("Content-Type", "Accept", "Cache-Control", "X-Request-Id", "Retry-After"):
            with self.subTest(name=name):
                self.assertFalse(journal_mod.secret_header_name(name))

    def test_header_for_journal(self) -> None:
        self.assertEqual(journal_mod.header_for_journal("Content-Type: application/json"),
                         "Content-Type: application/json")
        self.assertEqual(journal_mod.header_for_journal("Retry-After:5"), "Retry-After:5")
        self.assertNotIn("hunter2",
                         journal_mod.header_for_journal("X-Trace: password=hunter2") or "")
        for value in ("Authorization: Bearer x", "Cookie: s=1", "no colon here",
                      "https://h/p?token=x", "Bad Name: v", '{"a":"b"}', ": empty name", ""):
            with self.subTest(value=value):
                self.assertIsNone(journal_mod.header_for_journal(value))

    def test_with_the_parser_every_spelling_is_resolved(self) -> None:
        argv = ["network", "mock", "add", "--url", "https://x/", "--head", "Content-Type: a/b",
                "--hea=Accept: */*", "--header", "Authorization: Bearer PSECRET1",
                "--header=Cookie: PSECRET2", "--json", '{"k":"PSECRET3"}']
        scrubbed = self.scrub(argv)
        self.assertEqual(len(scrubbed), len(argv))
        self.assertEqual(scrubbed[6], "Content-Type: a/b")
        self.assertEqual(scrubbed[7], "--header=Accept: */*")
        self.assertEqual(scrubbed[9], "***")
        self.assertEqual(scrubbed[10], "--header=***")
        self.assertEqual(scrubbed[12], "***")
        self.assertNotIn("PSECRET", " ".join(scrubbed))

    def test_update_is_covered_too(self) -> None:
        argv = ["network", "mock", "update", "m_1", "--header", "Content-Type: text/html"]
        self.assertEqual(self.scrub(argv)[-1], "Content-Type: text/html")

    def test_without_a_parser_only_the_exact_flag_under_network_mock(self) -> None:
        argv = ["network", "mock", "add", "--url", "https://x/",
                "--header", "Content-Type: a/b", "--head", "Accept: */*",
                "--header=Authorization: Bearer NSECRET"]
        scrubbed = self.scrub(argv, parser=False)
        self.assertEqual(scrubbed[6], "Content-Type: a/b")
        self.assertEqual(scrubbed[8], "***")  # abbreviation: stays masked
        self.assertEqual(scrubbed[9], "--header=***")

    def test_typed_text_after_a_terminator_is_never_shown(self) -> None:
        argv = ["ui", "type", "--", "--header", "Content-Type: SECRETTYPED"]
        for parser in (False, True):
            with self.subTest(parser=parser):
                try:
                    scrubbed = self.scrub(argv, parser=parser)
                except SystemExit:
                    continue
                self.assertNotIn("SECRETTYPED", " ".join(scrubbed))
        argv = ["network", "mock", "add", "--url", "https://x/", "--note", "n", "--",
                "--header", "Content-Type: AFTERTERM"]
        self.assertNotIn("Content-Type: AFTERTERM", self.scrub(argv, parser=False))

    def test_other_commands_never_unmask_a_header_shaped_value(self) -> None:
        argv = ["note", "add", "--header", "Content-Type: x"]
        self.assertEqual(journal_mod._header_values(argv), [])  # noqa: SLF001

    # A harmless name in front of a line break would otherwise show a second,
    # smuggled header line (often a credential) in the journal.
    SMUGGLED = (
        "Accept: */*\r\nAuthorization: Bearer CTLSECRET",   # CRLF
        "Accept: */*\nAuthorization: Bearer CTLSECRET",     # LF
        "Accept: */*\rCookie: CTLSECRET",                   # bare CR
        "Content-Type: a/b\r\n",                            # trailing CRLF only
        "Content-Type: a/b\tCTLSECRET",                     # tab
        "Content-Type: a/b\x00CTLSECRET",                   # NUL
        "Content-Type: a/b\x1bCTLSECRET",                   # ESC
        "Content-Type: a/b\x7fCTLSECRET",                   # DEL
        "Content-Type: a/b\x85CTLSECRET",                   # C1 next line
        "Content-Type: a/b CTLSECRET",                 # line separator
        "Content-Type: a/b CTLSECRET",                 # paragraph separator
    )

    def test_a_value_with_a_control_character_is_fully_masked(self) -> None:
        for value in self.SMUGGLED:
            with self.subTest(value=value):
                self.assertIsNone(journal_mod.header_for_journal(value))
        # Non-control Unicode stays visible.
        self.assertEqual(journal_mod.header_for_journal("X-Label: café"), "X-Label: café")

    def test_control_characters_are_masked_in_every_spelling(self) -> None:
        for value in self.SMUGGLED:
            for parser in (True, False):
                for form in ("split", "inline"):
                    with self.subTest(value=value, parser=parser, form=form):
                        tail = (["--header", value] if form == "split"
                                else [f"--header={value}"])
                        argv = ["network", "mock", "add", "--url", "https://x/", *tail]
                        try:
                            scrubbed = self.scrub(argv, parser=parser)
                        except SystemExit:
                            self.fail("the parser refused the argv")
                        self.assertEqual(scrubbed[-1],
                                         "***" if form == "split" else "--header=***")
                        blob = " ".join(scrubbed)
                        self.assertNotIn("CTLSECRET", blob)
                        self.assertNotIn("\n", blob)
                        self.assertNotIn("\r", blob)


if __name__ == "__main__":
    unittest.main()
