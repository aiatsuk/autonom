#!/usr/bin/env python3
"""Parse and filter Android UI Automator hierarchy dumps.

Public helpers used by ui_query, ui_tree_summarize, and tests:
Bounds, UiNode, parse_bounds, parse_nodes, filter_nodes, summarize, …
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Sequence

_BOUNDS_PATTERN = re.compile(
    r"^\[(-?\d+)\s*,\s*(-?\d+)\]\s*\[(-?\d+)\s*,\s*(-?\d+)\]$"
)
_TRUTHY = frozenset({"true", "1", "yes", "on"})
_WS = re.compile(r"\s+")


def normalize(value: str | None) -> str:
    """Collapse whitespace and strip; empty input becomes empty string."""
    if not value:
        return ""
    return _WS.sub(" ", value).strip()


@dataclass(frozen=True, slots=True)
class Bounds:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def as_dict(self) -> dict[str, int]:
        cx, cy = self.center
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
            "width": self.width,
            "height": self.height,
            "center_x": cx,
            "center_y": cy,
        }


def parse_bounds(value: str) -> Bounds | None:
    matched = _BOUNDS_PATTERN.match((value or "").strip())
    if matched is None:
        return None
    left, top, right, bottom = (int(g) for g in matched.groups())
    return Bounds(left, top, right, bottom)


@dataclass(frozen=True, slots=True)
class UiNode:
    index: int
    depth: int
    attributes: Mapping[str, str]

    def _attr(self, key: str) -> str:
        return normalize(self.attributes.get(key, ""))

    @property
    def text(self) -> str:
        return self._attr("text")

    @property
    def description(self) -> str:
        return self._attr("content-desc")

    @property
    def resource_id(self) -> str:
        return self._attr("resource-id")

    @property
    def class_name(self) -> str:
        return self._attr("class")

    @property
    def package(self) -> str:
        return self._attr("package")

    @property
    def bounds(self) -> Bounds | None:
        return parse_bounds(self.attributes.get("bounds", ""))

    def bool_attr(self, name: str) -> bool:
        return self.attributes.get(name, "").strip().lower() in _TRUTHY

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "index": self.index,
            "depth": self.depth,
            "text": self.text,
            "content_description": self.description,
            "resource_id": self.resource_id,
            "class": self.class_name,
            "package": self.package,
            "clickable": self.bool_attr("clickable"),
            "enabled": self.bool_attr("enabled"),
            "focusable": self.bool_attr("focusable"),
            "scrollable": self.bool_attr("scrollable"),
            "selected": self.bool_attr("selected"),
            "checked": self.bool_attr("checked"),
        }
        box = self.bounds
        if box is not None:
            payload["bounds"] = box.as_dict()
        return payload


def trim_hierarchy_xml(text: str) -> str:
    """Extract the complete <hierarchy>…</hierarchy> fragment from adb noise."""
    open_at = text.find("<hierarchy")
    close_tag = "</hierarchy>"
    close_at = text.rfind(close_tag)
    if open_at < 0 or close_at < 0 or close_at < open_at:
        raise ValueError("UI dump does not contain a complete <hierarchy> element")
    return text[open_at : close_at + len(close_tag)]


def parse_nodes(text: str) -> list[UiNode]:
    """Depth-first enumeration of every <node> in a UI Automator dump."""
    document = ET.fromstring(trim_hierarchy_xml(text))
    collected: list[UiNode] = []
    next_index = 0

    stack: list[tuple[ET.Element, int]] = [(document, 0)]
    while stack:
        element, depth = stack.pop()
        child_depth = depth
        if element.tag == "node":
            collected.append(
                UiNode(index=next_index, depth=depth, attributes=dict(element.attrib))
            )
            next_index += 1
            child_depth = depth + 1
        # reverse so left-to-right order is preserved with LIFO stack
        children = list(element)
        for child in reversed(children):
            stack.append((child, child_depth))
    return collected


def read_nodes(path: Path) -> list[UiNode]:
    return parse_nodes(path.read_text(encoding="utf-8"))


def match_string(actual: str, expected: str, mode: str, case_sensitive: bool) -> bool:
    left, right = actual, expected
    if not case_sensitive:
        left, right = left.casefold(), right.casefold()
    if mode == "exact":
        return left == right
    if mode == "contains":
        return right in left
    if mode == "regex":
        flags = 0 if case_sensitive else re.IGNORECASE
        return re.search(expected, actual, flags) is not None
    raise ValueError(f"unknown match mode: {mode}")


_STRING_SELECTORS: dict[str, Callable[[UiNode], str]] = {
    "text": lambda n: n.text,
    "desc": lambda n: n.description,
    "resource_id": lambda n: n.resource_id,
    "class_name": lambda n: n.class_name,
    "package": lambda n: n.package,
}
_BOOL_SELECTORS: dict[str, str] = {
    "clickable": "clickable",
    "enabled": "enabled",
    "focusable": "focusable",
    "scrollable": "scrollable",
    "selected": "selected",
    "checked": "checked",
}


def filter_nodes(
    nodes: Iterable[UiNode],
    selectors: Mapping[str, str | bool | None],
    *,
    mode: str = "exact",
    case_sensitive: bool = False,
) -> list[UiNode]:
    """Return nodes that satisfy every non-None selector."""
    active_strings = [
        (key, value, _STRING_SELECTORS[key])
        for key, value in selectors.items()
        if key in _STRING_SELECTORS and value is not None
    ]
    active_bools = [
        (key, bool(value), _BOOL_SELECTORS[key])
        for key, value in selectors.items()
        if key in _BOOL_SELECTORS and value is not None
    ]

    hits: list[UiNode] = []
    for node in nodes:
        ok = True
        for key, expected, getter in active_strings:
            if not isinstance(expected, str):
                ok = False
                break
            try:
                if not match_string(getter(node), expected, mode, case_sensitive):
                    ok = False
                    break
            except re.error as exc:
                raise ValueError(f"invalid regular expression for {key}: {exc}") from exc
        if not ok:
            continue
        for _key, expected_bool, attr in active_bools:
            if node.bool_attr(attr) is not expected_bool:
                ok = False
                break
        if ok:
            hits.append(node)
    return hits


def visible_label(node: UiNode) -> str:
    for candidate in (node.text, node.description, node.resource_id, node.class_name):
        if candidate:
            return candidate
    return "<unlabelled>"


def summarize(nodes: Iterable[UiNode], max_depth: int = 30) -> Iterator[str]:
    """Yield indented one-line summaries for meaningful nodes."""
    flag_names: Sequence[str] = (
        "clickable",
        "focusable",
        "scrollable",
        "selected",
        "checked",
    )
    for node in nodes:
        if node.depth > max_depth:
            continue
        bits: list[str] = []
        if node.resource_id:
            bits.append(f"id={node.resource_id}")
        if node.text:
            bits.append(f'text="{node.text}"')
        if node.description:
            bits.append(f'desc="{node.description}"')
        raised = [name for name in flag_names if node.bool_attr(name)]
        if raised:
            bits.append("flags=" + ",".join(raised))
        box = node.bounds
        if box is not None:
            bits.append(f"bounds=[{box.left},{box.top}][{box.right},{box.bottom}]")
        if not bits and not node.class_name:
            continue
        short_class = node.class_name.rsplit(".", 1)[-1] if node.class_name else "Node"
        indent = "  " * node.depth
        trail = f" {' '.join(bits)}" if bits else ""
        yield f"{indent}{short_class}{trail}"
