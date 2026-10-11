"""Pre-test guard: action budget, time budget, forbidden targets, allowed apps.

``before_mutation`` runs at the start of every shared mutating function, before
any device command of that operation:

- ``ui.tap``, ``ui.long_press``, ``ui.double_tap`` (by node or point),
  ``ui.swipe``, ``ui.type_text``, ``ui.press_key``, ``ui.gesture``;
- ``device_state.open_url`` (flow ``openLink`` steps and the iOS ``open``);
- the Android app launches (``session.launch_app``,
  ``session.launch_app_fresh``) and ``session.force_stop`` /
  ``session.clear_data``;
- on iOS, ``ios_simctl.launch`` and ``ios_simctl.terminate``, which
  ``session`` wraps at import: they are the launch and stop choke points used by
  `session start --launch`, `session launch`, `session stop-app`, the flow
  executor and Canvas, none of which call a launch function of ``session``.

An app launch is one guarded operation. Android ``launch_app_fresh`` is one
function. An iOS fresh launch is ``simctl terminate`` followed at once by
``simctl launch`` of the same app, made by callers outside ``session``: the
single check (budget, minutes, allowed apps) runs before the terminate and
counts one action, and the launch that immediately follows it on the same
thread (``follow_up``) is neither counted nor checked again, so a spent budget
never leaves the app stopped half way.

Without an active pre-test (or inside ``exempt()``) it returns at once: it
reads the session pointer and the small ``pretests/active.json`` file, never
the device (tp-028).

With an active pre-test on the same target it refuses, in this order:

1. ``budget_exhausted`` -- ``max_minutes`` since the pre-test started have
   passed, or ``max_actions`` mutating actions were already counted;
2. ``forbidden_action`` -- the ``text``/``desc``/``label`` of what a tap,
   long-press or double-tap acts on matches a forbid pattern (case-insensitive
   regex): the named node, the smallest on-screen node under the point, the
   nearest enclosing control (a clickable node smaller than half the screen)
   and every node inside that control -- an Android row takes the tap while its
   label sits on a child. The tree is this thread's last snapshot of the target
   when the named node came from it, else a fresh read; when no tree can be
   read the action is refused (DEC-007);
3. ``app_not_allowed`` -- a launch, stop or clear-data of an app outside a
   non-empty allow list (an Android launch with a full ``pkg/.Activity``
   component is checked against that component's package), or an Android
   ``open`` whose handler, asked from the package manager first, is outside
   it (a handler that cannot be asked refuses). A URL the package manager
   hands to the system app chooser (``android/...ResolverActivity``: no app
   has verified the link) is refused as such (``chooser: true``), never as
   an app named "android".

Every guarded point tap reads a fresh tree and resolves the node at the
point: nothing is cached between taps, because the screen may change between
them (a tap that opens a dialog puts a different control under the same
point). So repeated point taps (a flow ``tapOn`` with ``repeat`` passes no
node for taps 2..N) are slow during a pre-test -- one tree read each, about
2.5 s on a device -- and a race criterion (a double tap) is judged only by
an observed request count, else inconclusive.

An allowed action is counted in ``budget.json`` under the pre-test lock (the
count and the limit are re-checked there, so two processes never both take
the last action). Every refusal is appended to ``budget.json.refusals``
(``{"code", "verb", "ts"}``) and raised as ``AutonomError``; the CLI's normal
error path journals it with its code.

``exempt()`` marks input that is not the agent's on this thread: human-origin
Canvas input, and the read-only ``ui tree`` Flutter accessibility recovery
(force-stop and relaunch), which the CLI wraps in it (contract section 9). It
is neither counted nor refused. Read-only verbs are never counted or refused.

Documented limits:

1. A flow ``eraseText`` step counts each key press (one action per DEL):
   counting per flow step needs the flow executor.
2. On iOS an ``open`` is counted but not checked against the allow list,
   because iOS cannot report which app will take a URL.
3. The Android ``autonom open`` CLI verb sends its own ``am start -a VIEW``;
   it is guarded only once its handler calls ``before_mutation("open",
   target=..., url=...)`` first (cli task, contract section 9).
4. An iOS ``simctl terminate`` cannot tell a stop from the first half of a
   fresh launch, so its check is recorded under the verb ``stop``. A stop
   followed within ``FOLLOW_UP_SECONDS`` on the same thread, with no other
   guarded action between, by a launch of the same app on the same simulator
   (a flow ``stopApp`` step straight before a resume ``launchApp``) counts as
   one action, exactly like the fresh launch it is equivalent to.
"""
from __future__ import annotations

