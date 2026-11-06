"""`network mock` flags for request conditions and rewrite rules (MR-11, MR-12).

Drives the real CLI in a subprocess against a temporary AUTONOM_HOME (and the
fake adb where a session and its journal are needed), so the assertions are
about what lands in the registry, on stdout and in `journal.ndjson`, not about
what a handler claims. The registry library, the addon and the Canvas bridge
have their own suites; this one pins the command line: the flags and their
list-replace and clear semantics, the refused combinations, the mode and
condition summary in `list` and `show`, secrets never printed or journaled, and
the pre-test repro of a rule that has the new fields.
"""
from __future__ import annotations

import contextlib
import importlib
import io
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
from autonom_lib.network import mocks, redact  # noqa: E402
from autonom_lib.pretest import repro as repro_mod  # noqa: E402

TOKEN = "abc123"
REDACTED = redact.PLACEHOLDER


class CliBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.registry = self.home / "mocks"
        state = Path(self.tmp.name) / "state.json"
        state.write_text(json.dumps({"devices": [["emulator-5554", "device", ""]]}),
                         encoding="utf-8")
        self.env = {**os.environ, "AUTONOM_HOME": str(self.home),
                    "AUTONOM_FAKE_STATE": str(state), "PYTHONDONTWRITEBYTECODE": "1"}

    def run_cli(self, *argv: str) -> tuple[int, dict, str]:
        """(exit code, the JSON payload, raw text) of one invocation."""
        completed = subprocess.run(
            [sys.executable, str(CLI), *argv, "--adb", str(FAKE_ADB)],
            capture_output=True, text=True, env=self.env, cwd=self.tmp.name, timeout=60)
        text = completed.stdout if completed.returncode == 0 else completed.stderr
        return completed.returncode, json.loads(text), text

    def ok(self, *argv: str) -> dict:
        code, payload, text = self.run_cli(*argv)
        self.assertEqual(code, 0, text)
        self.assertTrue(payload.get("ok"), text)
        return payload

    def refused(self, *argv: str, code: str = "invalid_value") -> dict:
        exit_code, payload, text = self.run_cli(*argv)
        self.assertNotEqual(exit_code, 0, text)
        self.assertEqual(payload.get("error_code"), code, text)
        return payload

    def add(self, *flags: str) -> dict:
        return self.ok("network", "mock", "add", *flags)["mock"]

    def update(self, identifier: str, *flags: str) -> dict:
        return self.ok("network", "mock", "update", identifier, *flags)["mock"]

    def stored(self, identifier: str = "m_1") -> dict:
        for rule in mocks.load(self.registry):
            if rule["id"] == identifier:
                return rule
        self.fail(f"{identifier} is not in the registry")

    def shown(self, identifier: str = "m_1") -> dict:
        return self.ok("network", "mock", "show", identifier)["mock"]

    def start_session(self) -> None:
        self.ok("session", "start", "--serial", "emulator-5554", "--app-id", "com.example.app")

    def journal_dir(self) -> Path:
        return next((self.home / "sessions").glob("*/journal.ndjson")).parent

    def journal(self) -> list[dict]:
        path = self.journal_dir() / "journal.ndjson"
        return [json.loads(line) for line in path.read_text("utf-8").splitlines()]

    def mock_entries(self, action: str) -> list[dict]:
        return [entry for entry in self.journal()
                if entry.get("verb") == "network mock" and action in entry.get("argv", [])]


