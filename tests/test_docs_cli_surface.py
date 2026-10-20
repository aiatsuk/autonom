"""The CLI surface in `docs/CAPABILITIES.md` is checked against the real parser.

Documentation drifts silently. `shots list|show` shipped and was absent from the
docs for several releases; `session start --log-stream`, `network attach
--install-ca`, and `--mocked-only` were all reachable and undocumented. Nothing
failed, because nothing compared the two.

So this compares them. `build_parser()` is walked for every leaf verb and every
option it accepts, the fenced `bash` block under "## CLI surface" is parsed back
into the same shape, and the two must agree in both directions: an undocumented
verb fails, and so does a documented verb that no longer exists.

The doc is prose first — it uses `|` alternatives, `[optional]` groups, and
`<placeholders>` — so parsing strips those before comparing. What it must not do
is let a verb hide inside decoration, which is why the tests below assert on the
extraction itself as well as on the comparison.
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs/CAPABILITIES.md"

# Repeated on every leaf by `target_flags_parent()`, and documented once as a
# sentence above the block rather than 40 times inside it.
TARGET_FLAGS = frozenset({
    "--platform", "--target", "--serial", "--udid",
    "--adb", "--simctl", "--idb", "--idb-host", "--idb-port", "--session-id", "--ui-backend",
})

# The selector set is shared by `ui find` and `ui tap`; the doc spells it out on
# find and refers to it on tap, which keeps the block readable.
SELECTOR_TOKEN = "[selector flags]"
SELECTOR_FLAGS = frozenset({
    "--text", "--desc", "--resource-id", "--class-name", "--package", "--role",
    "--clickable", "--enabled", "--mode", "--case-sensitive", "--index",
})

# Verbs the parser accepts that the doc writes as one optional word rather than
# a second entry: `autonom devices [list]` is the same command either way.
ALIASES = {"devices list": "devices"}


def load_cli():
    spec = importlib.util.spec_from_file_location("autonom_cli", ROOT / "scripts/autonom.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def parser_surface(parser: argparse.ArgumentParser) -> dict[str, set[str]]:
    """Every leaf verb path -> the long options it accepts (target flags aside)."""
    surface: dict[str, set[str]] = {}

    def walk(node: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        subparsers = [
            action for action in node._actions  # noqa: SLF001 - argparse exposes no public walk
            if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
        ]
        options = {
            option
            for action in node._actions  # noqa: SLF001
            for option in action.option_strings
            if option.startswith("--") and option not in TARGET_FLAGS and option != "--help"
        }
        if not subparsers:
            surface[" ".join(path)] = options
            return
        # `devices` is both a verb and a group: bare `autonom devices` lists.
        if node._defaults.get("func") is not None:  # noqa: SLF001
            surface.setdefault(" ".join(path), options)
        seen: set[int] = set()
        for action in subparsers:
            for name, child in action.choices.items():
                if id(child) in seen:
                    continue
                seen.add(id(child))
                walk(child, path + (name,))

    for action in parser._actions:  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for name, child in action.choices.items():
                walk(child, (name,))
    for alias, canonical in ALIASES.items():
        if alias in surface and canonical in surface:
            surface[canonical] |= surface.pop(alias)
    return surface


def split_alternatives(entry: str) -> list[str]:
    """Split on `|` at bracket depth 0 only.

    `[--mode exact|contains|regex]` is one option with three values, while
    `network detach|stop|status` is three commands. A naive split cannot tell
    them apart, and getting it wrong silently attributes every flag on the line
    to every verb on the line — which is exactly how a missing flag would hide.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in entry:
        if char in "[<":
            depth += 1
        elif char in "]>":
            depth = max(0, depth - 1)
        if char == "|" and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def leading_words(part: str) -> list[str]:
    """The bare command words at the start of an alternative, decoration removed."""
    bare = re.sub(r"\[[^\[\]]*\]|<[^<>]*>", " ", part)
    collected: list[str] = []
    for token in bare.split():
        if not re.fullmatch(r"[a-z][a-z0-9-]*", token):
            break
        collected.append(token)
    return collected


