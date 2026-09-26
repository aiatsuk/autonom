"""Flow selector → ``selector.select`` translation.

The shim never reimplements matching: field names were already translated to
``selector.py`` keys by ``schema.build_selector`` (``id``→``resource_id``,
``description``→``desc``), so this module only maps the flow match mode onto
``(mode, case_sensitive)`` and calls the shared engine — ambiguity refusal,
index handling, and regex errors surface identically on both platforms.

A flow only ever sees nodes that can be on screen (``visible_only``). iOS
also lists Flutter's off-screen nodes at ``[0, 0, 0, 0]``; counting them made
``assertVisible`` pass on nothing the user could see, ``scrollUntilVisible``
stop before scrolling, and ``tapOn`` hit (0, 0). Android's UI Automator
already leaves such nodes out, so dropping them also keeps a selector that is
unique on Android unique on iOS. ``tapOn`` on an off-screen node therefore
keeps polling and fails as ``flow_assertion_timeout`` — a test failure, not
the CLI's ``element_offscreen``, which the flow failure classes do not map.
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
        "visible_text": selectors.get("visible_text"),
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
        visible_only=True,
    )


def select_all(nodes: list, flow_selector: FlowSelector) -> list:
    """Assertion-style matching: all matches, relations included.

    ``index`` narrows to that occurrence (missing = simply "not there").
    Only on-screen nodes count, so an element Flutter keeps below the fold
    is "not visible" to assertVisible/assertNotVisible/scrollUntilVisible.
    A geometric relation whose anchor is off-screen matches nothing here —
    in an assertion context an absent anchor means the constrained element
    is not present, not an error (an ambiguous anchor still refuses).
    """
    mode, case_sensitive = MATCH_MODES[flow_selector.match]
    try:
        matches = selector_engine.select(
            nodes, _engine_keys(flow_selector), mode=mode,
            case_sensitive=case_sensitive, all_matches=True, visible_only=True,
        )
    except errors.AutonomError as exc:
        # A geometric anchor that is simply off-screen means "not present" in
        # an assertion context. An INVALID REGEX shares the same code but is a
        # flow-definition error and must never read as a clean negative.
        if exc.code == errors.NO_MATCHING_NODE and "regular expression" not in exc.message:
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