class FlagsTests(CliBase):
    def test_add_stores_every_kind_of_condition(self) -> None:
        shown = self.add("--match", "*/items*", "--query", "page=2", "--query", "debug",
                         "--request-header", "X-Client: ios*", "--body-contains", "needle",
                         "--body-json", "user.role=admin", "--body-json", "items.0.qty=2",
                         "--status", "500")
        rule = self.stored()
        self.assertEqual(rule["match"]["query"], [{"name": "page", "value": "2"},
                                                  {"name": "debug", "value": None}])
        self.assertEqual(rule["match"]["headers"], [{"name": "X-Client", "value": "ios*"}])
        self.assertEqual(rule["match"]["body"], {
            "contains": ["needle"],
            "json": [{"path": "user.role", "value": "admin"},
                     {"path": "items.0.qty", "value": 2}]})
        self.assertNotIn("mode", rule)  # a fake answer is the default and stays unwritten
        self.assertEqual(rule["response"]["status"], 500)
        self.assertEqual((shown["mode"], shown["conditions"]), ("respond", 6))

    def test_mr_11_s01_add_then_narrow(self) -> None:
        self.add("--match", "*/items*", "--query", "page=2", "--status", "500")
        self.update("m_1", "--request-header", "X-Client: ios*")
        shown = self.shown()
        self.assertEqual(shown["match"]["query"], [{"name": "page", "value": "2"}])
        self.assertEqual(shown["match"]["headers"], [{"name": "X-Client", "value": "ios*"}])
        self.assertEqual(shown["response"]["status"], 500)
        self.assertEqual(shown["conditions"], 2)

    def test_a_repeatable_flag_that_is_given_replaces_that_whole_list(self) -> None:
        self.add("--match", "*", "--query", "page=2", "--query", "debug",
                 "--request-header", "X-Client: ios*", "--body-contains", "needle")
        self.update("m_1", "--query", "only=this")
        match = self.stored()["match"]
        self.assertEqual(match["query"], [{"name": "only", "value": "this"}])
        self.assertEqual(match["headers"], [{"name": "X-Client", "value": "ios*"}])
        self.assertEqual(match["body"], {"contains": ["needle"]})

    def test_flags_that_are_not_given_keep_what_is_stored(self) -> None:
        self.add("--match", "*/items", "--query", "page=2", "--rewrite",
                 "--json-set", "a=1", "--remove-header", "ETag", "--status", "503")
        before = self.stored()
        self.update("m_1", "--note", "only a note")
        after = self.stored()
        for key in ("match", "rewrite", "mode"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after["response"]["status"], 503)

    def test_clear_conditions_removes_every_condition_and_nothing_else(self) -> None:
        self.add("--match", "*/items", "--method", "POST", "--query", "page=2",
                 "--request-header", "X-Client", "--body-contains", "n",
                 "--body-json", "a=1")
        self.update("m_1", "--clear-conditions")
        match = self.stored()["match"]
        for key in ("query", "headers", "body"):
            self.assertNotIn(key, match)
        self.assertEqual((match["url_glob"], match["method"]), ("*/items", "POST"))
        shown = self.shown()
        self.assertEqual(shown["conditions"], 0)
        self.assertNotIn("condition_summary", shown)

    def test_clear_conditions_with_a_new_list_leaves_only_the_new_list(self) -> None:
        self.add("--match", "*", "--query", "page=2", "--request-header", "X-Client")
        self.update("m_1", "--clear-conditions", "--query", "fresh")
        match = self.stored()["match"]
        self.assertEqual(match["query"], [{"name": "fresh", "value": None}])
        self.assertNotIn("headers", match)

    def test_rewrite_rule_without_a_status_keeps_the_real_one(self) -> None:
        shown = self.add("--match", "*/profile", "--rewrite", "--json-set", "user.plan=pro")
        rule = self.stored()
        self.assertEqual(rule["mode"], "rewrite")
        self.assertIsNone(rule["response"]["status"])
        self.assertEqual(shown["mode"], "rewrite")
        self.update("m_1", "--status", "203")
        self.assertEqual(self.stored()["response"]["status"], 203)

    def test_rewrite_edits_are_stored_as_given(self) -> None:
        self.add("--match", "*/profile", "--rewrite", "--status", "503",
                 "--header", "Retry-After: 5", "--remove-header", "ETag",
                 "--remove-header", "X-Debug", "--json-set", "user.plan=pro",
                 "--json-set", "user.seats=3", "--json-remove", "user.trial_ends",
                 "--replace", "Free", "Pro", "--replace", "a b", "c d")
        rule = self.stored()
        self.assertEqual(rule["rewrite"], {
            "remove_headers": ["ETag", "X-Debug"],
            "json_set": [{"path": "user.plan", "value": "pro"},
                         {"path": "user.seats", "value": 3}],
            "json_remove": ["user.trial_ends"],
            "replace": [{"find": "Free", "with": "Pro"}, {"find": "a b", "with": "c d"}]})
        self.assertEqual(rule["response"]["headers"], {"Retry-After": "5"})

    def test_a_replace_pair_may_come_before_the_id(self) -> None:
        self.add("--match", "*", "--rewrite", "--replace", "x", "y")
        self.ok("network", "mock", "update", "--replace", "Free", "Pro", "m_1")
        self.assertEqual(self.stored()["rewrite"]["replace"], [{"find": "Free", "with": "Pro"}])

    def test_clear_edits_removes_every_edit_and_keeps_the_mode(self) -> None:
        self.add("--match", "*", "--rewrite", "--remove-header", "ETag", "--json-set", "a=1",
                 "--json-remove", "b", "--replace", "x", "y", "--status", "503")
        self.update("m_1", "--clear-edits")
        rule = self.stored()
        self.assertNotIn("rewrite", rule)
        self.assertEqual((rule["mode"], rule["response"]["status"]), ("rewrite", 503))

    def test_a_new_edit_list_replaces_the_stored_one_for_that_kind(self) -> None:
        self.add("--match", "*", "--rewrite", "--json-set", "a=1", "--remove-header", "ETag")
        self.update("m_1", "--json-set", "b=2")
        rewrite = self.stored()["rewrite"]
        self.assertEqual(rewrite["json_set"], [{"path": "b", "value": 2}])
        self.assertEqual(rewrite["remove_headers"], ["ETag"])

    def test_no_rewrite_turns_a_rewrite_rule_back_into_a_fake_answer(self) -> None:
        self.add("--match", "*", "--rewrite")
        self.update("m_1", "--no-rewrite")
        rule = self.stored()
        self.assertNotIn("mode", rule)
        self.assertEqual(rule["response"]["status"], 200)

    def test_a_whole_body_is_allowed_in_rewrite_mode(self) -> None:
        self.add("--match", "*", "--rewrite", "--json", '{"fixed": true}')
        rule = self.stored()
        self.assertTrue(Path(rule["response"]["body_path"]).exists())
        self.assertNotIn("rewrite", rule)

    def test_delay_is_allowed_in_rewrite_mode(self) -> None:
        self.add("--match", "*", "--rewrite", "--delay", "1.5")
        self.assertEqual(self.stored()["delay_s"], 1.5)


