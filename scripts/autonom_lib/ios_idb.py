"""`idb` wrapper — the iOS Development Bridge (DEC-002).

idb is to iOS roughly what adb is to Android: a companion process talks to the
simulator or device, and a thin client drives it — optionally from another
machine, which is what makes a remote Mac farm possible (CAP-IOSS-006).

idb reaches its capabilities through Apple private frameworks, so it is the part
of the stack most likely to break on an Xcode upgrade (RISK-006). Every piece of
knowledge about idb's command line is therefore confined to this module; the
parser above it is fixture-driven and shape-tolerant, so a drift costs one
adapter rather than a rewrite.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import errors
from . import processes as processes_mod
from .platform import Target

BUTTONS = ("APPLE_PAY", "HOME", "LOCK", "SIDE_BUTTON", "SIRI")

# What `idb ui` actually accepts. Kept here, next to the code that builds those
# argv lists, because this list is the thing that goes stale on an idb upgrade.
UI_SUBCOMMANDS = (
    "describe-all", "describe-point", "tap", "button", "text", "key",
    "key-sequence", "swipe",
)
GESTURES = ("swipe",)
# Offered by the CLI, backed by nothing. idb has no pinch/rotate/shake — not
# under `idb ui`, not at the top level (verified against fb-idb 1.1.7) — so
# these were dispatched as commands that do not exist and came back as
# `backend_failed` carrying idb's own argparse usage text. Refusing them the way
# Android gestures are already refused is the honest shape: a typed code, and a
# hint that names the alternative instead of sending the reader to `doctor`.
UNBACKED_GESTURES = ("pinch", "rotate", "shake")

# Xcode 27 moved SimulatorKit.framework to <Xcode.app>/Contents/SharedFrameworks.
# idb_companion before 1.6.2 (upstream commit 9811012, built after 2026-06-09)
# still loads it from <DEVELOPER_DIR>/Library/PrivateFrameworks, so every HID
# verb (tap, swipe, text, key, button) fails while describe-all keeps working.
SIMULATORKIT_MARKER = "simulatorkit is required for hid"
COMPANION_FIXED_VERSION = (1, 6, 2)
COMPANION_FIXED_DATE = date(2026, 6, 9)
HID_UPGRADE_FIX = "brew update && brew upgrade idb-companion (and pipx upgrade fb-idb)"
HID_FRAMEWORK_HINT = (
    f"idb_companion cannot load SimulatorKit from this Xcode. Upgrade it: "
    f"{HID_UPGRADE_FIX}. Or install AXe (brew install cameroncooke/axe/axe) and "
    "set AUTONOM_IOS_HID=axe; the accessibility tree keeps working either way."
)
# Where the fb-idb client records local companions. Pruning is visible there
# as entries disappearing; AUTONOM_IDB_STATE_FILE points tests elsewhere.
IDB_STATE_FILE = "/tmp/idb/state"


def find_idb(explicit: str | None = None) -> str:
    """Resolve the idb client. Order: flag, environment, PATH."""
    candidate = explicit or os.environ.get("AUTONOM_IDB")
    if candidate:
        return candidate
    path = shutil.which("idb")
    if not path:
        raise errors.tool_missing("idb")
    return path


def companion_endpoint(host: str | None = None, port: int | None = None) -> str | None:
    """`host:port` for a remote companion, or None for a local one."""
    if host:
        return f"{host}:{port or 10882}"
    configured = os.environ.get("AUTONOM_IDB_COMPANION")
    return configured or None


def target_companion(target: Target | None) -> str | None:
    """The companion endpoint for ``target``: an ``idb_companion`` alias the
    CLI placed on the Target, else AUTONOM_IDB_COMPANION (which
    ``--idb-host/--idb-port`` are promoted to)."""
    aliases = getattr(target, "aliases", None) or {}
    return aliases.get("idb_companion") or companion_endpoint()


def idb_is_overridden(explicit: str | None = None) -> bool:
    """True when the idb client was pinned by flag or AUTONOM_IDB."""
    return bool(explicit or os.environ.get("AUTONOM_IDB"))


# Where an idb call to a local or remote companion failed (AL-04, DEC-005).
# Before a connection existed — the companion socket is gone (ENOENT: a
# killed companion's registration outlives it until `idb list-targets`
# prunes it) or nobody accepted the connection — nothing reached the device,
# so any command may be sent again. After the connection was made (a reset,
# a dropped gRPC stream, UNAVAILABLE mid-call) the command may already have
# run on the device: only a read is sent again, never an input (C-8).
BEFORE_CONNECTION = "before_connection"
AFTER_CONNECTION = "after_connection"
_REFUSED = re.compile(r"connection refused|\berrno 61\b|\beconnrefused\b", re.I)
_ENOENT = re.compile(r"no such file or directory|\berrno 2\b|\benoent\b", re.I)
# ENOENT names the companion's socket only when the failure is about reaching
# it (fb-idb wraps the OSError in IdbConnectionException); a bare "No such
# file" about a path the verb names is not a dead companion. fb-idb wraps
# every OSError of a call that way, so a missing local file can still read as
# before the connection: its one retry fails the same way and repeats nothing.
_ENOENT_CONTEXT = re.compile(r"companion|connect|socket|\.sock\b", re.I)
_AFTER_CONNECTION = re.compile(
    r"connection (?:reset|lost|aborted)|\beconnreset\b|broken pipe|stream ?terminated"
    r"|statuscode\.unavailable", re.I)
# Commands that only read: re-running one after a mid-call failure repeats nothing.
READ_ONLY_COMMANDS = (
    ("ui", "describe-all"), ("ui", "describe-point"), ("screenshot",), ("list-apps",),
    ("crash", "list"), ("ui", "quiet"),
)
# Inputs, named the way a hint speaks of them.
INPUT_COMMANDS = {
    ("ui", "tap"): "tap", ("ui", "swipe"): "swipe", ("ui", "text"): "text",
    ("ui", "key"): "key press", ("ui", "button"): "button press",
}
DEFAULT_HINT = "Check 'idb list-targets'; run 'autonom doctor' for the whole environment."


def failure_stage(message: str) -> str | None:
    """BEFORE_CONNECTION, AFTER_CONNECTION, or None (not a connection failure).

    A message with signs of both is taken as after the connection: that is
    the reading under which nothing is sent twice."""
    text = message or ""
    if _AFTER_CONNECTION.search(text):
        return AFTER_CONNECTION
    if _REFUSED.search(text) or (_ENOENT.search(text) and _ENOENT_CONTEXT.search(text)):
        return BEFORE_CONNECTION
    return None


def _command(args: Sequence[str], table: Any) -> Any:
    """The entry of ``table`` (prefix tuples) that ``args`` starts with."""
    for prefix in table:
        if tuple(args[:len(prefix)]) == prefix:
            return table[prefix] if isinstance(table, dict) else prefix
    return None


def is_read_only(args: Sequence[str]) -> bool:
    return _command(args, READ_ONLY_COMMANDS) is not None


def _may_resend(stage: str | None, args: Sequence[str]) -> bool:
    """The AL-04 retry rule: everything before a connection, only reads after."""
    if stage == BEFORE_CONNECTION:
        return True
    return stage == AFTER_CONNECTION and is_read_only(args)


def _failure_hint(stage: str | None, args: Sequence[str]) -> str:
    action = _command(args, INPUT_COMMANDS)
    if stage == AFTER_CONNECTION and action:
        return (f"The connection to idb_companion broke after the {action} was sent, so the "
                f"{action} may have been delivered, or may not; it was not sent again. Read "
                f"the screen ('autonom ui tree') before repeating it. {DEFAULT_HINT}")
    return DEFAULT_HINT


def is_hid_framework_failure(message: str) -> bool:
    return SIMULATORKIT_MARKER in (message or "").lower()


# idb verbs that take a subcommand (`ui text`, `crash show`, `file pull`).
_GROUP_COMMANDS = ("ui", "crash", "file")
_COMMAND_WORD = re.compile(r"^[a-z][a-z-]*$")


def _command_words(args: Sequence[str]) -> str:
    """The command words of an idb argv (`ui text`, `screenshot`), never
    the values the caller passed after them (typed text, paths, names)."""
    count = 2 if args[:1] and args[0] in _GROUP_COMMANDS else 1
    words = [str(word) for word in args[:count]]
    words = [word for word in words if _COMMAND_WORD.match(word)]
    return " ".join(words) or "command"


# --- the iOS device daemon seam (ID-06..ID-08, DEC-007) --------------------------
#
# Six `idb ui` subcommands are served by the simulator's device daemon
# (`ios_daemon`) when it can be used: `describe-all --json`, `quiet`, `tap`,
# `swipe`, `button` and `key`. Its reply comes back as the CompletedProcess the
# idb client would have given — the same stdout text and exit status — so the
# parsers above stay as they are. Everything else (`ui text`, `crash`, `file
# pull`, `screenshot`, `list-targets`, `connect`), a remote companion and a call
# without a UDID stay on the idb process. When the daemon was wanted and not
# used, the call goes to the idb process as before, and the reason is kept for
# the reply (`transport_reason`). An input the daemon may have received is never
# sent again through idb (INV-1): it fails with the after-connection error.

TRANSPORT_DAEMON = "daemon"
TRANSPORT_SUBPROCESS = "subprocess"
# The keys `transport_of` gives the CLI's reply (additive; `backend` keeps
# naming the input route).
TRANSPORT_KEYS = ("transport", "transport_reason", "ax_backend")
_TRANSPORT: dict[str, dict[str, Any]] = {}
_DAEMON_INPUTS = ("tap", "swipe", "button", "key")
# How long an observed input (`act`) may take beyond the observe budget (and
# the input's own duration). The daemon replies within its ACT_REPLY_MARGIN_MS
# (20 s: the input's deadline, the confirmation and grace windows, the cause
# check and one tree read) plus INPUT_REPLY_SLACK_MS (3 s), inside this.
ACT_MARGIN_S = 25.0
# How long the CLI waits for the daemon's reply to a plain input (tap, swipe,
# button, key), plus the input's own duration. The daemon gives up on an input
# at its DEFAULT_HID_WAIT_MS (15 s) from arrival, cancels what the companion has
# not performed, and replies within INPUT_REPLY_SLACK_MS (3 s) after that, so
# its `input_unconfirmed` reply always arrives before this runs out and nothing
# is performed after the CLI gave up (REV-029). Tests pin the pairing.
DAEMON_INPUT_BUDGET_S = 20.0
# The act an observed action armed in this thread (see `arm_act`).
_ACT = threading.local()


class _DaemonCompleted(subprocess.CompletedProcess):
    """A CompletedProcess the daemon answered (``transport`` says so)."""

    transport = TRANSPORT_DAEMON


def transport_of(udid: str | None) -> dict[str, Any]:
    """How this process's idb calls for ``udid`` were served: ``transport``
    (``daemon`` or ``subprocess``) of the command's input when it sent one,
    else of its last read; ``transport_reason`` when that call wanted the
    daemon and did not get it; and the ``ax_backend`` of the last daemon tree
    read. Empty when no call ran."""
    held = _TRANSPORT.get(udid or "", {})
    return {key: held[key] for key in TRANSPORT_KEYS if held.get(key) is not None}


def reset_transport() -> None:
    """Forget what was recorded (tests)."""
    _TRANSPORT.clear()
    disarm_act()


def _note_transport(udid: str, args: Sequence[str], transport: str, *,
                    reason: str | None = None, ax_backend: str | None = None,
                    wanted: bool = True) -> None:
    """Record how one call was served. An input decides what the reply says
    (the last one wins); a read only while no input was sent; a call the
    daemon never serves (`ui text` aside, which is an input) does not
    replace what a read recorded."""
    held = _TRANSPORT.setdefault(udid, {})
    if ax_backend:
        held["ax_backend"] = ax_backend
    is_input = _command(args, INPUT_COMMANDS) is not None
    if not is_input and (held.get("input") or (not wanted and "transport" in held)):
        return
    held["transport"] = transport
    held.pop("transport_reason", None)
    if transport == TRANSPORT_SUBPROCESS and wanted and reason:
        held["transport_reason"] = reason
    if is_input:
        held["input"] = True


def arm_act(udid: str, timeout_ms: int) -> None:
    """The next input this thread sends to ``udid`` on the daemon route is
    an observed one (ID-10): one `act` request sends it, waits for the
    confirmation and the settle within ``timeout_ms``, and reads the screen
    once; the observe step takes the result with ``take_act``."""
    _ACT.armed = {"udid": udid, "timeout_ms": max(int(timeout_ms), 0)}
    _ACT.result = None


def disarm_act() -> None:
    _ACT.armed = None
    _ACT.result = None


def take_act(udid: str) -> dict[str, Any] | None:
    """The result of the `act` this thread sent to ``udid``, once; None when
    the input did not go through `act`."""
    held = getattr(_ACT, "result", None)
    _ACT.result = None
    if not held or held[0] != udid:
        return None
    return held[1]


def _number(text: str) -> int | float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return int(value) if value.is_integer() and "." not in str(text) else value


def _daemon_call(args: Sequence[str]) -> dict[str, Any] | None:
    """The daemon request one of the six served argv shapes maps to, or
    None for any other argv (which stays on the idb process)."""
    words = [str(word) for word in args]
    if words[:1] != ["ui"] or len(words) < 2:
        return None
    verb, rest = words[1], words[2:]
    if verb == "describe-all":
        return {"op": "tree"} if rest == ["--json"] else None
    if verb == "quiet":
        if len(rest) != 4 or rest[1] != "--json" or rest[2] != "--quiet-window-ms":
            return None
        seconds, window = _number(rest[0]), _number(rest[3])
        if seconds is None or window is None or seconds <= 0 or window < 0:
            return None
        return {"op": "quiet", "timeout_ms": max(int(round(seconds * 1000)), 1),
                "quiet_ms": int(window), "_seconds": rest[0]}
    if verb == "tap":
        if len(rest) not in (2, 4) or (len(rest) == 4 and rest[2] != "--duration"):
            return None
        x, y = _number(rest[0]), _number(rest[1])
        if x is None or y is None:
            return None
        call: dict[str, Any] = {"op": "tap", "x": x, "y": y}
        if len(rest) == 4:
            duration = _number(rest[3])
            if duration is None or duration < 0:
                return None
            call["duration"] = duration
        return call
    if verb == "swipe":
        if len(rest) != 6 or rest[4] != "--duration":
            return None
        values = [_number(value) for value in (*rest[:4], rest[5])]
        if any(value is None for value in values):
            return None
        x1, y1, x2, y2, duration = values
        return {"op": "swipe", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration": duration}
    if verb == "button":
        return {"op": "button", "name": rest[0]} if len(rest) == 1 and rest[0] in BUTTONS else None
    if verb == "key":
        return ({"op": "key", "code": int(rest[0])}
                if len(rest) == 1 and rest[0].isdigit() else None)
    return None


def _quiet_stdout(reply: dict[str, Any], seconds: str) -> tuple[str, int]:
    """`idb ui quiet --json`'s lines and exit status for a daemon `quiet`
    reply: the last state, then `timed_out` (exit 1) when it ran out."""
    state = str(reply.get("state") or "quiet").lower()
    timed_out = bool(reply.get("timed_out"))
    if timed_out and state == "quiet":
        state = "busy"
    event: dict[str, Any] = {"event": "state", "state": state}
    if state == "busy":
        signals = reply.get("busy_signals")
        event["signals"] = sorted(str(name) for name in signals or () if isinstance(name, str))
    lines = [json.dumps(event, sort_keys=True)]
    if not timed_out:
        return lines[0] + "\n", 0
    timeout = _number(seconds)
    lines.append(json.dumps({"event": "timed_out", "timeout": float(timeout or 0)},
                            sort_keys=True))
    return "\n".join(lines) + "\n", 1


def _after_send_error(args: Sequence[str], detail: str) -> errors.AutonomError:
    action = _command(args, INPUT_COMMANDS) or "input"
    return errors.AutonomError(
        errors.IDB_COMPANION_UNAVAILABLE,
        f"the iOS device daemon failed after the {action} was sent: {detail}"[:400],
        _failure_hint(AFTER_CONNECTION, args),
        stage=AFTER_CONNECTION, transport=TRANSPORT_DAEMON)


# The hint of an input the daemon sent and idb_companion never confirmed, when
# the daemon's own reply carries none.
UNCONFIRMED_HINT = ("The input was sent once, but idb_companion did not confirm it: another client "
                    "held the simulator's input (idb ui text, a Canvas press) past the safety "
                    "limit, or the companion connection was lost. It was not sent again. Read the "
                    "screen ('autonom ui tree') before you repeat it.")


def _unconfirmed_error(args: Sequence[str], error: dict[str, Any],
                       message: str) -> errors.AutonomError:
    """The daemon's `input_unconfirmed` failure as the CLI's: its message,
    hint and `unconfirmed_events` kept, stage after_connection (sent once,
    never again)."""
    events = error.get("unconfirmed_events")
    extra: dict[str, Any] = {"stage": AFTER_CONNECTION, "transport": TRANSPORT_DAEMON}
    if isinstance(events, int) and not isinstance(events, bool):
        extra["unconfirmed_events"] = events
    hint = error.get("hint")
    return errors.AutonomError(
        errors.INPUT_UNCONFIRMED, message[:400],
        str(hint) if isinstance(hint, str) and hint.strip() else UNCONFIRMED_HINT, **extra)


def xcuitest_session(udid: str) -> bool:
    """Is ``udid`` served by the XCUITest runner (``--ui-backend xcuitest``,
    AUTONOM_UI_BACKEND, the session's preference, or an auto session that
    switched to it)? Such a session never starts or uses the device daemon
    (ID-06). When the backend cannot be told, the answer is yes: the idb
    process serves the call, as before the daemon existed."""
    try:
        from . import ui_ios
        from .platform import IOS

        return ui_ios.selected_backend(Target(IOS, udid, "xcrun")) == "xcuitest"
    except Exception:  # noqa: BLE001 - an unreadable backend never starts a daemon
        return True


def _via_daemon(args: Sequence[str], udid: str, timeout: float | None,
                binary: bool, spent: dict[str, Any] | None = None,
                ) -> subprocess.CompletedProcess | None:
    """Serve ``args`` through the device daemon: the CompletedProcess the
    idb client would have given, or None when the idb process serves it
    (the transport and the reason are recorded either way). When a read was
    sent and no usable reply came back, ``spent["after_send_s"]`` is how
    long that request took, so the idb process gets only what is left."""
    from . import ios_daemon

    call = _daemon_call(args) if not binary else None
    if call is None or xcuitest_session(udid):
        _note_transport(udid, args, TRANSPORT_SUBPROCESS, wanted=False)
        return None
    op = call["op"]
    is_input = op in _DAEMON_INPUTS
    try:
        registry = ios_daemon.ensure(udid)
    except ios_daemon.Unavailable as exc:
        _note_transport(udid, args, TRANSPORT_SUBPROCESS, reason=exc.reason)
        return None
    seconds = call.pop("_seconds", None)
    armed = getattr(_ACT, "armed", None)
    act = is_input and bool(armed) and armed.get("udid") == udid
    budget = float(timeout) if timeout else 30.0
    duration = float(call.get("duration") or 0) if op in ("tap", "swipe") else 0.0
    if act:
        _ACT.armed = None  # one input per observed command
        request = {"op": "act", "input": call, "timeout_ms": armed["timeout_ms"], "read": True}
        budget = armed["timeout_ms"] / 1000 + duration + ACT_MARGIN_S
    elif op == "quiet":
        request = call
        budget = max(budget, call["timeout_ms"] / 1000 + QUIET_GRACE_S)
    elif is_input:
        request = call
        budget = max(budget, DAEMON_INPUT_BUDGET_S) + duration
    else:
        request = call
    sent_at = time.monotonic()
    try:
        reply = ios_daemon.request(str(registry["socket"]), request, timeout_s=budget)
    except ios_daemon.Unavailable as exc:
        if exc.stage == ios_daemon.AFTER_SEND and is_input:
            _note_transport(udid, args, TRANSPORT_DAEMON)
            raise _after_send_error(args, exc.message) from exc
        # nothing was written, or only a read was: the idb process, once
        if exc.stage == ios_daemon.AFTER_SEND and spent is not None:
            spent["after_send_s"] = time.monotonic() - sent_at
            spent["message"] = exc.message
        ios_daemon.remember(udid, exc.reason)
        _note_transport(udid, args, TRANSPORT_SUBPROCESS, reason=exc.reason)
        return None
    error = reply.get("error") if isinstance(reply.get("error"), dict) else {}
    if reply.get("ok") is not True:
        code = str(error.get("code") or errors.BACKEND_FAILED)
        message = str(error.get("message") or f"the iOS device daemon refused {op}")
        stage = error.get("stage")
        if act and "input_confirmed" in reply:
            # the input went out and was answered; only the read after it
            # failed: the action ran, observe reports the failure
            _note_transport(udid, args, TRANSPORT_DAEMON)
            _ACT.result = (udid, {**reply, "error": {"code": code, "message": message,
                                                    "hint": error.get("hint")}})
            return _DaemonCompleted(list(args), 0, "", "")
        if code == errors.IOS_DISPLAY_NOT_LIT:
            _note_transport(udid, args, TRANSPORT_DAEMON)
            raise errors.AutonomError(
                errors.IOS_DISPLAY_NOT_LIT, message,
                str(error.get("hint") or "The simulator's display is not lit yet: a foldable "
                    "device or a display change is in progress. Wait, or use a device type "
                    "with one fixed display."),
                transport=TRANSPORT_DAEMON)
        if code == errors.INPUT_UNCONFIRMED and is_input:
            # sent once and given up at the daemon's deadline (its calls cancelled) or
            # lost with the companion: never resent, never served by idb (INV-1)
            _note_transport(udid, args, TRANSPORT_DAEMON)
            raise _unconfirmed_error(args, error, message)
        if stage != ios_daemon.BEFORE_SEND and is_input:
            _note_transport(udid, args, TRANSPORT_DAEMON)
            raise _after_send_error(args, message)
        reason = (ios_daemon.COMPANION_TOO_OLD if code == ios_daemon.COMPANION_TOO_OLD
                  else ios_daemon.DAEMON_UNREACHABLE)
        ios_daemon.remember(udid, reason)
        _note_transport(udid, args, TRANSPORT_SUBPROCESS, reason=reason)
        return None
    if act:
        _note_transport(udid, args, TRANSPORT_DAEMON, ax_backend=reply.get("ax_backend"))
        _ACT.result = (udid, reply)
        return _DaemonCompleted(list(args), 0, "", "")
    if op == "tree":
        text = reply.get("json")
        if not isinstance(text, str):
            ios_daemon.remember(udid, ios_daemon.DAEMON_UNREACHABLE)
            _note_transport(udid, args, TRANSPORT_SUBPROCESS, reason=ios_daemon.DAEMON_UNREACHABLE)
            return None
        _note_transport(udid, args, TRANSPORT_DAEMON, ax_backend=reply.get("ax_backend"))
        return _DaemonCompleted(list(args), 0, text if text.endswith("\n") else text + "\n", "")
    _note_transport(udid, args, TRANSPORT_DAEMON)
    if op == "quiet":
        stdout, code = _quiet_stdout(reply, seconds or "0")
        return _DaemonCompleted(list(args), code, stdout, "")
    return _DaemonCompleted(list(args), 0, "", "")


# Less than this left of a read's budget after the daemon kept it: the idb
# process is not asked, the read fails instead.
MIN_FALLBACK_S = 1.0


def _remaining_budget(
    args: Sequence[str], timeout: float | None, spent: Mapping[str, Any],
    resend: Callable[[], tuple[Sequence[str], float | None] | None] | None,
) -> tuple[Sequence[str], float | None]:
    """The argv and process timeout the idb process gets after the daemon
    took a read and gave no usable reply: ``resend`` (the caller's own
    deadline) when there is one, else ``timeout`` less what the daemon took.
    Raises ``backend_failed`` when nothing worth a call is left, so one read
    never takes about twice its budget (RISK-012)."""
    detail = str(spent.get("message") or "no reply")
    if resend is not None:
        again = resend()
        if again is None:
            raise errors.AutonomError(
                errors.BACKEND_FAILED,
                f"the iOS device daemon did not answer idb {_command_words(args)}: {detail}"[:400],
                DEFAULT_HINT, transport=TRANSPORT_DAEMON)
        return again[0], again[1]
    if timeout is None:
        return args, None
    left = float(timeout) - float(spent.get("after_send_s") or 0.0)
    if left < MIN_FALLBACK_S:
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            (f"the iOS device daemon did not answer idb {_command_words(args)}: {detail} "
             f"(not asked again through idb: nothing was left of the {float(timeout):g} s "
             "budget)")[:400],
            DEFAULT_HINT, transport=TRANSPORT_DAEMON)
    return args, left


def run_idb(
    idb: str,
    args: Sequence[str],
    *,
    udid: str | None = None,
    timeout: float | None = 30,
    check: bool = True,
    binary: bool = False,
    companion: str | None = None,
    accept: Callable[[subprocess.CompletedProcess], bool] | None = None,
    resend: Callable[[], tuple[Sequence[str], float | None] | None] | None = None,
    _retry: bool = True,
) -> subprocess.CompletedProcess:
    # `accept`: a non-zero exit this predicate accepts is an answer, not a
    # failure, and comes back as it is (`ui quiet` exits 1 with a `timed_out`
    # event when the app stayed busy). Any other non-zero exit is classified
    # and retried exactly as without it.
    # `resend`: for a call bounded by its caller's deadline. Asked right
    # before the one AL-04 retry (after the prune), it returns the argv and
    # process timeout to send again — the same command, carrying only what is
    # left of the budget — or None when nothing is left, and the failure is
    # then raised as it is. Without it the retry repeats `args` and `timeout`.
    # `--companion` is a top-level idb option, so it goes before the verb.
    endpoint = companion or companion_endpoint()
    if udid and _retry:
        # the device daemon first (DEC-007); a remote companion is never its
        if endpoint:
            _note_transport(udid, args, TRANSPORT_SUBPROCESS, wanted=False)
        else:
            spent: dict[str, Any] = {}
            served = _via_daemon(args, udid, timeout, binary, spent)
            if served is not None:
                if check and served.returncode != 0 and not (accept and accept(served)):
                    raise errors.AutonomError(
                        errors.BACKEND_FAILED,
                        f"idb {_command_words(args)} failed ({served.returncode})",
                        DEFAULT_HINT, transport=TRANSPORT_DAEMON)
                return served
            if "after_send_s" in spent:
                # a read the daemon took and never answered: the idb process
                # gets only what is left of the caller's budget (RISK-012)
                args, timeout = _remaining_budget(args, timeout, spent, resend)
    command = [idb, *(["--companion", endpoint] if endpoint else []), *args]
    if udid:
        command += ["--udid", udid]
    # A local idb call may spawn an idb_companion for the simulator; the
    # tracker registers one that appears during this call under the session
    # that owns the simulator, so `session stop` can stop it. A remote
    # companion is on another Mac and never ours to track. The tracker lists
    # every process on the machine before and after the call; when idb's
    # registry already holds a live local companion for the simulator, idb
    # uses that one and spawns none, so there is nothing to find (AL-02).
    tracker = (processes_mod.track_idb_companions(udid)
               if udid and not endpoint and not _state_lists_live_companion(udid)
               else contextlib.nullcontext())
    try:
        with tracker:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout,
                text=not binary,
            )
    except FileNotFoundError as exc:
        raise errors.tool_missing("idb") from exc
    if check and completed.returncode != 0 and not (accept and accept(completed)):
        raw = completed.stderr if not binary else (completed.stderr or b"").decode("utf-8", "replace")
        # Only what idb itself printed is classified. With nothing on stderr
        # the message names the command words alone: the argv also holds the
        # caller's values (the text `ui text` types, which may be sensitive),
        # and a typed "errno 61" or "connect" must neither reach the envelope
        # nor read as a connection failure that sends the input again (INV-1).
        detail = (raw or "").strip()
        message = detail or f"idb {_command_words(args)} failed ({completed.returncode})"
        if is_hid_framework_failure(detail):
            # Checked first: the message also says "companion"-ish things, and
            # a retry cannot fix a framework the companion cannot load.
            raise errors.AutonomError(
                errors.IOS_HID_FRAMEWORK_MISSING, message, HID_FRAMEWORK_HINT,
                fix=HID_UPGRADE_FIX,
            )
        stage = failure_stage(detail)
        lowered = detail.lower()
        code = (
            errors.IDB_COMPANION_UNAVAILABLE
            if stage or "companion" in lowered or "connect" in lowered
            else errors.BACKEND_FAILED
        )
        if code == errors.IDB_COMPANION_UNAVAILABLE and _retry and _may_resend(stage, args):
            # Seen on a real Mac: the client kept a companion entry from an
            # earlier boot or a killed companion (a dead pid, its socket gone
            # or refusing), so every verb died with ENOENT or "Connection
            # refused" until `idb list-targets` noticed and dropped it. Prune
            # the same way and re-dispatch, once: the next call spawns a fresh
            # companion. Whether the prune dropped anything does not matter —
            # another client may have pruned already, and a failure before
            # the connection reached nothing. A reset or disconnect may arrive
            # after the action was delivered, and `ui text` sent twice types
            # the text twice, so after a connection only reads come back here.
            # A remote companion is never pruned: list-targets would drop its
            # registration, and only `idb connect` brings it back.
            if not endpoint:
                _prune_companions(idb)
            again = resend() if resend is not None else (args, timeout)
            if again is not None:
                return run_idb(idb, again[0], udid=udid, timeout=again[1], check=check,
                               binary=binary, companion=companion, accept=accept,
                               _retry=False)
        raise errors.AutonomError(code, message, _failure_hint(stage, args))
    return completed


def _state_path() -> Path | None:
    """The fb-idb client's companion registry, when it is knowably this idb's.

    AUTONOM_IDB_STATE_FILE wins. The default /tmp/idb/state belongs to the
    PATH-resolved client; a pinned client (flag or AUTONOM_IDB) may keep its
    registry elsewhere — or be a test fake — so its state is unknown.
    """
    configured = os.environ.get("AUTONOM_IDB_STATE_FILE")
    if configured:
        return Path(configured)
    return None if idb_is_overridden() else Path(IDB_STATE_FILE)


def _state_companions() -> list[dict[str, Any]] | None:
    """The companions the idb client has registered, or None if unknown."""
    path = _state_path()
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, list):
        return None
    return [entry for entry in payload if isinstance(entry, dict)]


def _pid_alive(pid: Any) -> bool:
    """A process with this pid exists. Only a real positive pid counts:
    ``os.kill(0, 0)`` and ``os.kill(-1, 0)`` would test whole process groups."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # it exists; it belongs to someone else
    except (OSError, OverflowError):
        return False
    return True


