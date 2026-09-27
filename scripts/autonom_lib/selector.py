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
    "role": "role",
}
# `visible_text` is the one field that matches ACROSS node keys: the label a
# human (or a screen reader) sees, wherever the platform put it — Android
# usually `text`, Flutter/iOS the accessibility label in `desc`. It is the
# honest equivalent of Maestro's `text`, which matches the union of
# text/hintText/accessibilityText (Filters.kt).
LABEL_SOURCES = ("text", "desc", "hint")
BOOL_FIELDS = ("clickable", "enabled", "focusable", "focused", "scrollable",
               "selected", "checked")
MODES = ("exact", "contains", "regex")

# Relational constraints. Values are {"fields": {...engine keys...},
# "mode": ..., "case_sensitive": ...} anchor specs. The geometric four need a
# UNIQUE anchor (a reference rectangle); the ancestry two are per-candidate
# predicates and tolerate any number of anchor-shaped nodes.
GEOMETRIC_RELATIONS = ("above", "below", "left_of", "right_of")
ANCESTRY_RELATIONS = ("child_of", "contains_child", "contains_descendants")
RELATIONAL_FIELDS = GEOMETRIC_RELATIONS + ANCESTRY_RELATIONS

_WHITESPACE = re.compile(r"\s+")


def normalize_space(value: str) -> str:
    """Collapse every whitespace run to one space and strip the ends.

    One on-screen label reaches the engine differently per platform: iOS
    joins the parts of a merged Flutter label with a newline, Android's
    parser with a space. Exact matching on the raw strings made one selector
    match on one platform only.
    """
    return _WHITESPACE.sub(" ", value).strip()


def match_string(actual: str | None, expected: str, mode: str, case_sensitive: bool) -> bool:
    """Compare one node value with one selector value.

    ``exact`` and ``contains`` (either case mode) compare whitespace-
    normalized strings on both sides; ``regex`` sees the raw value, so a
    pattern can still target a newline on purpose. A selector made only of
    whitespace is compared raw, as before: normalized it would be "", and
    ``contains ""`` matches every node.
    """
    actual = actual or ""
    if mode != "regex" and normalize_space(expected):
        actual = normalize_space(actual)
        expected = normalize_space(expected)
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


# --- on-screen visibility ----------------------------------------------------
#
# iOS lists Flutter's scroll-cache nodes (laid out, not on screen) with a
# 0x0 frame at the origin. Tapping one "succeeded" at (0, 0) and asserting
# one passed. Visible therefore means *provably could be on screen*: positive
# area, and overlapping the screen rectangle when the tree carries one.

VIEWPORT_ROLES = ("app", "window")

Rect = tuple[float, float, float, float]


def _rect(node: Mapping[str, Any]) -> Rect | None:
    bounds = node.get("bounds")
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return None
    try:
        left, top, right, bottom = (float(value) for value in bounds)
    except (TypeError, ValueError):
        return None
    return left, top, right, bottom


def viewport(nodes: Iterable[Mapping[str, Any]]) -> Rect | None:
    """The screen rectangle the tree itself reports, if it has one.

    That is the root application/window frame (iOS always lists it). An
    Android dump has no such role and is not needed: UI Automator already
    leaves out nodes that are not visible to the user.
    """
    for node in nodes:
        if node.get("role") not in VIEWPORT_ROLES or (node.get("depth") or 0) != 0:
            continue
        rect = _rect(node)
        if rect is not None and rect[2] > rect[0] and rect[3] > rect[1]:
            return tuple(node["bounds"])  # type: ignore[return-value]
    return None


def is_visible(node: Mapping[str, Any], view: Rect | None = None) -> bool:
    """Could this node be on screen?

    False for a node already marked ``visible: false``, for zero or negative
    area, and for a rectangle entirely outside ``view``. A node without
    bounds proves nothing either way and counts as visible, so trees without
    geometry keep their old behaviour.
    """
    if node.get("visible") is False:
        return False
    rect = _rect(node)
    if rect is None:
        return True
    left, top, right, bottom = rect
    if right <= left or bottom <= top:
        return False
    if view is not None:
        v_left, v_top, v_right, v_bottom = view
        if right <= v_left or left >= v_right or bottom <= v_top or top >= v_bottom:
            return False
    return True


