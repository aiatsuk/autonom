"""docs/FLOW.md's language-surface block vs the real command registry.

The same discipline as ``test_docs_cli_surface.py``: enumerate the actual
thing from code (here ``autonom_lib.flow.schema``'s tables rather than
argparse), parse the doc's fenced block back into the same shape, and assert
both directions — plus a meta-guard so a silently broken extractor cannot
make everything pass vacuously.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib.flow import schema  # noqa: E402

DOC = ROOT / "docs/FLOW.md"
_HEADING = "## Language surface"


def documented_surface() -> dict:
    text = DOC.read_text(encoding="utf-8")
    if _HEADING not in text:
        raise AssertionError(f"docs/FLOW.md lost its '{_HEADING}' heading")
    section = text.split(_HEADING, 1)[1]
    fence = re.search(r"```text\n(.*?)```", section, re.S)
    if not fence:
        raise AssertionError("no ```text fence under the language-surface heading")
    surface: dict = {"commands": {}, "lists": {}}
    for line in fence.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        head, _, rest = line.partition(":")
        values = rest.split()
        if head.startswith("command "):
            surface["commands"][head.split()[1]] = values
        else:
            surface["lists"][head] = values
    return surface


def registry_surface() -> dict:
    return {
        "commands": {name: [arg.name for arg in spec.args]
                     for name, spec in schema.REGISTRY.items()},
        "lists": {
            "header": list(schema.HEADER_FIELDS),
            "selector-strings": list(schema.SELECTOR_STRING_FIELDS),
            "selector-bools": list(schema.SELECTOR_BOOL_FIELDS),
            "selector-relational": list(schema.SELECTOR_RELATIONAL_FIELDS),
            "match-modes": list(schema.MATCH_MODES),
            "deferred": list(schema.DEFERRED_COMMANDS),
            "requires-capabilities": list(schema.KNOWN_CAPABILITIES),
        },
    }


class FlowSurfaceTests(unittest.TestCase):
    def test_extraction_finds_the_obvious_commands(self) -> None:
        documented = documented_surface()
        self.assertGreaterEqual(len(documented["commands"]), 15)
        for name in ("tapOn", "assertVisible", "runFlow"):
            self.assertIn(name, documented["commands"])

    def test_every_registry_command_is_documented(self) -> None:
        documented = documented_surface()["commands"]
        actual = registry_surface()["commands"]
        self.assertEqual(sorted(actual) , sorted(documented),
                         "command set drifted between schema.REGISTRY and docs/FLOW.md")
        for name, args in actual.items():
            self.assertEqual(args, documented[name],
                             f"argument list for {name} drifted")

    def test_field_lists_match(self) -> None:
        documented = documented_surface()["lists"]
        actual = registry_surface()["lists"]
        self.assertEqual(sorted(actual), sorted(documented),
                         "surface list names drifted")
        for name, values in actual.items():
            self.assertEqual(values, documented[name], f"list {name!r} drifted")

    def test_every_command_has_a_mutating_flag_and_failure_classes_exist(self) -> None:
        for name, spec in schema.REGISTRY.items():
            self.assertIsInstance(spec.mutating, bool, name)
        self.assertEqual(schema.failure_class("flow_assertion_timeout"),
                         schema.TEST_FAILURE)
        self.assertEqual(schema.failure_class("flow_parse_error"),
                         schema.FLOW_DEFINITION)
        # unknown codes never blame the app
        self.assertEqual(schema.failure_class("some_future_code"),
                         schema.INFRASTRUCTURE)


if __name__ == "__main__":
    unittest.main()
