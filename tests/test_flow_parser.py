"""Flow v1 parser: the accepted grammar, and every rejection with line/column.

The rejection table is the contract: each case asserts the ``reason`` slug
and the exact 1-based position, so error messages stay usable by agents and
humans alike. The corpus files under ``tests/fixtures/flowdsl/`` are the
accept cases — every construct §7 of the research document uses.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import errors  # noqa: E402
from autonom_lib.flow import parser  # noqa: E402

CORPUS = ROOT / "tests/fixtures/flowdsl"

_HEAD = "schema: autonom.dev/flow/v1\nname: x\n---\n"


def _parse(text: str):
    return parser.parse_document(text, "t.yaml")


class AcceptTests(unittest.TestCase):
    def test_corpus_files_parse(self) -> None:
        files = sorted(CORPUS.rglob("*.yaml"))
        self.assertGreaterEqual(len(files), 3, "corpus went missing")
        for file in files:
            with self.subTest(file=file.name):
                document = parser.parse_document(
                    file.read_text(encoding="utf-8"), str(file))
                self.assertIsInstance(document.header, parser.Mapping)
                self.assertIsInstance(document.commands, parser.Sequence)

    def test_positions_are_exact(self) -> None:
        document = _parse(_HEAD + "- tapOn:\n    selector:\n      text: Login\n")
        step = document.commands.items[0]
        key, value = step.pairs[0]
        self.assertEqual((key.line, key.col), (4, 3))
        selector_key, selector_value = value.pairs[0]
        text_key, text_value = selector_value.pairs[0]
        self.assertEqual((text_key.line, text_key.col), (6, 7))
        self.assertEqual((text_value.line, text_value.col), (6, 13))
        self.assertEqual(text_value.text, "Login")

    def test_quoting_and_comments(self) -> None:
        document = _parse(
            "schema: autonom.dev/flow/v1\n"
            "name: x  # trailing comment\n"
            "description: 'it''s here # not a comment'\n"
            "---\n"
            '- note: "tab\\there \\u00e9"\n'
        )
        self.assertEqual(document.header.get("name").text, "x")
        self.assertEqual(document.header.get("description").text,
                         "it's here # not a comment")
        note = document.commands.items[0].pairs[0][1]
        self.assertEqual(note.text, "tab\there é")
        self.assertEqual(note.style, "double")

    def test_inline_lists(self) -> None:
        document = _parse("schema: autonom.dev/flow/v1\nname: x\n"
                          "tags: [a, 'b c', \"d\"]\nempty: []\n---\n- back\n")
        self.assertEqual([s.text for s in document.header.get("tags").items],
                         ["a", "b c", "d"])
        self.assertEqual(document.header.get("empty").items, [])

    def test_interpolation_forms(self) -> None:
        document = _parse(_HEAD + "- inputText:\n    value: a ${VAR_1} $${literal}\n")
        value = document.commands.items[0].pairs[0][1].pairs[0][1]
        self.assertEqual(value.text, "a ${VAR_1} $${literal}")

    def test_urls_with_colons_stay_plain(self) -> None:
        document = _parse(_HEAD + "- openLink: https://example.com/a?b=1\n")
        self.assertEqual(document.commands.items[0].pairs[0][1].text,
                         "https://example.com/a?b=1")


class RejectTests(unittest.TestCase):
    CASES: list[tuple[str, str, str, int, int]] = [
        # (name, text, reason, line, col)
        ("no separator", "a: 1\n- x\n", "missing_separator", 2, 1),
        ("two separators", "a: 1\n---\n- x\n---\n- y\n", "multiple_separators", 4, 1),
        ("tab indent", "a: 1\n---\n\t- x\n", "tab_indent", 3, 1),
        ("directive", "%YAML 1.2\na: 1\n---\n- x\n", "directive", 1, 1),
        ("anchor", "a: &x 1\n---\n- y\n", "anchor", 1, 4),
        ("alias", "a: *x\n---\n- y\n", "alias", 1, 4),
        ("yaml tag", "a: !!str 1\n---\n- y\n", "tag", 1, 4),
        ("block scalar pipe", "a: |\n  x\n---\n- y\n", "block_scalar", 1, 4),
        ("block scalar fold", "a: >\n  x\n---\n- y\n", "block_scalar", 1, 4),
        ("flow mapping", "a: {b: 1}\n---\n- y\n", "flow_mapping", 1, 4),
        ("duplicate key", "a: 1\nb: 2\na: 3\n---\n- y\n", "duplicate_key", 3, 1),
        ("bad dedent", _HEAD + "- tapOn:\n    selector:\n      text: x\n   bad: 1\n",
         "bad_dedent", 7, 4),
        ("unterminated double", 'a: "open\n---\n- y\n', "unterminated_quote", 1, 4),
        ("unterminated single", "a: 'open\n---\n- y\n", "unterminated_quote", 1, 4),
        ("unknown escape", 'a: "b\\qc"\n---\n- y\n', "invalid_escape", 1, 6),
        ("bad interpolation", "a: ${9bad}\n---\n- y\n", "invalid_interpolation", 1, 4),
        ("unclosed interpolation", "a: ${OPEN\n---\n- y\n", "invalid_interpolation", 1, 4),
        ("seq at key column", "tags:\n- a\n---\n- y\n", "sequence_not_indented", 2, 1),
        ("missing value", "a:\n---\n- y\n", "missing_value", 1, 3),
        ("value then block", "a: 1\n  b: 2\n---\n- y\n", "unexpected_indent", 2, 3),
        ("nested inline list", "a: [1, [2]]\n---\n- y\n", "nested_flow_sequence", 1, 8),
        ("empty inline item", "a: [1, , 2]\n---\n- y\n", "empty_flow_item", 1, 7),
        ("unterminated list", "a: [1, 2\n---\n- y\n", "unterminated_flow_sequence", 1, 4),
        ("header is sequence", "- x\n---\n- y\n", "header_not_mapping", 1, 1),
        ("commands are mapping", "a: 1\n---\nfoo: bar\n", "commands_not_sequence", 3, 1),
        ("empty seq item", _HEAD + "-\n", "empty_sequence_item", 4, 1),
        ("item in mapping", "a: 1\n- x\n---\n- y\n", "item_in_mapping", 2, 1),
        ("key in sequence", _HEAD + "- back\nfoo: 1\n", "key_in_sequence", 5, 1),
        ("no space after colon", "a:1\n---\n- y\n", "missing_space_after_colon", 1, 3),
        ("invalid key", '"a b": 1\n---\n- y\n', "invalid_key", 1, 1),
        ("merge key", "<<: base\n---\n- y\n", "invalid_key", 1, 1),
        ("seq on value line", "a: - x\n---\n- y\n", "sequence_on_value_line", 1, 4),
        ("indented top level", "  a: 1\n---\n- y\n", "bad_indent", 1, 3),
        ("quote then junk", "a: 'x' y\n---\n- y\n", "trailing_content", 1, 8),
    ]

    def test_every_rejection_is_positioned(self) -> None:
        for name, text, reason, line, col in self.CASES:
            with self.subTest(case=name):
                with self.assertRaises(errors.AutonomError) as caught:
                    _parse(text)
                exc = caught.exception
                self.assertEqual(exc.code, errors.FLOW_PARSE_ERROR, name)
                self.assertEqual(exc.extra.get("reason"), reason, str(exc))
                self.assertEqual(
                    (exc.extra.get("line"), exc.extra.get("column")), (line, col),
                    f"{name}: {exc}")
                self.assertTrue(str(exc).startswith(f"t.yaml:{line}:{col}:"),
                                f"message not position-prefixed: {exc}")

    def test_fuzz_mutations_never_traceback(self) -> None:
        """Random single-character mutations must yield a positioned error or
        parse — never an unhandled exception."""
        base = (CORPUS / "login.yaml").read_text(encoding="utf-8")
        # deterministic pseudo-random positions: a simple LCG, no random module
        seed = 0x5EED
        for _ in range(300):
            seed = (seed * 1103515245 + 12345) % (2 ** 31)
            position = seed % len(base)
            seed = (seed * 1103515245 + 12345) % (2 ** 31)
            replacement = chr(32 + (seed % 90))
            mutated = base[:position] + replacement + base[position + 1:]
            try:
                parser.parse_document(mutated, "fuzz.yaml")
            except errors.AutonomError as exc:
                self.assertIn("line", exc.extra)
                self.assertIn("column", exc.extra)


if __name__ == "__main__":
    unittest.main()