def documented_surface() -> dict[str, set[str]]:
    """Parse the fenced block under '## CLI surface' back into verb -> flags."""
    text = DOC.read_text(encoding="utf-8")
    section = text.split("## CLI surface", 1)
    if len(section) != 2:
        raise AssertionError("docs/CAPABILITIES.md has no '## CLI surface' section")
    block = re.search(r"```bash\n(.*?)```", section[1], re.DOTALL)
    if not block:
        raise AssertionError("the CLI surface section has no fenced bash block")

    # Join continuation lines: a line that does not start a new command belongs
    # to the one above it, so a wrapped flag list is not read as a new verb.
    entries: list[str] = []
    for raw in block.group(1).splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("autonom "):
            entries.append(line[len("autonom "):].strip())
        elif entries:
            entries[-1] += " " + line.strip()

    surface: dict[str, set[str]] = {}
    for entry in entries:
        alternatives = split_alternatives(entry)
        if not alternatives:
            continue
        first = leading_words(alternatives[0])
        if not first:
            continue
        prefix = first[:-1]
        last = " ".join(first)
        for index, alternative in enumerate(alternatives):
            flags = {
                flag for flag in re.findall(r"--[a-z][a-z0-9-]*", alternative)
                if flag not in TARGET_FLAGS
            }
            if SELECTOR_TOKEN in alternative:
                flags |= SELECTOR_FLAGS
            tail = leading_words(alternative)
            if index and tail:
                # `network mock list | show <id>` means `network mock show`; but
                # `ui pinch … | ui rotate` already carries its own prefix.
                last = " ".join(tail if len(tail) > 1 or not prefix else prefix + tail)
            elif index:
                # A trailing alternative of pure flags — `ui tap … | [--x X --y Y]`
                # — belongs to the verb before it, not to a new one.
                pass
            surface.setdefault(last, set()).update(flags)
    for alias, canonical in ALIASES.items():
        if alias in surface:
            surface.setdefault(canonical, set()).update(surface.pop(alias))
    return surface


class DocumentedCliSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.parser = parser_surface(load_cli().build_parser())
        cls.documented = documented_surface()

    def test_extraction_finds_the_obvious_verbs(self) -> None:
        """Guard the parser above: a silent extraction failure would pass everything."""
        for verb in ("version", "doctor", "ui tree", "network mock clear", "shots show"):
            self.assertIn(verb, self.documented, f"doc extraction lost '{verb}'")
        self.assertGreater(len(self.documented), 40)

    def test_every_verb_is_documented(self) -> None:
        missing = sorted(set(self.parser) - set(self.documented))
        self.assertEqual(
            missing, [],
            "these verbs exist in the CLI but not in the docs/CAPABILITIES.md "
            f"CLI surface block: {missing}",
        )

    def test_no_documented_verb_has_been_removed(self) -> None:
        stale = sorted(set(self.documented) - set(self.parser))
        self.assertEqual(
            stale, [],
            f"docs/CAPABILITIES.md documents verbs the CLI no longer has: {stale}",
        )

    def test_every_flag_is_documented(self) -> None:
        gaps = {
            verb: sorted(flags - self.documented.get(verb, set()))
            for verb, flags in self.parser.items()
            if flags - self.documented.get(verb, set())
        }
        self.assertEqual(gaps, {}, f"undocumented flags: {gaps}")

    def test_no_documented_flag_has_been_removed(self) -> None:
        gaps = {
            verb: sorted(flags - self.parser.get(verb, set()))
            for verb, flags in self.documented.items()
            if verb in self.parser and flags - self.parser.get(verb, set())
        }
        self.assertEqual(gaps, {}, f"documented flags the CLI does not accept: {gaps}")


