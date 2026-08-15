"""Flow v1 schema layer: typing, registry integrity, header and step rules."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import errors  # noqa: E402
from autonom_lib.flow import parser, schema  # noqa: E402

_HEAD = "schema: autonom.dev/flow/v1\nname: x\n---\n"


def _build(text: str) -> schema.Flow:
    return schema.build_flow(parser.parse_document(text, "t.yaml"))


def _rejects(testcase, text: str, code: str, contains: str = "") -> errors.AutonomError:
    with testcase.assertRaises(errors.AutonomError) as caught:
        _build(text)
    exc = caught.exception
    testcase.assertEqual(exc.code, code, str(exc))
    if contains:
        testcase.assertIn(contains, str(exc))
    testcase.assertIn("line", exc.extra, "schema errors must be positioned")
    testcase.assertIn("column", exc.extra)
    return exc


class RegistryIntegrityTests(unittest.TestCase):
    def test_every_command_declares_mutating_and_slice(self) -> None:
        for name, spec in schema.REGISTRY.items():
            with self.subTest(command=name):
                self.assertIsInstance(spec.mutating, bool)
                self.assertIn(spec.since,
                              ("0.20.0", "0.20.1", "0.20.2", "0.21.0"))

    def test_assertions_are_never_mutating(self) -> None:
        for spec in schema.REGISTRY.values():
            if spec.assertion:
                self.assertFalse(spec.mutating, spec.name)

    def test_every_flow_error_code_has_a_class(self) -> None:
        flow_codes = [value for name, value in vars(errors).items()
                      if name.startswith("FLOW_") and isinstance(value, str)
                      and value != errors.FLOW_NOT_FOUND]
        for code in flow_codes:
            with self.subTest(code=code):
                self.assertIn(
                    schema.failure_class(code),
                    (schema.TEST_FAILURE, schema.FLOW_DEFINITION,
                     schema.INFRASTRUCTURE))

    def test_the_dsl_never_uses_the_network_code(self) -> None:
        """flow_not_found belongs to network capture forever (COMPATIBILITY.md)."""
        flow_dir = ROOT / "scripts/autonom_lib/flow"
        for file in flow_dir.glob("*.py"):
            self.assertNotIn("FLOW_NOT_FOUND", file.read_text(encoding="utf-8"),
                             f"{file.name} references the network-owned code")


class HeaderTests(unittest.TestCase):
    def test_schema_field_is_mandatory_and_pinned(self) -> None:
        _rejects(self, "name: x\n---\n- back\n", errors.FLOW_SCHEMA_UNSUPPORTED)
        _rejects(self, "schema: autonom.dev/flow/v2\nname: x\n---\n- back\n",
                 errors.FLOW_SCHEMA_UNSUPPORTED, "autonom.dev/flow/v1")

    def test_unknown_header_field(self) -> None:
        _rejects(self, "schema: autonom.dev/flow/v1\nname: x\nbogus: 1\n---\n- back\n",
                 errors.FLOW_HEADER_INVALID, "bogus")

    def test_name_is_required(self) -> None:
        _rejects(self, "schema: autonom.dev/flow/v1\n---\n- back\n",
                 errors.FLOW_HEADER_INVALID, "name")

    def test_empty_commands_is_an_error(self) -> None:
        _rejects(self, "schema: autonom.dev/flow/v1\nname: x\n---\n",
                 errors.FLOW_COMMAND_INVALID, "no commands")

    def test_platform_and_evidence_enums(self) -> None:
        _rejects(self, "schema: autonom.dev/flow/v1\nname: x\n"
                       "requires:\n  platform: [windows]\n---\n- back\n",
                 errors.FLOW_HEADER_INVALID, "windows")
        _rejects(self, "schema: autonom.dev/flow/v1\nname: x\n"
                       "evidence:\n  mode: sometimes\n---\n- back\n",
                 errors.FLOW_HEADER_INVALID, "sometimes")
        _rejects(self, "schema: autonom.dev/flow/v1\nname: x\n"
                       "evidence:\n  collect:\n    - screenshots\n---\n- back\n",
                 errors.FLOW_HEADER_INVALID, "screenshots")

    def test_env_names_are_interpolation_shaped(self) -> None:
        # ('9BAD' is already a parse error — keys must start with a letter —
        # so the schema-level check guards the names the parser lets through.)
        _rejects(self, "schema: autonom.dev/flow/v1\nname: x\n"
                       "env:\n  MY-VAR: x\n---\n- back\n",
                 errors.FLOW_HEADER_INVALID, "MY-VAR")


class StepTests(unittest.TestCase):
    def test_unknown_command_is_never_ignored(self) -> None:
        _rejects(self, _HEAD + "- frobnicate\n", errors.FLOW_UNKNOWN_COMMAND)
        _rejects(self, _HEAD + "- frobnicate:\n    a: 1\n",
                 errors.FLOW_UNKNOWN_COMMAND)

    def test_deferred_commands_reject_with_a_pointed_hint(self) -> None:
        exc = _rejects(self, _HEAD + "- runScript: x.js\n",
                       errors.FLOW_UNKNOWN_COMMAND)
        self.assertIn("script engine", exc.hint)
        exc = _rejects(self, _HEAD + "- extendedWaitUntil:\n    timeout: 1\n",
                       errors.FLOW_UNKNOWN_COMMAND)
        self.assertIn("waitUntil", exc.hint)

    def test_one_command_per_item(self) -> None:
        _rejects(self, _HEAD + "- tapOn: a\n  label: b\n",
                 errors.FLOW_COMMAND_INVALID, "one command per")

    def test_bool_typing_is_positional_not_guessed(self) -> None:
        self.assertTrue(_build(_HEAD + "- launchApp:\n    clearState: true\n")
                        .steps[0].args["clearState"] is True)
        _rejects(self, _HEAD + '- launchApp:\n    clearState: "true"\n',
                 errors.FLOW_COMMAND_INVALID, "quoted")
        _rejects(self, _HEAD + "- launchApp:\n    clearState: yes\n",
                 errors.FLOW_COMMAND_INVALID)
        # the Norway problem stays dead: a string arg keeps its text
        flow = _build(_HEAD + "- note: false\n")
        self.assertEqual(flow.steps[0].args["text"], "false")

    def test_required_and_unknown_arguments(self) -> None:
        _rejects(self, _HEAD + "- tapOn\n", errors.FLOW_COMMAND_INVALID, "selector")
        _rejects(self, _HEAD + "- openLink:\n    label: x\n",
                 errors.FLOW_COMMAND_INVALID, "url")
        _rejects(self, _HEAD + "- tapOn:\n    selector:\n      id: a\n    frob: 1\n",
                 errors.FLOW_COMMAND_INVALID, "frob")

    def test_choices_apply_to_shorthand_too(self) -> None:
        _rejects(self, _HEAD + "- swipe: diagonal\n",
                 errors.FLOW_COMMAND_INVALID, "up, down, left, right")

    def test_wait_until_takes_exactly_one_condition(self) -> None:
        _rejects(self, _HEAD + "- waitUntil:\n    timeoutMs: 5\n",
                 errors.FLOW_COMMAND_INVALID, "exactly one")
        text = (_HEAD + "- waitUntil:\n    visible:\n      id: a\n"
                        "    notVisible:\n      id: b\n    timeoutMs: 5\n")
        _rejects(self, text, errors.FLOW_COMMAND_INVALID, "exactly one")

    def test_optional_rules(self) -> None:
        _rejects(self, _HEAD + "- tapOn:\n    selector:\n      id: a\n"
                               "    optional: true\n",
                 errors.FLOW_OPTIONAL_ASSERTION_FORBIDDEN, "reason")
        # optional on an assertion is refused at the argument level already
        _rejects(self, _HEAD + "- assertVisible:\n    selector:\n      id: a\n"
                               "    optional: true\n",
                 errors.FLOW_COMMAND_INVALID, "optional")
        flow = _build(_HEAD + "- tapOn:\n    selector:\n      id: a\n"
                              "    optional: true\n    reason: external dialog\n")
        self.assertTrue(flow.steps[0].args["optional"])

    def test_when_is_runflow_only(self) -> None:
        text = (_HEAD + "- tapOn:\n    selector:\n      id: a\n"
                        "    when:\n      platform: android\n")
        _rejects(self, text, errors.FLOW_COMMAND_INVALID, "when")


class SelectorTests(unittest.TestCase):
    def test_field_mapping_to_selector_keys(self) -> None:
        flow = _build(_HEAD + "- tapOn:\n    selector:\n      id: a\n"
                              "      text: b\n      description: c\n      role: d\n"
                              "      enabled: true\n")
        selector = flow.steps[0].selector
        self.assertEqual(selector.fields, {
            "resource_id": "a", "text": "b", "desc": "c", "role": "d",
            "enabled": True,
        })

    def test_match_modes_map_onto_selector_py(self) -> None:
        self.assertEqual(schema.MATCH_MODES["exact"], ("exact", True))
        self.assertEqual(schema.MATCH_MODES["caseInsensitiveExact"], ("exact", False))
        self.assertEqual(schema.MATCH_MODES["contains"], ("contains", True))
        self.assertEqual(schema.MATCH_MODES["regex"], ("regex", True))
        flow = _build(_HEAD + "- tapOn: Login\n")
        self.assertEqual(flow.steps[0].selector.match, "exact")

    def test_relational_fields_build_engine_specs(self) -> None:
        flow = _build(_HEAD + "- tapOn:\n    selector:\n      text: Settings\n"
                              "      leftOf:\n        id: anchor_id\n"
                              "        match: contains\n")
        selector = flow.steps[0].selector
        self.assertEqual(selector.relations["left_of"], {
            "fields": {"resource_id": "anchor_id"},
            "mode": "contains", "case_sensitive": True,
        })
        self.assertIn("leftOf", selector.source_relations)

    def test_relational_anchor_rules(self) -> None:
        # anchors carry no index and no nested relations
        _rejects(self, _HEAD + "- tapOn:\n    selector:\n      text: a\n"
                               "      below:\n        id: x\n        index: 0\n",
                 errors.FLOW_SELECTOR_INVALID, "index")
        _rejects(self, _HEAD + "- tapOn:\n    selector:\n      text: a\n"
                               "      below:\n        id: x\n"
                               "        above:\n          id: y\n",
                 errors.FLOW_SELECTOR_INVALID, "nest")
        # a relation alone is a legal selector; a bare state field is not
        flow = _build(_HEAD + "- assertVisible:\n    selector:\n"
                              "      childOf:\n        id: list\n")
        self.assertIn("child_of", flow.steps[0].selector.relations)

    def test_focused_is_a_selector_field(self) -> None:
        flow = _build(_HEAD + "- assertVisible:\n    selector:\n"
                              "      id: email\n      focused: true\n")
        self.assertIs(flow.steps[0].selector.fields["focused"], True)

    def test_retry_static_rules(self) -> None:
        _rejects(self, _HEAD + "- retry:\n    maxAttempts: 4\n    commands:\n"
                               "      - assertVisible:\n          selector:\n"
                               "            id: a\n",
                 errors.FLOW_COMMAND_INVALID, "between 1 and 3")
        _rejects(self, _HEAD + "- retry:\n    commands:\n"
                               "      - tapOn:\n          selector:\n"
                               "            id: a\n",
                 errors.FLOW_COMMAND_INVALID, "allowMutations")
        _rejects(self, _HEAD + "- retry:\n    commands:\n"
                               "      - retry:\n          commands:\n"
                               "            - assertVisible:\n"
                               "                selector:\n"
                               "                  id: a\n",
                 errors.FLOW_COMMAND_INVALID, "retry cannot contain")
        flow = _build(_HEAD + "- retry:\n    allowMutations: true\n"
                              "    onlyOn:\n      - flow_assertion_timeout\n"
                              "    commands:\n"
                              "      - tapOn:\n          selector:\n"
                              "            id: a\n")
        self.assertEqual(flow.steps[0].args["maxAttempts"], 2)

    def test_group_rules(self) -> None:
        _rejects(self, _HEAD + "- group:\n    commands:\n      - back\n",
                 errors.FLOW_COMMAND_INVALID, "label")
        _rejects(self, _HEAD + "- group:\n    label: outer\n    commands:\n"
                               "      - group:\n          label: inner\n"
                               "          commands:\n            - back\n",
                 errors.FLOW_COMMAND_INVALID, "nest")

    def test_state_only_selector_is_too_broad(self) -> None:
        _rejects(self, _HEAD + "- tapOn:\n    selector:\n      enabled: true\n",
                 errors.FLOW_SELECTOR_INVALID, "at least one")

    def test_unknown_selector_field(self) -> None:
        _rejects(self, _HEAD + "- tapOn:\n    selector:\n      texts: x\n",
                 errors.FLOW_SELECTOR_INVALID, "texts")


if __name__ == "__main__":
    unittest.main()
