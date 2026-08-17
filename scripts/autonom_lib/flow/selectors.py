"""Flow selector → ``selector.select`` translation.

The shim never reimplements matching: field names were already translated to
``selector.py`` keys by ``schema.build_selector`` (``id``→``resource_id``,
``description``→``desc``), so this module only maps the flow match mode onto
``(mode, case_sensitive)`` and calls the shared engine — ambiguity refusal,
index handling, and regex errors surface identically on both platforms.
"""
from __future__ import annotations

from .. import errors
from .. import selector as selector_engine
from .schema import MATCH_MODES, FlowSelector


def _engine_keys(flow_selector: FlowSelector) -> dict:
    # selector.select expects flow-facing None for unset fields and the
    # class-name remap key; flow fields are already engine keys except that
    # the engine's public surface names resource_id/class_name — build the
    # kwargs dict it filters on.
    selectors = {key: value for key, value in flow_selector.fields.items()}
    return {
        "text": selectors.get("text"),
        "desc": selectors.get("desc"),
        "resource_id": selectors.get("resource_id"),
        "role": selectors.get("role"),
        "enabled": selectors.get("enabled"),
        "checked": selectors.get("checked"),
        "selected": selectors.get("selected"),
        "focused": selectors.get("focused"),
        **flow_selector.relations,
    }


def select(nodes: list, flow_selector: FlowSelector) -> list:
    mode, case_sensitive = MATCH_MODES[flow_selector.match]
    return selector_engine.select(
        nodes, _engine_keys(flow_selector), mode=mode,
        case_sensitive=case_sensitive, index=flow_selector.index,
    )


def select_all(nodes: list, flow_selector: FlowSelector) -> list:
    """Assertion-style matching: all matches, relations included.

    ``index`` narrows to that occurrence (missing = simply "not there").
    A geometric relation whose anchor is off-screen matches nothing here —
    in an assertion context an absent anchor means the constrained element
    is not present, not an error (an ambiguous anchor still refuses).
    """
    mode, case_sensitive = MATCH_MODES[flow_selector.match]
    try:
        matches = selector_engine.select(
            nodes, _engine_keys(flow_selector), mode=mode,
            case_sensitive=case_sensitive, all_matches=True,
        )
    except errors.AutonomError as exc:
        if exc.code == errors.NO_MATCHING_NODE:  # geometric anchor absent
            return []
        raise
    if flow_selector.index is not None:
        try:
            matches = [matches[flow_selector.index]]
        except IndexError:
            matches = []
    return matches


def describe(flow_selector: FlowSelector) -> dict:
    """Canonical, redaction-safe representation for events and errors."""
    described = dict(flow_selector.source_fields)
    described["match"] = flow_selector.match
    if flow_selector.index is not None:
        described["index"] = flow_selector.index
    for name, anchor in flow_selector.source_relations.items():
        described[name] = describe(anchor)
    return described
