"""Canonical Flow v1 emitter: typed model → deterministic YAML-subset text.

Powers ``flow fmt``. Guarantees, both enforced by tests:

- everything emitted here parses under ``parser.py`` (no other YAML features
  are ever produced);
- ``emit ∘ parse`` is idempotent, and re-parsing the emission yields a model
  with the same fingerprint (positions aside).

Canonical form: 2-space indent, block style for all non-empty lists, fixed
key order from the schema specs, shorthand commands expanded
(``- tapOn: Login`` → the selector form with ``match: exact`` materialized),
bare commands kept bare, minimal double-quoting only where the grammar
requires it. ``label:`` is never invented — labels stay authorial (D2).
"""
from __future__ import annotations

import re

from . import FLOW_SCHEMA_ID
from .parser import _KEY_RE
from .schema import (
    Evidence,
    Flow,
    FlowSelector,
    REGISTRY,
    Step,
    WhenClause,
)

_HEADER_ORDER = ("schema", "id", "appId", "name", "description", "tags",
                 "properties", "env", "requires", "evidence",
                 "onFlowStart", "onFlowComplete")
_SELECTOR_ORDER = ("id", "text", "description", "role",
                   "enabled", "checked", "selected")
_EVIDENCE_ORDER = ("mode", "beforeMutation", "afterAssertion", "collect", "bodies")
_PLAIN_SAFE_FIRST = re.compile(r"[^\[\]{}&*!|>#%'\"@`?,\s-]")


