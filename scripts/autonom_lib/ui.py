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
    return selector.mark_visibility(ui_android.parse_tree(
        xml_text,
        meaningful_only=meaningful_only,
        max_depth=max_depth,
        max_nodes=max_nodes,
    ))


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
        selector.mark_visibility(ui_android.parse_all(xml_text)),
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
    confusion (AGENTS rule 8) — say so instead of "run ui tree". Android has
    the same trap: Flutter exposes its labels as the content-description,
    and so do image buttons, so the hint applies on every platform.
    """
    value = fields.get("text")
    if value is None or fields.get("desc") is not None:
        return None
    swapped = {**fields, "text": None, "desc": value}
    try:
        hits = selector.filter_nodes(nodes, swapped, mode=mode, case_sensitive=case_sensitive)
    except errors.AutonomError:
        return None
    if not hits:
        return None
    found = f"{len(hits)} node(s) have desc matching {value!r}"
    retry = "Retry with --desc instead of --text."
    if platform == IOS:
        return f"On iOS the visible label is in desc (AXLabel), not text: {found}. {retry}"
    if platform == ANDROID:
        return ("On Android, Flutter apps (and image buttons) carry the label in desc "
                f"(content-description), not text: {found}. {retry}")
    return f"The label is in desc, not text: {found}. {retry}"


def no_match_error(platform: str, nodes: list[dict[str, Any]], fields: Mapping[str, Any],
                   *, mode: str = "contains", case_sensitive: bool = False,
                   message: str = "no matching node") -> errors.AutonomError:
    """The `no_matching_node` envelope, carrying the --desc hint when it applies."""
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


def center_of(node: dict[str, Any], *,
              viewport: tuple[int, int, int, int] | None = None) -> tuple[int, int]:
    """Where a tap on this node lands — refused when it has no place on screen.

    iOS lists Flutter's off-screen nodes with a 0x0 frame at the origin; their
    "centre" is (0, 0), and tapping it reported success while nothing was
    touched. A node without area raises ``element_offscreen``, and so does
    one entirely outside an explicit ``viewport``.

    A node *with* area outside the screen is deliberately left to the tap
    guard (INV-06): when a whole tree is off by the display scale, every
    node sits outside the screen and ``coordinate_space_mismatch`` is the
    diagnosis that helps. ``snapshot`` still marks such a node
    ``visible: false``, and flows never select it.
    """
    bounds = node.get("bounds")
    if not bounds or len(bounds) != 4:
        raise errors.AutonomError(
            errors.NO_MATCHING_NODE,
            f"node {node.get('ref')} has no bounds",
            "Pick a node with bounds, or tap by --x/--y.",
        )
    # geometry only: the `visible` mark also covers the scale mix-up above
    if not selector.is_visible({"bounds": bounds}, viewport):
        raise offscreen_error(node)
    left, top, right, bottom = bounds
    return ((left + right) // 2, (top + bottom) // 2)


def offscreen_error(node: Mapping[str, Any], *, match_count: int = 1) -> errors.AutonomError:
    """`element_offscreen`: the selector matched, but not anything on screen."""
    where = (f"node {node.get('ref')} ({selector.visible_label(node)}) is not on screen "
             f"(bounds {list(node.get('bounds') or [])})")
    if match_count > 1:
        where = f"{match_count} nodes matched and none is on screen; {where}"
    return errors.AutonomError(
        errors.ELEMENT_OFFSCREEN,
        f"{where}; a tap there would land nowhere",
        "Scroll it into view first — 'autonom ui swipe --from X,Y --to X,Y', or "
        "scrollUntilVisible in a flow — then select it again. 'ui find --all' marks "
        "off-screen matches with visible: false.",
        ref=node.get("ref"),
        bounds=list(node.get("bounds") or []),
        match_count=match_count,
    )


def _has_area(node: Mapping[str, Any]) -> bool:
    """Laid out with a size at all, wherever that is (bounds-less counts)."""
    return selector.is_visible({"bounds": node.get("bounds")})


def resolve_match(
    nodes: list[dict[str, Any]],
    fields: Mapping[str, Any],
    *,
    mode: str = "contains",
    case_sensitive: bool = False,
    index: int | None = None,
) -> tuple[dict[str, Any] | None, int]:
    """The one node a selector picks, and how many nodes it matched.

    The single resolution core behind ``ui tap`` (``select_for_action``) and
    ``ui find`` (``select_for_find``), so the two cannot drift: the same
    duplicates, the same ``--index`` counting, the same ambiguity listing.

    Matches that can be on screen are resolved first — duplicates and
    ``index`` counted among them, exactly as a flow counts. iOS also lists
    Flutter's off-screen scroll cache at 0x0; counting it made a selector
    unique on Android ambiguous on iOS, and made ``--index`` mean another
    node. Only when no match is on screen is every match resolved over
    instead. Ambiguity and a bad index raise; no match is ``(None, 0)``.
    """
    def pick(**kwargs: Any) -> list[dict[str, Any]]:
        return selector.select(nodes, fields, mode=mode, case_sensitive=case_sensitive,
                               **kwargs)

    everything = pick(all_matches=True)
    if not everything:
        return None, 0
    on_screen = pick(index=index, visible_only=True)
    if on_screen:
        return on_screen[0], len(everything)
    return pick(index=index)[0], len(everything)


def select_for_action(
    nodes: list[dict[str, Any]],
    fields: Mapping[str, Any],
    *,
    mode: str = "contains",
    case_sensitive: bool = False,
    index: int | None = None,
) -> dict[str, Any] | None:
    """The one node an action verb (tap, long press) acts on; None if none matches.

    Resolved by ``resolve_match``, exactly as ``ui find`` resolves it. Then,
    when no match was on screen:

    - the node has no area (never laid out on screen): ``element_offscreen``
      with the scroll hint;
    - the node has real area outside the screen: returned, and its centre
      meets the tap guard, which answers ``coordinate_space_mismatch`` — the
      right diagnosis when a whole tree is off by the display scale (INV-06).
    """
    node, matched = resolve_match(nodes, fields, mode=mode, case_sensitive=case_sensitive,
                                  index=index)
    if node is not None and not _has_area(node):
        raise offscreen_error(node, match_count=matched)
    return node


def select_for_find(
    nodes: list[dict[str, Any]],
    fields: Mapping[str, Any],
    *,
    mode: str = "contains",
    case_sensitive: bool = False,
    index: int | None = None,
    all_matches: bool = False,
) -> list[dict[str, Any]]:
    """What ``ui find`` reports: the node ``ui tap`` would act on, or every match.

    Without ``all_matches`` it is ``resolve_match`` — the node tap resolves,
    raising the same ambiguity and index errors — but it never refuses an
    off-screen node: find inspects, so it shows it. Either way a node that
    cannot be on screen is marked ``visible: false``, even when the tree came
    unmarked (a ``--dump``).

    With ``all_matches`` every match is listed in the order ``--index``
    counts them, so position ``i`` of the list is what ``--index i`` selects:
    on-screen matches first (tree order), then the off-screen ones (tree
    order) — or plain tree order when none is on screen, which is how
    ``resolve_match`` falls back. Every reported match — with or without
    ``all_matches`` — carries ``index``: the non-negative ``--index`` that
    selects it, or None for an off-screen match that no index reaches while
    another match is on screen.
    """
    view = selector.viewport(nodes)
    ordered = _ordered_matches(nodes, fields, mode=mode, case_sensitive=case_sensitive,
                               view=view)
    if all_matches:
        found = ordered
    else:
        node, _matched = resolve_match(nodes, fields, mode=mode,
                                       case_sensitive=case_sensitive, index=index)
        found = [] if node is None else [node]
        if node is not None:
            node["index"] = next((match["index"] for match in ordered
                                  if match.get("ref") == node.get("ref")), None)
    for match in found:  # copies: the caller's tree is never touched
        if not selector.is_visible(match, view):
            match["visible"] = False
    return found


def _ordered_matches(nodes: list[dict[str, Any]], fields: Mapping[str, Any], *,
                     mode: str, case_sensitive: bool,
                     view: Any) -> list[dict[str, Any]]:
    """Every match in the order ``--index`` counts them, each with its ``index``."""
    found = selector.select(nodes, fields, mode=mode, case_sensitive=case_sensitive,
                            all_matches=True)
    on_screen = [match for match in found if selector.is_visible(match, view)]
    if on_screen:
        found = on_screen + [match for match in found
                             if not selector.is_visible(match, view)]
    for position, match in enumerate(found):
        match["index"] = position if not on_screen or position < len(on_screen) else None
    return found


# --- live dispatch -----------------------------------------------------------


def _ios():
    from . import ui_ios  # imported lazily so a machine without Xcode can still import autonom

    return ui_ios


def snapshot(target: Target) -> list[dict[str, Any]]:
    """Every node the tree lists, unfiltered — the search corpus for find/tap.

    Nodes that cannot be on screen stay in the list and carry
    ``visible: false`` (see ``selector.mark_visibility``).
    """
    if target.platform == ANDROID:
        return selector.mark_visibility(annotate_parents(
            ui_android.parse_all(ui_android.dump_hierarchy(target.tool, target.target_id))))
    if target.platform == IOS:
        return selector.mark_visibility(
            annotate_parents(_ios().parse_all(_ios().describe_all(target))))
    raise errors.AutonomError(errors.UNKNOWN_PLATFORM, f"unknown platform: {target.platform}")


def tree(
    target: Target,
    *,
    meaningful_only: bool = True,
    max_depth: int | None = None,
    max_nodes: int | None = 200,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compact tree plus any warnings (e.g. a sparse accessibility tree).

    Off-screen nodes are kept — and counted — with ``visible: false``.
    """
    if target.platform == ANDROID:
        nodes = ui_android.parse_tree(
            ui_android.dump_hierarchy(target.tool, target.target_id),
            meaningful_only=meaningful_only,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
        return selector.mark_visibility(nodes), []
    ios = _ios()
    payload = ios.describe_all(target)
    nodes, warnings = ios.parse_tree(
        payload,
        meaningful_only=meaningful_only,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )
    # the meaningful filter drops an unlabelled application root; the screen
    # rectangle then comes from the same payload (a re-parse, no device call)
    view = selector.viewport(nodes) or selector.viewport(ios.parse_all(payload))
    return selector.mark_visibility(nodes, view), warnings


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

    A dump is not instant (an Android UI Automator dump takes a second or
    more), so the loop never starts a snapshot its forecast says cannot
    finish before the deadline. The forecast is the slower of the last two
    dumps after the first. The first dump pays one-time costs (idb reaching
    its companion, UI Automator starting up), so on its own it forecasts
    half its time — a cold first dump must not end the wait early. A dump
    therefore overruns only when it is slower than forecast: with dumps of
    even cost the second by at most half a dump, a later one not at all.
    `elapsed_ms` is always the real clock, never clamped, so an overrun is
    reported, not hidden.
    """
    take = snapshot_fn or (lambda: snapshot(target))  # type: ignore[arg-type]
    started = clock()
    deadline = started + max(timeout_ms, 0) / 1000
    interval = max(interval_ms, 10) / 1000
    previous: tuple | None = None
    stable_since = started
    snapshots = 0
    changes = 0
    first = 0.0                # how long the first (cold) dump took
    recent: list[float] = []   # the last two dumps after it

    def report(settled: bool, now: float) -> dict[str, Any]:
        # `dump_ms`: the slowest dump seen, so an unsettled answer can say
        # whether the budget ever had room for two
        slowest = max([first, *recent]) if snapshots else 0.0
        # `stable_ms`: how long the last tree had held still when it ended
        return {"settled": settled, "snapshots": snapshots, "changes": changes,
                "elapsed_ms": int((now - started) * 1000),
                "dump_ms": int(slowest * 1000),
                "stable_ms": int((now - stable_since) * 1000) if snapshots else 0}

    while True:
        began = clock()
        signature = tree_signature(take())
        snapshots += 1
        now = clock()
        if snapshots == 1:
            first = now - began
        else:
            recent = (recent + [now - began])[-2:]
        if previous is not None and signature == previous:
            if now - stable_since >= max(quiet_ms, 0) / 1000:
                return report(True, now)
        else:
            if previous is not None:
                changes += 1
            previous, stable_since = signature, now
        remaining = deadline - now
        if remaining <= 0:
            return report(False, now)
        pause = min(interval, remaining)
        forecast = max(recent) if recent else first / 2
        if pause + forecast > remaining:
            # the next dump would still be running at the deadline
            return report(False, now)
        sleep(pause)


def unsettled_message(result: Mapping[str, Any], timeout_ms: int, quiet_ms: int, *,
                      timeout_name: str = "--timeout-ms",
                      node_wait: str = "wait for a specific node with 'ui find'",
                      ) -> tuple[str, str]:
    """The ``screen_not_settled`` warning for an unsettled `settle` result:
    ``(error, hint)``, one precise statement and one piece of advice.

    "Still changing" is a claim only a change at the end supports. The other
    cases say what was seen: the tree changed and then held still, but for
    less than `quiet_ms`; identical snapshots that never spanned `quiet_ms`;
    or a single snapshot (an Android UI Automator dump can take two
    seconds, so a 3 s budget fits one) — settling could not be confirmed.
    `timeout_name` spells the budget the caller has (`--timeout-ms` on the
    CLI, `timeoutMs` in a flow); `node_wait` is how that caller waits for a
    node instead.
    """
    snapshots = int(result.get("snapshots") or 0)
    changes = int(result.get("changes") or 0)
    dump_ms = int(result.get("dump_ms") or 0)
    stable_ms = int(result.get("stable_ms") or 0)
    dump = f" (a dump takes ~{dump_ms} ms)" if dump_ms else ""
    if changes and (stable_ms <= 0 or snapshots < 2):
        return (f"the tree was still changing after {timeout_ms} ms ({changes} "
                f"change(s) over {snapshots} snapshots)",
                f"An animation, spinner or loading list keeps it moving: raise "
                f"{timeout_name}, or {node_wait}.")
    if changes:
        short = max(quiet_ms - stable_ms, 0) + dump_ms
        return (f"the tree changed {changes} time(s), then held still for {stable_ms} "
                f"ms, less than the {quiet_ms} ms quiet window, when {timeout_ms} ms "
                f"ran out{dump}",
                f"It may have just settled: raise {timeout_name} by at least ~{short} ms "
                f"so the quiet window fits.")
    if snapshots < 2:
        shown = "no snapshot" if snapshots == 0 else "only 1 snapshot"
        error = (f"could not confirm the screen settled: {shown} fit in "
                 f"{timeout_ms} ms{dump}; raise {timeout_name}")
    else:
        error = (f"could not confirm the screen settled: {snapshots} identical "
                 f"snapshots held still for {stable_ms} ms, less than the {quiet_ms} ms "
                 f"quiet window, within {timeout_ms} ms{dump}; raise {timeout_name}")
    needed = f" to at least ~{2 * dump_ms + quiet_ms} ms" if dump_ms else ""
    return (error,
            f"Nothing was seen changing, but the budget was too short to prove the "
            f"screen still: raise {timeout_name}{needed} (two dumps plus the quiet "
            f"window).")


_INTERACTABLE_ROLES = ("button", "textfield", "searchfield", "securetextfield", "switch",
                       "checkbox", "radio", "link", "slider", "tab")


def is_interactable(node: Mapping[str, Any]) -> bool:
    """Could an agent act on this node? Disabled nodes never count.

    Acting means clicking, long-clicking, checking, scrolling or typing.
    Being focusable is not enough: Flutter on Android marks every static
    label focusable (for the screen reader), and those are not controls.
    Android nodes carry ``long_clickable`` and ``checkable`` (UI Automator's
    ``long-clickable``/``checkable``), so a long-press-only or checkable-only
    Flutter control still counts.
    """
    if node.get("enabled") is False:
        return False
    if (node.get("clickable") or node.get("long_clickable") or node.get("checkable")
            or node.get("scrollable") or is_editable(node)):
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
    if node.get("visible") is False:
        flags.append("offscreen")  # listed by the tree, but not on screen
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

    Android reports window focus too: the window's FrameLayout says
    ``focused`` ahead of the EditText that holds the input focus. So an
    editable focused node wins; otherwise the deepest focused node (the
    first of equals), which is where input focus sits in the hierarchy.
    """
    focused = [node for node in nodes if node.get("focused")]
    if not focused:
        return None
    editable = [node for node in focused if is_editable(node)]
    return max(editable or focused, key=lambda node: int(node.get("depth") or 0))
