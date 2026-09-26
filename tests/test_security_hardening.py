"""Security hardening regressions: device shell quoting, journal redaction,
URL query redaction, and network mock/proxy input validation.

SEC-001  typed text reaches the device shell as one literal word.
SEC-002  the journal masks secrets wherever they sit in argv.
SEC-003  sensitive query values never reach flows, listings or HAR.
CLI-004  (network parts) malformed mock input is refused, a mismatched
         `network start` warns, and the iOS attach answer is honest.

Everything runs against the fakes in tests/fakes; no device is contacted.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import os
import random
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
SERIAL = "emulator-5554"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, journal as journal_mod, ui_android  # noqa: E402
from autonom_lib.network import (  # noqa: E402
    device_proxy_ios, har, mitm_addon, mocks, proxy, redact, store,
)
from autonom_lib.platform import Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


_REAL_STDIN = None


def setUpModule() -> None:
    # The consent gate prompts when stdin is a TTY; keep it away from one.
    global _REAL_STDIN
    _REAL_STDIN = sys.stdin
    sys.stdin = io.StringIO()


def tearDownModule() -> None:
    if _REAL_STDIN is not None:
        sys.stdin = _REAL_STDIN


class CliBase(EnvSandboxMixin, unittest.TestCase):
    """Drives the real CLI against the fake adb with a private AUTONOM_HOME."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.state = root / "state.json"
        self.log = root / "log.jsonl"
        self.state.write_text(json.dumps({"devices": [[SERIAL, "device", ""]]}),
                              encoding="utf-8")
        self.set_env(
            AUTONOM_FAKE_STATE=str(self.state),
            AUTONOM_FAKE_LOG=str(self.log),
            AUTONOM_HOME=str(self.home),
            AUTONOM_ADB=None, AUTONOM_SIMCTL=None, AUTONOM_IDB=None,
        )
        self.env = dict(os.environ)

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--adb", str(FAKE_ADB), *argv],
            capture_output=True, text=True, env=self.env, timeout=120, cwd=self.tmp.name,
        )
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.returncode in (0, 1) else completed.stderr
        return completed.returncode, json.loads(stream)

    def start_session(self) -> Path:
        code, payload = self.run_cli("session", "start", "--serial", SERIAL,
                                     "--app-id", "com.example.app")
        self.assertEqual(code, 0, payload)
        # Sessions are global, under AUTONOM_HOME/sessions/<id>/.
        found = [path for path in (self.home / "sessions").iterdir() if path.is_dir()]
        self.assertEqual(len(found), 1, found)
        return found[0]

    def adb_argv(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text("utf-8").splitlines() if line]
        return [row["argv"] for row in rows if row.get("tool") == "adb"]

    def journal_blob(self, session_dir: Path) -> str:
        path = session_dir / journal_mod.JOURNAL_FILE
        return path.read_text("utf-8") if path.exists() else ""


# --- SEC-001 ----------------------------------------------------------------


class DeviceShellQuotingTests(CliBase):
    """`adb shell` joins argv into one `sh -c` string on the device."""

    def _typed_words(self) -> list[str]:
        typed = [argv for argv in self.adb_argv()
                 if "shell" in argv and argv[-3:-1] == ["input", "text"]]
        self.assertEqual(len(typed), 1, typed)
        shell = typed[0][typed[0].index("shell") + 1:]
        # What the device shell sees is the space-joined remainder.
        return shlex.split(" ".join(shell))

    def test_metacharacters_stay_one_literal_word(self) -> None:
        code, payload = self.run_cli("ui", "type", "a;reboot $HOME", "--serial", SERIAL)
        self.assertEqual(code, 0, payload)
        words = self._typed_words()
        self.assertEqual(words, ["input", "text", "a;reboot%s$HOME"],
                         "the device shell must see exactly one text word")
        typed = [argv for argv in self.adb_argv() if argv[-3:-1] == ["input", "text"]]
        self.assertTrue(typed[0][-1].startswith("'") and typed[0][-1].endswith("'"))

    def test_apostrophes_and_every_metacharacter_survive(self) -> None:
        text = "it's; a & b | c $(d) `e` <f> \"g\""
        code, payload = self.run_cli("ui", "type", text, "--serial", SERIAL)
        self.assertEqual(code, 0, payload)
        self.assertEqual(self._typed_words(), ["input", "text", text.replace(" ", "%s")])

    def test_quote_helper_round_trips(self) -> None:
        for value in ("", "plain", "it's", "'''", "a;b&&c||d", "$(x)`y`"):
            self.assertEqual(shlex.split(ui_android.device_shell_quote(value)), [value])


# --- SEC-002 ----------------------------------------------------------------


class JournalRedactionTests(CliBase):
    def test_ui_type_secret_is_masked_at_any_argv_position(self) -> None:
        session_dir = self.start_session()
        self.run_cli("ui", "type", "--serial", SERIAL, "--sensitive", "SECRETPOS1")
        self.run_cli("ui", "type", "--sensitive", "SECRETPOS2")
        self.run_cli("ui", "type", "--", "-SECRETPOS3")
        blob = self.journal_blob(session_dir)
        self.assertIn('"verb": "ui type"', blob)
        for secret in ("SECRETPOS1", "SECRETPOS2", "SECRETPOS3"):
            self.assertNotIn(secret, blob)
        self.assertIn("<10 chars>", blob)
        self.assertIn(SERIAL, blob, "a target serial is not a secret")

    def test_canvas_token_never_reaches_the_journal(self) -> None:
        session_dir = Path(self.tmp.name) / "session"
        record = {"artifacts_dir": str(session_dir)}
        journal_mod.record_action(record, verb="canvas serve",
                                  argv=["canvas", "serve", "--token", "TOKENSPACE"],
                                  payload=None, ok=True)
        journal_mod.record_action(record, verb="canvas serve",
                                  argv=["canvas", "serve", "--token=TOKENEQUALS"],
                                  payload=None, ok=True)
        blob = self.journal_blob(session_dir)
        self.assertNotIn("TOKENSPACE", blob)
        self.assertNotIn("TOKENEQUALS", blob)
        self.assertIn("--token=***", blob)