class PretestDocsTests(unittest.TestCase):
    """tp-027: every `pretest` verb is documented in CAPABILITIES and walked
    through in USAGE, and every pre-test error code is in the CAPABILITIES table."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = load_cli()
        cls.verbs = sorted(verb for verb in parser_surface(cls.cli.build_parser())
                           if verb.startswith("pretest "))

    def test_the_parser_has_the_pretest_verbs(self) -> None:
        for verb in ("pretest start", "pretest check end", "pretest finding add",
                     "pretest review", "pretest kit"):
            self.assertIn(verb, self.verbs)

    def test_every_pretest_verb_is_in_capabilities_and_usage(self) -> None:
        documented = documented_surface()
        usage = (ROOT / "docs/USAGE.md").read_text(encoding="utf-8")
        for verb in self.verbs:
            self.assertIn(verb, documented, f"'{verb}' is not in the CAPABILITIES CLI block")
            self.assertTrue(f"autonom {verb}" in usage, f"docs/USAGE.md never shows '{verb}'")

    def test_every_pretest_error_code_is_documented(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        section = text.split("### Ticket pre-test (`autonom pretest`)", 1)
        self.assertEqual(len(section), 2, "CAPABILITIES has no pre-test section")
        errors = self.cli.errors
        for name in ("NO_ACTIVE_PRETEST", "PRETEST_ALREADY_ACTIVE", "PRETEST_FINALIZED",
                     "CHECK_ALREADY_OPEN", "NO_OPEN_CHECK", "UNKNOWN_CHECK",
                     "INVALID_VERDICT", "UNKNOWN_FINDING", "INVALID_SEVERITY",
                     "EVIDENCE_REF_UNKNOWN", "BUDGET_EXHAUSTED", "FORBIDDEN_ACTION",
                     "APP_NOT_ALLOWED", "REVIEW_NEEDS_FINDING", "NOT_AUTOMATIC",
                     "PROFILE_INVALID", "NOT_A_PRETEST_PACK", "DRAFT_UNAVAILABLE",
                     "PRETEST_DAMAGED", "RACE_NEEDS_COUNT", "CHECK_NOT_ENDED", "GIT_MISSING"):
            code = getattr(errors, name)
            self.assertIn(f"`{code}`", section[1].split("\n## ", 1)[0], code)

    def test_plan_status_and_changelog(self) -> None:
        plan = (ROOT / "docs/plans/PHASE_7_TICKET_PRETEST.md").read_text(encoding="utf-8")
        self.assertIn("**Status:** implemented", plan)
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        # Unreleased until the release that ships it, then its 0.36.0 section.
        sections = {}
        for chunk in changelog.split("\n## [")[1:]:
            name, _, body = chunk.partition("]")
            sections[name] = body
        self.assertIn("autonom pretest",
                      sections.get("Unreleased", "") + sections.get("0.36.0", ""))


class FollowupDocsTests(unittest.TestCase):
    """The pre-test follow-ups: the new verb and flags are in the CLI block
    (checked both ways above), walked through in USAGE, and each item has a
    line in the changelog section that shipped them."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.surface = parser_surface(load_cli().build_parser())
        cls.documented = documented_surface()
        cls.usage = (ROOT / "docs/USAGE.md").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        sections = {}
        for chunk in changelog.split("\n## [")[1:]:
            name, _, body = chunk.partition("]")
            sections[name] = body
        cls.release_notes = sections.get("Unreleased", "") + sections.get("0.36.2", "")

    def test_new_flags_exist_and_are_documented(self) -> None:
        expected = {
            "pretest check amend": {"--verdict", "--note", "--observed-count"},
            "pretest check end": {"--observed-count"},
            "pretest stop": {"--force"},
            "network mock add": {"--delay", "--hang"},
            "network mock update": {"--delay", "--hang"},
        }
        for verb, flags in expected.items():
            with self.subTest(verb=verb):
                self.assertTrue(flags <= self.surface[verb], self.surface.get(verb))
                self.assertTrue(flags <= self.documented[verb], self.documented.get(verb))

    def test_usage_walks_through_them(self) -> None:
        for text in ("autonom pretest check amend chk-1 --verdict blocked",
                     "--observed-count 1", "--delay 3", "--hang",
                     "autonom pretest stop --force", "pretest_damaged",
                     "race_needs_count", "check_not_ended"):
            self.assertIn(text, self.usage)

    def test_release_notes_name_every_item(self) -> None:
        self.assertIn("### Added", self.release_notes)
        self.assertIn("### Changed", self.release_notes)
        self.assertIn("### Fixed", self.release_notes)
        for text in ("--delay SECONDS", "--hang", "completed: false", "check amend",
                     "--observed-count", "stop --force", "verified_reason",
                     "is for <platform>", "connection_strategy=lazy", "Content-Type",
                     "rotation", "evidence `always`", "small-target", "origin",
                     "printed once", "clipboard_unavailable", "pretest_damaged",
                     "ui swipe --duration", "report verify <pack>", "shot:<n>",
                     "git_missing", "skipped_untracked", "not UTF-8", "not an object",
                     "--out` can be written", "--ticket <dir>", "--env NAME",
                     "flow-draft --out ''", "--adb <path>"):
            self.assertIn(text, self.release_notes, text)