def mark_visibility(nodes: list[dict[str, Any]],
                    view: Rect | None = None) -> list[dict[str, Any]]:
    """Add ``visible: false`` to each node that cannot be on screen.

    In place, and additive: off-screen nodes stay in the list (a tree still
    counts them) and on-screen nodes gain no key at all. ``view`` defaults to
    the tree's own :func:`viewport`.
    """
    if view is None:
        view = viewport(nodes)
    for node in nodes:
        if not is_visible(node, view):
            node["visible"] = False
    return nodes


def _plain_match(node: Mapping[str, Any], fields: Mapping[str, Any],
                 mode: str, case_sensitive: bool) -> bool:
    """String + bool field matching for one node (no relational).

    A malformed regex raises the same positioned envelope as the main
    filter — an anchor pattern must never crash with a raw ``re.error``."""
    def _match(actual, expected, name):
        try:
            return match_string(actual, expected, mode, case_sensitive)
        except re.error as exc:
            raise errors.AutonomError(
                errors.NO_MATCHING_NODE,
                f"invalid regular expression for {name}: {exc}",
                "Fix the pattern, or use match: contains.",
            ) from exc

    for name, key in STRING_FIELDS.items():
        expected = fields.get(name)
        if expected is None:
            continue
        if not isinstance(expected, str):
            return False
        if not _match(node.get(key), expected, name):
            return False
    expected = fields.get("visible_text")
    if expected is not None:
        if not isinstance(expected, str):
            return False
        present = [node.get(source) for source in LABEL_SOURCES
                   if node.get(source)]
        if not any(_match(value, expected, "visible_text")
                   for value in present):
            return False
    for name in BOOL_FIELDS:
        expected = fields.get(name)
        if expected is None:
            continue
        if bool(node.get(name)) is not bool(expected):
            return False
    return True


