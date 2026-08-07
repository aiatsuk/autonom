"""Shared selector matching for both platforms (CAP-PLAT-004).

Extracted from `ui.py` so Android and iOS cannot drift apart: a selector that is
ambiguous on one platform must be ambiguous on the other, under the same rule
and with the same error text. Everything here operates on **compact nodes**
(plain dicts), which is the one schema both backends produce.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from . import errors

STRING_FIELDS = {
    "text": "text",
    "desc": "desc",
    "resource_id": "resource_id",
    "class_name": "class",
    "package": "package",
}
BOOL_FIELDS = ("clickable", "enabled", "focusable", "scrollable", "selected", "checked")
MODES = ("exact", "contains", "regex")


def match_string(actual: str | None, expected: str, mode: str, case_sensitive: bool) -> bool:
    actual = actual or ""
    if not case_sensitive:
        actual = actual.casefold()
        expected = expected.casefold()
    if mode == "exact":
        return actual == expected
    if mode == "contains":
        return expected in actual
    if mode == "regex":
        flags = 0 if case_sensitive else re.IGNORECASE
        return re.search(expected, actual, flags) is not None
    raise errors.AutonomError(
        errors.UNSUPPORTED_ON_PLATFORM,
        f"unknown match mode: {mode}",
        "Valid modes: " + ", ".join(MODES),
    )


def visible_label(node: Mapping[str, Any]) -> str:
    for field in ("text", "desc", "resource_id", "class"):
        value = node.get(field)
        if value:
            return str(value)
    return "<unlabelled>"


def filter_nodes(
    nodes: Iterable[Mapping[str, Any]],
    selectors: Mapping[str, Any],
    *,
    mode: str = "exact",
    case_sensitive: bool = False,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for node in nodes:
        matched = True
        for name, key in STRING_FIELDS.items():
            expected = selectors.get(name)
            if expected is None:
                continue
            if not isinstance(expected, str):
                matched = False
                break
            try:
                if not match_string(node.get(key), expected, mode, case_sensitive):
                    matched = False
                    break
            except re.error as exc:
                raise errors.AutonomError(
                    errors.NO_MATCHING_NODE,
                    f"invalid regular expression for {name}: {exc}",
                    "Fix the pattern, or use --mode contains.",
                ) from exc
        if not matched:
            continue
        for name in BOOL_FIELDS:
            expected = selectors.get(name)
            if expected is None:
                continue
            if bool(node.get(name)) is not bool(expected):
                matched = False
                break
        if matched:
            result.append(dict(node))
    return result


def has_selector(selectors: Mapping[str, Any]) -> bool:
    return any(selectors.get(name) is not None for name in (*STRING_FIELDS, *BOOL_FIELDS))


def select(
    nodes: Iterable[Mapping[str, Any]],
    selectors: Mapping[str, Any],
    *,
    mode: str = "contains",
    case_sensitive: bool = False,
    index: int | None = None,
    all_matches: bool = False,
) -> list[dict[str, Any]]:
    """Filter, then resolve duplicates.

    A selector matching several nodes is an error rather than a silent "first
    wins": an agent that taps the wrong one of three identical buttons produces
    convincing but false evidence.
    """
    matches = filter_nodes(nodes, selectors, mode=mode, case_sensitive=case_sensitive)
    if not matches:
        return []
    if all_matches:
        return matches
    if index is not None:
        normalized = index if index >= 0 else len(matches) + index
        if normalized < 0 or normalized >= len(matches):
            raise errors.AutonomError(
                errors.SELECTOR_INDEX_OUT_OF_RANGE,
                f"index {index} outside {len(matches)} match(es)",
                f"Use an index between 0 and {len(matches) - 1}, or --all to see every match.",
            )
        return [matches[normalized]]
    if len(matches) > 1:
        labels = ", ".join(f"{i}:{visible_label(node)}" for i, node in enumerate(matches[:8]))
        raise errors.AutonomError(
            errors.AMBIGUOUS_SELECTOR,
            f"selector matched {len(matches)} nodes; pass --index or tighten selector. "
            f"Matches: {labels}",
            "Add --index <n>, tighten the selector, or pass --all to list every match.",
            match_count=len(matches),
        )
    return matches