class RefusalTests(CliBase):
    EDIT_FLAGS = (("--remove-header", "ETag"), ("--json-set", "a=1"),
                  ("--json-remove", "a"), ("--replace", "x", "y"))

    def registry_text(self) -> str:
        path = mocks.registry_file(self.registry)
        return path.read_text("utf-8") if path.exists() else ""

    def test_mr_11_s02_a_respond_rule_with_an_edit_flag(self) -> None:
        for flags in self.EDIT_FLAGS:
            with self.subTest(flag=flags[0]):
                payload = self.refused("network", "mock", "add", "--match", "*", *flags)
                self.assertIn("--rewrite", payload["error"] + payload["hint"])
                self.assertIn(flags[0], payload["error"])
        self.assertEqual(mocks.load(self.registry), [])

    def test_no_rewrite_with_an_edit_flag_is_refused_too(self) -> None:
        self.refused("network", "mock", "add", "--match", "*", "--no-rewrite", "--json-set", "a=1")
        self.assertEqual(mocks.load(self.registry), [])

    def test_an_update_may_not_leave_edits_on_a_fake_answer(self) -> None:
        self.add("--match", "*", "--rewrite", "--json-set", "a=1")
        before = self.registry_text()
        payload = self.refused("network", "mock", "update", "m_1", "--no-rewrite")
        self.assertIn("--rewrite", payload["error"] + payload["hint"])
        self.assertEqual(self.registry_text(), before)
        self.update("m_1", "--no-rewrite", "--clear-edits")  # the way out
        self.assertNotIn("mode", self.stored())

    def test_an_edit_flag_on_a_respond_rule_update_is_refused(self) -> None:
        self.add("--match", "*")
        before = self.registry_text()
        self.refused("network", "mock", "update", "m_1", "--replace", "x", "y")
        self.assertEqual(self.registry_text(), before)

    def test_rewrite_and_hang_exclude_each_other(self) -> None:
        payload = self.refused("network", "mock", "add", "--match", "*", "--rewrite", "--hang")
        self.assertIn("--hang", payload["error"])
        self.assertEqual(mocks.load(self.registry), [])
        self.add("--match", "*", "--rewrite")
        self.add("--match", "*/held", "--hang")
        before = self.registry_text()
        self.refused("network", "mock", "update", "m_1", "--hang")
        self.refused("network", "mock", "update", "m_2", "--rewrite")  # it is held
        self.assertEqual(self.registry_text(), before)
        self.update("m_2", "--rewrite", "--delay", "2")  # a delay replaces the hold
        self.assertEqual((self.stored("m_2")["mode"], self.stored("m_2")["delay_s"]),
                         ("rewrite", 2.0))

    def test_a_whole_body_and_body_edits_exclude_each_other(self) -> None:
        self.refused("network", "mock", "add", "--match", "*", "--rewrite",
                     "--json", '{"a": 1}', "--json-set", "b=2")
        self.assertEqual(mocks.load(self.registry), [])

    def test_mr_05_s01_a_bad_header_name(self) -> None:
        self.add("--match", "*/keep")
        before = self.registry_text()
        payload = self.refused("network", "mock", "add", "--match", "*",
                               "--request-header", "Bad Name: x")
        self.assertIn("--request-header", payload["error"])
        self.assertEqual(self.registry_text(), before)

    def test_malformed_conditions_and_edits_are_refused_with_the_flag_named(self) -> None:
        cases = (
            (("--query", "=x"), "--query"),
            (("--query", " "), "--query"),
            (("--body-json", "no-equals"), "--body-json"),
            (("--body-json", "a..b=1"), "--body-json"),
            (("--body-json", "=1"), "--body-json"),
            (("--body-contains", ""), "--body-contains"),
            (("--request-header", ": x"), "--request-header"),
            (("--request-header", "X" * 3000), "--request-header"),
            (("--query", "a=" + "v" * 2049), "--query"),
        )
        for flags, named in cases:
            with self.subTest(flags=flags[0], value=flags[1][:12]):
                payload = self.refused("network", "mock", "add", "--match", "*", *flags)
                self.assertIn(named, payload["error"])
        too_many = [part for n in range(21) for part in ("--query", f"q{n}")]
        payload = self.refused("network", "mock", "add", "--match", "*", *too_many)
        self.assertIn("--query", payload["error"])
        edits = (
            (("--rewrite", "--json-set", "x"), "--json-set"),
            (("--rewrite", "--json-remove", "a..b"), "--json-remove"),
            (("--rewrite", "--remove-header", "Bad Name"), "--remove-header"),
            (("--rewrite", "--replace", "", "y"), "--replace"),
        )
        for flags, named in edits:
            with self.subTest(flags=flags[1]):
                payload = self.refused("network", "mock", "add", "--match", "*", *flags)
                self.assertIn(named, payload["error"])
        self.assertEqual(mocks.load(self.registry), [])

    def test_rewrite_flags_exclude_each_other_and_replace_takes_two_values(self) -> None:
        self.refused("network", "mock", "add", "--match", "*", "--rewrite", "--no-rewrite",
                     code="usage_error")
        self.refused("network", "mock", "add", "--match", "*", "--rewrite", "--replace", "x",
                     code="usage_error")
        self.assertEqual(mocks.load(self.registry), [])


class ParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.parser = importlib.import_module("autonom").build_parser()

    def parse(self, verb: str, *flags: str):
        return self.parser.parse_args(["network", "mock", verb, *flags])

    def parse_quietly(self, verb: str, *flags: str):
        with contextlib.redirect_stderr(io.StringIO()):
            return self.parse(verb, *flags)

    def test_the_older_abbreviations_keep_their_meaning(self) -> None:
        args = self.parse("add", "--match", "*", "--j", "{}", "--head", "A: b", "--bo", "f",
                          "--no", "n", "--st", "200")
        self.assertEqual((args.json, args.header, args.body_file, args.note, args.status),
                         ("{}", ["A: b"], "f", "n", 200))
        args = self.parse("update", "m_1", "--hea=A: b", "--js", "{}")
        self.assertEqual((args.header, args.json), (["A: b"], "{}"))

    def test_the_new_flags_are_matched_only_when_spelled_in_full(self) -> None:
        for flag in ("--quer", "--request", "--rew", "--no-rew", "--remove", "--json-s",
                     "--repl", "--body-c", "--body-j"):
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                self.parse_quietly("add", "--match", "*", flag, "x")
        args = self.parse("add", "--match", "*", "--rewrite", "--query", "a", "--replace", "x", "y")
        self.assertEqual((args.rewrite, args.query, args.replace), (True, ["a"], [["x", "y"]]))
        self.assertIs(self.parse("add", "--match", "*", "--no-rewrite").rewrite, False)
        self.assertIsNone(self.parse("add", "--match", "*").rewrite)

    def test_clear_flags_belong_to_update_only(self) -> None:
        self.assertTrue(self.parse("update", "m_1", "--clear-conditions").clear_conditions)
        self.assertTrue(self.parse("update", "m_1", "--clear-edits").clear_edits)
        with self.assertRaises(SystemExit):
            self.parse_quietly("add", "--match", "*", "--clear-edits")


