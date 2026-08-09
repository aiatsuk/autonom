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
    "--adb", "--simctl", "--idb", "--idb-host", "--idb-port",
})

# The selector set is shared by `ui find` and `ui tap`; the doc spells it out on
# find and refers to it on tap, which keeps the block readable.
SELECTOR_TOKEN = "[selector flags]"
SELECTOR_FLAGS = frozenset({
    "--text", "--desc", "--resource-id", "--class-name", "--package",
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


if __name__ == "__main__":
    unittest.main()