def _apply_relations(
    all_nodes: list[Mapping[str, Any]],
    candidates: list[dict[str, Any]],
    relations: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Narrow candidates by geometric/ancestry constraints.

    Deterministic by design: geometry is a pure edge comparison against one
    unique anchor rectangle (no "nearest" heuristics), ancestry follows the
    parent refs the snapshot carries. Both platforms share this code, so a
    relation that is ambiguous on one is ambiguous on the other.
    """
    by_ref = {node.get("ref"): node for node in all_nodes if node.get("ref")}
    view = viewport(all_nodes)

    def anchor_matches(spec: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        return [node for node in all_nodes
                if _plain_match(node, spec["fields"], spec.get("mode", "exact"),
                                spec.get("case_sensitive", True))]

    for name, spec in relations.items():
        if name in GEOMETRIC_RELATIONS:
            # an off-screen copy (0x0 at the origin on iOS) is no reference
            # rectangle, and must not make an on-screen anchor ambiguous
            anchors = [a for a in anchor_matches(spec)
                       if a.get("bounds") and is_visible(a, view)]
            if not anchors:
                raise errors.AutonomError(
                    errors.NO_MATCHING_NODE,
                    f"relational anchor for {name!r} matched nothing",
                    "The reference element must be on screen (and have bounds).",
                )
            if len(anchors) > 1:
                labels = ", ".join(visible_label(a) for a in anchors[:8])
                raise errors.AutonomError(
                    errors.AMBIGUOUS_SELECTOR,
                    f"relational anchor for {name!r} matched {len(anchors)} "
                    f"nodes ({labels}); a geometric relation needs exactly one",
                    "Tighten the anchor selector.",
                    match_count=len(anchors),
                )
            left, top, right, bottom = anchors[0]["bounds"]

            def keeps(node: Mapping[str, Any], relation: str = name) -> bool:
                bounds = node.get("bounds")
                if not bounds:
                    return False
                if relation == "above":
                    return bounds[3] <= top
                if relation == "below":
                    return bounds[1] >= bottom
                if relation == "left_of":
                    return bounds[2] <= left
                return bounds[0] >= right  # right_of

            candidates = [node for node in candidates if keeps(node)]
        elif name == "child_of":
            def has_matching_ancestor(node: Mapping[str, Any]) -> bool:
                seen: set[str] = set()
                parent = node.get("parent")
                while parent and parent not in seen:
                    seen.add(parent)
                    ancestor = by_ref.get(parent)
                    if ancestor is None:
                        return False
                    if _plain_match(ancestor, spec["fields"],
                                    spec.get("mode", "exact"),
                                    spec.get("case_sensitive", True)):
                        return True
                    parent = ancestor.get("parent")
                return False

            candidates = [node for node in candidates if has_matching_ancestor(node)]
        elif name == "contains_child":  # a DIRECT child matches
            children_of: dict[str, list[Mapping[str, Any]]] = {}
            for node in all_nodes:
                parent = node.get("parent")
                if parent:
                    children_of.setdefault(parent, []).append(node)

            def has_matching_child(node: Mapping[str, Any]) -> bool:
                for child in children_of.get(node.get("ref"), []):
                    if _plain_match(child, spec["fields"],
                                    spec.get("mode", "exact"),
                                    spec.get("case_sensitive", True)):
                        return True
                return False

            candidates = [node for node in candidates if has_matching_child(node)]
        else:  # contains_descendants — ANY depth below the candidate matches
            descendants_of: dict[str, list[Mapping[str, Any]]] = {}
            for node in all_nodes:
                seen: set[str] = set()
                parent = node.get("parent")
                while parent and parent not in seen:
                    seen.add(parent)
                    descendants_of.setdefault(parent, []).append(node)
                    ancestor = by_ref.get(parent)
                    parent = ancestor.get("parent") if ancestor else None

            def has_matching_descendant(node: Mapping[str, Any]) -> bool:
                for descendant in descendants_of.get(node.get("ref"), []):
                    if _plain_match(descendant, spec["fields"],
                                    spec.get("mode", "exact"),
                                    spec.get("case_sensitive", True)):
                        return True
                return False

            candidates = [node for node in candidates
                          if has_matching_descendant(node)]
    return candidates


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
        expected = selectors.get("visible_text")
        if expected is not None:
            if not isinstance(expected, str):
                continue
            present = [node.get(source) for source in LABEL_SOURCES
                       if node.get(source)]
            try:
                if not any(match_string(value, expected, mode, case_sensitive)
                           for value in present):
                    continue
            except re.error as exc:
                raise errors.AutonomError(
                    errors.NO_MATCHING_NODE,
                    f"invalid regular expression for visibleText: {exc}",
                    "Fix the pattern, or use match: contains.",
                ) from exc
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
    return any(selectors.get(name) is not None
               for name in (*STRING_FIELDS, *BOOL_FIELDS, *RELATIONAL_FIELDS))


def select(
    nodes: Iterable[Mapping[str, Any]],
    selectors: Mapping[str, Any],
    *,
    mode: str = "contains",
    case_sensitive: bool = False,
    index: int | None = None,
    all_matches: bool = False,
    visible_only: bool = False,
) -> list[dict[str, Any]]:
    """Filter, then resolve duplicates.

    A selector matching several nodes is an error rather than a silent "first
    wins": an agent that taps the wrong one of three identical buttons produces
    convincing but false evidence.

    ``visible_only`` drops nodes that cannot be on screen before duplicates
    and ``index`` are resolved — what a flow means by "visible", and what
    ``ui tap`` and ``ui find`` try first (``ui.resolve_match``). Without it
    every match counts, off-screen ones included (``ui find --all``).
    """
    all_nodes = list(nodes)
    view = viewport(all_nodes)
    matches = filter_nodes(all_nodes, selectors, mode=mode,
                           case_sensitive=case_sensitive)
    if visible_only:
        matches = [node for node in matches if is_visible(node, view)]
    relations = {name: spec for name, spec in selectors.items()
                 if name in RELATIONAL_FIELDS and spec is not None}
    if relations and matches:
        matches = _apply_relations(all_nodes, matches, relations)
    if not matches:
        return []
    if all_matches:
        return matches
    if index is not None:
        normalized = index if index >= 0 else len(matches) + index
        if normalized < 0 or normalized >= len(matches):
            counted = "on-screen match(es)" if visible_only else "match(es)"
            raise errors.AutonomError(
                errors.SELECTOR_INDEX_OUT_OF_RANGE,
                f"index {index} outside {len(matches)} {counted}",
                f"Use an index between 0 and {len(matches) - 1}, or --all to see every match.",
            )
        return [matches[normalized]]
    if len(matches) > 1:
        # an off-screen duplicate is the usual reason on iOS; name it, so
        # the right --index is obvious without another dump
        labels = ", ".join(
            f"{i}:{visible_label(node)}" + ("" if is_visible(node, view) else " (off-screen)")
            for i, node in enumerate(matches[:8]))
        raise errors.AutonomError(
            errors.AMBIGUOUS_SELECTOR,
            f"selector matched {len(matches)} nodes; pass --index or tighten selector. "
            f"Matches: {labels}",
            "Add --index <n>, tighten the selector, or pass --all to list every match.",
            match_count=len(matches),
        )
    return matches
