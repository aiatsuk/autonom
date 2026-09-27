"""Platform dispatch and the compact node schema.

The compact node is the most important contract in Autonom: an iOS node and an
Android node must be indistinguishable in shape, which is what lets one skill
body drive both platforms (C-03). Parsing and actuation live in `ui_android.py`
and `ui_ios.py`; selection lives in `selector.py`. This module only routes.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping

from . import errors, selector, ui_android
from .platform import ANDROID, IOS, Target

# Re-exported for callers that predate the platform split.
from .ui_android import compact_node, is_meaningful, role_for_class  # noqa: F401

COMPACT_FIELDS = (
    "ref", "role", "text", "desc", "resource_id", "class", "package", "bounds",
    "clickable", "enabled", "focusable", "focused", "scrollable", "selected",
    "checked", "depth", "parent",
)


def annotate_parents(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add a ``parent`` ref to each node of a depth-first compact list.

    Both platform parsers emit pre-order depth-first lists with a ``depth``
    field, so the parent of a node is the most recent node one level
    shallower. Additive: relational selectors (childOf/containsChild) need
    ancestry, and the flat list otherwise cannot express it.
    """
    stack: list[tuple[int, str]] = []  # (depth, ref)
    for node in nodes:
        depth = node.get("depth") or 0
        while stack and stack[-1][0] >= depth:
            stack.pop()
        node["parent"] = stack[-1][1] if stack else None
        ref = node.get("ref")
        if ref:
            stack.append((depth, ref))
    return nodes


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
    fields = {
        "text": text,
        "desc": desc,
        "resource_id": resource_id,
        "class_name": class_name,
        "package": package,
        "clickable": clickable,
        "enabled": enabled,
    }
    require_selector(fields)
    return selector.select(
        ui_android.parse_all(xml_text),
        fields,
        mode=mode,
        case_sensitive=case_sensitive,
        index=index,
        all_matches=all_matches,
    )


def require_selector(fields: Mapping[str, Any], verb: str = "ui find") -> None:
    """Refuse an empty selector by name.

    An empty selector matches every node, which surfaced as
    `ambiguous_selector: matched 78 nodes` — true, but not the mistake made.
    """
    if any(value is not None for value in fields.values()):
        return
    raise errors.AutonomError(
        errors.SELECTOR_REQUIRED,
        f"{verb} needs a selector (--text/--desc/--resource-id/...)",
        "Run 'autonom ui tree' to see what is on screen, then select by "
        "--desc/--text/--resource-id.",
    )


def no_match_hint(platform: str, nodes: list[dict[str, Any]], fields: Mapping[str, Any],
                  *, mode: str = "contains", case_sensitive: bool = False) -> str | None:
    """A better hint for a selector that matched nothing, when there is one.

    On iOS the visible label lives in `desc` (AXLabel), not `text`, so a
    `--text` miss whose value does match a `desc` is almost always that
    confusion (AGENTS rule 8) — say so instead of "run ui tree".
    """
    value = fields.get("text")
    if platform != IOS or value is None or fields.get("desc") is not None:
        return None
    swapped = {**fields, "text": None, "desc": value}
    try:
        hits = selector.filter_nodes(nodes, swapped, mode=mode, case_sensitive=case_sensitive)
    except errors.AutonomError:
        return None
    if not hits:
        return None
    return (f"On iOS the visible label is in desc (AXLabel), not text: {len(hits)} node(s) "
            f"have desc matching {value!r}. Retry with --desc instead of --text.")


def no_match_error(platform: str, nodes: list[dict[str, Any]], fields: Mapping[str, Any],
                   *, mode: str = "contains", case_sensitive: bool = False,
                   message: str = "no matching node") -> errors.AutonomError:
    """The `no_matching_node` envelope, carrying the iOS --desc hint when it applies."""
    hint = no_match_hint(platform, nodes, fields, mode=mode, case_sensitive=case_sensitive)
    return errors.AutonomError(
        errors.NO_MATCHING_NODE, message,
        hint or "Run 'autonom ui tree' to see what is on screen.",
    )