def _scalar(value, item_context: bool = False) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if _needs_quote(text, item_context):
        escaped = (text.replace("\\", "\\\\").replace('"', '\\"')
                   .replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r"))
        return f'"{escaped}"'
    return text


def _needs_quote(text: str, item_context: bool) -> bool:
    if text == "" or text != text.strip(" "):
        return True
    if any(ch in text for ch in "\n\t\r"):
        return True
    if not _PLAIN_SAFE_FIRST.match(text[0]):
        # covers [, {, }, ], &, *, !, |, >, #, %, quotes, @, `, ?, comma,
        # whitespace, and a leading '-' (safe forms like -5 re-allowed below)
        if not (text[0] == "-" and len(text) > 1 and text[1] != " "):
            return True
    if " #" in text:
        return True
    if text in ("true", "false") or text.lstrip("-").isdigit():
        # keep strings that look like other types unambiguous for humans;
        # (typing is positional, so this is cosmetic consistency)
        return False
    if item_context:
        match = _KEY_RE.match(text)
        if match and (match.group(2) == "" or match.group(2).startswith(" ")):
            return True  # would re-parse as a mapping pair
    return False


class _Writer:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def line(self, indent: int, text: str) -> None:
        self.lines.append(" " * indent + text)

    def pair(self, indent: int, key: str, value) -> None:
        self.line(indent, f"{key}: {_scalar(value)}")

    def string_list(self, indent: int, key: str, values: list) -> None:
        if not values:
            self.line(indent, f"{key}: []")
            return
        self.line(indent, f"{key}:")
        for value in values:
            self.line(indent + 2, f"- {_scalar(value, item_context=True)}")

    def string_map(self, indent: int, key: str, mapping: dict) -> None:
        if not mapping:
            return
        self.line(indent, f"{key}:")
        for name, value in mapping.items():
            self.pair(indent + 2, name, value)

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _emit_selector(writer: _Writer, indent: int, key: str,
                   selector: FlowSelector) -> None:
    writer.line(indent, f"{key}:")
    for name in _SELECTOR_ORDER:
        if name in selector.source_fields:
            writer.pair(indent + 2, name, selector.source_fields[name])
    writer.pair(indent + 2, "match", selector.match)
    if selector.index is not None:
        writer.pair(indent + 2, "index", selector.index)


def _emit_when(writer: _Writer, indent: int, when: WhenClause) -> None:
    writer.line(indent, "when:")
    if when.platform:
        writer.pair(indent + 2, "platform", when.platform)
    if when.visible is not None:
        _emit_selector(writer, indent + 2, "visible", when.visible)
    if when.not_visible is not None:
        _emit_selector(writer, indent + 2, "notVisible", when.not_visible)
    if when.env_equals:
        writer.string_map(indent + 2, "envEquals", when.env_equals)


def _emit_step(writer: _Writer, indent: int, step: Step) -> None:
    spec = REGISTRY[step.command]
    if not step.args:
        writer.line(indent, f"- {step.command}")
        return
    writer.line(indent, f"- {step.command}:")
    arg_indent = indent + 4
    for arg in spec.args:
        if arg.name not in step.args:
            continue
        value = step.args[arg.name]
        if arg.kind == "selector":
            _emit_selector(writer, arg_indent, arg.name, value)
        elif arg.kind == "env":
            writer.string_map(arg_indent, arg.name, value)
        elif arg.kind == "when":
            _emit_when(writer, arg_indent, value)
        else:
            writer.pair(arg_indent, arg.name, value)


def _emit_evidence(writer: _Writer, indent: int, evidence: Evidence) -> None:
    explicit = set(evidence.explicit)
    if not explicit:
        return
    writer.line(indent, "evidence:")
    values = {
        "mode": evidence.mode,
        "beforeMutation": evidence.before_mutation,
        "afterAssertion": evidence.after_assertion,
        "bodies": evidence.bodies,
    }
    for name in _EVIDENCE_ORDER:
        if name not in explicit:
            continue
        if name == "collect":
            writer.string_list(indent + 2, "collect", evidence.collect)
        else:
            writer.pair(indent + 2, name, values[name])


def emit_flow(flow: Flow) -> str:
    writer = _Writer()
    for name in _HEADER_ORDER:
        if name == "schema":
            writer.pair(0, "schema", FLOW_SCHEMA_ID)
        elif name == "id" and flow.flow_id is not None:
            writer.pair(0, "id", flow.flow_id)
        elif name == "appId" and flow.app_id is not None:
            writer.pair(0, "appId", flow.app_id)
        elif name == "name":
            writer.pair(0, "name", flow.name)
        elif name == "description" and flow.description is not None:
            writer.pair(0, "description", flow.description)
        elif name == "tags" and flow.tags:
            writer.string_list(0, "tags", flow.tags)
        elif name == "properties" and flow.properties:
            writer.string_map(0, "properties", flow.properties)
        elif name == "env" and flow.env:
            writer.string_map(0, "env", flow.env)
        elif name == "requires" and (flow.requires_platforms
                                     or flow.requires_capabilities):
            writer.line(0, "requires:")
            if flow.requires_platforms:
                writer.string_list(2, "platform", flow.requires_platforms)
            if flow.requires_capabilities:
                writer.string_list(2, "capabilities", flow.requires_capabilities)
        elif name == "evidence" and flow.evidence is not None:
            _emit_evidence(writer, 0, flow.evidence)
        elif name == "onFlowStart" and flow.on_flow_start:
            writer.line(0, "onFlowStart:")
            for step in flow.on_flow_start:
                _emit_step(writer, 2, step)
        elif name == "onFlowComplete" and flow.on_flow_complete:
            writer.line(0, "onFlowComplete:")
            for step in flow.on_flow_complete:
                _emit_step(writer, 2, step)

    writer.line(0, "---")
    for step in flow.steps:
        _emit_step(writer, 0, step)
    return writer.text()


# --- Fingerprint (position-free equality for round-trip tests) ---------------


def _selector_fp(selector: FlowSelector):
    return ("selector", tuple(sorted(selector.fields.items())),
            selector.match, selector.index)


def _when_fp(when: WhenClause):
    return ("when", when.platform,
            _selector_fp(when.visible) if when.visible else None,
            _selector_fp(when.not_visible) if when.not_visible else None,
            tuple(sorted(when.env_equals.items())))


def _step_fp(step: Step):
    args = []
    for name in sorted(step.args):
        value = step.args[name]
        if isinstance(value, FlowSelector):
            args.append((name, _selector_fp(value)))
        elif isinstance(value, WhenClause):
            args.append((name, _when_fp(value)))
        elif isinstance(value, dict):
            args.append((name, tuple(sorted(value.items()))))
        else:
            args.append((name, value))
    return (step.command, tuple(args))


def fingerprint(flow: Flow):
    """Structural identity of a flow, independent of formatting/positions."""
    evidence = flow.evidence
    return {
        "name": flow.name,
        "app_id": flow.app_id,
        "flow_id": flow.flow_id,
        "description": flow.description,
        "tags": tuple(flow.tags),
        "properties": tuple(sorted(flow.properties.items())),
        "env": tuple(sorted(flow.env.items())),
        "requires": (tuple(flow.requires_platforms),
                     tuple(flow.requires_capabilities)),
        "evidence": None if evidence is None else (
            evidence.mode, evidence.before_mutation, evidence.after_assertion,
            tuple(evidence.collect), evidence.bodies, tuple(sorted(set(evidence.explicit)))),
        "on_flow_start": tuple(_step_fp(s) for s in flow.on_flow_start),
        "on_flow_complete": tuple(_step_fp(s) for s in flow.on_flow_complete),
        "steps": tuple(_step_fp(s) for s in flow.steps),
    }
