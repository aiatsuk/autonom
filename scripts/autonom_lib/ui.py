"""Platform dispatch and the compact node schema.

The compact node is the most important contract in Autonom: an iOS node and an
Android node must be indistinguishable in shape, which is what lets one skill
body drive both platforms (C-03). Parsing and actuation live in `ui_android.py`
and `ui_ios.py`; selection lives in `selector.py`. This module only routes.
"""
from __future__ import annotations

from typing import Any

from . import errors, selector, ui_android
from .platform import ANDROID, IOS, Target

# Re-exported for callers that predate the platform split.
from .ui_android import compact_node, is_meaningful, role_for_class  # noqa: F401

COMPACT_FIELDS = (
    "ref", "role", "text", "desc", "resource_id", "class", "package", "bounds",
    "clickable", "enabled", "focusable", "scrollable", "selected", "checked", "depth",
)


# --- offline parsing (fixtures, `--dump`) ------------------------------------


def parse_compact_tree(
    xml_text: str,
    *,
    meaningful_only: bool = True,
    max_depth: int | None = None,
    max_nodes: int | None = 200,
) -> list[dict[str, Any]]:
    """Android UI Automator XML -> compact nodes. Kept for 0.4.0 callers."""
    return ui_android.parse_tree(
        xml_text,
        meaningful_only=meaningful_only,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )


def find_nodes(
    xml_text: str,
    *,
    text: str | None = None,
    desc: str | None = None,
    resource_id: str | None = None,
    class_name: str | None = None,
    package: str | None = None,
    clickable: bool | None = None,
    enabled: bool | None = None,
    mode: str = "contains",
    case_sensitive: bool = False,
    index: int | None = None,
    all_matches: bool = False,
) -> list[dict[str, Any]]:
    """Android XML search. Kept for 0.4.0 callers; delegates to `selector`."""
    return selector.select(
        ui_android.parse_all(xml_text),
        {
            "text": text,
            "desc": desc,
            "resource_id": resource_id,
            "class_name": class_name,
            "package": package,
            "clickable": clickable,
            "enabled": enabled,
        },
        mode=mode,
        case_sensitive=case_sensitive,
        index=index,
        all_matches=all_matches,
    )


def center_of(node: dict[str, Any]) -> tuple[int, int]:
    bounds = node.get("bounds")
    if not bounds or len(bounds) != 4:
        raise errors.AutonomError(
            errors.NO_MATCHING_NODE,
            f"node {node.get('ref')} has no bounds",
            "Pick a node with bounds, or tap by --x/--y.",
        )
    left, top, right, bottom = bounds
    return ((left + right) // 2, (top + bottom) // 2)


# --- live dispatch -----------------------------------------------------------


def _ios():
    from . import ui_ios  # imported lazily so a machine without Xcode can still import autonom

    return ui_ios


def snapshot(target: Target) -> list[dict[str, Any]]:
    """Every node on screen, unfiltered — the search corpus for find/tap."""
    if target.platform == ANDROID:
        return ui_android.parse_all(ui_android.dump_hierarchy(target.tool, target.target_id))
    if target.platform == IOS:
        return _ios().parse_all(_ios().describe_all(target))
    raise errors.AutonomError(errors.UNKNOWN_PLATFORM, f"unknown platform: {target.platform}")


def tree(
    target: Target,
    *,
    meaningful_only: bool = True,
    max_depth: int | None = None,
    max_nodes: int | None = 200,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compact tree plus any warnings (e.g. a sparse accessibility tree)."""
    if target.platform == ANDROID:
        nodes = ui_android.parse_tree(
            ui_android.dump_hierarchy(target.tool, target.target_id),
            meaningful_only=meaningful_only,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
        return nodes, []
    ios = _ios()
    return ios.parse_tree(
        ios.describe_all(target),
        meaningful_only=meaningful_only,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )


def screen_size(target: Target) -> tuple[int, int] | None:
    if target.platform == ANDROID:
        return ui_android.screen_size(target.tool, target.target_id)
    return _ios().screen_size(target)


def tap(target: Target, x: int, y: int, *, screen: tuple[int, int] | None = None) -> None:
    _guard_point(target, x, y, screen=screen)
    if target.platform == ANDROID:
        ui_android.tap(target.tool, target.target_id, x, y)
        return
    _ios().tap(target, x, y)


def swipe(target: Target, x1: int, y1: int, x2: int, y2: int, duration: float,
          *, screen: tuple[int, int] | None = None) -> None:
    for point in ((x1, y1), (x2, y2)):
        _guard_point(target, *point, screen=screen)
    if target.platform == ANDROID:
        ui_android.swipe(target.tool, target.target_id, x1, y1, x2, y2, duration)
        return
    _ios().swipe(target, x1, y1, x2, y2, duration)


def type_text(target: Target, text: str) -> None:
    if target.platform == ANDROID:
        ui_android.type_text(target.tool, target.target_id, text)
        return
    _ios().type_text(target, text)


def press_key(target: Target, key: str) -> None:
    if target.platform == ANDROID:
        ui_android.press_key(target.tool, target.target_id, key)
        return
    _ios().press_key(target, key)


def gesture(target: Target, name: str, **kwargs: Any) -> None:
    """Gestures Android's `input` cannot express are refused, not faked."""
    if target.platform == ANDROID:
        raise errors.AutonomError(
            errors.UNSUPPORTED_ON_PLATFORM,
            f"'{name}' is not supported on android",
            "Android input supports tap, swipe, text, and keyevent only.",
        )
    _ios().gesture(target, name, **kwargs)


def _guard_point(target: Target, x: int, y: int,
                 *, screen: tuple[int, int] | None = None) -> None:
    """INV-06 — refuse a point outside the screen rather than tapping blind.

    A point/pixel mix-up on Retina simulators produces coordinates ~3x too
    large. Dispatching them would 'succeed' while landing nowhere, so the agent
    would report a defect that does not exist (RISK-005).

    ``screen`` lets a caller that already knows the size (the flow executor
    caches it once per run) skip the lookup — on iOS ``screen_size`` re-runs a
    full accessibility dump, so the default path costs one extra dump per tap.
    """
    size = screen if screen is not None else screen_size(target)
    if not size:
        return
    width, height = size
    if 0 <= x <= width and 0 <= y <= height:
        return
    raise errors.AutonomError(
        errors.COORDINATE_SPACE_MISMATCH,
        f"point ({x}, {y}) is outside the {width}x{height} screen of {target.target_id}",
        "On iOS the accessibility tree reports points, not pixels — do not scale by the display "
        "factor. Re-dump the tree and use the reported bounds.",
        point=[x, y],
        screen=[width, height],
    )
