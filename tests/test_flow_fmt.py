"""Canonical emitter contracts: round-trip, idempotence, quoting.

Two properties hold over the whole corpus, and they are what make ``flow
fmt`` safe to run blindly: re-parsing the emission yields a structurally
identical flow (fingerprint equality), and formatting an already-canonical
file changes nothing (text equality).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib.flow import canonical, parser, schema  # noqa: E402

CORPUS = ROOT / "tests/fixtures/flowdsl"


def _build(text: str, name: str = "t.yaml") -> schema.Flow:
    return schema.build_flow(parser.parse_document(text, name))


class RoundTripTests(unittest.TestCase):
    def test_corpus_round_trips_and_is_idempotent(self) -> None:
        files = sorted(CORPUS.rglob("*.yaml")) + [
            ROOT / "tests/fixtures/flows/settings_smoke.yaml"]
        for file in files:
            with self.subTest(file=file.name):
                flow = _build(file.read_text(encoding="utf-8"), str(file))
                emitted = canonical.emit_flow(flow)
                reparsed = _build(emitted, str(file))
                self.assertEqual(canonical.fingerprint(flow),
                                 canonical.fingerprint(reparsed))
                self.assertEqual(emitted, canonical.emit_flow(reparsed),
                                 "fmt is not idempotent")

    def test_shorthand_expands_with_match_materialized(self) -> None:
        emitted = canonical.emit_flow(_build(
            "schema: autonom.dev/flow/v1\nname: x\n---\n- tapOn: Login\n"))
        self.assertIn("- tapOn:\n    selector:\n      text: Login\n"
                      "      match: exact\n", emitted)

    def test_bare_commands_stay_bare(self) -> None:
        emitted = canonical.emit_flow(_build(
            "schema: autonom.dev/flow/v1\nname: x\n---\n- launchApp\n- back\n"))
        self.assertIn("- launchApp\n- back\n", emitted)

    def test_awkward_strings_survive(self) -> None:
        """Strings the grammar would misread must come back quoted but equal."""
        awkward = [
            "has # hash", "[starts-bracket", "{starts-brace", "-", "- leads",
            "trailing space ", "  leading", 'quo"te', "new\nline", "tab\there",
            "key: shaped", "#leading-hash", "'single'", "",
        ]
        flow = _build("schema: autonom.dev/flow/v1\nname: x\n---\n- back\n")
        flow.steps = [schema.Step("note", {"text": text}) for text in awkward]
        flow.tags = [t for t in awkward if t.strip(" ")]
        emitted = canonical.emit_flow(flow)
        reparsed = _build(emitted)
        self.assertEqual([s.args["text"] for s in reparsed.steps], awkward)
        self.assertEqual(reparsed.tags, flow.tags)

    def test_evidence_defaults_are_not_materialized(self) -> None:
        text = ("schema: autonom.dev/flow/v1\nname: x\n"
                "evidence:\n  mode: always\n---\n- back\n")
        emitted = canonical.emit_flow(_build(text))
        self.assertIn("evidence:\n  mode: always\n", emitted)
        self.assertNotIn("collect", emitted)
        self.assertNotIn("beforeMutation", emitted)

    def test_small_floats_never_go_scientific(self) -> None:
        """str(1e-05) would emit '1e-05', which the decimal-only grammar
        refuses — fmt must never write what parse cannot read back."""
        text = ("schema: autonom.dev/flow/v1\nappId: a.b\nname: x\n---\n"
                "- setLocation:\n    latitude: 0.00001\n"
                "    longitude: -122.4194\n")
        flow = _build(text)
        emitted = canonical.emit_flow(flow)
        self.assertIn("latitude: 0.00001\n", emitted)
        self.assertNotIn("e-", emitted)
        reparsed = _build(emitted)
        self.assertEqual(reparsed.steps[0].args["latitude"], 0.00001)

    def test_no_label_is_invented(self) -> None:
        emitted = canonical.emit_flow(_build(
            "schema: autonom.dev/flow/v1\nname: x\n---\n- tapOn: Login\n"))
        self.assertNotIn("label", emitted)


if __name__ == "__main__":
    unittest.main()