class ListAndShowTests(CliBase):
    def test_list_shows_the_mode_and_a_short_condition_summary(self) -> None:
        self.add("--match", "*/plain")
        self.add("--match", "*/items*", "--query", "page=2", "--request-header", "X-Client: ios*")
        self.add("--match", "*/profile", "--rewrite", "--status", "503", "--remove-header", "ETag",
                 "--json-set", "user.plan=pro", "--body-json", "user.role=admin")
        listed = self.ok("network", "mock", "list")
        plain, narrowed, rewrite = listed["mocks"]
        self.assertEqual((plain["mode"], plain["conditions"]), ("respond", 0))
        self.assertNotIn("condition_summary", plain)
        self.assertEqual((narrowed["mode"], narrowed["conditions"]), ("respond", 2))
        self.assertEqual(narrowed["condition_summary"], "page=2 · +1 header")
        self.assertEqual((rewrite["mode"], rewrite["conditions"]), ("rewrite", 1))
        self.assertIn("status 503", rewrite["edit_summary"])
        self.assertIn("json_set×1", rewrite["edit_summary"])
        self.assertEqual(listed["count"], 3)

    def test_show_lists_every_condition_and_edit(self) -> None:
        self.add("--match", "*/profile", "--rewrite", "--query", "page=2",
                 "--request-header", "X-Client: ios*", "--body-contains", "needle",
                 "--body-json", "user.role=admin", "--remove-header", "ETag",
                 "--json-set", "user.plan=pro", "--json-remove", "user.trial_ends",
                 "--replace", "Free", "Pro", "--status", "503")
        shown = self.shown()
        self.assertEqual(shown["match"]["query"], [{"name": "page", "value": "2"}])
        self.assertEqual(shown["match"]["headers"], [{"name": "X-Client", "value": "ios*"}])
        self.assertEqual(shown["match"]["body"]["contains"], ["needle"])
        self.assertEqual(shown["match"]["body"]["json"], [{"path": "user.role", "value": "admin"}])
        self.assertEqual(shown["rewrite"], self.stored()["rewrite"])
        self.assertEqual((shown["mode"], shown["conditions"], shown["edits"]), ("rewrite", 4, 4))

    def test_a_rule_written_before_these_flags_lists_as_it_did(self) -> None:
        self.registry.mkdir(parents=True)
        mocks.registry_file(self.registry).write_text(json.dumps({
            "version": 1, "next_id": 2, "mocks": [{
                "id": "m_1", "enabled": True, "note": None,
                "match": {"url_glob": "*/old", "method": "GET", "host": None,
                          "ignore_query": False},
                "response": {"status": 418, "headers": {"X-A": "1"}, "body_path": None},
                "delay_s": 2.0}]}), encoding="utf-8")
        (rule,) = self.ok("network", "mock", "list")["mocks"]
        stored = self.stored()
        for key, value in stored.items():
            self.assertEqual(rule[key], value, key)
        self.assertEqual((rule["mode"], rule["conditions"]), ("respond", 0))

    def test_a_damaged_hand_edited_rule_does_not_break_the_listing(self) -> None:
        self.registry.mkdir(parents=True)
        mocks.registry_file(self.registry).write_text(json.dumps({
            "version": 1, "next_id": 3, "mocks": [
                {"id": "m_1", "enabled": True, "mode": "rewrite", "rewrite": "oops",
                 "match": {"url_glob": "*", "query": "oops", "headers": [1, None],
                           "body": 5}, "response": {"status": "x", "headers": 7}},
                {"id": "m_2", "enabled": True, "match": {"url_glob": "*/ok"},
                 "response": {"status": 200}}]}), encoding="utf-8")
        listed = self.ok("network", "mock", "list")
        self.assertEqual([rule["id"] for rule in listed["mocks"]], ["m_1", "m_2"])
        self.ok("network", "mock", "show", "m_1")

    def test_enable_and_disable_print_the_rule_masked(self) -> None:
        self.add("--match", "*", "--request-header", f"Authorization: Bearer {TOKEN}")
        for verb in ("disable", "enable"):
            text = self.run_cli("network", "mock", verb, "m_1")[2]
            self.assertNotIn(TOKEN, text)
            self.assertIn(REDACTED, text)