def clip(nodes: list[dict[str, Any]], max_nodes: int | None) -> tuple[list[dict[str, Any]], bool]:
    """Cut a node list to `max_nodes` and say whether anything was cut.

    Give it a list fetched with `max_nodes + 1`: a screen of exactly
    `max_nodes` nodes is then complete (`truncated: false`), where a
    `len(nodes) >= max_nodes` test called it truncated.
    """
    if max_nodes is None or len(nodes) <= max_nodes:
        return nodes, False
    return nodes[:max_nodes], True


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
        return annotate_parents(
            ui_android.parse_all(ui_android.dump_hierarchy(target.tool, target.target_id)))
    if target.platform == IOS:
        return annotate_parents(_ios().parse_all(_ios().describe_all(target)))
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


def tree_clipped(
    target: Target,
    *,
    meaningful_only: bool = True,
    max_depth: int | None = None,
    max_nodes: int | None = 200,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """`tree` plus an exact `truncated` flag: fetches one node past the limit."""
    nodes, warnings = tree(
        target, meaningful_only=meaningful_only, max_depth=max_depth,
        max_nodes=None if max_nodes is None else max_nodes + 1,
    )
    nodes, truncated = clip(nodes, max_nodes)
    return nodes, warnings, truncated


# --- settle / outline --------------------------------------------------------


def tree_signature(nodes: list[dict[str, Any]]) -> tuple:
    """What "the screen changed" means: any node's role, label, id or box."""
    return tuple(
        (node.get("role"), node.get("text"), node.get("desc"), node.get("resource_id"),
         tuple(node.get("bounds") or ()))
        for node in nodes
    )


def settle(
    target: Target | None,
    timeout_ms: int = 5000,
    quiet_ms: int = 500,
    *,
    interval_ms: int = 200,
    snapshot_fn: Callable[[], list[dict[str, Any]]] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll the tree until it stops changing, bounded by `timeout_ms`.

    Settled means two consecutive snapshots carry the same signature and that
    signature has held for at least `quiet_ms` (the same idea as a
    wait-for-app-to-settle step, a "settled" predicate, or a stable bounding
    box). An animation, a spinner or a list still loading keeps changing the
    signature; a timeout returns `settled: false` rather than raising, so the
    caller decides whether an unsettled screen is fatal.
    """
    take = snapshot_fn or (lambda: snapshot(target))  # type: ignore[arg-type]
    started = clock()
    deadline = started + max(timeout_ms, 0) / 1000
    previous: tuple | None = None
    stable_since = started
    snapshots = 0
    changes = 0
    while True:
        signature = tree_signature(take())
        snapshots += 1
        now = clock()
        if previous is not None and signature == previous:
            if now - stable_since >= max(quiet_ms, 0) / 1000:
                return {"settled": True, "snapshots": snapshots, "changes": changes,
                        "elapsed_ms": int((now - started) * 1000)}
        else:
            if previous is not None:
                changes += 1
            previous, stable_since = signature, now
        if now >= deadline:
            return {"settled": False, "snapshots": snapshots, "changes": changes,
                    "elapsed_ms": int((now - started) * 1000)}
        sleep(max(0.0, min(max(interval_ms, 10) / 1000, deadline - now)))


_INTERACTABLE_ROLES = ("button", "textfield", "searchfield", "securetextfield", "switch",
                       "checkbox", "radio", "link", "slider", "tab")


def is_interactable(node: Mapping[str, Any]) -> bool:
    """Could an agent act on this node? Disabled nodes never count."""
    if node.get("enabled") is False:
        return False
    if node.get("clickable") or node.get("scrollable") or node.get("focusable"):
        return True
    role = str(node.get("role") or "").lower()
    return any(marker in role for marker in _INTERACTABLE_ROLES)


def interactable(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [node for node in nodes if is_interactable(node)]


_OUTLINE_FLAGS = ("clickable", "scrollable", "focused", "selected", "checked")
_OUTLINE_LABEL_MAX = 80


def outline_line(node: Mapping[str, Any], indent: int = 0) -> str:
    """`<ref> <role> "<label>" id=<resource_id> @x,y wxh [flags]`, parts omitted when absent."""
    parts = [str(node.get("ref") or "?"), str(node.get("role") or "node")]
    label = node.get("text") or node.get("desc")
    if label:
        label = str(label)
        if len(label) > _OUTLINE_LABEL_MAX:
            label = label[: _OUTLINE_LABEL_MAX - 3] + "..."
        parts.append(json.dumps(label, ensure_ascii=False))
    if node.get("resource_id"):
        parts.append(f"id={node['resource_id']}")
    bounds = node.get("bounds")
    if bounds and len(bounds) == 4:
        left, top, right, bottom = bounds
        parts.append(f"@{left},{top} {right - left}x{bottom - top}")
    flags = [flag for flag in _OUTLINE_FLAGS if node.get(flag)]
    if node.get("enabled") is False:
        flags.append("disabled")
    if flags:
        parts.append("[" + " ".join(flags) + "]")
    return "  " * max(indent, 0) + " ".join(parts)


def outline(nodes: list[dict[str, Any]], *, interactable_only: bool = False) -> str:
    """One line per node, indented by depth — a compact read of the whole screen.

    The shape follows accessibility-snapshot outlines: an agent reads roles,
    labels and refs at a glance and taps by ref or selector, instead of
    paging through JSON.
    """
    chosen = interactable(nodes) if interactable_only else list(nodes)
    if not chosen:
        return ""
    base = min(int(node.get("depth") or 0) for node in chosen)
    return "\n".join(outline_line(node, int(node.get("depth") or 0) - base) for node in chosen)


def screen_size(target: Target) -> tuple[int, int] | None:
    if target.platform == ANDROID:
        return ui_android.screen_size(target.tool, target.target_id)
    return _ios().screen_size(target)


# The actuation verbs return the backend that delivered the input: "adb" on
# Android; on iOS "idb" or "axe" as `ui_ios` resolved it (AUTONOM_IOS_HID,
# AXe fallback), so a payload can say which one ran. Callers that ignore the
# return value are unaffected. `ui_ios.last_backend()` reports the same.
ANDROID_BACKEND = "adb"


def tap(target: Target, x: int, y: int, *, screen: tuple[int, int] | None = None) -> str:
    _guard_point(target, x, y, screen=screen)
    if target.platform == ANDROID:
        ui_android.tap(target.tool, target.target_id, x, y)
        return ANDROID_BACKEND
    return _ios().tap(target, x, y)


def long_press(target: Target, x: int, y: int, duration_ms: int = 600,
               *, screen: tuple[int, int] | None = None) -> str:
    _guard_point(target, x, y, screen=screen)
    if target.platform == ANDROID:
        ui_android.long_press(target.tool, target.target_id, x, y, duration_ms)
        return ANDROID_BACKEND
    # through ui_ios, not idb directly: AXe carries it when idb's HID cannot
    return _ios().tap(target, x, y, duration=duration_ms / 1000)


def double_tap(target: Target, x: int, y: int,
               *, screen: tuple[int, int] | None = None) -> str:
    _guard_point(target, x, y, screen=screen)
    # Guard once, dispatch twice — the platform helpers skip the guard.
    if target.platform == ANDROID:
        ui_android.tap(target.tool, target.target_id, x, y)
        ui_android.tap(target.tool, target.target_id, x, y)
        return ANDROID_BACKEND
    _ios().tap(target, x, y)
    # the second tap reports the backend that finished the gesture (a first
    # tap that fell back to AXe makes the second go straight to AXe)
    return _ios().tap(target, x, y)


def swipe(target: Target, x1: int, y1: int, x2: int, y2: int, duration: float,
          *, screen: tuple[int, int] | None = None) -> str:
    for point in ((x1, y1), (x2, y2)):
        _guard_point(target, *point, screen=screen)
    if target.platform == ANDROID:
        ui_android.swipe(target.tool, target.target_id, x1, y1, x2, y2, duration)
        return ANDROID_BACKEND
    return _ios().swipe(target, x1, y1, x2, y2, duration)


def type_text(target: Target, text: str) -> str:
    if target.platform == ANDROID:
        ui_android.type_text(target.tool, target.target_id, text)
        return ANDROID_BACKEND
    return _ios().type_text(target, text)


def press_key(target: Target, key: str) -> str:
    if target.platform == ANDROID:
        ui_android.press_key(target.tool, target.target_id, key)
        return ANDROID_BACKEND
    return _ios().press_key(target, key)


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
    if target.platform == ANDROID:
        hint = ("Android coordinates are pixels of the reported screen size; a screenshot "
                "of this target has the same size. Re-dump the tree and use the reported "
                "bounds.")
    else:
        hint = ("On iOS the accessibility tree reports points, not pixels — do not scale "
                "by the display factor. Re-dump the tree and use the reported bounds.")
    raise errors.AutonomError(
        errors.COORDINATE_SPACE_MISMATCH,
        f"point ({x}, {y}) is outside the {width}x{height} screen of {target.target_id}",
        hint,
        point=[x, y],
        screen=[width, height],
    )


def screen_from_nodes(nodes: list[dict[str, Any]]) -> tuple[int, int] | None:
    """The screen rectangle a compact tree already carries, without another
    device round-trip: the root application/window node's bounds. None when
    the tree has no such root (a dump filtered down to leaves)."""
    for node in nodes:
        if node.get("role") in {"app", "window"} and node.get("bounds"):
            left, top, right, bottom = node["bounds"]
            if right > left and bottom > top:
                return right - left, bottom - top
    return None


_TEXT_ROLES = ("textfield", "searchfield", "textview", "edittext", "textarea", "securetextfield")
# Android classes that take typed text. Compose and Flutter text fields both
# report android.widget.EditText; the rest are its framework subclasses.
_EDITABLE_CLASSES = ("EditText", "AutoCompleteTextView", "MultiAutoCompleteTextView",
                     "ExtractEditText", "SearchAutoComplete")


def is_editable(node: Mapping[str, Any]) -> bool:
    """Does this node accept typed text, as far as the tree can tell?"""
    if node.get("editable"):
        return True
    short = str(node.get("class") or "").rsplit(".", 1)[-1]
    if short.endswith(_EDITABLE_CLASSES):
        return True
    role = str(node.get("role") or "").lower()
    return role in {"textfield", "searchfield", "securetextfield", "textarea", "edittext"}


def typing_target(platform: str, nodes: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """Where typed text will land, and how sure that is.

    Returns `(node, certainty)`: `"focused"` when an editable node reports
    keyboard focus; `"focused_not_editable"` when the focused node is not a
    text field (a focused button or list row on Android swallows `input
    text` just the same, so this is never reported as verified); on iOS,
    where idb's accessibility dump carries no focus attribute at all
    (verified on a Simulator: Spotlight's active field reads `AXFocused:
    None`), `"field_present"` when a text field is on screen; `(None,
    "none")` when nothing could take the text.
    """
    focused = focused_node(nodes)
    if focused is not None:
        if platform == ANDROID and not is_editable(focused):
            return focused, "focused_not_editable"
        return focused, "focused"
    if platform == IOS:
        for node in nodes:
            role = str(node.get("role") or "").lower()
            if any(marker in role for marker in _TEXT_ROLES):
                return node, "field_present"
    return None, "none"


def focused_node(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The node that will receive typed text, if the tree exposes one.

    Typing goes to whatever has keyboard focus; when nothing does, `input
    text` on Android and `idb ui text` on iOS both "succeed" while the
    characters land nowhere. Both trees carry `focused` (UIAutomator's
    attribute, AXFocused on iOS), so the absence of a focused node is the
    only signal there is that a type would be swallowed.
    """
    for node in nodes:
        if node.get("focused"):
            return node
    return None