import calendar
import contextlib
import re
import threading
import time
from typing import Any, Iterator, Mapping

from .. import errors
from . import store

DEFAULT_FORBID = tuple(store.DEFAULT_FORBID_TEXT)

# The verbs the guard knows, as the journal names them.
MUTATING_VERBS = ("tap", "long-press", "double-tap", "swipe", "type", "key", "gesture",
                  "open", "launch", "stop", "clear")
# Verbs whose target is a node or a point on screen: the forbid patterns apply.
POINTED_VERBS = frozenset({"tap", "long-press", "double-tap"})
# Verbs that start, stop or reset an app: the allow list applies.
APP_VERBS = frozenset({"open", "launch", "stop", "clear"})

NODE_TEXT_FIELDS = ("text", "desc", "label")

_LOCAL = threading.local()
_clock = time.time  # patched by tests

# Package and class of Android's app chooser, which resolve-activity names
# when no app has verified a link.
CHOOSER_PACKAGE = "android"


# --- exemption ---------------------------------------------------------------------


@contextlib.contextmanager
def exempt() -> Iterator[None]:
    """Human-origin input on this thread: not counted, never refused."""
    _LOCAL.depth = getattr(_LOCAL, "depth", 0) + 1
    try:
        yield
    finally:
        _LOCAL.depth -= 1


def is_exempt() -> bool:
    return getattr(_LOCAL, "depth", 0) > 0


# --- lookup ------------------------------------------------------------------------


def _current_session() -> dict[str, Any] | None:
    """The session this invocation is bound to; None on any problem (a
    missing or unreadable store must never fail an existing command)."""
    try:
        from .. import session as session_mod

        record = session_mod.load_current()
    except Exception:  # noqa: BLE001 - no pre-test can be read: nothing to guard
        return None
    if not isinstance(record, dict) or record.get("stopped_at"):
        return None
    return record


def _governed(record: Mapping[str, Any], target: Any) -> bool:
    """A pre-test governs its own device only."""
    if target is None:
        return True
    target_id = target.get("target_id") if isinstance(target, Mapping) \
        else getattr(target, "target_id", None)
    expected = (record.get("target") or {}).get("target_id")
    return not expected or not target_id or str(target_id) == str(expected)


def _active(target: Any) -> tuple[dict[str, Any], dict[str, Any]] | None:
    session = _current_session()
    if session is None or store.active_ids(session) is None:
        return None
    record = store.active(session)
    if record is None or not _governed(record, target):
        return None
    return session, record


# --- time --------------------------------------------------------------------------


