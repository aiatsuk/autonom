"""Flow selector → ``selector.select`` translation.

The shim never reimplements matching: field names were already translated to
``selector.py`` keys by ``schema.build_selector`` (``id``→``resource_id``,
``description``→``desc``), so this module only maps the flow match mode onto
``(mode, case_sensitive)`` and calls the shared engine — ambiguity refusal,
index handling, and regex errors surface identically on both platforms.
"""
from __future__ import annotations

from .. import selector as selector_engine
from .schema import MATCH_MODES, FlowSelector


def select(nodes: list, flow_selector: FlowSelector) -> list:
    mode, case_sensitive = MATCH_MODES[flow_selector.match]
    selectors = {key: value for key, value in flow_selector.fields.items()}
    # selector.select expects flow-facing None for unset fields and the
    # class-name remap key; flow fields are already engine keys except that
    # the engine's public surface names resource_id/class_name — build the
    # kwargs dict it filters on.
    engine_keys = {
        "text": selectors.get("text"),
        "desc": selectors.get("desc"),
        "resource_id": selectors.get("resource_id"),
        "role": selectors.get("role"),
        "enabled": selectors.get("enabled"),
        "checked": selectors.get("checked"),
        "selected": selectors.get("selected"),
    }
    return selector_engine.select(
        nodes, engine_keys, mode=mode, case_sensitive=case_sensitive,
        index=flow_selector.index,
    )


def describe(flow_selector: FlowSelector) -> dict:
    """Canonical, redaction-safe representation for events and errors."""
    described = dict(flow_selector.source_fields)
    described["match"] = flow_selector.match
    if flow_selector.index is not None:
        described["index"] = flow_selector.index
    return described