class JournalAbbreviationTests(CliBase):
    """argparse accepts unambiguous prefixes, so the journal must too."""

    def test_abbreviated_secret_flags_never_reach_the_journal(self) -> None:
        session_dir = self.start_session()
        code, payload = self.run_cli("network", "mock", "add", "--url", "https://x.example/",
                                     "--head", "Authorization: Bearer HDRLEAK",
                                     "--json", "{}")
        self.assertEqual(code, 0, payload)
        self.run_cli("simulator", "clipboard", "set", "--val", "text=CLIPLEAK")
        self.run_cli("simulator", "clipboard", "set", "--valu=text=CLIPLEAK2")
        # One-letter prefixes are accepted by argparse too.
        self.run_cli("simulator", "clipboard", "set", "--v", "text=VLEAK")
        self.run_cli("simulator", "clipboard", "set", "--j", '{"text":"JLEAK"}')
        code, payload = self.run_cli("network", "mock", "add", "--url", "https://x.example/",
                                     "--j", '{"msg":"MOCKLEAK"}')
        self.assertEqual(code, 0, payload)
        code, payload = self.run_cli("network", "mock", "add", "--url", "https://y.example/",
                                     '--j={"msg":"MOCKLEAK2"}')
        self.assertEqual(code, 0, payload)
        blob = self.journal_blob(session_dir)
        self.assertIn('"verb": "network mock"', blob)
        self.assertIn('"verb": "simulator clipboard"', blob)
        for secret in ("HDRLEAK", "CLIPLEAK", "CLIPLEAK2", "VLEAK", "JLEAK",
                       "MOCKLEAK", "MOCKLEAK2"):
            self.assertNotIn(secret, blob)


def _walk_parser(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()):
    """Every (command path, parser) pair of the real CLI, subcommands included."""
    yield path, parser
    for action in parser._actions:  # noqa: SLF001 - read-only introspection
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for name, sub in action.choices.items():
                yield from _walk_parser(sub, path + (name,))


def _long_options(parser: argparse.ArgumentParser) -> dict[str, argparse.Action]:
    return {option: action for action in parser._actions  # noqa: SLF001
            for option in action.option_strings if option.startswith("--")}


def _accepted_abbreviations(option: str, options: dict[str, argparse.Action]) -> list[str]:
    """Every spelling argparse (allow_abbrev) resolves to `option` in this parser."""
    target = options[option]
    accepted = []
    for end in range(3, len(option) + 1):
        prefix = option[:end]
        if prefix in options:
            if options[prefix] is target:
                accepted.append(prefix)
            continue  # an exact, different flag wins over the abbreviation
        owners = {id(action) for name, action in options.items() if name.startswith(prefix)}
        if owners == {id(target)}:
            accepted.append(prefix)
    return accepted


class ParserWalkRedactionTests(unittest.TestCase):
    """Walk the real CLI parser so no abbreviation length can ever regress."""

    VALUE = "WALKSECRET42"

    @classmethod
    def setUpClass(cls) -> None:
        cls.parser = importlib.import_module("autonom").build_parser()

    def test_every_accepted_abbreviation_of_a_secret_flag_is_masked(self) -> None:
        checked: set[tuple[tuple[str, ...], str]] = set()
        for path, parser in _walk_parser(self.parser):
            options = _long_options(parser)
            for option, action in options.items():
                if option not in journal_mod._SENSITIVE_VALUE_FLAGS or action.nargs == 0:
                    continue
                for spelling in _accepted_abbreviations(option, options):
                    checked.add((path, spelling))
                    for argv in ([*path, spelling, self.VALUE],
                                 [*path, f"{spelling}={self.VALUE}"]):
                        scrubbed = journal_mod.scrub_argv(argv)
                        self.assertNotIn(self.VALUE, json.dumps(scrubbed),
                                         f"{argv} -> {scrubbed}")
        # The walk must actually reach the flags the review reproduced.
        for expected in ((("simulator", "clipboard"), "--v"),
                         (("simulator", "clipboard"), "--j"),
                         (("network", "mock", "add"), "--j"),
                         (("network", "mock", "add"), "--head"),
                         (("canvas", "serve"), "--to"),
                         (("session", "launch"), "--setenv"),
                         (("flow", "run"), "--secret")):
            self.assertIn(expected, checked)

    def test_exact_non_secret_flags_that_prefix_a_secret_flag_are_kept(self) -> None:
        """The exemption table must list every such flag, or evidence is lost."""
        found = 0
        for path, parser in _walk_parser(self.parser):
            options = _long_options(parser)
            for option, action in options.items():
                if option in journal_mod._SENSITIVE_VALUE_FLAGS or action.nargs == 0:
                    continue
                if not any(secret.startswith(option)
                           for secret in journal_mod._SENSITIVE_VALUE_FLAGS):
                    continue
                found += 1
                argv = [*path, option, "KEEPVAL"]
                self.assertIn("KEEPVAL", journal_mod.scrub_argv(argv),
                              f"{' '.join(path)} {option} is a real flag; add it to "
                              "journal._EXACT_NON_SECRET_PREFIXES")
        self.assertGreater(found, 0)

    def test_namespace_secret_dests_cover_every_secret_flag(self) -> None:
        dests = set()
        for _path, parser in _walk_parser(self.parser):
            for option, action in _long_options(parser).items():
                if option in journal_mod._SENSITIVE_VALUE_FLAGS and action.nargs != 0:
                    dests.add(action.dest)
        self.assertTrue(dests)
        self.assertLessEqual(dests, set(journal_mod._SENSITIVE_DESTS))


# Argv lists that leaked in earlier review rounds, as the reviewer ran them.
REVIEW_REPROS = [
    # round 1: positional-only redaction
    ["ui", "type", "--serial", SERIAL, "--sensitive", "LEAK_R1A"],
    ["ui", "type", "--sensitive", "LEAK_R1B"],
    ["ui", "type", "--", "-LEAK_R1C"],
    ["canvas", "serve", "--token", "LEAK_R1D"],
    ["network", "mock", "add", "--url", "https://x/", "--header=Authorization: Bearer LEAK_R1E"],
    ["simulator", "clipboard", "set", "--value=text=LEAK_R1F"],
    # round 2: multi-letter abbreviations
    ["network", "mock", "add", "--url", "https://x/", "--head", "Authorization: Bearer LEAK_R2A",
     "--json", "{}"],
    ["simulator", "clipboard", "set", "--val", "text=LEAK_R2B"],
    ["canvas", "serve", "--tok", "LEAK_R2C"],
    ["canvas", "serve", "--to=LEAK_R2D"],
    # round 3: one-letter abbreviations
    ["simulator", "clipboard", "set", "--v", "text=LEAK_R3A"],
    ["simulator", "clipboard", "set", "--j", '{"text":"LEAK_R3B"}'],
    ["network", "mock", "add", "--url", "https://x/", "--j", '{"msg":"LEAK_R3C"}'],
    ["network", "mock", "add", "--url", "https://x/", '--j={"msg":"LEAK_R3D"}'],
    # round 4: abbreviated global target flags and a spoofed command path
    ["--ser", SERIAL, "ui", "type", "LEAK_R4A"],
    ["--plat", "android", "ui", "type", "LEAK_R4B"],
    ["--ser", "proof", "network", "mock", "add", "--url", "https://x/",
     "--head", "Authorization: Bearer LEAK_R4C", "--json", "{}"],
]