class SecretTests(CliBase):
    """MR-12: a credential in a condition or an edit is not printed or journaled."""

    # Printed masked by name: sensitive headers, secret-looking query names and
    # credential JSON fields. The free text of --body-contains and --replace is
    # the rule's own definition and is printed as written, but never journaled.
    SECRET_FLAGS = (
        ("--request-header", f"Authorization: Bearer {TOKEN}"),
        ("--request-header", f"Cookie: sid={TOKEN}"),
        ("--query", f"token={TOKEN}"),
        ("--body-json", f"user.password={TOKEN}"),
        ("--rewrite",),
        ("--json-set", f"auth.token={TOKEN}"),
        ("--header", f"Set-Cookie: sid={TOKEN}"),
    )
    TEXT = "zz987"

    def flags(self) -> list[str]:
        flags = [part for pair in self.SECRET_FLAGS for part in pair]
        return [*flags, "--body-contains", self.TEXT, "--replace", self.TEXT, "swapped"]

    def test_mr_12_s01_a_token_in_a_header_condition(self) -> None:
        self.start_session()
        added = self.run_cli("network", "mock", "add", "--match", "*", "--request-header",
                             f"Authorization: Bearer {TOKEN}", "--status", "401")
        self.assertEqual(added[0], 0, added[2])
        outputs = [added[2],
                   self.run_cli("network", "mock", "list")[2],
                   self.run_cli("network", "mock", "show", "m_1")[2]]
        for text in outputs:
            self.assertNotIn(TOKEN, text)
        journal = json.dumps(self.journal())
        self.assertNotIn(TOKEN, journal)
        self.assertNotIn("Bearer", journal)
        self.assertEqual(self.stored()["match"]["headers"][0]["value"], f"Bearer {TOKEN}")

    def test_every_output_and_the_journal_stay_clean(self) -> None:
        self.start_session()
        seen = [self.run_cli("network", "mock", "add", "--match", "*/secrets", *self.flags())[2]]
        seen.append(self.run_cli("network", "mock", "update", "m_1", "--query", f"api_key={TOKEN}",
                                 "--request-header", f"X-Api-Key: {TOKEN}",
                                 "--json-remove", "auth.token")[2])
        for verb in ("list", "show m_1", "disable m_1", "enable m_1"):
            seen.append(self.run_cli("network", "mock", *verb.split())[2])
        for text in seen:
            self.assertNotIn(TOKEN, text)
        text = (self.journal_dir() / "journal.ndjson").read_text("utf-8")
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(self.TEXT, text)

    def test_output_masks_by_name_and_not_wholesale(self) -> None:
        self.add("--match", "*", "--request-header", f"Authorization: Bearer {TOKEN}",
                 "--request-header", "X-Client: ios*", "--query", f"token={TOKEN}",
                 "--query", "page=2", "--body-json", f"user.password={TOKEN}",
                 "--body-json", "user.role=admin")
        match = self.shown()["match"]
        self.assertEqual(match["headers"], [{"name": "Authorization", "value": REDACTED},
                                            {"name": "X-Client", "value": "ios*"}])
        self.assertEqual(match["query"], [{"name": "token", "value": REDACTED},
                                          {"name": "page", "value": "2"}])
        self.assertEqual(match["body"]["json"], [{"path": "user.password", "value": REDACTED},
                                                 {"path": "user.role", "value": "admin"}])
        listed = self.ok("network", "mock", "list")["mocks"][0]
        self.assertEqual(listed["match"], match)
        self.assertNotIn(TOKEN, listed["condition_summary"])

    def test_a_masked_value_sent_back_keeps_the_stored_secret(self) -> None:
        self.add("--match", "*", "--request-header", f"Authorization: Bearer {TOKEN}")
        shown = self.shown()["match"]["headers"][0]
        self.update("m_1", "--request-header", f"{shown['name']}: {shown['value']}",
                    "--request-header", "X-New: 1")
        self.assertEqual(self.stored()["match"]["headers"], [
            {"name": "Authorization", "value": f"Bearer {TOKEN}"},
            {"name": "X-New", "value": "1"}])

    def test_the_journal_names_each_flag_and_counts_it(self) -> None:
        self.start_session()
        self.add("--match", "*/items*", "--rewrite", "--query", "page=2", "--query", "debug",
                 "--request-header", "X-Client: ios*", "--json-set", "user.plan=pro",
                 "--status", "503")
        (entry,) = self.mock_entries("add")
        argv = entry["argv"]
        self.assertEqual(argv[-3:], ["query=2", "request_header=1", "json_set=1"])
        self.assertEqual([argv.count(flag) for flag in
                          ("--query", "--request-header", "--json-set")], [2, 1, 1])
        for value in ("page=2", "debug", "ios", "user.plan", "pro"):
            self.assertFalse(any(value in word for word in argv), (value, argv))
        self.assertIn("--rewrite", argv)
        self.assertEqual(argv[argv.index("--status") + 1], "503")  # plain flags stay as typed
        self.update("m_1", "--clear-conditions", "--replace", "a", "b", "--replace", "c", "d")
        (changed,) = self.mock_entries("update")
        self.assertEqual(changed["argv"][-1], "replace=2")
        self.assertIn("--clear-conditions", changed["argv"])
        # the journal masks every value of a value flag with "***" (journal.scrub_argv)
        self.assertEqual(changed["argv"].count("***"), 4)

    def test_the_journal_records_a_listing_without_condition_values(self) -> None:
        self.start_session()
        self.add("--match", f"https://api.test/items?token={TOKEN}*", "--rewrite",
                 "--query", "page=2", "--request-header", "X-Client: ios*",
                 "--replace", self.TEXT, "x", "--status", "503", "--note", self.TEXT)
        self.ok("network", "mock", "list")
        (entry,) = [e for e in self.journal() if "list" in e.get("argv", [])]
        (rule,) = entry["result"]["mocks"]
        self.assertEqual((rule["id"], rule["enabled"], rule["mode"], rule["conditions"],
                          rule["edits"], rule["status"]), ("m_1", True, "rewrite", 2, 1, 503))
        self.assertEqual(rule["match"]["method"], None)
        for value in (TOKEN, self.TEXT, "page=2", "ios"):
            self.assertNotIn(value, json.dumps(entry))

    def test_other_verbs_keep_their_argv(self) -> None:
        cli = importlib.import_module("autonom")
        argv = ["ui", "tap", "--desc", "Query", "query=2"]
        args = cli.build_parser().parse_args(["ui", "tap", "--desc", "Query"])
        self.assertEqual(cli._mock_condition_words(args, argv), argv)  # noqa: SLF001
        args = cli.build_parser().parse_args(["network", "mock", "list"])
        self.assertEqual(cli._mock_condition_words(args, ["network", "mock", "list"]),  # noqa: SLF001
                         ["network", "mock", "list"])

    def test_inline_and_terminated_spellings_are_masked_too(self) -> None:
        cli = importlib.import_module("autonom")
        flags = ["network", "mock", "update", f"--query=token={TOKEN}", "--replace", TOKEN, "x",
                 "--note", "n", "--", "m_1"]
        args = cli.build_parser().parse_args(flags)
        words = cli._mock_condition_words(args, flags)  # noqa: SLF001
        self.assertEqual(words, ["network", "mock", "update", f"--query={REDACTED}", "--replace",
                                 REDACTED, REDACTED, "--note", "n", "--", "m_1",
                                 "query=1", "replace=1"])


