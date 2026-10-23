"""The screen an action leads to (AL-11).

With observe on (`--observe`, or an agent-profile session), `ui tap`
(long press included), `ui type`, `ui swipe` and `ui key` do not end at the
input. After it, the screen is given time to settle within the observe
budget (`--observe-timeout-ms`, 3000 ms by default) the way `ui wait
--settled` settles it: on the iOS idb route idb is asked whether the app is
quiet and the tree is then read once; elsewhere the tree is polled until two
reads match. The read the settle ended on is the new screen. It is saved to
the session's tree history exactly as `ui tree` saves a tree, and the reply
gains:

- ``screen``: ``outline`` (the `ui tree --format outline` lines), ``count``,
  ``truncated`` and, when it was written, ``saved`` (the history file);
- ``changes``: how that tree differs from the baseline: ``added``,
  ``removed`` and ``changed`` counts and up to 20 outline ``lines`` marked
  ``+`` (new), ``-`` (gone) or ``~`` (same node, other state).
  ``changes_baseline`` names the baseline, and no read is ever made for it:

  1. ``pre_read``: the command's own full read before the input — a
     selector tap's resolution read, the focus read of `ui type`, or, on the
     iOS idb route, the `describe-all` the coordinate guard looked the
     screen size up with — built into the tree observe reads (meaningful
     nodes, at most 200);
  2. else ``saved_tree``: the session's latest saved tree of this target,
     but only when it records that it was read the way observe reads
     (``read_shape``: no ``--all``, ``--interactable`` or ``--max-depth``,
     ``--max-nodes`` 200, not cut). Trees of different shapes are never
     compared: a filtered tree would show unchanged nodes as gone or new;
  3. else ``none``: ``changes`` is null and ``changes_baseline_reason``
     says why (no session, no saved tree, a tree read another way, or one
     saved by an older version or another command, which records no shape).

  Every tree `ui tree` or observe saves carries ``read_shape`` (see
  ``read_shape``);
- ``foreground``: ``app`` and, when known, ``screen``. Android: the package
  most nodes of the read belong to, and the resumed activity `dumpsys`
  reports for it (best effort). iOS: the label of the application root and
  the first heading.

A node is the same node in both trees when its role, resource id, text,
description and bounds agree; it is ``changed`` when its state words (the
outline flags: checked, selected, focused, disabled, ...) differ.

Observe only reads: it never sends an input, so the action's single input
stays single (INV-1). A failure of any observe step never fails the action:
the reply keeps ``ok: true`` and carries ``observe_error`` (``code``,
``message``, ``hint`` when there is one) instead of the new screen.

An interrupt (Ctrl-C, a host's SIGINT) while observing ends observing at
once. The input was already sent, so the reply still says ``ok: true`` and
carries ``observe_error`` with code ``interrupted`` (the message says the
input was sent and which part of observe was cut; the hint says to read the
screen with `autonom ui tree`, not to send the input again), plus whatever
observe had already obtained: ``screen`` once the settle and its read
ended, ``changes`` once compared, ``screen.saved`` once saved, and on
Android ``foreground.app`` when only the foreground lookup was cut. The CLI
prints and journals that reply and then exits 130 (``interrupted``), so a
shell chain or a ``set -e`` script still sees the cancel.

After `ui type --sensitive`, the typed value is kept out of everything
observe reports or saves: the text and description of editable or focused
nodes, and of any node that may show the value or a part of it however
formatted (``shows_secret``), read ``<N chars>``.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

from . import adb as adb_mod
from . import errors, ui
from . import session as session_mod
from .platform import ANDROID, IOS, Target

DEFAULT_TIMEOUT_MS = 3000
# The quiet window: the one `ui wait --settled` uses by default, so an
# observed screen and an explicit wait agree on what "settled" means.
QUIET_MS = 500
# As many nodes as `ui tree` prints by default.
MAX_NODES = 200
CHANGE_LINES = 20

BASELINE_PRE_READ = "pre_read"
BASELINE_SAVED = "saved_tree"
BASELINE_NONE = "none"

# observe_error.code of an interrupt during observe: the code of the CLI's
# exit-130 envelope (errors.INTERRUPTED), but here the action itself ran.
# Only an interrupt ever gives this code: the CLI builds that envelope from
# a KeyboardInterrupt alone, and `after_action` turns any other failure that
# carries it into backend_failed, so `interrupted()` can tell the CLI to
# exit 130 after printing the reply.
INTERRUPTED = errors.INTERRUPTED

# How far observe got, for the message of an interrupt: what the reply has.
STAGE_SETTLE = "settle"          # settling and reading the screen: nothing yet
STAGE_COMPARE = "compare"        # read (screen); comparing with the baseline
STAGE_SAVE = "save"              # read and compared (screen, changes); saving
STAGE_FOREGROUND = "foreground"  # read, compared (and saved); the foreground lookup

_CUT_AT = {
    STAGE_SETTLE: "observe was interrupted before the screen it led to was read, "
                  "so this reply has no screen",
    STAGE_COMPARE: "the screen it led to was read (screen), then observe was "
                   "interrupted before comparing it with the screen before the input",
    STAGE_SAVE: "the screen it led to was read and compared (screen, changes), then "
                "observe was interrupted while saving it to the session's tree history",
    STAGE_FOREGROUND: "the screen it led to was read and compared (screen, changes, "
                      "and screen.saved when a session keeps it); only the foreground "
                      "lookup was interrupted",
}

_DUMPSYS_TIMEOUT_S = 10
_RESUMED = re.compile(r"(?:topResumedActivity|mResumedActivity|ResumedActivity)"
                      r"[^\n]*?\s([A-Za-z][\w.]*)/([\w.$]+)")


# --- when ---------------------------------------------------------------------------


def owns(record: Mapping[str, Any] | None, target: Target | None) -> bool:
    """Is ``record`` the session of ``target`` (same device, same platform)?"""
    if not record or target is None:
        return False
    return (record.get("target_id") == target.target_id
            and (record.get("platform") or ANDROID) == target.platform)


def session_for(target: Target) -> dict[str, Any] | None:
    """The session this invocation is bound to, when it owns ``target``;
    None otherwise (no session, or one on another device)."""
    try:
        record = session_mod.load_current()
    except Exception:  # noqa: BLE001 - an unreadable record names no session
        return None
    return record if owns(record, target) else None


def wanted(flag: bool | None, record: Mapping[str, Any] | None) -> bool:
    """Observe or not: ``--observe`` / ``--no-observe`` win; without either,
    on exactly in an agent-profile session (AL-09)."""
    if flag is not None:
        return bool(flag)
    return session_mod.profile_of(dict(record) if record else None) == session_mod.AGENT_PROFILE


def check_timeout(timeout_ms: int | None) -> int:
    """``--observe-timeout-ms``: milliseconds >= 0, refused before any input."""
    if timeout_ms is None:
        return DEFAULT_TIMEOUT_MS
    if timeout_ms < 0:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--observe-timeout-ms must be >= 0, got {timeout_ms}",
            f"Pass a duration in milliseconds (default {DEFAULT_TIMEOUT_MS}).")
    return int(timeout_ms)


def read_mark(target: Target) -> tuple[Any, int]:
    """Where this thread stands when a command starts: its last read of
    ``target`` and how many inputs it has sent. A read and an input the
    command makes itself can then be told from an earlier command's
    (several commands can run in one process)."""
    return ui.last_snapshot(target), ui.inputs_sent()


def own_read(target: Target, since: tuple[Any, int] | None = None) -> Any:
    """The raw read this command made of ``target`` before its input: the
    read the thread held when the command's input went out (a selector
    tap's resolution read, the focus read of `ui type`, the iOS guard's
    `describe-all` lookup), noted by ``ui`` right before it was sent. None
    when the command sent no input since ``since`` (``read_mark`` at its
    start) or made no read of its own before it. Never reads."""
    held_before, inputs_before = since if since is not None else (None, 0)
    at_input = ui.read_at_input(target, after=inputs_before)
    if at_input is None:
        return None
    nodes, payload = at_input
    if nodes is None or nodes is held_before:
        return None
    return payload


# --- trees --------------------------------------------------------------------------


def _compact(platform: str, payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """The tree `ui tree` prints for a read: meaningful nodes, at most
    MAX_NODES, with an exact ``truncated``."""
    nodes, warnings = ui.compact_from_payload(platform, payload, max_nodes=MAX_NODES + 1)
    nodes, truncated = ui.clip(nodes, MAX_NODES)
    return nodes, warnings, truncated


# How a saved tree was read (`read_shape`, written beside its nodes by every
# tree `ui tree` and observe save): which verb read it (``source``), which
# nodes it keeps (``filter``: ``default`` — the meaningful nodes `ui tree`
# prints without a flag —, ``all``, ``interactable`` or ``all+interactable``),
# ``max_depth`` (null: no limit), ``max_nodes`` and whether that limit cut
# the list (``truncated``). Two trees of different shapes list different
# nodes of one screen, so only a tree read the way observe reads is ever
# compared with observe's read.
SHAPE_UI_TREE = "ui_tree"
SHAPE_OBSERVE = "observe"
FILTER_DEFAULT = "default"
_FILTER_FLAGS = {"all": "--all", "interactable": "--interactable",
                 "all+interactable": "--all --interactable"}
_SHAPE_KEYS = frozenset({"filter", "max_depth", "max_nodes", "truncated"})


def read_shape(source: str, *, all_nodes: bool = False, interactable: bool = False,
               max_depth: int | None = None, max_nodes: int | None = MAX_NODES,
               truncated: bool = False) -> dict[str, Any]:
    """The ``read_shape`` of a tree read with these `ui tree` options."""
    kept = "+".join(name for name, on in (("all", all_nodes), ("interactable", interactable))
                    if on) or FILTER_DEFAULT
    return {"source": source, "filter": kept, "max_depth": max_depth,
            "max_nodes": max_nodes, "truncated": bool(truncated)}


def shape_mismatch(shape: Any) -> str | None:
    """None when a tree read with ``shape`` lists what observe's own read
    lists (the default filter, no depth limit, MAX_NODES, not cut); else
    why it cannot be compared with it."""
    if not isinstance(shape, dict) or not _SHAPE_KEYS <= shape.keys():
        return ("saved tree does not record how it was read "
                "(saved by an older version or by another command)")
    kept = shape.get("filter")
    if kept != FILTER_DEFAULT:
        return f"saved tree read with {_FILTER_FLAGS.get(str(kept), f'filter {kept!r}')}"
    if shape.get("max_depth") is not None:
        return f"saved tree read with --max-depth {shape.get('max_depth')}"
    if shape.get("max_nodes") != MAX_NODES:
        return f"saved tree read with --max-nodes {shape.get('max_nodes')}"
    if shape.get("truncated") is not False:
        return f"saved tree cut at {MAX_NODES} nodes"
    return None


def saved_baseline(record: Mapping[str, Any] | None,
                   target: Target) -> tuple[list[dict[str, Any]] | None, str | None]:
    """``(nodes, None)`` of the session's latest saved tree
    (`trees/latest.json`) when it is a device read of ``target`` read the
    way observe reads (``shape_mismatch``); ``(None, why not)`` otherwise.
    Never touches the device and never raises."""
    if not record:
        return None, "no session of this target to keep a saved tree"
    if not record.get("artifacts_dir"):
        return None, "no saved tree in this session"
    try:
        path = Path(str(record["artifacts_dir"])) / "trees" / "latest.json"
        if not path.is_file():
            return None, "no saved tree in this session"
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None, "saved tree unreadable"
    if not isinstance(saved, dict) or saved.get("source") != "device":
        return None, "saved tree is not a device read"
    if saved.get("target") != target.target_id or saved.get("platform") != target.platform:
        return None, "saved tree is of another target"
    why = shape_mismatch(saved.get("read_shape"))
    if why is not None:
        return None, why
    nodes = saved.get("nodes")
    if not isinstance(nodes, list) or not all(isinstance(node, dict) for node in nodes):
        return None, "saved tree has no nodes"
    return nodes, None


def save_tree(record: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    """Write ``payload`` as the next ``trees/NNNN_HHMMSS.json`` of the
    session's history and as ``trees/latest.json``, the way `ui tree` keeps
    its trees; returns the history path. Raises OSError when it cannot."""
    blob = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    # Keep the full history, not just the latest: a run's trees are evidence
    # of how the screen changed. `latest.json` stays as a stable pointer.
    stamp = time.strftime("%H%M%S", time.gmtime())
    directory = session_mod.artifact_path(dict(record), "trees")
    existing = sorted(directory.glob("[0-9]*.json"))
    seq = len(existing) + 1
    history = session_mod.artifact_path(dict(record), "trees", f"{seq:04d}_{stamp}.json")
    while history.exists():  # never replace a tree another call just saved
        seq += 1
        history = session_mod.artifact_path(dict(record), "trees", f"{seq:04d}_{stamp}.json")
    history.write_text(blob, encoding="utf-8")
    session_mod.artifact_path(dict(record), "trees", "latest.json").write_text(
        blob, encoding="utf-8")
    return str(history)


# --- diff ---------------------------------------------------------------------------


def identity(node: Mapping[str, Any]) -> tuple:
    """Which node this is, across two reads: role, resource id, text,
    description and bounds (refs are positions in one read, not names)."""
    bounds = node.get("bounds")
    return (node.get("role"), node.get("resource_id"), node.get("text"), node.get("desc"),
            tuple(bounds) if isinstance(bounds, (list, tuple)) else None)


def diff(before: list[dict[str, Any]], after: list[dict[str, Any]], *,
         limit: int = CHANGE_LINES) -> dict[str, Any]:
    """``{"added", "removed", "changed", "lines"}``: counts, and up to
    ``limit`` outline lines — the added nodes (``+``), then the changed ones
    (``~``, the new state), then the removed ones (``-``), each group in
    tree order. Repeated identical nodes are paired in order."""
    pending: dict[tuple, list[int]] = {}
    for index, node in enumerate(before):
        pending.setdefault(identity(node), []).append(index)
    matched: set[int] = set()
    added: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for node in after:
        queue = pending.get(identity(node))
        if queue:
            index = queue.pop(0)
            matched.add(index)
            if ui.outline_flags(before[index]) != ui.outline_flags(node):
                changed.append(node)
        else:
            added.append(node)
    removed = [node for index, node in enumerate(before) if index not in matched]
    lines = ([f"+ {ui.outline_line(node)}" for node in added]
             + [f"~ {ui.outline_line(node)}" for node in changed]
             + [f"- {ui.outline_line(node)}" for node in removed])
    return {"added": len(added), "removed": len(removed), "changed": len(changed),
            "lines": lines[:max(limit, 0)]}


# --- foreground ---------------------------------------------------------------------


def _android_package(nodes: list[dict[str, Any]]) -> str | None:
    from .atlas import fingerprint

    found = fingerprint.foreground_package(nodes)
    if found:
        return found
    return next((str(node["package"]) for node in nodes if node.get("package")), None)


def _resumed_activity(target: Target) -> tuple[str, str] | None:
    """(package, activity) of the resumed activity `dumpsys` reports; None
    when it cannot tell. Best effort: never raises."""
    try:
        completed = adb_mod.run_adb(target.tool, ["shell", "dumpsys", "activity", "activities"],
                                    serial=target.target_id, timeout=_DUMPSYS_TIMEOUT_S,
                                    check=False)
    except (errors.AutonomError, subprocess.TimeoutExpired, OSError):
        return None
    text = completed.stdout if isinstance(completed.stdout, str) else ""
    match = _RESUMED.search(text)
    return (match.group(1), match.group(2)) if match else None


def _ios_label(node: Mapping[str, Any]) -> str | None:
    label = node.get("desc") or node.get("text")
    return str(label) if label else None


def foreground(target: Target, nodes: list[dict[str, Any]],
               into: dict[str, Any] | None = None) -> dict[str, Any]:
    """``{"app"}`` plus ``screen`` when known; ``app`` is None when the read
    does not say. Filled into ``into`` (when given) as it is learnt, so an
    interrupt during the Android `dumpsys` call leaves the app the read
    named there."""
    if target.platform == ANDROID:
        app = _android_package(nodes)
        result: dict[str, Any] = into if into is not None else {}
        result["app"] = app
        resumed = _resumed_activity(target)
        if resumed is not None and (app is None or resumed[0] == app):
            # an activity of another package (a dialog over the app) is not
            # the screen this read shows
            result["app"] = app or resumed[0]
            result["screen"] = resumed[1]
        return result
    app = next((_ios_label(node) for node in nodes
                if node.get("role") == "app" and int(node.get("depth") or 0) == 0), None)
    result = into if into is not None else {}
    result["app"] = app
    heading = next((_ios_label(node) for node in nodes
                    if node.get("role") == "heading" and _ios_label(node)), None)
    if heading is None:
        heading = next((_ios_label(node) for node in nodes
                        if node.get("role") == "navbar" and _ios_label(node)), None)
    if heading is not None:
        result["screen"] = heading
    return result


# --- the step -----------------------------------------------------------------------


# A run of this many letters and digits of the typed value, found in a text,
# counts as the value shown in part (fewer for a shorter value).
_SHOWN_RUN = 4
_WORD = re.compile(r"[^\W_]+")


def _plain(value: str) -> str:
    """Letters and digits only, case folded: how a value compares however a
    field spaces, groups, punctuates or capitalises it."""
    return "".join(char for char in value.casefold() if char.isalnum())


def shows_secret(value: str, secret: str) -> bool:
    """Could ``value`` (a node's text or description) show ``secret`` or a
    part of it? A field does not have to echo what was typed: it formats a
    card number in groups, splits a code into one-digit boxes, cuts a PIN at
    its maximum length or shows the first characters only. So, letters and
    digits compared without case:

    - the whole value appears in it, however spaced or punctuated;
    - it is one character long (every such text, not only the ones that
      occur in the value, so which ones were hidden tells nothing: a one-digit
      code box and a key of a PIN pad look the same);
    - all of it, or one of its words, is a run of at least 4 characters
      (the whole value when shorter) of the value: a truncated, partly
      rendered or grouped value, or its last digits in a sentence.
    """
    if secret in value:
        return True
    plain = _plain(value)
    if not plain:
        return False  # punctuation, bullets or dots only: nothing of the value
    if len(plain) == 1:
        return True
    plain_secret = _plain(secret)
    if not plain_secret:
        return False  # a value of symbols only is matched exactly, above
    if plain_secret in plain:
        return True
    run = min(_SHOWN_RUN, len(plain_secret))
    if len(plain) >= run and plain in plain_secret:
        return True
    return any(len(word) >= run and word in plain_secret
               for word in _WORD.findall(value.casefold()))


def masked(nodes: list[dict[str, Any]], secret: str | None) -> list[dict[str, Any]]:
    """``nodes`` as they may be reported and saved after `ui type --sensitive`
    typed ``secret``: the reply and the saved tree keep only its length, as
    every other artifact of the command does.

    Matching the typed string is not enough, since a field may reformat,
    split or cut what it shows. So the text and description of every
    editable or focused node (where the value went, however it is shown
    there) and of every node that ``shows_secret`` read ``<N chars>``, N
    being the length of the typed value. Role, resource id, bounds and state
    stay, so the screen can still be acted on."""
    if not secret:
        return nodes
    shown = f"<{len(secret)} chars>"
    out: list[dict[str, Any]] = []
    for node in nodes:
        field = ui.is_editable(node) or bool(node.get("focused"))
        hidden = [key for key in ("text", "desc")
                  if isinstance(node.get(key), str) and node[key]
                  and (field or shows_secret(node[key], secret))]
        if hidden:
            node = dict(node)
            for key in hidden:
                node[key] = shown
        out.append(node)
    return out


def _scrubbed(error: dict[str, Any], secret: str | None) -> dict[str, Any]:
    """An observe error with ``secret`` cut out of its message and hint."""
    if not secret:
        return error
    shown = f"<{len(secret)} chars>"
    return {key: value.replace(secret, shown) if isinstance(value, str) and key != "code"
            else value for key, value in error.items()}


def _error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, errors.AutonomError):
        error: dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.hint:
            error["hint"] = exc.hint
        return error
    if isinstance(exc, subprocess.TimeoutExpired):
        return {"code": errors.BACKEND_FAILED,
                "message": f"the screen read timed out after {exc.timeout:g} s"}
    return {"code": errors.BACKEND_FAILED,
            "message": " ".join(str(exc).split())[:300] or exc.__class__.__name__}


def interrupted(keys: Mapping[str, Any]) -> bool:
    """Did an interrupt end the observe step that gave ``keys``? Then the
    CLI prints and journals the reply as usual and exits 130 afterwards."""
    error = keys.get("observe_error")
    return isinstance(error, dict) and error.get("code") == INTERRUPTED


def _interrupted_error(verb: str, stage: str) -> dict[str, Any]:
    return {
        "code": INTERRUPTED,
        "message": f"the input of {verb} was sent; {_CUT_AT[stage]}",
        "hint": "The input was delivered: do not send it again. Run autonom ui tree "
                "to see the screen.",
    }


def after_action(target: Target, record: Mapping[str, Any] | None, *, verb: str,
                 baseline_payload: Any = None, timeout_ms: int = DEFAULT_TIMEOUT_MS,
                 quiet_ms: int = QUIET_MS, secret: str | None = None,
                 ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Observe after the input of ``verb``: ``(reply keys, warnings)``.

    Never raises: a failure comes back as ``{"observe_error": {...}}``
    instead of the new screen. An interrupt (Ctrl-C, or a host's SIGINT
    while the settle polls) ends observing at once and comes back the same
    way, with code ``interrupted`` (see ``interrupted``), beside what observe
    had already obtained: the input was already sent, so the action ran,
    and a reply that said it failed would invite the caller to send the
    input again. ``baseline_payload`` is the command's own read before the
    input (``own_read``), or None; ``secret`` is a typed value to keep out
    of everything observe reports or saves."""
    progress: dict[str, Any] = {"stage": STAGE_SETTLE, "keys": {}, "warnings": []}
    try:
        _observe(target, record, verb=verb, baseline_payload=baseline_payload,
                 timeout_ms=timeout_ms, quiet_ms=quiet_ms, secret=secret, progress=progress)
    except KeyboardInterrupt:
        keys = dict(progress["keys"])
        keys["observe_error"] = _interrupted_error(verb, progress["stage"])
        return keys, list(progress["warnings"])
    except Exception as exc:  # noqa: BLE001 - observing must never fail the action
        error = _scrubbed(_error(exc), secret)
        if error.get("code") == INTERRUPTED:  # that code is the interrupt's alone
            error["code"] = errors.BACKEND_FAILED
        return {"observe_error": error}, []
    return progress["keys"], progress["warnings"]


def _observe(target: Target, record: Mapping[str, Any] | None, *, verb: str,
             baseline_payload: Any, timeout_ms: int, quiet_ms: int,
             secret: str | None, progress: dict[str, Any]) -> None:
    """Fill ``progress`` (``keys`` and ``warnings`` of the reply, and the
    ``stage`` reached) as each part is obtained, so an interrupt keeps what
    came before it."""
    held: dict[str, Any] = {}
    keys: dict[str, Any] = progress["keys"]
    warnings: list[dict[str, Any]] = progress["warnings"]

    def read() -> list[dict[str, Any]]:
        payload = ui.read_payload(target)
        nodes, read_warnings, truncated = _compact(target.platform, payload)
        held["read"] = (nodes, read_warnings, truncated)
        return nodes

    # the AL-08 settle: idb quiet then one read on the iOS idb route, polling
    # elsewhere; every read it makes is ours, so the last one is kept. A read
    # of a settle that did not end is not reported: it may be mid-change.
    settled = ui.settle(target, timeout_ms=timeout_ms, quiet_ms=quiet_ms,
                        snapshot_fn=read, quiet_signal=True)
    if "read" not in held:
        read()  # the settle made no read of its own: one, and only one, more
    nodes, tree_warnings, truncated = held["read"]
    nodes = masked(nodes, secret)
    keys["screen"] = screen = {"outline": ui.outline(nodes), "count": len(nodes),
                               "truncated": truncated}
    if not settled.get("settled"):
        error, hint = ui.unsettled_message(settled, timeout_ms, quiet_ms,
                                           timeout_name="--observe-timeout-ms")
        warnings.append({"code": "screen_not_settled", "error": error, "hint": hint})
    progress["stage"] = STAGE_COMPARE

    # the baseline is fixed before this read is saved over `latest.json`:
    # the command's own read before the input, built into this read's shape;
    # else a saved tree read the same way; else none, and why
    baseline: list[dict[str, Any]] | None = None
    source = BASELINE_NONE
    why_none: str | None = None
    if baseline_payload is not None:
        try:
            baseline = masked(_compact(target.platform, baseline_payload)[0], secret)
            source = BASELINE_PRE_READ
        except Exception:  # noqa: BLE001 - an unusable baseline is no baseline
            baseline = None
    if baseline is None:
        baseline, why_none = saved_baseline(record, target)
        if baseline is not None:
            baseline = masked(baseline, secret)
        source = BASELINE_SAVED if baseline is not None else BASELINE_NONE
    changes = diff(baseline, nodes) if baseline is not None else None
    keys["changes"] = changes
    keys["changes_baseline"] = source
    if source == BASELINE_NONE:
        keys["changes_baseline_reason"] = why_none or "no baseline"
    progress["stage"] = STAGE_SAVE

    if record is not None:
        tree_payload: dict[str, Any] = {
            "ok": True, "source": "device", "target": target.target_id,
            "count": len(nodes), "nodes": nodes, **target.identity(),
            "purpose": "observe", "after": verb,
            "read_shape": read_shape(SHAPE_OBSERVE, truncated=truncated),
        }
        if truncated:
            tree_payload["truncated"] = True
        if target.platform == IOS:
            size = ui.screen_from_nodes(nodes)
            if size:
                tree_payload["screen"] = list(size)
        if tree_warnings:
            tree_payload["warnings"] = tree_warnings
        from . import screenshot as shot_mod

        attribution = shot_mod.pretest_attribution(dict(record))
        if attribution:
            tree_payload.update(attribution)
            if attribution.get("check_id"):
                tree_payload["task"] = attribution["check_id"]
        try:
            screen["saved"] = save_tree(record, tree_payload)
        except OSError as exc:
            warnings.append({
                "code": "evidence_write_failed",
                "error": " ".join(str(exc).split())[:300] or exc.__class__.__name__,
                "hint": "The action ran and the screen was read; only the tree was not "
                        "saved to the session history. Check free space and permissions "
                        f"of {Path(str(record.get('artifacts_dir') or '')) / 'trees'}."})
    progress["stage"] = STAGE_FOREGROUND

    keys["foreground"] = {}
    foreground(target, nodes, into=keys["foreground"])