def _state_lists_live_companion(udid: str) -> bool:
    """idb's registry holds a local companion for ``udid`` whose process is
    alive. idb then connects to that companion and spawns none, so the call
    has no companion of its own to track. False whenever unsure: the
    registry is unknown (a pinned client) or unreadable, the entry is
    remote, or its pid is gone (a stale entry the call may replace)."""
    for entry in _state_companions() or ():
        if (entry.get("udid") == udid and entry.get("is_local") is True
                and _pid_alive(entry.get("pid"))):
            return True
    return False


def _prune_companions(idb: str) -> None:
    """`idb list-targets` drops companion registrations whose companion no
    longer answers, so the next call for that simulator spawns a fresh one.
    Best effort: a prune that cannot run leaves the retry to fail on its own."""
    try:
        subprocess.run(
            [idb, "list-targets"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def version(idb: str) -> str | None:
    """Liveness + identity probe.

    `idb` has no `--version` flag (it errors with "unrecognized arguments"), so
    readiness is established by a command that actually exercises the client and
    its companion. `list-targets` is the cheapest one that does both.
    """
    endpoint = companion_endpoint()
    completed = subprocess.run(
        [idb, *(["--companion", endpoint] if endpoint else []), "list-targets", "--json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=False, timeout=30,
    )
    if completed.returncode != 0:
        return None
    targets = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    return f"idb client ok ({len(targets)} target(s))"


_SEMVER = re.compile(r"\b(\d+)\.(\d+)(?:\.(\d+))?\b")


def companion_info(companion: str = "idb_companion") -> dict[str, Any] | None:
    """`idb_companion --version`, reduced to known fields.

    The companion can print its whole environment (tokens included) when it
    starts, so nothing it prints is passed through verbatim: only the
    version and build date are kept. stderr is never read.
    """
    try:
        completed = subprocess.run(
            [companion, "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, text=True, check=False, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    info: dict[str, Any] = {"version": None, "build_date": None, "build_time": None}
    for line in (completed.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            for key in info:
                value = payload.get(key)
                if isinstance(value, str) and len(value) <= 64:
                    info[key] = value
            break
    if info["version"] is None:
        match = _SEMVER.search((completed.stdout or "").splitlines()[0]
                               if completed.stdout else "")
        if match:
            info["version"] = match.group(0)
    if not any(info.values()):
        return None
    return info


def companion_version(companion: str = "idb_companion") -> str | None:
    """One-line summary of `companion_info` (kept for existing callers)."""
    info = companion_info(companion)
    if not info:
        return None
    parts = []
    if info.get("version"):
        parts.append(info["version"])
    if info.get("build_date"):
        parts.append(f"built {info['build_date']}")
    return " ".join(parts) or None


def _parse_build_date(value: str | None) -> date | None:
    if not value:
        return None
    text = " ".join(value.split())
    for fmt in ("%b %d %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def companion_predates_xcode27(info: dict[str, Any] | None) -> bool | None:
    """True when this companion only knows the pre-Xcode-27 SimulatorKit path.

    A version string decides when present; otherwise the build date does.
    None when neither can be read.
    """
    if not info:
        return None
    match = _SEMVER.search(info.get("version") or "")
    if match:
        parsed = tuple(int(part or 0) for part in match.groups())
        return parsed < COMPANION_FIXED_VERSION
    built = _parse_build_date(info.get("build_date"))
    if built:
        return built < COMPANION_FIXED_DATE
    return None


def hid_assess(xcode: dict[str, Any], info: dict[str, Any] | None) -> dict[str, Any]:
    """Pure verdict from `ios_simctl.xcode_info` and `companion_info`.

    ``ready`` is False only when the failure is determined: the Developer dir
    lacks the legacy SimulatorKit, Xcode is 27+ or has the new location, and
    the companion predates the fix.
    """
    stale = companion_predates_xcode27(info)
    moved = (not xcode.get("simulatorkit_legacy")
             and bool(xcode.get("simulatorkit_shared") or (xcode.get("xcode_major") or 0) >= 27))
    ready = not (moved and stale)
    return {
        "ready": ready,
        "reason": None if ready else (
            "idb_companion loads SimulatorKit from Library/PrivateFrameworks, which "
            f"Xcode {xcode.get('xcode_version') or '27+'} no longer has"),
        "companion_predates_fix": stale,
        "fix": None if ready else HID_UPGRADE_FIX,
    }


def hid_status(companion_path: str | None = None, *,
               developer: str | None = None) -> dict[str, Any]:
    """Can idb deliver HID events on this machine? Read from disk and
    `idb_companion --version` only; never touches a simulator."""
    from . import ios_simctl

    xcode = ios_simctl.xcode_info(developer)
    path = companion_path or shutil.which("idb_companion")
    info = companion_info(path) if path else None
    return {**hid_assess(xcode, info), "xcode": xcode, "companion_path": path,
            "companion": info}


def connect(idb: str, endpoint: str | None, udid: str) -> None:
    """Attach the client to a companion.

    `idb connect <host> <port>` for a remote companion; for a local one idb
    spawns/needs `idb_companion` itself, so connecting by udid is enough.
    """
    if endpoint:
        host, _, port = endpoint.partition(":")
        run_idb(idb, ["connect", host, port or "10882"], timeout=30, check=False)
        return
    run_idb(idb, ["connect", udid], timeout=30, check=False)


def _client(target: Target, args: Any = None) -> str:
    idb = find_idb(getattr(args, "idb", None) if args else None)
    return idb


def probe(idb_path: str | None = None, endpoint: str | None = None) -> dict[str, Any]:
    """Readiness snapshot cached in the session record (CAP-IOSS-006)."""
    import time

    try:
        idb = find_idb(idb_path)
    except errors.AutonomError as exc:
        return {"state": "missing", "version": None, "companion": endpoint or "local",
                "error": exc.message, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    resolved = version(idb)
    snapshot: dict[str, Any] = {
        "state": "ready" if resolved else "error",
        "version": resolved,
        "path": idb,
        "companion": endpoint or "local",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # A pinned client (flag or AUTONOM_IDB) may not pair with the companion on
    # PATH, so the HID check only speaks for the PATH-resolved install; a
    # remote companion is on another machine's Xcode entirely.
    if resolved and not endpoint and not idb_is_overridden(idb_path):
        status = hid_status()
        snapshot["hid"] = {"ready": status["ready"], "reason": status["reason"],
                           "fix": status["fix"]}
    return snapshot


# --- UI ----------------------------------------------------------------------


def describe_all(target: Target, *, idb_path: str | None = None) -> str:
    idb = find_idb(idb_path)
    completed = run_idb(idb, ["ui", "describe-all", "--json"], udid=target.target_id,
                        timeout=20, companion=target_companion(target))
    return completed.stdout or ""


# `idb ui quiet <seconds> --json --quiet-window-ms <ms>` (fb-idb 1.6.5 on the
# bench Mac, AL-08) waits until the frontmost application is quiet — its run
# loop idle and no animation running, for the whole quiet window — and exits
# 0; each change of state is one JSON line (`{"event": "state", "state":
# "settling"|"busy"|"quiet", "pid": N}`). When the timeout passes first it
# prints `{"event": "timed_out", ...}` and exits 1. A timeout of 0 would mean
# "wait for as long as it takes", so a budget is never sent as 0.
#
# An idb without the command refuses it the way argparse refuses any unknown
# choice, and a companion without the quiescence call answers UNIMPLEMENTED.
# Either is remembered for this process (per client and companion), so the
# next settle polls the tree without paying for the refusal again.
QUIET_GRACE_S = 5.0  # idb's start-up and connection, on top of its own timeout
_QUIET_UNSUPPORTED_MARKERS = ("invalid choice", "unrecognized arguments", "unimplemented")
_QUIET_UNSUPPORTED: set[tuple[str, str | None]] = set()


def reset_quiet_support() -> None:
    """Forget which idb clients refused `ui quiet` (tests)."""
    _QUIET_UNSUPPORTED.clear()


def _quiet_events(stdout: Any) -> list[dict[str, Any]]:
    """The JSON objects among the lines `ui quiet --json` printed."""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    events: list[dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _quiet_timed_out(completed: subprocess.CompletedProcess) -> bool:
    """Exit 1 with a `timed_out` event: idb's answer "not quiet in time",
    which is not a failure of the call."""
    return completed.returncode == 1 and any(
        event.get("event") == "timed_out" for event in _quiet_events(completed.stdout))


def _quiet_seconds(timeout_ms: int) -> str:
    """Milliseconds as idb's seconds argument, at least 1 ms (never "0")."""
    text = f"{max(int(timeout_ms), 1) / 1000:.3f}".rstrip("0").rstrip(".")
    return text or "0.001"


def _quiet_unsupported(detail: str) -> errors.AutonomError:
    return errors.AutonomError(
        errors.UNSUPPORTED_ON_PLATFORM,
        f"this idb cannot report whether the app is quiet ('idb ui quiet'): {detail}"[:400],
        "Settling polls the accessibility tree instead. Upgrade the client and companion "
        "(pipx upgrade fb-idb; brew upgrade idb-companion) to settle with one read.",
    )


def ui_quiet(target: Target, timeout_ms: int, quiet_ms: int, *,
             idb_path: str | None = None,
             clock: Callable[[], float] = time.monotonic,
             deadline: float | None = None) -> dict[str, Any]:
    """Wait up to ``timeout_ms`` for the frontmost app to be quiet for
    ``quiet_ms``, through `idb ui quiet`. Never reads the tree.

    Returns ``{"quiet": bool, "state": str | None, "signals": [str]}``:
    ``quiet`` is True when idb reported quiet, False when its timeout passed
    first; ``state`` is the last state idb reported (``busy``, ``settling``,
    ``quiet``) and ``signals`` the busy signals it named. Raises
    ``unsupported_on_platform`` when this idb or its companion has no such
    command (remembered for the process), and the usual idb errors
    (``backend_failed``, ``idb_companion_unavailable``) for any other failure,
    including a call that outlives its timeout by more than QUIET_GRACE_S.
    A dead companion is pruned and the call retried once like any read
    (AL-04: `ui quiet` sends nothing to the app), but the retry carries only
    what is left before ``deadline`` on ``clock`` (by default ``timeout_ms``
    from now; a caller with its own budget passes its deadline), and is not
    sent when nothing is left (RISK-012): the whole wait stays within the
    budget plus one QUIET_GRACE_S.
    """
    if deadline is None:
        deadline = clock() + max(int(timeout_ms), 0) / 1000
    idb = find_idb(idb_path)
    companion = target_companion(target)
    known = (idb, companion)
    if known in _QUIET_UNSUPPORTED:
        raise _quiet_unsupported("it refused the command earlier in this process")
    window = str(max(int(quiet_ms), 0))
    # what the last call carried: idb's own timeout and the process timeout
    sent: dict[str, Any] = {"seconds": _quiet_seconds(timeout_ms), "skipped": False}

    def argv_for(seconds: str) -> list[str]:
        sent["seconds"], sent["limit"] = seconds, float(seconds) + QUIET_GRACE_S
        return ["ui", "quiet", seconds, "--json", "--quiet-window-ms", window]

    def resend() -> tuple[list[str], float] | None:
        left_ms = int((deadline - clock()) * 1000)
        if left_ms < 1:
            sent["skipped"] = True
            return None
        argv = argv_for(_quiet_seconds(left_ms))
        return argv, sent["limit"]

    argv = argv_for(sent["seconds"])
    try:
        completed = run_idb(idb, argv, udid=target.target_id, timeout=sent["limit"],
                            companion=companion, accept=_quiet_timed_out, resend=resend)
    except subprocess.TimeoutExpired as exc:
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"idb ui quiet did not answer within {sent['limit']:g} s "
            f"(its own timeout was {sent['seconds']} s)",
            DEFAULT_HINT,
        ) from exc
    except OSError as exc:  # the client could not be started (a missing one is tool_missing)
        raise errors.AutonomError(
            errors.BACKEND_FAILED, f"idb ui quiet could not run: {exc}", DEFAULT_HINT,
        ) from exc
    except errors.AutonomError as exc:
        if any(marker in (exc.message or "").lower() for marker in _QUIET_UNSUPPORTED_MARKERS):
            _QUIET_UNSUPPORTED.add(known)
            lines = (exc.message or "").strip().splitlines() or [""]
            raise _quiet_unsupported(lines[-1]) from exc
        if sent["skipped"]:
            raise errors.AutonomError(
                exc.code,
                f"{exc.message} (not asked again: nothing was left of the "
                f"{max(int(timeout_ms), 0)} ms budget)",
                exc.hint, **exc.extra,
            ) from exc
        raise
    states = [event for event in _quiet_events(completed.stdout)
              if event.get("event") == "state" and isinstance(event.get("state"), str)]
    last = states[-1] if states else {}
    state = last.get("state")
    if completed.returncode == 0 and state != "quiet":
        # exit 0 is idb's "quiet"; without the event saying so, the reply is
        # not one this wrapper understands, and guessing would claim a settle
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            "idb ui quiet exited 0 without reporting a quiet state",
            DEFAULT_HINT,
        )
    signals = last.get("signals") if state == "busy" else None
    answer: dict[str, Any] = {
        "quiet": completed.returncode == 0,
        "state": state,
        "signals": sorted(str(name) for name in signals if isinstance(name, str))
        if isinstance(signals, list) else [],
    }
    if getattr(completed, "transport", None) == TRANSPORT_DAEMON:
        # answered from the daemon's open quiet stream (ID-10)
        answer["transport"] = TRANSPORT_DAEMON
    return answer


def tap(target: Target, x: int, y: int, *, duration: float | None = None,
        idb_path: str | None = None) -> None:
    idb = find_idb(idb_path)
    argv = ["ui", "tap", str(x), str(y)]
    if duration is not None:
        argv += ["--duration", str(duration)]  # seconds; idb's long press
    run_idb(idb, argv, udid=target.target_id, timeout=20,
            companion=target_companion(target))


def swipe(target: Target, x1: int, y1: int, x2: int, y2: int, duration: float,
          *, idb_path: str | None = None) -> None:
    idb = find_idb(idb_path)
    run_idb(
        idb,
        ["ui", "swipe", str(x1), str(y1), str(x2), str(y2), "--duration", str(duration)],
        udid=target.target_id,
        timeout=20,
        companion=target_companion(target),
    )


def text(target: Target, value: str, *, idb_path: str | None = None) -> None:
    idb = find_idb(idb_path)
    run_idb(idb, ["ui", "text", value], udid=target.target_id, timeout=20,
            companion=target_companion(target))


def button(target: Target, name: str, *, idb_path: str | None = None) -> None:
    idb = find_idb(idb_path)
    run_idb(idb, ["ui", "button", name], udid=target.target_id, timeout=10,
            companion=target_companion(target))


def key(target: Target, keycode: str, *, idb_path: str | None = None) -> None:
    idb = find_idb(idb_path)
    run_idb(idb, ["ui", "key", keycode], udid=target.target_id, timeout=10,
            companion=target_companion(target))


def gesture(target: Target, name: str, *, idb_path: str | None = None, **kwargs: Any) -> None:
    if name in UNBACKED_GESTURES:
        raise errors.AutonomError(
            errors.UNSUPPORTED_ON_PLATFORM,
            f"idb provides no '{name}' command, so it cannot be sent to a simulator",
            "Use 'autonom ui swipe' for anything reachable by a drag. Rotation and "
            "shake have no Autonom path; rotate the window by hand in Simulator "
            "(Device > Rotate) if the test needs it.",
            gesture=name,
        )
    raise errors.AutonomError(
        errors.UNSUPPORTED_ON_PLATFORM,
        f"unknown gesture: {name}",
        "Supported gestures: " + ", ".join(GESTURES),
    )


def screenshot(target: Target, output: Path, *, idb_path: str | None = None) -> Path:
    idb = find_idb(idb_path)
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    run_idb(idb, ["screenshot", str(output)], udid=target.target_id, timeout=60,
            companion=target_companion(target))
    return output


# --- diagnostics -------------------------------------------------------------


# What the fb-idb client prints when it could not reach a companion, read
# from fb-idb 1.1.7's own sources: an OSError surfaces as
# `IdbConnectionException(<strerror>)` ("Connection refused", asyncio's
# "Connect call failed (...)"), spawning fails with "Failed to spawn
# companion ...", a dropped gRPC stream as "Connection lost"/"StreamTerminated",
# and a gRPC UNAVAILABLE status by name. The client's INFO lines ("No existing
# companion at ..., spawning one...", "Companion at ... spawned for <udid>")
# mention the companion without being failures, so the bare words
# "companion"/"connect" are never enough on their own.
_COMPANION_FAILURE = re.compile(
    r"IdbConnectionException|connection refused|connect call failed|failed to connect"
    r"|could not connect|couldn't connect|failed to spawn companion|connection (?:lost|reset)"
    r"|streamterminated|statuscode\.unavailable|companion (?:is )?not running"
    r"|not connected to (?:a|any) companion|errno 61|econnrefused", re.I)


def crash_list(target: Target, *, idb_path: str | None = None) -> str:
    """idb's crash store listing, or a typed failure.

    This ran with ``check=False`` and returned stdout whatever happened, so a
    client that had lost its companion (seen on a real Mac: `crash show`
    answered idb_companion_unavailable seconds later) printed nothing and the
    verb reported ``count: 0`` — an empty crash store the device never
    claimed. A non-zero exit now raises exactly like every other idb verb
    (IDB_COMPANION_UNAVAILABLE for companion/connection trouble, else
    BACKEND_FAILED); an exit 0 with no listing and a connection failure on
    stderr (``_COMPANION_FAILURE``, never a mere mention of the companion) is
    the same failure and raises the same way.
    """
    idb = find_idb(idb_path)
    completed = run_idb(idb, ["crash", "list"], udid=target.target_id, timeout=30,
                        companion=target_companion(target))
    stdout = completed.stdout or ""
    stderr = (completed.stderr or "").strip()
    if not stdout.strip() and stderr and _COMPANION_FAILURE.search(stderr):
        raise errors.AutonomError(
            errors.IDB_COMPANION_UNAVAILABLE,
            stderr[-400:],
            "idb printed no crash listing because it could not reach its companion; "
            "an empty store was not confirmed. Check 'idb list-targets', then retry.",
        )
    return stdout


def crash_show(target: Target, name: str, *, idb_path: str | None = None) -> str:
    idb = find_idb(idb_path)
    return run_idb(idb, ["crash", "show", name], udid=target.target_id, timeout=30,
                   companion=target_companion(target)).stdout or ""


def file_pull(target: Target, bundle_id: str, remote: str, local: Path,
              *, idb_path: str | None = None) -> Path:
    idb = find_idb(idb_path)
    local.parent.mkdir(parents=True, exist_ok=True)
    run_idb(
        idb,
        ["file", "pull", "--bundle-id", bundle_id, remote, str(local)],
        udid=target.target_id,
        timeout=60,
        companion=target_companion(target),
    )
    return local