SKILL_DIR = ROOT / "plugins/autonom/skills/ticket-pretest"


def _load_kit():
    spec = importlib.util.spec_from_file_location(
        "autonom_pretest_kit", ROOT / "scripts/autonom_lib/pretest/kit.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class KitDocsTests(unittest.TestCase):
    """The ticket-pretest skill's kit table and kit reference mirror `kit.py`."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.kit = _load_kit().KIT
        cls.skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        cls.reference = (SKILL_DIR / "references/edge-case-kit.md").read_text(encoding="utf-8")

    def table_rows(self) -> dict[str, tuple[str, list[str]]]:
        section = self.skill.split("## The edge-case kit", 1)[1].split("\n## ", 1)[0]
        rows: dict[str, tuple[str, list[str]]] = {}
        for line in section.splitlines():
            match = re.match(r"^\| `([a-z-]+)` \| ([^|]+) \| (.+) \|$", line)
            if match:
                rows[match.group(1)] = (match.group(2).strip(),
                                        re.findall(r"`([^`]+)`", match.group(3)))
        return rows

    def reference_section(self, case_id: str) -> str:
        parts = self.reference.split(f"### `{case_id}` — ", 1)
        self.assertEqual(len(parts), 2, f"the kit reference has no section for {case_id}")
        return parts[1].split("\n### ", 1)[0].split("\n## ", 1)[0]

    def test_the_skill_table_lists_every_case_with_the_kit_verbs(self) -> None:
        rows = self.table_rows()
        self.assertEqual(list(rows), [case["id"] for case in self.kit])
        for case in self.kit:
            with self.subTest(case=case["id"]):
                self.assertEqual(rows[case["id"]], (case["title"], case["verbs"]))

    def test_slow_and_timeout_name_the_mock_verbs_and_no_shaping(self) -> None:
        rows = self.table_rows()
        for case in self.kit:
            if case["id"] not in ("slow", "timeout"):
                continue
            with self.subTest(case=case["id"]):
                section = self.reference_section(case["id"])
                self.assertTrue(section.startswith(case["title"]), section[:80])
                flat = " ".join(section.split())  # verbs may wrap across lines
                for verb in case["verbs"]:
                    self.assertIn(f"`{verb}`", flat, verb)
                self.assertNotIn("simulator network shape", section)
                self.assertNotIn("simulator network shape", " ".join(rows[case["id"]][1]))
                self.assertFalse(any("simulator network shape" in verb
                                     for verb in case["verbs"]))


class UnreleasedNotesTests(unittest.TestCase):
    """Each follow-up after 0.36.2 has a line in the changelog's Unreleased section."""

    def test_unreleased_names_every_followup(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        sections = {}
        for chunk in changelog.split("\n## [")[1:]:
            name, _, body = chunk.partition("]")
            sections[name] = body
        notes = " ".join(sections.get("Unreleased", "").split())
        for text in ("`slow` and `timeout`", "--delay 3", "`--hang`",
                     "never completed (<error_kind>)", "status None",
                     "Canvas", "pretest warmup", "pretest_damaged",
                     "docs/COMPATIBILITY.md", "clipboard_unavailable",
                     "race_needs_count", "check_not_ended", "git_missing",
                     "inline `--json`", "Content-Type: application/json",
                     "also next to other headers", "header-only `network mock update`"):
            self.assertIn(text, notes, text)


if __name__ == "__main__":
    unittest.main()