def _epoch(stamp: Any) -> float | None:
    if not isinstance(stamp, str):
        return None
    text = stamp.strip().rstrip("Z")
    base, _, fraction = text.partition(".")
    try:
        seconds = calendar.timegm(time.strptime(base, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
    try:
        return seconds + (float("0." + fraction) if fraction.isdigit() else 0.0)
    except ValueError:
        return float(seconds)


def _minutes_elapsed(record: Mapping[str, Any]) -> float | None:
    started = _epoch(record.get("created_at"))
    if started is None:
        return None
    return max(0.0, (_clock() - started) / 60.0)


def _limits(record: Mapping[str, Any]) -> tuple[int, float]:
    budget = record.get("budget") or {}
    max_actions = budget.get("max_actions")
    max_minutes = budget.get("max_minutes")
    if isinstance(max_actions, bool) or not isinstance(max_actions, int) or max_actions < 1:
        max_actions = store.DEFAULT_MAX_ACTIONS
    if isinstance(max_minutes, bool) or not isinstance(max_minutes, (int, float)) \
            or max_minutes <= 0:
        max_minutes = store.DEFAULT_MAX_MINUTES
    return max_actions, float(max_minutes)


# --- refusals ----------------------------------------------------------------------


def _refuse(session: dict[str, Any], record: Mapping[str, Any], verb: str,
            error: errors.AutonomError) -> errors.AutonomError:
    """Append the refusal to budget.json (best effort) and hand the error back."""
    pretest_id = record["pretest_id"]
    try:
        with store.lock(session, pretest_id):
            budget = store.read_budget(session, pretest_id)
            budget["refusals"].append({"code": error.code, "verb": verb, "ts": store.now()})
            store.write_json(store.pretest_dir(session, pretest_id) / store.BUDGET_FILE,
                             budget)
    except Exception:  # noqa: BLE001 - the refusal itself must still be raised
        pass
    return error


def _budget_error(record: Mapping[str, Any], verb: str, *, used: int,
                  max_actions: int, minutes: float | None,
                  max_minutes: float) -> errors.AutonomError | None:
    common = {"pretest_id": record.get("pretest_id"), "verb": verb,
              "actions_used": used, "max_actions": max_actions,
              "max_minutes": max_minutes}
    if minutes is not None and minutes >= max_minutes:
        return errors.AutonomError(
            errors.BUDGET_EXHAUSTED,
            f"the pre-test's {max_minutes:g}-minute budget is used up; no input was sent",
            hint="Finish with 'autonom pretest pack', or start a new pre-test with a "
                 "larger --max-minutes.",
            minutes_elapsed=round(minutes, 2), **common)
    if used >= max_actions:
        return errors.AutonomError(
            errors.BUDGET_EXHAUSTED,
            f"the pre-test's budget of {max_actions} actions is used up; no input was sent",
            hint="Finish with 'autonom pretest pack', or start a new pre-test with a "
                 "larger --max-actions.",
            **common)
    return None


# --- forbidden targets -------------------------------------------------------------


def _match(pattern: str, value: str) -> bool:
    try:
        return re.search(pattern, value, re.IGNORECASE) is not None
    except re.error:  # a hand-edited record: fall back to a literal match
        return pattern.casefold() in value.casefold()


def _forbidden_field(node: Mapping[str, Any], patterns: list[str]) -> tuple[str, str] | None:
    for field in NODE_TEXT_FIELDS:
        value = node.get(field)
        if not isinstance(value, str) or not value:
            continue
        for pattern in patterns:
            if _match(pattern, value):
                return field, pattern
    return None


def _box(bounds: Any) -> tuple[int, int, int, int] | None:
    """``[left, top, right, bottom]`` as ints, None when malformed or empty."""
    try:
        left, top, right, bottom = (int(v) for v in bounds)
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def node_at(nodes: list[dict[str, Any]], x: int, y: int) -> dict[str, Any] | None:
    """The smallest on-screen node whose bounds contain the point (the deepest
    one on a tie: pre-order lists put children after their parent)."""
    best: dict[str, Any] | None = None
    best_area: int | None = None
    for node in nodes:
        if node.get("visible") is False:
            continue
        box = _box(node.get("bounds"))
        if box is None:
            continue
        left, top, right, bottom = box
        if not (left <= x <= right and top <= y <= bottom):
            continue
        area = (right - left) * (bottom - top)
        if best_area is None or area <= best_area:
            best, best_area = node, area
    return best


# A clickable node at least this share of the screen is a layout, not one
# control: its whole subtree is not what a tap on it means.
CONTROL_MAX_SCREEN_SHARE = 0.5


def _is_control(node: Mapping[str, Any], screen_area: int) -> bool:
    """A node that takes the tap for everything drawn inside it (an Android
    clickable row whose label sits on a child TextView)."""
    if not (node.get("clickable") or node.get("long_clickable") or node.get("checkable")):
        return False
    box = _box(node.get("bounds"))
    if box is None:
        return False
    area = (box[2] - box[0]) * (box[3] - box[1])
    return not screen_area or area < screen_area * CONTROL_MAX_SCREEN_SHARE


def _screen_area(nodes: list[dict[str, Any]]) -> int:
    areas = [(b[2] - b[0]) * (b[3] - b[1])
             for b in (_box(n.get("bounds")) for n in nodes) if b is not None]
    return max(areas, default=0)


def _locate(nodes: list[dict[str, Any]] | None,
            node: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """``node`` in ``nodes``: the same object, else the same ref and bounds."""
    if not nodes or node is None:
        return None
    for item in nodes:
        if item is node:
            return item
    ref, bounds = node.get("ref"), node.get("bounds")
    if not ref:
        return None
    for item in nodes:
        if item.get("ref") == ref and item.get("bounds") == bounds:
            return item
    return None


def _scope(nodes: list[dict[str, Any]], hit: dict[str, Any],
           screen_area: int) -> Iterator[tuple[dict[str, Any], str]]:
    """What a tap on ``hit`` acts on, labelled by relation: the node itself;
    its ancestors up to the nearest enclosing control; and every node inside
    that control (the tap fires the control, whatever child shows the label)."""
    yield hit, "target"
    by_ref = {n["ref"]: n for n in nodes if n.get("ref")}
    control: dict[str, Any] | None = hit if _is_control(hit, screen_area) else None
    chain: list[dict[str, Any]] = []
    seen = {id(hit)}
    current = hit
    while control is None:
        parent = by_ref.get(current.get("parent"))
        if parent is None or id(parent) in seen:
            break
        seen.add(id(parent))
        chain.append(parent)
        if _is_control(parent, screen_area):
            control = parent
        current = parent
    if control is None:
        return  # no enclosing control: only the node itself takes the tap
    for ancestor in chain:
        yield ancestor, "ancestor"
    children: dict[str, list[dict[str, Any]]] = {}
    for item in nodes:
        parent_ref = item.get("parent")
        if parent_ref:
            children.setdefault(parent_ref, []).append(item)
    stack = list(children.get(control.get("ref"), [])) if control.get("ref") else []
    inside: set[int] = {id(control)}
    while stack:
        item = stack.pop()
        if id(item) in inside:
            continue
        inside.add(id(item))
        if item is not hit:
            yield item, "descendant"
        if item.get("ref"):
            stack.extend(children.get(item["ref"], []))


def _unreadable(record: Mapping[str, Any], verb: str, point: tuple[int, int] | None,
                reason: str) -> errors.AutonomError:
    where = f"at ({point[0]}, {point[1]})" if point is not None else "around the target"
    payload: dict[str, Any] = {"pretest_id": record.get("pretest_id"), "verb": verb,
                               "reason": reason}
    if point is not None:
        payload["point"] = list(point)
    return errors.AutonomError(
        errors.FORBIDDEN_ACTION,
        f"could not read the screen to check what is {where}; no input was sent",
        hint="While a pre-test is active a tap needs a readable UI tree "
             "(DEC-007). Run 'autonom ui tree' and tap by selector "
             "(--text/--desc/--resource-id) instead.",
        **payload)


_RELATION_TEXT = {
    "target": "",
    "ancestor": " (on the control that takes the tap)",
    "descendant": " (inside the control that takes the tap)",
}


def _forbidden(record: Mapping[str, Any], verb: str, field: str, pattern: str,
               relation: str, ref: Any = None) -> errors.AutonomError:
    extra = {"node_ref": ref} if ref else {}
    return errors.AutonomError(
        errors.FORBIDDEN_ACTION,
        f"the {verb} target's {field}{_RELATION_TEXT[relation]} matches the forbidden "
        f"pattern {pattern!r}; no input was sent",
        hint="The pre-test forbids this action (--forbid-text). Do it by hand if it "
             "is really needed, or start the pre-test with different --forbid-text "
             "patterns.",
        pretest_id=record.get("pretest_id"), verb=verb, pattern=pattern, field=field,
        matched=relation, **extra)


def _read_tree(target: Any) -> list[dict[str, Any]]:
    from .. import ui as ui_mod  # lazy: ui imports this module's caller chain

    return ui_mod.snapshot(target)


def _cached_tree(target: Any) -> list[dict[str, Any]] | None:
    from .. import ui as ui_mod

    try:
        return ui_mod.last_snapshot(target)
    except Exception:  # noqa: BLE001 - no cache: read a fresh tree
        return None


def _forbid_error(record: Mapping[str, Any], verb: str, node: Mapping[str, Any] | None,
                  point: tuple[int, int] | None, target: Any) -> errors.AutonomError | None:
    """Refuse a tap whose target, enclosing control or the labels inside that
    control match a forbid pattern.

    The node the caller names is checked first (no read). Then the tree it
    came from (this thread's last snapshot of the target, else a fresh read;
    unreadable or empty refuses, DEC-007) gives its surroundings, and the
    point gives the smallest on-screen node under it, with the same
    surroundings."""
    patterns = [str(p) for p in ((record.get("budget") or {}).get("forbid_text") or [])]
    if not patterns:
        return None
    if node is not None:
        hit = _forbidden_field(node, patterns)
        if hit is not None:
            return _forbidden(record, verb, *hit, "target", node.get("ref"))
    if node is None and point is None:
        return None
    if target is None:
        if point is None:
            return None  # only the named node can be checked
        return _unreadable(record, verb, point, "no target to read")
    nodes = _cached_tree(target) if node is not None else None
    located = _locate(nodes, node)
    if located is None:
        try:
            nodes = _read_tree(target)
        except Exception as exc:  # noqa: BLE001 - any read failure refuses (DEC-007)
            code = getattr(exc, "code", None) or type(exc).__name__
            return _unreadable(record, verb, point, str(code))
        if not any(_box(item.get("bounds")) for item in nodes or []):
            return _unreadable(record, verb, point, "empty tree")
        located = _locate(nodes, node)
    nodes = nodes or []
    targets = [located] if located is not None else []
    if point is not None:
        at = node_at(nodes, int(point[0]), int(point[1]))
        if at is not None and at is not located:
            targets.append(at)
    screen_area = _screen_area(nodes)
    for start in targets:
        for candidate, relation in _scope(nodes, start, screen_area):
            hit = _forbidden_field(candidate, patterns)
            if hit is not None:
                return _forbidden(record, verb, *hit, relation, candidate.get("ref"))
    return None


# --- allowed apps ------------------------------------------------------------------


def _url_handler(target: Any, url: str) -> tuple[bool, str | None]:
    """(known, package) of the app that would take ``url``. Android asks the
    package manager; elsewhere the handler is unknown. The system app
    chooser (no app verified the link) answers ``CHOOSER_PACKAGE``."""
    if getattr(target, "platform", None) != "android":
        return False, None
    import shlex

    from .. import adb as adb_mod

    try:
        completed = adb_mod.run_adb(
            target.tool,
            ["shell", "cmd", "package", "resolve-activity", "--brief",
             "-a", "android.intent.action.VIEW", "-d", shlex.quote(url)],
            serial=target.target_id, timeout=30, check=True)
    except Exception:  # noqa: BLE001 - unresolvable: the caller refuses
        return True, ""
    text = completed.stdout if isinstance(completed.stdout, str) else ""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if "/" in line and " " not in line:
            return True, line.split("/", 1)[0]
    return True, None  # nothing takes the URL: no app would open


def _app_error(record: Mapping[str, Any], verb: str, app_id: str | None,
               url: str | None, target: Any) -> errors.AutonomError | None:
    allow = [str(a) for a in ((record.get("budget") or {}).get("allow_apps") or [])]
    if not allow or verb not in APP_VERBS:
        return None
    common = {"pretest_id": record.get("pretest_id"), "verb": verb, "allow_apps": allow}
    hint = ("The pre-test only opens, launches, stops and clears the apps in its allow list "
            "(--allow-app; default: the session's app). Start the pre-test with "
            "--allow-app for this app if it is part of the ticket.")
    if app_id is None and url is not None:
        known, handler = _url_handler(target, url)
        if not known or handler is None:
            return None
        if handler == "":
            return errors.AutonomError(
                errors.APP_NOT_ALLOWED,
                "could not tell which app takes this URL; no input was sent",
                hint="The pre-test checks the app a URL opens against its allow list; "
                     "make sure the device answers 'adb shell cmd package "
                     "resolve-activity' and retry.", **common)
        if handler == CHOOSER_PACKAGE and CHOOSER_PACKAGE not in allow:
            return errors.AutonomError(
                errors.APP_NOT_ALLOWED,
                "this URL opens the system app chooser \u2014 the link is not verified "
                "for an allowed app; no input was sent",
                hint="Android shows a chooser when no app has verified the link. Use the "
                     "app's own scheme or a verified App Link, or open the screen from "
                     "inside the app; do not pick from the chooser by hand.",
                chooser=True, **common)
        app_id = handler
    if app_id is None or app_id in allow:
        return None
    return errors.AutonomError(
        errors.APP_NOT_ALLOWED,
        f"{app_id} is not in the pre-test's allowed apps; no input was sent",
        hint=hint, app_id=app_id, **common)


# --- the guard ---------------------------------------------------------------------


def before_mutation(verb: str, *, node: Mapping[str, Any] | None = None,
                    point: tuple[int, int] | None = None, target: Any = None,
                    app_id: str | None = None, url: str | None = None) -> bool:
    """Count one mutating action or refuse it; a no-op without an active
    pre-test on ``target`` and inside ``exempt()``. ``url`` (an ``open``)
    lets the guard find the app that takes it when ``app_id`` is not given.
    Returns True when the action was counted."""
    _LOCAL.follow_up = None  # any other guarded action ends a pending fresh launch
    if is_exempt():
        return False
    found = _active(target)
    if found is None:
        return False
    session, record = found
    pretest_id = record["pretest_id"]
    max_actions, max_minutes = _limits(record)

    # Cheap, lock-free first look: an exhausted budget refuses before any read.
    used = store.read_budget(session, pretest_id)["actions_used"]
    error = _budget_error(record, verb, used=used, max_actions=max_actions,
                          minutes=_minutes_elapsed(record), max_minutes=max_minutes)
    if error is None and verb in POINTED_VERBS:
        error = _forbid_error(record, verb, node, point, target)
    if error is None:
        error = _app_error(record, verb, app_id, url, target)
    if error is not None:
        raise _refuse(session, record, verb, error)

    try:
        held = store.lock(session, pretest_id)
    except errors.AutonomError:
        return False  # the pre-test directory vanished since the lookup: nothing to guard
    with held:
        budget = store.read_budget(session, pretest_id)
        error = _budget_error(record, verb, used=budget["actions_used"],
                              max_actions=max_actions,
                              minutes=_minutes_elapsed(record), max_minutes=max_minutes)
        if error is None:
            budget["actions_used"] += 1
            store.write_json(store.pretest_dir(session, pretest_id) / store.BUDGET_FILE,
                             budget)
    if error is not None:
        raise _refuse(session, record, verb, error)
    return True


# --- one operation, several commands ---------------------------------------------

# How long after the first command of an operation its follow-up may come.
# The callers of an iOS fresh launch run `simctl launch` right after
# `simctl terminate` returns, without any step between.
FOLLOW_UP_SECONDS = 1.0
_monotonic = time.monotonic  # patched by tests


def expect_follow_up(key: tuple[Any, ...]) -> None:
    """The guarded command just made on this thread is the first half of one
    operation; ``take_follow_up(key)`` lets its second half through once."""
    _LOCAL.follow_up = (tuple(key), _monotonic())


def take_follow_up(key: tuple[Any, ...]) -> bool:
    """True (once) when ``key`` is the follow-up ``expect_follow_up`` set on
    this thread, no other guarded action came between and it is not older
    than ``FOLLOW_UP_SECONDS``; the pending follow-up is cleared either way."""
    pending = getattr(_LOCAL, "follow_up", None)
    _LOCAL.follow_up = None
    if not pending:
        return False
    pending_key, at = pending
    return pending_key == tuple(key) and 0 <= _monotonic() - at <= FOLLOW_UP_SECONDS


def usage(session: dict[str, Any] | None,
          record: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The active (or given) pre-test's budget use, for ``pretest status``;
    ``{}`` when there is none."""
    if record is None:
        record = store.active(session)
    if not session or not record:
        return {}
    max_actions, max_minutes = _limits(record)
    budget = store.read_budget(session, record["pretest_id"])
    minutes = _minutes_elapsed(record)
    refusals = budget["refusals"]
    budget_block = record.get("budget") or {}
    return {
        "pretest_id": record["pretest_id"],
        "actions_used": budget["actions_used"],
        "max_actions": max_actions,
        "actions_left": max(0, max_actions - budget["actions_used"]),
        "minutes_elapsed": None if minutes is None else round(minutes, 2),
        "max_minutes": max_minutes,
        "minutes_left": None if minutes is None else round(max(0.0, max_minutes - minutes), 2),
        "forbid_text": list(budget_block.get("forbid_text") or []),
        "allow_apps": list(budget_block.get("allow_apps") or []),
        "refusals": len(refusals),
        "last_refusal": refusals[-1] if refusals else None,
    }