class CanonicalArgvTests(unittest.TestCase):
    """DEC-010: argv resolved against the real parser, exactly as argparse does."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.parser = importlib.import_module("autonom").build_parser()
        root = _long_options(cls.parser)
        # Every spelling argparse accepts for the value-taking global target flags.
        cls.global_spellings = {
            option: _accepted_abbreviations(option, root)
            for option in ("--serial", "--platform", "--target", "--udid", "--adb")
        }

    def _parse(self, argv: list[str]):
        """The namespace argparse builds, or None when it rejects argv (quietly)."""
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            try:
                return self.parser.parse_args(argv)
            except SystemExit:
                return None

    def _scrubbers(self):
        yield "parser", lambda argv, args=None: journal_mod.scrub_canonical(
            self.parser, argv, args)
        yield "fallback", journal_mod.scrub_argv

    def _globals(self, rng: random.Random, options: dict | None = None) -> list[str]:
        """A random mix of (abbreviated) global target flags with values."""
        chosen: list[str] = []
        for option, spellings in self.global_spellings.items():
            if options is not None:
                spellings = [s for s in _accepted_abbreviations(option, options)] \
                    if option in options else []
            if not spellings or rng.random() < 0.5:
                continue
            spelling = rng.choice(spellings)
            value = "android" if option == "--platform" else f"v-{option[2:]}"
            chosen += [f"{spelling}={value}"] if rng.random() < 0.3 else [spelling, value]
        return chosen

    def test_every_secret_spelling_with_mixed_global_flags(self) -> None:
        rng = random.Random(20260926)
        value = "WALKSECRET77"
        checked = 0
        for path, parser in _walk_parser(self.parser):
            if not path:
                continue
            options = _long_options(parser)
            for option, action in options.items():
                if option not in journal_mod._SENSITIVE_VALUE_FLAGS or action.nargs == 0:
                    continue
                for spelling in _accepted_abbreviations(option, options):
                    for form in ([spelling, value], [f"{spelling}={value}"]):
                        before = self._globals(rng)
                        after = self._globals(rng, options)
                        argv = [*before, *path, *after, *form]
                        for name, scrub in self._scrubbers():
                            scrubbed = scrub(argv)
                            self.assertNotIn(value, json.dumps(scrubbed),
                                             f"{name}: {argv} -> {scrubbed}")
                        checked += 1
        self.assertGreater(checked, 50)

    CONTENT_REPROS = [
        # Credentials inside values of options that are not secret by name.
        (["flow", "run", "f.yaml", "--env", "password=HUNTER1"], "HUNTER1"),
        (["flow", "run", "f.yaml", "--env=password=HUNTER2"], "HUNTER2"),
        (["ci", "run", "f.yaml", "--env", "token=HUNTER3"], "HUNTER3"),
        (["network", "mock", "add", "--url", "https://x/?access_token=HUNTER4", "--json", "{}"],
         "HUNTER4"),
        (["session", "launch", "--arg=-password=HUNTER5"], "HUNTER5"),
        (["open", "myapp://cb?token=HUNTER6"], "HUNTER6"),
        (["note", "add", "--author", "password=HUNTER7", "x"], "HUNTER7"),
    ]

    def test_credentials_inside_ordinary_values_are_scrubbed(self) -> None:
        for argv, secret in self.CONTENT_REPROS:
            tokens = journal_mod.canonical_argv(self.parser, argv)
            self.assertFalse([t.raw for t in tokens if t.role in ("unknown", "stray")],
                             f"{argv} must resolve in parser mode")
            args = self._parse(argv)
            for name, scrub in self._scrubbers():
                for namespace in (None, args):
                    self.assertNotIn(secret, json.dumps(scrub(argv, namespace)),
                                     f"{name}: {argv}")

    def _superset_corpus(self):
        """Walk corpus: secret options, plus content secrets in every other option.

        Only secret-shaped content is used. A plain value of a non-secret flag
        (`--p android` meaning `--platform`) is masked by the fallback merely
        because it cannot tell `--p` from a `--password` prefix; parser mode
        knows better and keeps it, which is not a leak.
        """
        rng = random.Random(4242)
        contents = ("password=MARK{n}", "https://h.example/p?token=MARK{n}&page=2",
                    '{{"secret": "MARK{n}"}}', "x=1&api_key=MARK{n}",
                    "https://bob:MARK{n}@h.example/")
        counter = 0
        for path, parser in _walk_parser(self.parser):
            if not path:
                continue
            options = _long_options(parser)
            for option, action in options.items():
                if action.nargs == 0 or option == "--help":
                    continue
                spellings = _accepted_abbreviations(option, options)
                for content in contents:
                    counter += 1
                    marker = f"MARK{counter}"
                    value = content.format(n=counter)
                    spelling = rng.choice(spellings)
                    before = self._globals(rng)
                    yield [*before, *path, spelling, value], marker
                    counter += 1
                    marker = f"MARK{counter}"
                    value = content.format(n=counter)
                    yield [*before, *path, f"{spelling}={value}"], marker
        for argv, secret in self.CONTENT_REPROS:
            yield argv, secret
        for argv in REVIEW_REPROS:
            token = next(t for t in argv if "LEAK_" in t)
            yield argv, token[token.index("LEAK_"):].split('"')[0]

    def test_parser_mode_masks_at_least_what_the_fallback_masks(self) -> None:
        compared = 0
        corpus = list(self._superset_corpus())
        # The secret-option walk corpus as well (markers in secret slots).
        for path, parser in _walk_parser(self.parser):
            options = _long_options(parser)
            for option, action in options.items():
                if option in journal_mod._SENSITIVE_VALUE_FLAGS and action.nargs != 0:
                    for spelling in _accepted_abbreviations(option, options):
                        corpus.append(([*path, spelling, "MARKSECRET"], "MARKSECRET"))
                        corpus.append(([*path, f"{spelling}=MARKSECRET"], "MARKSECRET"))
        for argv, marker in corpus:
            fallback_leaks = marker in json.dumps(journal_mod.scrub_argv(argv))
            parser_leaks = marker in json.dumps(
                journal_mod.scrub_canonical(self.parser, argv))
            if not fallback_leaks:
                self.assertFalse(parser_leaks, f"parser mode is weaker for {argv}")
            compared += 1
        self.assertGreater(compared, 500)

    def test_negative_numbers_resolve_like_argparse(self) -> None:
        for argv in (["ui", "swipe", "--from", "-5,10", "--to", "3,4"],
                     ["ui", "type", "-5.5"], ["ui", "type", "-.5"],
                     ["ui", "type", "-5,10"], ["ui", "type", "-1e3x"]):
            tokens = journal_mod.canonical_argv(self.parser, argv)
            rejected = [t.raw for t in tokens if t.role in ("unknown", "stray")]
            # Canonicalization follows the running argparse's own negative-number
            # rule: 3.14 accepts `-5,10` as a value, 3.11 rejects it as an option.
            if self._parse(argv) is None:
                self.assertTrue(rejected, argv)
            else:
                self.assertFalse(rejected, argv)
            for scrubbed in (journal_mod.scrub_canonical(self.parser, argv),
                             journal_mod.scrub_argv(argv)):
                self.assertEqual(len(scrubbed), len(argv), argv)
        scrubbed = journal_mod.scrub_canonical(self.parser, ["ui", "type", "-5.5"])
        self.assertEqual(scrubbed, ["ui", "type", "<4 chars>"])

    def test_review_repros_never_leak(self) -> None:
        for argv in REVIEW_REPROS:
            leak = next(token for token in argv if "LEAK_" in token)
            secret = leak[leak.index("LEAK_"):].split('"')[0]
            args = self.parser.parse_args(argv)
            for name, scrub in self._scrubbers():
                for namespace in (None, args):
                    scrubbed = scrub(argv, namespace)
                    self.assertNotIn(secret, json.dumps(scrubbed), f"{name}: {argv}")

    def test_ui_type_text_is_masked_with_abbreviated_globals(self) -> None:
        cases = [
            ["--ser", SERIAL, "ui", "type", "TYPEDLEAK1"],
            ["--plat", "android", "--ser=" + SERIAL, "ui", "type", "TYPEDLEAK2"],
            ["ui", "type", "--ser", SERIAL, "--plat", "android", "TYPEDLEAK3"],
            ["--pl", "android", "ui", "type", "--sens", "TYPEDLEAK4"],
            ["--u", "UDID-1", "ui", "type", "--", "-TYPEDLEAK5"],
        ]
        for argv in cases:
            secret = next(t for t in argv if "TYPEDLEAK" in t).lstrip("-")
            args = self.parser.parse_args(argv)
            self.assertIn(secret, args.text)
            for name, scrub in self._scrubbers():
                for namespace in (None, args):
                    scrubbed = scrub(argv, namespace)
                    self.assertNotIn(secret, json.dumps(scrubbed), f"{name}: {argv}")
                    self.assertTrue(any(t.startswith("<") and t.endswith(" chars>")
                                        for t in scrubbed), f"{name}: {scrubbed}")

    def test_a_spoofed_path_cannot_trigger_an_exemption(self) -> None:
        spoofs = [
            ["--ser", "proof", "network", "mock", "add", "--url", "u", "--head", "SPOOF1"],
            ["--se", "proof", "network", "mock", "add", "--url", "u", "--head", "SPOOF2"],
            ["--plat", "android", "--ser", "ui", "canvas", "serve", "--to", "SPOOF3"],
            ["--udid", "atlas", "network", "mock", "update", "m_1", "--head", "SPOOF4"],
        ]
        for argv in spoofs:
            secret = argv[-1]
            # An argv argparse rejects is never journaled; still scrub it.
            args = self._parse(argv)
            for name, scrub in self._scrubbers():
                self.assertNotIn(secret, scrub(argv, args), f"{name}: {argv}")

    def test_real_non_secret_flags_are_kept_verbatim_when_resolved(self) -> None:
        for argv, kept in ((["ui", "swipe", "--from", "1,2", "--to", "3,4"], "3,4"),
                           (["proof", "--head", "main"], "main"),
                           (["--ser", SERIAL, "atlas", "diff", "--head", "snap.json"],
                            "snap.json")):
            self.assertIn(kept, journal_mod.scrub_canonical(self.parser, argv))

    def test_canonical_argv_expands_spellings_and_resolves_the_path(self) -> None:
        tokens = journal_mod.canonical_argv(
            self.parser, ["--ser", "proof", "network", "mock", "add", "--hea=X: y", "--j", "{}"])
        self.assertEqual([t.role for t in tokens],
                         ["option", "value", "command", "command", "command", "option",
                          "option", "value"])
        self.assertEqual(tokens[-1].path, ("network", "mock", "add"))
        self.assertEqual([t.option for t in tokens if t.role == "option"],
                         ["--serial", "--header", "--json"])

    def test_record_action_with_the_parser_writes_no_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = {"artifacts_dir": tmp}
            for argv in REVIEW_REPROS:
                journal_mod.record_action(record, verb="x", argv=argv, payload=None,
                                          ok=True, args=self.parser.parse_args(argv),
                                          parser=self.parser)
            blob = (Path(tmp) / journal_mod.JOURNAL_FILE).read_text("utf-8")
        self.assertNotIn("LEAK_", blob)
        self.assertEqual(blob.count("\n"), len(REVIEW_REPROS))

    def test_a_parser_that_cannot_resolve_falls_back(self) -> None:
        class Broken:
            _actions: list = []

            @property
            def _option_string_actions(self):
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            record = {"artifacts_dir": tmp}
            journal_mod.record_action(record, verb="ui type", argv=["ui", "type", "FALLBACK1"],
                                      payload=None, ok=True, parser=Broken())
            blob = (Path(tmp) / journal_mod.JOURNAL_FILE).read_text("utf-8")
        self.assertNotIn("FALLBACK1", blob)
        self.assertIn("<9 chars>", blob)


# The exact secret flags of the original journal (bdb7044). Parser mode must
# never mask less than that rule did, spelled exactly.
HEAD_SECRET_FLAGS = frozenset({
    "--json", "--header", "--setenv", "--data", "--body", "--raw", "--value",
})


def _head_scrub_argv(argv: list[str]) -> list[str]:
    """The original (bdb7044) argv rule, restated: the baseline to never fall below."""
    out: list[str] = []
    skip_next = False
    for index, token in enumerate(argv):
        if skip_next:
            out.append("***")
            skip_next = False
            continue
        if token in HEAD_SECRET_FLAGS:
            out.append(token)
            skip_next = True
            continue
        if index >= 2 and argv[index - 1] == "type" and argv[index - 2] == "ui" \
                and not token.startswith("-"):
            out.append(f"<{len(token)} chars>")
            continue
        out.append(redact.scrub_body(token) if "=" in token or "{" in token else token)
    return out


class HeadDifferentialTests(unittest.TestCase):
    """Parser mode masks every marker the fallback or the original rule masks.

    A seeded generator builds a few thousand argv lists that argparse accepts:
    every subcommand, random options in random spellings (unique prefixes and
    `=` forms), required options and positionals filled, abbreviated global
    target flags before and after the verb, and a secret-shaped marker in
    every value.
    """

    SHAPES = (
        lambda m: m, lambda m: f"k={m}", lambda m: f"password={m}",
        lambda m: '{"password":"%s"}' % m, lambda m: f"https://h/p?token={m}",
        lambda m: f"Authorization: Bearer {m}", lambda m: f"-9{m}", lambda m: f"{m} sp",
        lambda m: f"a=1&token={m}", lambda m: f"myapp://cb?access_token={m}&x=1",
        lambda m: f"https://u:{m}@h/", lambda m: f"token={m}", lambda m: f"{{{m}}}",
        lambda m: f"Cookie: s={m}", lambda m: '{"a":"password=%s"}' % m,
    )
    GLOBALS = ("--platform", "--target", "--serial", "--udid", "--adb", "--simctl",
               "--idb", "--idb-host", "--idb-port")
    BODY_REPROS = [
        ["network", "mock", "add", "--url", "https://x/", "--body", "Bearer sk_live_MK901X"],
        ["network", "mock", "update", "m_1", "--body", "Bearer sk_live_MK902X"],
        ["network", "mock", "add", "--url", "https://x/", "--body=hunter2MK903X"],
    ]
    MARKER = re.compile(r"MK\d+X")

    @classmethod
    def setUpClass(cls) -> None:
        cls.parser = importlib.import_module("autonom").build_parser()

    def _parse(self, argv: list[str]):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            try:
                return self.parser.parse_args(argv)
            except SystemExit:
                return None

    def _leaves(self, parser, path=()):
        subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]  # noqa: SLF001
        if not subs:
            yield path, parser
        for action in subs:
            for name, sub in action.choices.items():
                yield from self._leaves(sub, path + (name,))

    def _corpus(self, seed: int, trials: int):
        rng = random.Random(seed)
        counter = [0]

        def marker() -> str:
            counter[0] += 1
            return f"MK{counter[0]}X"

        def value(action) -> str:
            if action.choices:
                return rng.choice(list(action.choices))
            if action.type in (int, float):
                return str(rng.randint(1, 9))
            return rng.choice(self.SHAPES)(marker())

        def option_argv(action) -> list[str]:
            option = rng.choice(action.option_strings)
            if option.startswith("--") and rng.random() < 0.5:
                option = option[:rng.randint(3, len(option))]
            if action.nargs == 0:
                return [option]
            item = value(action)
            return [option, item] if rng.random() < 0.6 else [f"{option}={item}"]

        for path, leaf in self._leaves(self.parser):
            options = [a for a in leaf._actions if a.option_strings and a.dest != "help"]  # noqa: SLF001
            positionals = [a for a in leaf._actions if not a.option_strings]  # noqa: SLF001
            required = [a for a in options if a.required]
            for _trial in range(trials):
                before: list[str] = []
                after: list[str] = []
                for _ in range(rng.randint(0, 2)):
                    flag = rng.choice(self.GLOBALS)
                    spelled = flag[:rng.randint(3, len(flag))]
                    item = (rng.choice(["android", "ios"]) if flag == "--platform"
                            else "5" if flag == "--idb-port"
                            else rng.choice(["proof", "ui", "type", "network", marker()]))
                    target = before if rng.random() < 0.5 else after
                    target.extend([spelled, item] if rng.random() < 0.6
                                  else [f"{spelled}={item}"])
                words: list[str] = []
                for action in positionals:
                    count = 1 if action.nargs in (None, "+") else rng.randint(0, 1)
                    words += [value(action) for _ in range(count)]
                flags: list[str] = []
                for action in required:
                    flags += option_argv(action)
                for action in rng.sample(options, min(len(options), rng.randint(0, 4))):
                    flags += option_argv(action)
                body = words + flags if rng.random() < 0.5 else flags + words
                if rng.random() < 0.15 and words:
                    body = flags + ["--"] + words
                argv = before + list(path) + after + body
                args = self._parse(argv)
                if args is not None:
                    yield argv, args

    def _leaked(self, output: list[str]) -> set[str]:
        return set(self.MARKER.findall(json.dumps(output)))

    # Reviewer repros (plan v2, third review): HEAD's adjacency rule on values
    # of other commands, `--` before the verb, and dash-leading typed text.
    V3_REPROS = [
        ["report", "annotate", "--author", "ui", "type", "SK901X"],
        ["app-skill", "promote", "--approval", "ui", "type", "SK902X"],
        ["report", "merge", "--out", "ui", "type", "SK903X"],
        ["ci", "merge", "--out", "ui", "type", "SK904X"],
        ["--", "ui", "type", "SK905X"],
        ["--plat", "ios", "--", "ui", "type", "SK906X"],
        ["--ser", "x", "--", "ui", "type", "SK907X"],
        ["ui", "type", "--sensitive", "-9SK908X"],
        ["ui", "type", "--sensitive", "-SK909X x"],
        ["--ser", "x", "ui", "type", "-9SK910X"],
        ["ui", "type", "-.5SK911X"],
    ]
    V3_MARKER = re.compile(r"SK\d+X")

    def _modes(self, argv: list[str], args):
        """The three ways the CLI can call the journal."""
        yield "parser", journal_mod.scrub_for_journal(argv, args, self.parser)
        yield "args", journal_mod.scrub_for_journal(argv, args)
        yield "neither", journal_mod.scrub_for_journal(argv)

    def _full_corpus(self):
        corpus = list(self._corpus(seed=20260926, trials=30))
        corpus += [(argv, self._parse(argv)) for argv in self.BODY_REPROS + self.V3_REPROS]
        return corpus

    def test_the_vendored_head_rule_is_the_original(self) -> None:
        for argv, _args in self._full_corpus():
            self.assertEqual(journal_mod._head_rule_scrub(argv), _head_scrub_argv(argv), argv)

    def test_every_mode_never_masks_less_than_fallback_or_head(self) -> None:
        compared = 0
        for argv, args in self._full_corpus():
            head = self._leaked(journal_mod._head_rule_scrub(argv))
            for namespace in (None, args):
                fallback = self._leaked(journal_mod.scrub_argv(argv, namespace))
                for name, output in self._modes(argv, namespace):
                    leaked = self._leaked(output)
                    self.assertLessEqual(leaked, head, f"{name} weaker than HEAD: {argv}")
                    self.assertLessEqual(leaked, fallback, f"{name} weaker than fallback: {argv}")
            compared += 1
        self.assertGreater(compared, 2000)

    def test_every_mode_is_a_per_token_superset_of_head(self) -> None:
        rank = journal_mod._mask_rank
        for argv, args in self._full_corpus():
            head = journal_mod._head_rule_scrub(argv)
            for name, output in self._modes(argv, args):
                self.assertEqual(len(output), len(argv), f"{name}: not 1:1 for {argv}")
                for index, raw in enumerate(argv):
                    if head[index] != raw:
                        self.assertGreaterEqual(
                            rank(raw, output[index]), rank(raw, head[index]),
                            f"{name}: token {index} of {argv}: {output[index]!r} "
                            f"vs HEAD {head[index]!r}")

    def _accepts_leading_separator(self) -> bool:
        return self._parse(["--", "ui", "type", "PROBE"]) is not None

    def test_v3_review_repros_are_masked_in_every_mode(self) -> None:
        for argv in self.V3_REPROS:
            args = self._parse(argv)
            # Acceptance is version-dependent (3.11 argparse rejects a `--` before
            # the subcommand, and `-9X` as an unknown option); masking in every
            # mode is not. Any rejection must be one canonicalization foresees.
            before_verb = argv[:argv.index("ui")] if "ui" in argv else argv
            tokens = journal_mod.canonical_argv(self.parser, argv)
            foreseen = any(t.role in ("unknown", "stray") for t in tokens) or (
                "--" in before_verb and not self._accepts_leading_separator())
            if not foreseen:
                self.assertIsNotNone(args, argv)
            for name, output in self._modes(argv, args):
                self.assertFalse(self.V3_MARKER.findall(json.dumps(output)), f"{name}: {output}")
            for name, output in self._modes(argv, None):
                self.assertFalse(self.V3_MARKER.findall(json.dumps(output)), f"{name}: {output}")

    def test_record_action_writes_the_union(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = {"artifacts_dir": tmp}
            for argv in self.V3_REPROS + self.BODY_REPROS:
                args = self._parse(argv)
                for parser in (self.parser, None):
                    for namespace in (args, None):
                        journal_mod.record_action(record, verb="x", argv=argv, payload=None,
                                                  ok=True, args=namespace, parser=parser)
            blob = (Path(tmp) / journal_mod.JOURNAL_FILE).read_text("utf-8")
        self.assertFalse(self.V3_MARKER.findall(blob))
        self.assertFalse(self.MARKER.findall(blob))

    def test_merge_refuses_a_scrub_that_is_not_one_to_one(self) -> None:
        with self.assertRaises(ValueError):
            journal_mod._most_masked(["a", "b"], [["a", "b"], ["a"]])
        self.assertEqual(journal_mod._most_masked(["a", "k=v"],
                                                  [["a", "k=v"], ["***", "k=v"]]),
                         ["***", "k=v"])

    def test_body_abbreviation_of_body_file_is_masked(self) -> None:
        for argv in self.BODY_REPROS:
            args = self._parse(argv)
            self.assertIsNotNone(args, argv)
            self.assertTrue(getattr(args, "body_file", None))
            for namespace in (None, args):
                for scrubbed in (journal_mod.scrub_canonical(self.parser, argv, namespace),
                                 journal_mod.scrub_argv(argv, namespace)):
                    self.assertFalse(self._leaked(scrubbed), scrubbed)
            canonical = journal_mod.scrub_canonical(self.parser, argv, args)
            self.assertTrue(any(t.startswith("--body-file") for t in canonical),
                            "the canonical spelling is kept")


class ScrubArgvTests(unittest.TestCase):
    def test_abbreviations_of_secret_flags_are_masked(self) -> None:
        cases = [
            (["network", "mock", "add", "--head", "Authorization: Bearer X"], 4, "***"),
            (["simulator", "clipboard", "set", "--val", "text=X"], 4, "***"),
            (["canvas", "serve", "--tok", "TOK3"], 3, "***"),
            (["canvas", "serve", "--to=TOK4"], 2, "--to=***"),
            (["canvas", "serve", "--to", "TOK5"], 3, "***"),
            (["flow", "run", "f.yaml", "--pass", "hunter2"], 4, "***"),
            (["flow", "run", "f.yaml", "--sec=NAME"], 3, "--sec=***"),
        ]
        for argv, index, expected in cases:
            self.assertEqual(journal_mod.scrub_argv(argv)[index], expected, argv)

    def test_real_flags_that_look_like_prefixes_are_kept(self) -> None:
        for argv in (["ui", "swipe", "--from", "1,2", "--to", "3,4"],
                     ["atlas", "diff", "--head", "snap.json"],
                     ["proof", "--head", "main"],
                     ["ui", "tap", "--x", "1", "--y", "2"]):
            self.assertEqual(journal_mod.scrub_argv(argv), argv)

    def test_namespace_secret_values_are_masked_anywhere(self) -> None:
        args = SimpleNamespace(command="canvas", canvas_command="serve", token="TOK6",
                               header=["Authorization: Bearer H7"], value=["text=V8"],
                               secret=None, password=None)
        argv = ["canvas", "serve", "--port", "1", "--weird", "TOK6",
                "--x=Authorization: Bearer H7", "text=V8"]
        blob = " ".join(journal_mod.scrub_argv(argv, args))
        for secret in ("TOK6", "H7", "V8"):
            self.assertNotIn(secret, blob)
        self.assertIn("--port 1", blob)


    def test_flag_equals_value_forms_are_masked(self) -> None:
        cases = {
            "--header=Authorization: Bearer XYZ": "--header=***",
            "--value=text=XYZ": "--value=***",
            "--json={\"k\":\"XYZ\"}": "--json=***",
            "--password=XYZ": "--password=***",
            "--secret=XYZ": "--secret=***",
        }
        for token, expected in cases.items():
            scrubbed = journal_mod.scrub_argv(["network", "mock", "add", token])
            self.assertEqual(scrubbed[-1], expected, token)

    def test_space_separated_secret_flags_are_masked(self) -> None:
        argv = ["simulator", "clipboard", "set", "--value", "text=XYZ", "--password", "p"]
        self.assertEqual(journal_mod.scrub_argv(argv),
                         ["simulator", "clipboard", "set", "--value", "***",
                          "--password", "***"])

    def test_parsed_namespace_is_authoritative(self) -> None:
        args = SimpleNamespace(command="ui", ui_command="type", text="--odd-SECRET",
                               sensitive=True)
        argv = ["ui", "type", "--serial", SERIAL, "--", "--odd-SECRET"]
        scrubbed = journal_mod.scrub_argv(argv, args)
        self.assertNotIn("--odd-SECRET", scrubbed)
        self.assertIn(SERIAL, scrubbed)

    def test_global_target_flags_before_the_verb(self) -> None:
        argv = ["--serial", SERIAL, "ui", "type", "hunter2"]
        self.assertEqual(journal_mod.scrub_argv(argv),
                         ["--serial", SERIAL, "ui", "type", "<7 chars>"])

    def test_other_verbs_pass_through(self) -> None:
        argv = ["ui", "tap", "--desc", "Continue", "--serial", SERIAL]
        self.assertEqual(journal_mod.scrub_argv(argv), argv)


# --- SEC-003 ----------------------------------------------------------------


SECRET_URL = "https://api.example.net/v1/items?token=secret123&page=2&API_KEY=k-999#access_token=frag777"


def _flow_record(url: str = SECRET_URL) -> dict:
    return {
        "id": "f_0001", "started_at": "2026-09-01T00:00:00Z", "method": "GET",
        "url": url, "host": "api.example.net", "path": "/v1/items", "status": 302,
        "duration_ms": 3,
        "request_headers_preview": {"authorization": "Bearer RAWHEADER"},
        "response_headers_preview": {"location": "https://x.example/cb?code=authcode42&state=s1"},
        "request_body_preview": None, "response_body_preview": "",
        "mocked": False, "mock_id": None,
        "sizes": {"request_bytes": 0, "response_bytes": 0},
    }


class ScrubUrlTests(unittest.TestCase):
    def test_sensitive_values_go_and_the_rest_stays(self) -> None:
        scrubbed = redact.scrub_url(SECRET_URL)
        for secret in ("secret123", "k-999", "frag777"):
            self.assertNotIn(secret, scrubbed)
        self.assertIn("page=2", scrubbed)
        self.assertTrue(scrubbed.startswith("https://api.example.net/v1/items?token="))
        self.assertIn("API_KEY=" + redact.PLACEHOLDER, scrubbed)

    def test_percent_encoded_keys_are_recognised(self) -> None:
        scrubbed = redact.scrub_url("https://h/p?access%5Ftoken=enc1&Client_Secret=enc2")
        self.assertNotIn("enc1", scrubbed)
        self.assertNotIn("enc2", scrubbed)
        self.assertIn("access%5Ftoken=", scrubbed, "the key itself is kept as sent")

    def test_hyphenated_array_and_bearer_style_keys(self) -> None:
        url = ("https://h/p?Access-Token=a1&api-key=a2&jwt=a3&bearer=a4&token[]=a5"
               "&Token[0]=a6&id%2Dtoken=a7&page=2")
        scrubbed = redact.scrub_url(url)
        for secret in ("a1", "a2", "a3", "a4", "a5", "a6", "a7"):
            self.assertNotIn("=" + secret, scrubbed)
        self.assertIn("page=2", scrubbed)
        self.assertIn("token[]=", scrubbed)

    def test_userinfo_password_is_redacted(self) -> None:
        self.assertEqual(redact.scrub_url("https://bob:pa55@h.example:8443/p?page=2"),
                         f"https://bob:{redact.PLACEHOLDER}@h.example:8443/p?page=2")
        self.assertEqual(redact.scrub_url("https://bob:p@ss@h.example/"),
                         f"https://bob:{redact.PLACEHOLDER}@h.example/")
        self.assertEqual(redact.scrub_url("https://h.example/a@b"), "https://h.example/a@b")

    def test_referer_is_scrubbed_in_flows_and_har(self) -> None:
        record = _flow_record()
        record["request_headers_preview"]["referer"] = "https://app.example/cb?token=REFLEAK"
        self.assertNotIn("REFLEAK", json.dumps(redact.scrub_flow(record)))
        self.assertNotIn("REFLEAK", json.dumps(har.entry_for(record)))

    def test_urls_without_secrets_are_untouched(self) -> None:
        for url in ("https://h/p", "https://h/p?page=2&q=a%20b", "", None,
                    "https://h/p?flag&keyboard=1"):
            self.assertEqual(redact.scrub_url(url), url)

    def test_the_addon_copy_matches(self) -> None:
        self.assertEqual(redact.SENSITIVE_QUERY_KEYS, mitm_addon.SENSITIVE_QUERY_KEYS)
        for url in (SECRET_URL, "https://h/p?Sig=1&x=2", "https://h/p?page=1",
                    "https://h/p?access%5Ftoken=enc1&api+key=2",
                    "https://u:pw@h/p?Access-Token=1&token[]=2#jwt=3"):
            self.assertEqual(redact.scrub_url(url), mitm_addon.scrub_url(url))


class AddonUrlRecordingTests(unittest.TestCase):
    def test_the_recorded_url_is_scrubbed_before_it_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recorder = mitm_addon.AutonomRecorder()
            recorder.directory = tmp
            headers = SimpleNamespace(items=lambda: [
                ("Host", "api.example.net"),
                ("Referer", "https://app.example/cb?access_token=REFLEAK")])
            flow = SimpleNamespace(
                request=SimpleNamespace(method="GET", pretty_url=SECRET_URL,
                                        host="api.example.net",
                                        path="/v1/items?token=secret123&page=2",
                                        headers=headers, content=b"",
                                        timestamp_start=1.0),
                response=SimpleNamespace(
                    status_code=302, content=b"", timestamp_end=1.1,
                    headers=SimpleNamespace(items=lambda: [
                        ("Location", "https://x.example/cb?code=authcode42")])),
                metadata={},
            )
            recorder.response(flow)
            written = (Path(tmp) / "flows.jsonl").read_text("utf-8")
        self.assertNotIn("secret123", written)
        self.assertNotIn("authcode42", written)
        self.assertNotIn("REFLEAK", written)
        self.assertIn("page=2", written)


class StoredFlowRedactionTests(CliBase):
    """Flows recorded before the fix are scrubbed when they are read back."""

    def test_list_show_and_har_export_hide_query_secrets(self) -> None:
        session_dir = self.start_session()
        flows = session_dir / "network" / "flows.jsonl"
        flows.parent.mkdir(parents=True, exist_ok=True)
        flows.write_text(json.dumps(_flow_record()) + "\n", encoding="utf-8")

        code, listed = self.run_cli("network", "requests", "list")
        self.assertEqual(code, 0, listed)
        code, shown = self.run_cli("network", "requests", "show", "f_0001")
        self.assertEqual(code, 0, shown)
        code, exported = self.run_cli("network", "export", "--har", "out.har")
        self.assertEqual(code, 0, exported)
        document = Path(exported["path"]).read_text("utf-8")

        for blob in (json.dumps(listed), json.dumps(shown), document):
            for secret in ("secret123", "k-999", "frag777", "authcode42"):
                self.assertNotIn(secret, blob)
            self.assertIn("page=2", blob)
        entry = json.loads(document)["log"]["entries"][0]
        query = {item["name"]: item["value"] for item in entry["request"]["queryString"]}
        self.assertEqual(query["page"], "2")
        self.assertEqual(query["token"], redact.PLACEHOLDER)
        self.assertEqual(query["API_KEY"], redact.PLACEHOLDER)

    def test_har_scrubs_a_flow_that_bypassed_the_store(self) -> None:
        entry = har.entry_for(_flow_record())
        blob = json.dumps(entry)
        self.assertNotIn("secret123", blob)
        self.assertNotIn("authcode42", blob)
        self.assertIn("page", [item["name"] for item in entry["request"]["queryString"]])


class StoreReaderTests(unittest.TestCase):
    def test_read_all_scrubs_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = {"artifacts_dir": tmp}
            store.flows_path(record).parent.mkdir(parents=True)
            store.flows_path(record).write_text(json.dumps(_flow_record()) + "\n", "utf-8")
            flows, _ = store.read_all(record)
            found = store.find(record, "f_0001")
        self.assertNotIn("secret123", flows[0]["url"])
        self.assertNotIn("secret123", found["url"])
        self.assertIn("page=2", found["url"])


# --- CLI-004 network parts ---------------------------------------------------


class MockValidationTests(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.set_env(AUTONOM_HOME=self.tmp.name)
        self.registry = Path(self.tmp.name) / "mocks"
        self.registry.mkdir()

    def _refused(self, code: str, **kwargs) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            mocks.add(registry=self.registry, **{"url_glob": "*/login", **kwargs})
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(mocks.load(self.registry), [], "a refused rule must not be saved")

    def test_invalid_json_body_is_refused(self) -> None:
        self._refused(errors.INVALID_VALUE, body_text="{bad")
        self._refused(errors.INVALID_VALUE, body_text="[1,")

    def test_plain_text_body_is_still_allowed(self) -> None:
        rule = mocks.add(url_glob="*/x", body_text="plain text", registry=self.registry)
        self.assertTrue(rule["response"]["body_path"])

    def test_status_outside_http_range_is_refused(self) -> None:
        for status in (0, 99, 600, 1000, -1):
            self._refused(errors.INVALID_VALUE, status=status)
        for status in (100, 599):
            mocks.add(url_glob="*/ok", status=status, registry=self.registry)

    def test_update_validates_too(self) -> None:
        rule = mocks.add(url_glob="*/x", registry=self.registry)
        with self.assertRaises(errors.AutonomError) as caught:
            mocks.update(rule["id"], status=700, registry=self.registry)
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        with self.assertRaises(errors.AutonomError) as caught:
            mocks.update(rule["id"], body_text="{nope", registry=self.registry)
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertEqual(mocks.get(rule["id"], self.registry)["response"]["status"], 200)

    def test_missing_target_is_a_selector_error(self) -> None:
        self._refused(errors.SELECTOR_REQUIRED, url_glob="")
        self._refused(errors.SELECTOR_REQUIRED, url_glob=None)

    def test_malformed_header_is_refused(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            mocks.parse_header_args(["nocolon"])
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        with self.assertRaises(errors.AutonomError):
            mocks.parse_header_args([": value"])
        self.assertIsNone(mocks.parse_header_args(None))
        self.assertEqual(mocks.parse_header_args(["X-A: 1", "X-B:two:three"]),
                         {"X-A": "1", "X-B": "two:three"})


class MockCliValidationTests(CliBase):
    def test_bad_json_and_status_are_refused_by_the_cli(self) -> None:
        code, payload = self.run_cli("network", "mock", "add", "--match", "*/login",
                                     "--json", "{bad")
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["error_code"], errors.INVALID_VALUE)
        code, payload = self.run_cli("network", "mock", "add", "--match", "*/login",
                                     "--status", "700")
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["error_code"], errors.INVALID_VALUE)
        code, listed = self.run_cli("network", "mock", "list", "--all")
        self.assertEqual(listed["count"], 0)


class ProxyAlreadyRunningTests(unittest.TestCase):
    RUNNING = {"running": True, "pid": 4242, "port": 8080, "proxy_host": "127.0.0.1",
               "capture_bodies": False, "started_at": "2026-09-01T00:00:00Z"}

    def _start(self, **kwargs) -> dict:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(proxy, "assert_safe_permissions"), \
                mock.patch.object(proxy, "status", return_value=dict(self.RUNNING)), \
                mock.patch.object(proxy.subprocess, "Popen") as popen:
            result = proxy.start({"artifacts_dir": tmp}, **kwargs)
            popen.assert_not_called()
        return result

    def test_a_different_port_or_body_setting_warns(self) -> None:
        result = self._start(port=9090, capture_bodies=True)
        self.assertTrue(result["running"])
        self.assertEqual(result["port"], 8080, "the running proxy is kept")
        self.assertEqual(result["requested"], {"port": 9090, "capture_bodies": True})
        self.assertEqual([w["code"] for w in result["warnings"]], ["proxy_already_running"])
        self.assertIn("9090", result["warnings"][0]["error"])
        self.assertIn("8080", result["warnings"][0]["error"])

    def test_the_same_settings_are_quiet(self) -> None:
        for kwargs in ({}, {"port": 8080}, {"capture_bodies": False}):
            result = self._start(**kwargs)
            self.assertTrue(result["already_running"])
            self.assertNotIn("warnings", result)


class IosAttachHonestyTests(unittest.TestCase):
    def test_manual_steps_route_through_the_macos_system_proxy(self) -> None:
        steps = device_proxy_ios.manual_steps(8080, Path("/tmp/ca.pem"))
        text = "\n".join(steps)
        self.assertNotIn("Wi-Fi", text, "the Simulator has no Wi-Fi settings pane")
        self.assertIn("System Settings", text)
        self.assertIn("Secure Web Proxy (HTTPS)", text)
        self.assertNotIn("  ", text, "no stray double spaces from string joins")
        self.assertEqual([step.split(".", 1)[0] for step in steps],
                         [str(n) for n in range(1, len(steps) + 1)])

    def test_attached_stays_unknown_with_a_separate_state(self) -> None:
        target = Target("ios", "UDID-1", "xcrun", {"udid": "UDID-1"})
        with tempfile.TemporaryDirectory() as tmp:
            record = {"artifacts_dir": tmp, "network": {}}
            with mock.patch.object(device_proxy_ios.consent, "require", return_value={}), \
                    mock.patch.object(device_proxy_ios.consent, "record"), \
                    mock.patch.object(device_proxy_ios.proxy_mod, "ca_certificate",
                                      return_value=None):
                result = device_proxy_ios.attach(target, record, port=8080,
                                                 acknowledged=True)
        self.assertEqual(result["attached"], "unknown")
        self.assertEqual(result["attach_state"], "manual")


if __name__ == "__main__":
    unittest.main()