class ReproTests(CliBase):
    """MR-12: a pre-test step for a rule with the new fields stays runnable and masked."""

    def session(self) -> dict:
        return {"artifacts_dir": self.tmp.name}

    def act(self, argv: list[str], *, ok: bool = True) -> None:
        journal_mod.append(self.session(), {"kind": "action", "verb": "network mock",
                                            "argv": argv, "ok": ok, "origin": "agent"})

    def steps(self) -> list[dict]:
        return repro_mod.steps(self.session(), {"seq_from": 1, "seq_to": None})

    def test_the_cli_journal_replays_as_flags_with_masked_values(self) -> None:
        self.start_session()
        self.add("--match", "*/items*", "--rewrite", "--request-header",
                 f"Authorization: Bearer {TOKEN}", "--query", "page=2", "--json-set",
                 "user.plan=pro", "--status", "503")
        self.update("m_1", "--replace", "Free", "Pro")
        session = {"artifacts_dir": str(self.journal_dir())}
        steps = repro_mod.steps(session, {"seq_from": 1, "seq_to": None})
        add_step, update_step = [s for s in steps if s["verb"] == "network mock"]
        self.assertEqual(add_step["args"][0], "add")
        self.assertEqual(add_step["args"][add_step["args"].index("--request-header") + 1],
                         "***")
        for flag in ("--rewrite", "--query", "--json-set", "--status"):
            self.assertIn(flag, add_step["args"])
        self.assertEqual(add_step["args"][-3:], ["query=1", "request_header=1", "json_set=1"])
        self.assertEqual(update_step["args"][:2], ["update", "m_1"])
        self.assertEqual(update_step["args"][-1], "replace=1")
        self.assertNotIn(TOKEN, json.dumps(steps))

    def test_raw_values_are_shown_only_when_they_cannot_be_a_credential(self) -> None:
        self.act(["network", "mock", "add", "--match", "*/items", "--rewrite",
                  "--query", "page=2", "--query", f"token={TOKEN}", "--query", "debug",
                  "--request-header", "X-Client: ios*",
                  "--request-header", f"Authorization: Bearer {TOKEN}",
                  "--request-header", f"X-Session-Id: {TOKEN}", "--request-header", "X-Trace",
                  "--body-contains", TOKEN, "--body-json", "user.role=admin",
                  "--body-json", f"user.password={TOKEN}", "--remove-header", "ETag",
                  "--json-set", "user.plan=pro", "--json-set", f"auth.token={TOKEN}",
                  "--json-remove", "user.trial_ends", "--replace", TOKEN, "swapped",
                  "--status", "503"])
        (step,) = self.steps()
        args = step["args"]
        self.assertEqual(args, [
            "add", "--match", "*/items", "--rewrite",
            "--query", "page=2", "--query", f"token={REDACTED}", "--query", "debug",
            "--request-header", "X-Client: ios*",
            "--request-header", f"Authorization: {REDACTED}",
            "--request-header", f"X-Session-Id: {REDACTED}", "--request-header", "X-Trace",
            "--body-contains", REDACTED, "--body-json", "user.role=admin",
            "--body-json", f"user.password={REDACTED}", "--remove-header", "ETag",
            "--json-set", "user.plan=pro", "--json-set", f"auth.token={REDACTED}",
            "--json-remove", "user.trial_ends", "--replace", REDACTED, REDACTED,
            "--status", "503"])
        self.assertNotIn(TOKEN, json.dumps(step))

    def test_inline_flag_spellings_are_masked_too(self) -> None:
        self.act(["network", "mock", "add", "--match", "*", f"--query=token={TOKEN}",
                  f"--request-header=Cookie: sid={TOKEN}", "--query=page=2"])
        (step,) = self.steps()
        self.assertEqual(step["args"][-3:], [f"--query=token={REDACTED}",
                                             f"--request-header=Cookie: {REDACTED}",
                                             "--query=page=2"])

    def test_mocks_active_gains_mode_and_a_condition_count(self) -> None:
        journal_mod.append(self.session(), repro_mod.mock_state_entry([
            {"id": "m_1", "enabled": True, "mode": "rewrite",
             "match": {"url_glob": "*/profile", "query": [{"name": "page", "value": "2"}],
                       "headers": [{"name": "Authorization", "value": f"Bearer {TOKEN}"}],
                       "body": {"contains": ["n"], "json": [{"path": "a", "value": 1}]}},
             "response": {"status": None}, "rewrite": {"json_set": [{"path": "a", "value": TOKEN}]}},
            {"id": "m_2", "enabled": True, "match": {"url_glob": "*/plain"},
             "response": {"status": 500}},
            {"id": "m_3", "enabled": True, "match": {"url_glob": "*/narrow", "query": [
                {"name": "q", "value": None}]}, "response": {"status": 404}}]))
        self.act(["network", "mock", "list"])  # read-only, not a step
        self.act(["network", "mock", "disable", "m_9"])
        (step,) = self.steps()
        self.assertEqual(step["mocks_active"], [
            {"id": "m_1", "match": "*/profile", "mode": "rewrite", "conditions": 4,
             "enabled": True},
            {"id": "m_2", "match": "*/plain", "status": 500, "enabled": True},
            {"id": "m_3", "match": "*/narrow", "status": 404, "conditions": 1, "enabled": True},
        ])
        self.assertNotIn(TOKEN, json.dumps(step))

    def test_the_state_entry_has_no_condition_or_edit_values(self) -> None:
        entry = repro_mod.mock_state_entry([{
            "id": "m_1", "enabled": True, "mode": "rewrite",
            "match": {"url_glob": "*/x", "headers": [{"name": "Authorization",
                                                      "value": f"Bearer {TOKEN}"}]},
            "response": {"status": 503, "headers": {"Set-Cookie": TOKEN}},
            "rewrite": {"replace": [{"find": TOKEN, "with": "y"}]}}])
        self.assertEqual(entry["mocks"], [{"id": "m_1", "enabled": True, "match": "*/x",
                                           "status": 503, "mode": "rewrite", "conditions": 1}])
        self.assertNotIn(TOKEN, json.dumps(entry))

    def test_replay_without_state_entries_follows_mode_and_conditions(self) -> None:
        self.act(["network", "mock", "add", "--match", "*/a", "--rewrite", "--query", "page=2",
                  "--query", "debug"])
        self.act(["network", "mock", "update", "--query", "x", "m_1"])
        self.act(["network", "mock", "update", "m_1", "--clear-conditions"])
        self.act(["network", "mock", "update", "m_1", "--no-rewrite"])
        self.act(["network", "mock", "add", "--match", "*/b", "--status", "500"])
        self.act(["network", "mock", "update", "--replace", "a", "b", "m_2", "--status", "404"])
        active = [step["mocks_active"] for step in self.steps()]
        self.assertEqual(active[0], [{"match": "*/a", "mode": "rewrite", "conditions": 2,
                                      "enabled": True}])
        self.assertEqual(active[1], [{"id": "m_1", "match": "*/a", "mode": "rewrite",
                                      "conditions": 1, "enabled": True}])  # the list was replaced
        self.assertEqual(active[2], [{"id": "m_1", "match": "*/a", "mode": "rewrite",
                                      "enabled": True}])
        self.assertEqual(active[3], [{"id": "m_1", "match": "*/a", "enabled": True}])
        self.assertEqual(active[4][1], {"match": "*/b", "status": 500, "enabled": True})
        # `--replace a b` takes two values: the id is m_2, not `a`
        self.assertEqual(active[5][1], {"id": "m_2", "match": "*/b", "status": 404,
                                        "enabled": True})

    def test_a_rule_without_the_new_fields_replays_as_before(self) -> None:
        self.act(["network", "mock", "add", "--match", "*/api/x", "--status", "500",
                  "--json", REDACTED])
        (step,) = self.steps()
        self.assertEqual(step["args"], ["add", "--match", "*/api/x", "--status", "500",
                                        "--json", REDACTED])
        self.assertEqual(step["mocks_active"], [{"match": "*/api/x", "status": 500,
                                                 "enabled": True}])


if __name__ == "__main__":
    unittest.main()
