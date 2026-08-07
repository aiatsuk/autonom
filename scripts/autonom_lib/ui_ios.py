"""iOS accessibility tree -> the compact node schema (CAP-IOSUI-001).

`idb ui describe-all` returns the *accessibility* hierarchy, not the UIKit or
SwiftUI view tree. Its quality therefore depends on how well the app is
labelled; a Flutter app without `Semantics` can produce almost nothing, which
this module reports as `sparse_accessibility_tree` rather than letting an agent
conclude the screen is empty (RISK-013).

The parser accepts both a nested (`children`) and a flat element list, because
the exact shape varies across idb versions (U-001) and is confirmed by the
TASK-2.0.1 probe rather than assumed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from . import errors, ios_idb, ios_simctl
from .platform import Target

CHILD_KEYS = ("children", "child_elements", "elements")

# AX type / trait -> compact role. Unknown types fall back to a lowercased type.
# Types observed on iOS 26.0 via idb 1.1.7 / idb-companion 1.1.8, plus the
# common XCUIElementType names. Anything unmapped falls back to a lowercased
# type, which stays honest rather than guessing.
ROLE_BY_TYPE = {
    "Button": "button", "StaticText": "text", "Text": "text", "Heading": "heading",
    "TextField": "textfield", "SecureTextField": "textfield", "TextView": "textfield",
    "SearchField": "textfield", "Image": "image", "Cell": "cell", "Switch": "switch",
    "Toggle": "switch", "Slider": "slider", "Link": "link", "Table": "list",
    "CollectionView": "list", "ScrollView": "scroll", "NavigationBar": "navbar",
    "TabBar": "tabbar", "Toolbar": "toolbar", "Alert": "alert", "Sheet": "sheet",
    "ProgressIndicator": "progress", "Group": "group", "Other": "node",
    "Application": "app", "Window": "window",
}
CLICKABLE_ROLES = {"button", "link", "cell", "switch", "tabbar", "slider"}


def _first(source: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in source and source[name] not in (None, ""):
            return source[name]
    return None


def _flatten(payload: Any) -> list[dict[str, Any]]:
    """Depth-first element list from either tree shape."""
    elements: list[dict[str, Any]] = []

    def walk(node: Any, depth: int) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, depth)
            return
        if not isinstance(node, dict):
            return
        children: Iterable[Any] = ()
        for key in CHILD_KEYS:
            if isinstance(node.get(key), list):
                children = node[key]
                break
        record = {key: value for key, value in node.items() if key not in CHILD_KEYS}
        record["_depth"] = depth
        elements.append(record)
        for child in children:
            walk(child, depth + 1)

    walk(payload, 0)
    return elements


def _bounds(element: dict[str, Any]) -> list[int] | None:
    frame = _first(element, "frame", "AXFrame", "rect")
    if isinstance(frame, dict):
        try:
            x = float(_first(frame, "x", "X", "origin_x") or 0)
            y = float(_first(frame, "y", "Y", "origin_y") or 0)
            width = float(_first(frame, "width", "Width", "w") or 0)
            height = float(_first(frame, "height", "Height", "h") or 0)
        except (TypeError, ValueError):
            return None
        # Points, never pixels — do not scale by the display factor (INV-06).
        return [int(x), int(y), int(x + width), int(y + height)]
    return None


def _role(element: dict[str, Any]) -> str:
    raw = _first(element, "type", "AXType", "role", "element_type") or ""
    short = str(raw).replace("XCUIElementType", "")
    return ROLE_BY_TYPE.get(short, short.lower() or "node")


def _truthy(element: dict[str, Any], *names: str, default: bool = False) -> bool:
    value = _first(element, *names)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def compact_node(element: dict[str, Any], ref: str) -> dict[str, Any]:
    role = _role(element)
    traits = element.get("traits") or element.get("AXTraits") or []
    trait_text = " ".join(traits) if isinstance(traits, list) else str(traits)
    return {
        "ref": ref,
        "role": role,
        "text": _first(element, "AXValue", "value", "title") or None,
        "desc": _first(element, "AXLabel", "label", "name") or None,
        # The accessibility identifier is iOS's stable selector, the closest
        # thing to Android's resource-id. Plan §2.4 assumed null here; mapping it
        # gives Flutter/SwiftUI apps a durable way to be targeted.
        "resource_id": _first(element, "AXUniqueId", "identifier", "accessibility_identifier") or None,
        "class": str(_first(element, "type", "AXType", "role") or "") or None,
        "package": _first(element, "bundle_id", "bundleID") or None,
        "bounds": _bounds(element),
        "clickable": role in CLICKABLE_ROLES or "Button" in trait_text
        or _truthy(element, "hittable", "AXHittable"),
        "enabled": _truthy(element, "enabled", "AXEnabled", default=True),
        "focusable": _truthy(element, "focused", "AXFocused", "has_focus"),
        "scrollable": role in {"scroll", "list"},
        "selected": _truthy(element, "selected", "AXSelected"),
        "checked": _truthy(element, "checked", "AXChecked"),
        "depth": int(element.get("_depth") or 0),
    }


def is_meaningful(node: dict[str, Any]) -> bool:
    return bool(
        node.get("text")
        or node.get("desc")
        or node.get("resource_id")
        or node.get("clickable")
        or node.get("scrollable")
        or node.get("role") in {"textfield", "switch", "slider", "button"}
    )


def _load(payload: str | dict | list) -> Any:
    if isinstance(payload, (dict, list)):
        return payload
    text = (payload or "").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Some idb builds emit one JSON object per line.
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise errors.AutonomError(
                    errors.BACKEND_FAILED,
                    f"could not parse idb describe-all output: {exc}",
                    "Capture the raw output and check the idb version with 'autonom doctor'.",
                ) from exc
        return records


def parse_all(payload: str | dict | list) -> list[dict[str, Any]]:
    return [
        compact_node(element, f"n{index}")
        for index, element in enumerate(_flatten(_load(payload)))
    ]


def parse_tree(
    payload: str | dict | list,
    *,
    meaningful_only: bool = True,
    max_depth: int | None = None,
    max_nodes: int | None = 200,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: list[dict[str, Any]] = []
    for node in parse_all(payload):
        if max_depth is not None and node["depth"] > max_depth:
            continue
        if meaningful_only and not is_meaningful(node):
            continue
        nodes.append(node)
        if max_nodes is not None and len(nodes) >= max_nodes:
            break

    warnings: list[dict[str, Any]] = []
    if meaningful_only and len(nodes) < 3:
        warnings.append({
            "code": "sparse_accessibility_tree",
            "error": f"only {len(nodes)} meaningful node(s) found",
            "hint": "The app may not expose accessibility data. Add accessibility labels and "
                    "identifiers (Flutter: Semantics; SwiftUI: .accessibilityLabel / "
                    ".accessibilityIdentifier), re-run with --all, or fall back to a screenshot.",
        })
    if nodes and not any(node.get("resource_id") for node in nodes):
        warnings.append({
            "code": "no_accessibility_identifiers",
            "error": "no element exposes an accessibility identifier",
            "hint": "Select by --text or --desc; --resource-id cannot match on this screen.",
        })
    return nodes, warnings


# --- actuation ---------------------------------------------------------------


def describe_all(target: Target) -> str:
    return ios_idb.describe_all(target)


def screen_size(target: Target) -> tuple[int, int] | None:
    """Screen rectangle in points, used by the tap guard (INV-06).

    Taken from the accessibility tree's own root frame so it is expressed in
    the same coordinate space as the nodes; a mismatch is exactly what the guard
    exists to catch.
    """
    try:
        return screen_size_from(describe_all(target))
    except errors.AutonomError:
        return None


def screen_size_from(payload: str | dict | list) -> tuple[int, int] | None:
    """The root application/window frame, else the widest extent seen."""
    nodes = parse_all(payload)
    for node in nodes:
        if node["depth"] == 0 and node.get("role") in {"app", "window"} and node.get("bounds"):
            bounds = node["bounds"]
            return bounds[2] - bounds[0], bounds[3] - bounds[1]
    widest = 0
    tallest = 0
    for node in nodes:
        bounds = node.get("bounds")
        if bounds:
            widest = max(widest, bounds[2])
            tallest = max(tallest, bounds[3])
    return (widest, tallest) if widest and tallest else None


def tap(target: Target, x: int, y: int) -> None:
    ios_idb.tap(target, x, y)


def swipe(target: Target, x1: int, y1: int, x2: int, y2: int, duration: float) -> None:
    ios_idb.swipe(target, x1, y1, x2, y2, duration)


def type_text(target: Target, text: str) -> None:
    ios_idb.text(target, text)


def press_key(target: Target, key: str) -> None:
    """Named hardware buttons, or a numeric HID keycode.

    Android `KEYCODE_*` names are rejected with the valid list rather than
    silently doing nothing — and iOS genuinely has no global Back button, which
    the message says so an agent taps the navigation control instead.
    """
    upper = key.upper()
    if upper in ios_idb.BUTTONS:
        ios_idb.button(target, upper)
        return
    if key.isdigit():
        ios_idb.key(target, key)
        return
    hint = "Valid iOS buttons: " + ", ".join(ios_idb.BUTTONS) + "; or a numeric HID keycode."
    if upper.startswith("KEYCODE_"):
        hint += (" iOS has no global Back button — tap the navigation bar's back control "
                 "found via 'ui find --desc Back'.")
    raise errors.AutonomError(
        errors.UNSUPPORTED_KEY_FOR_PLATFORM,
        f"'{key}' is not an iOS key or button",
        hint,
    )


def gesture(target: Target, name: str, **kwargs: Any) -> None:
    ios_idb.gesture(target, name, **kwargs)


def screenshot(target: Target, output: Path) -> Path:
    return ios_simctl.screenshot(target.tool, target.target_id, output)
