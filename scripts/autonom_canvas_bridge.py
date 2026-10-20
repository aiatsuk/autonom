#!/usr/bin/env python3
"""Persistent NDJSON action bridge used by Mobile Canvas.

Canvas never actuates adb/idb directly.  Every input crosses this process,
uses the same platform-neutral action functions as the CLI, and is journaled
with an explicit human/agent/replay/system origin.  Journaling goes into the
live Autonom session on this Canvas's own target (found by the target's
session pointer, or `current.json` when that session is on this target); a
session on another device never receives this Canvas's actions.

The exceptions are the streamed transports: on scrcpy (Android) Canvas
writes streamed input to the scrcpy control socket itself, and on idb (the iOS
Simulator) touches, the wheel and Home/Power go to the HID stream of the
idb_companion it owns, because a process hop per pointer move is too slow.
It then sends one `record` per completed action, which only journals and
never actuates.  Text on idb still comes here as a `text` operation.  Display
size changes (`wm size`/`wm density`) follow the record path on every Android
transport: Canvas runs them on its own serial, then sends one `display` record.

While a pre-test is active on this target, input with the `human` origin runs
inside `pretest.guard.exempt()`: a QA person's own taps are journaled (with the
pre-test and open check ids) but never counted against the budget or refused.
Input of every other origin goes through the same guard as the CLI's verbs and
can be refused (`budget_exhausted`, `forbidden_action`, `app_not_allowed`, or
`pretest_damaged` when the active pre-test record is damaged); a refusal is journaled
with its code and nothing reaches the device. A `record` only journals input
Canvas already streamed, so it is never counted or refused.

A request may also carry `session_id` (the Autonom session the Canvas started
or reused for this device) and `control` (the Canvas's claim on the host-wide
control lease, `{"holder_id", "generation"}`). A bound request journals into
that session only and is refused (`session_stopped`,
`session_target_mismatch`, `session_not_found`) before any device action when
the session stopped or belongs to another target. Actuating ops (`tap`,
`swipe`, `key`, `text`) are admitted against the target's lease before the
actuator runs. The `control-*` ops are the Canvas's only way to write a lease
file. Requests without these fields behave exactly as before.

Order for an actuating op: the request is parsed, the bound session is
checked, then the host lease admits the input, then the pre-test guard (run
inside the shared `ui` functions) counts or refuses it, then the device acts.
A lease refusal therefore never consumes pre-test budget, and neither refusal
sends anything to the device.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, ContextManager

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autonom_lib import actions, control, errors, ios_idb, journal, session, ui  # noqa: E402
from autonom_lib.platform import ANDROID, IOS, Target  # noqa: E402
from autonom_lib.pretest import guard  # noqa: E402

ORIGINS = ("human", "agent", "replay", "system")
# The page's buttons that iOS has a hardware button for.
IOS_BUTTON_FOR_KEY = {"KEYCODE_HOME": "HOME", "KEYCODE_POWER": "LOCK",
                      "KEYCODE_VOLUME_UP": "VOLUME_UP", "KEYCODE_VOLUME_DOWN": "VOLUME_DOWN"}
# The iOS buttons only idb presses: `ui.press_key` takes the buttons every iOS input
# backend has, so the bridge presses these through idb (`idb ui button`) itself.
IOS_IDB_ONLY_BUTTONS = ("VOLUME_UP", "VOLUME_DOWN")
RECORD_KINDS = ("gesture", "scroll", "key", "text", "paste", "system", "control", "display")
# The transports whose input Canvas streams itself: scrcpy on Android, idb on
# the iOS Simulator. A record without a transport names scrcpy, as before idb.
RECORD_TRANSPORTS = ("scrcpy", "idb")
# Canvas changes the display size on every Android transport, so a `display`
# record may name any of them; display presets are Android-only, so never idb.
DISPLAY_TRANSPORTS = ("scrcpy", "screenrecord", "screencap")
# The presets Canvas applies, `default` (the device's own size) and `restore`
# (the stop-time return to what the device had before the first change).
DISPLAY_PRESETS = ("small", "pixel-11", "pixel-fold", "tablet", "default", "restore")
MAX_DISPLAY_SIDE = 10_000
MAX_DENSITY = 1_000
SYSTEM_OPS = ("back", "home", "app-switch", "power", "volume-up", "volume-down", "wake",
              "notifications", "quick-settings", "collapse", "rotate", "keyframe")
# `lost`: the Canvas lost its host lease to another holder or to expiry;
# `idle`: the Canvas released its host lease after its idle timeout.
CONTROL_MODES = ("pause", "resume", "takeover", "release", "lost", "idle")
CONTROL_OWNERS = ("shared",) + ORIGINS
KEY_NAME = re.compile(r"[A-Za-z0-9_]{1,64}")
MAX_COUNT = 1_000_000
MAX_PIXEL = 100_000
MAX_SCROLL = 1_000_000.0
# Control messages are at most 64 KiB, so no single text entry can be longer.
MAX_TEXT = 64 * 1024
# The operations that send input to the device.
INPUT_OPS = ("tap", "swipe", "key", "text")
# The pre-test guard's refusals: journaled with their code (tp-006). A damaged
# active pre-test record refuses every guarded input the same way.
GUARD_REFUSALS = (errors.BUDGET_EXHAUSTED, errors.FORBIDDEN_ACTION, errors.APP_NOT_ALLOWED,
                  errors.PRETEST_DAMAGED)
# The ops that drive the device and therefore pass the host lease admission.
LEASED_OPS = ("tap", "swipe", "key", "text")
# The lease ops of the Canvas (contract 4.3): they use the bridge's own target,
# need no session and journal nothing.
CONTROL_OPS = ("control-acquire", "control-release", "control-renew", "control-status")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--platform", choices=(ANDROID, IOS), required=True)
    value.add_argument("--target", required=True)
    value.add_argument("--tool", required=True)
    return value


def _invalid(message: str) -> errors.AutonomError:
    return errors.AutonomError(errors.FLOW_COMMAND_INVALID, message)


def _is_number(value: Any) -> bool:
    # JSON booleans arrive as Python bools, which are ints; never count them.
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _integer(payload: dict[str, Any], name: str, maximum: int = MAX_COUNT,
             minimum: int = 0) -> int | None:
    value = payload.get(name)
    if value is None:
        return None
    if not _is_number(value) or value != int(value) or not minimum <= value <= maximum:
        raise _invalid(f"Canvas record {name} must be an integer from {minimum} to {maximum}")
    return int(value)


def _number(payload: dict[str, Any], name: str, limit: float) -> float | None:
    value = payload.get(name)
    if value is None:
        return None
    if not _is_number(value) or abs(value) > limit:
        raise _invalid(f"Canvas record {name} must be a finite number from "
                       f"-{limit:g} to {limit:g}")
    return round(float(value), 3)


def _point(payload: dict[str, Any], name: str) -> list[int] | None:
    value = payload.get(name)
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) != 2
            or not all(_is_number(item) and 0 <= item <= MAX_PIXEL for item in value)):
        raise _invalid(f"Canvas record {name} must be [x, y] in device pixels")
    return [int(round(value[0])), int(round(value[1]))]


def _choice(payload: dict[str, Any], name: str, allowed: tuple[str, ...]) -> str:
    value = payload.get(name)
    if value not in allowed:
        raise _invalid(f"Canvas record {name} must be one of: {', '.join(allowed)}")
    return value


def _key(payload: dict[str, Any]) -> int | str:
    value = payload.get("key")
    if _is_number(value) and value == int(value) and 0 <= value <= 0xFFFF:
        return int(value)
    if isinstance(value, str) and KEY_NAME.fullmatch(value):
        return value
    raise _invalid("Canvas record key must be an Android keycode name or number")


def _text_length(payload: dict[str, Any]) -> int | None:
    length = _integer(payload, "text_len")
    text = payload.get("text")
    if text is not None and (not isinstance(text, str) or len(text) > MAX_TEXT):
        raise _invalid(f"Canvas record text must be a string of at most {MAX_TEXT} characters")
    if length is None and text is not None:
        length = len(text)
    return length


def _display_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """The preset and the size and density read back after it.

    The numbers are optional, so a restore whose read-back failed still leaves
    its record, but a size is never journaled with only one side.
    """
    summary = {"preset": _choice(payload, "preset", DISPLAY_PRESETS),
               "width": _integer(payload, "width", MAX_DISPLAY_SIDE, minimum=1),
               "height": _integer(payload, "height", MAX_DISPLAY_SIDE, minimum=1),
               "density": _integer(payload, "density", MAX_DENSITY, minimum=1)}
    if (summary["width"] is None) != (summary["height"] is None):
        raise _invalid("Canvas record width and height must be given together")
    return summary


def record_summary(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The journaled fields of one completed action, rebuilt from an allowlist.

    Nothing is copied through: a field the kind does not name is dropped, so
    clipboard content or sensitive text can never reach the journal even when
    the sender includes it by mistake.
    """
    if kind == "gesture":
        duration = _number(payload, "duration_ms", 86_400_000)
        if duration is not None and duration < 0:
            raise _invalid("Canvas record duration_ms must not be negative")
        summary = {"pointers": _integer(payload, "pointers", 100),
                   "moves": _integer(payload, "moves"),
                   "duration_ms": None if duration is None else int(round(duration)),
                   "start": _point(payload, "start"),
                   "end": _point(payload, "end")}
    elif kind == "scroll":
        summary = {"events": _integer(payload, "events"),
                   "dx": _number(payload, "dx", MAX_SCROLL),
                   "dy": _number(payload, "dy", MAX_SCROLL)}
    elif kind == "key":
        summary = {"key": _key(payload)}
    elif kind == "text":
        sensitive = payload.get("sensitive", False)
        if not isinstance(sensitive, bool):
            raise _invalid("Canvas record sensitive must be true or false")
        length = _text_length(payload)
        text = payload.get("text")
        summary = {"sensitive": sensitive, "text_len": length,
                   "text": None if sensitive or text is None else text}
    elif kind == "paste":
        # Clipboard content is never journaled, only its length.
        summary = {"text_len": _text_length(payload)}
    elif kind == "system":
        summary = {"op": _choice(payload, "op", SYSTEM_OPS)}
    elif kind == "display":
        summary = _display_summary(payload)
    else:
        summary = {"mode": _choice(payload, "mode", CONTROL_MODES)}
        if payload.get("owner") is not None:
            summary["owner"] = _choice(payload, "owner", CONTROL_OWNERS)
    return {name: value for name, value in summary.items()
            if value is not None or name == "text"}


def record_completed(record: dict[str, Any] | None, origin: str,
                     payload: Any, admitted: dict[str, Any] | None = None) -> dict[str, Any]:
    """Journal one action Canvas already performed. Never touches the device.

    `admitted` is the lease attribution of an input this bridge itself ran
    (an iOS paste), added to the detail and the result."""
    if not isinstance(payload, dict):
        raise _invalid("Canvas record payload must be an object")
    kind = payload.get("kind")
    if kind not in RECORD_KINDS:
        raise _invalid(f"unsupported Canvas record kind {kind!r}; "
                       f"expected one of: {', '.join(RECORD_KINDS)}")
    transports = DISPLAY_TRANSPORTS if kind == "display" else RECORD_TRANSPORTS
    transport = payload.get("transport", RECORD_TRANSPORTS[0])
    if transport not in transports:
        raise _invalid(f"unsupported Canvas record transport {transport!r}")
    detail_payload = {"kind": kind, "origin": origin, "canvas": True,
                      "transport": transport, **record_summary(kind, payload)}
    result: dict[str, Any] = {"ok": True, "recorded": kind, "via": transport}
    if admitted is not None:
        detail_payload["control"] = dict(admitted)
        result["control"] = dict(admitted)
    detail, evidence_warning = actions.record_detail_checked(
        record, f"canvas-{kind}", detail_payload)
    if detail:
        result["detail"] = detail
    if evidence_warning:
        result.setdefault("warnings", []).append(evidence_warning)
    journal.record_action(
        record, verb=f"ui {kind}", argv=["ui", kind, "<canvas>"],
        payload=result, ok=True, origin=origin,
    )
    return result


def journal_session(target: Target) -> dict[str, Any] | None:
    """The live session on this Canvas's own target, else None.

    Each Canvas device journals into its own session: the one whose target
    pointer names this target (a session a workspace Canvas started beside the
    primary), or `current.json`'s when it is on this target. A session on
    another device never receives this Canvas's actions; with None the action
    still runs and nothing is journaled.

    A session that is not the current one is also selected for the rest of
    this operation, so whatever the operation saves into it (a location fix,
    a proxy) stays in that session and never replaces `current.json`.
    """
    record = session.live_for_target(target.target_id, target.platform)
    current_id = None
    if record is not None:
        try:
            current_id = session._current_id(None)
        except Exception:  # noqa: BLE001 - an unreadable current.json names nothing
            current_id = None
    if record is not None and record.get("session_id") != current_id:
        session.select(record.get("session_id"))
    else:
        session.select(None)
    return record


def input_scope(origin: str) -> ContextManager[Any]:
    """Human input is the QA person's own: exempt from the pre-test guard (never
    counted, never refused). Every other origin goes through the guard that the
    shared `ui` functions run, exactly like the CLI's verbs."""
    return guard.exempt() if origin == "human" else contextlib.nullcontext()


def _actuate(target: Target, record: dict[str, Any] | None, operation: str,
             payload: dict[str, Any], origin: str,
             detail_payload: dict[str, Any], claim: control.Claim | None,
             ) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
    """Send one input to the device. Returns the result, whether it is final
    (already journaled, as an iOS paste is) and the lease attribution.

    The host lease admits the input after its payload is parsed and before
    the pre-test guard (inside the shared `ui` functions) or the device sees
    it: a lease refusal never consumes pre-test budget."""
    if operation == "tap":
        x, y = int(payload["x"]), int(payload["y"])
        admitted = admit_input(target, claim)
        ui.tap(target, x, y)
        detail_payload.update({"coordinate": True, "x": x, "y": y})
        return {"ok": True, "x": x, "y": y}, False, admitted
    if operation == "swipe":
        x1, y1 = int(payload["x1"]), int(payload["y1"])
        x2, y2 = int(payload["x2"]), int(payload["y2"])
        duration_ms = max(1, min(5000, int(payload.get("duration", 250))))
        admitted = admit_input(target, claim)
        ui.swipe(target, x1, y1, x2, y2, duration_ms / 1000)
        detail_payload.update({"from": [x1, y1], "to": [x2, y2],
                               "duration_ms": duration_ms})
        return {"ok": True, "x1": x1, "y1": y1,
                "x2": x2, "y2": y2, "duration": duration_ms}, False, admitted
    if operation == "key":
        key = str(payload["key"])
        if target.platform == IOS:
            # The page's Home and Power buttons send Android names; iOS has buttons.
            key = IOS_BUTTON_FOR_KEY.get(key, key)
        admitted = admit_input(target, claim)
        if target.platform == IOS and key in IOS_IDB_ONLY_BUTTONS:
            # `ui.press_key` (which guards itself) does not take these buttons:
            # the same guard runs here before idb is asked.
            guard.before_mutation("key", target=target)
            ios_idb.button(target, key)
        else:
            ui.press_key(target, key)
        detail_payload["key"] = key
        return {"ok": True, "key": key}, False, admitted
    # text
    text = str(payload.get("text", ""))
    sensitive = bool(payload.get("sensitive", False))
    admitted = admit_input(target, claim)
    ui.type_text(target, text)
    if payload.get("paste") is True and payload.get("transport") in RECORD_TRANSPORTS:
        # An iOS paste Canvas also typed: journaled as the paste it is, with its
        # length only, never the clipboard text.
        return record_completed(record, origin, {
            "kind": "paste", "transport": payload["transport"], "text_len": len(text)},
            admitted), True, admitted
    detail_payload.update({"sensitive": sensitive,
                           "text": None if sensitive else text,
                           "text_len": len(text)})
    # Text typed from the idb transport's control socket says so; any other
    # value is dropped rather than copied into the journal.
    if payload.get("transport") in RECORD_TRANSPORTS:
        detail_payload["transport"] = payload["transport"]
    return {"ok": True, "typed": f"<{len(text)} chars>"}, False, admitted


def _session_refusal(code: str, message: str, hint: str, session_id: str,
                     **extra: Any) -> errors.AutonomError:
    return errors.AutonomError(code, message, hint, session_id=session_id, **extra)


def bound_session(target: Target, session_id: str | None) -> dict[str, Any] | None:
    """The session a request is bound to, checked before any device action.

    None: today's lookup (`journal_session`). Otherwise the named session
    must exist (`session_not_found`), must not have stopped
    (`session_stopped`) and must be on this bridge's target
    (`session_target_mismatch`); it is then selected exactly like
    `journal_session` selects (a session other than the current one is
    selected for the rest of the operation) and returned."""
    if session_id is None:
        return journal_session(target)
    if not isinstance(session_id, str):
        raise _invalid("Canvas request session_id must be a session id string or null")
    try:
        record = session.load_by_id(session_id)
    except errors.AutonomError as exc:
        raise _session_refusal(
            errors.SESSION_NOT_FOUND, f"no session {session_id!r}",
            exc.hint or "Start a session on this device, or drop the binding.",
            session_id) from None
    except (OSError, ValueError):
        raise _session_refusal(
            errors.SESSION_NOT_FOUND, f"the record of session {session_id!r} is unreadable",
            "Start a new session on this device.", session_id) from None
    if not isinstance(record, dict):
        raise _session_refusal(
            errors.SESSION_NOT_FOUND, f"the record of session {session_id!r} is unreadable",
            "Start a new session on this device.", session_id)
    stopped = bool(record.get("stopped_at") or session.on_disk(record).get("stopped_at"))
    if not stopped:
        try:
            current = session.load_current()
        except Exception:  # noqa: BLE001 - an unreadable current.json names nothing
            current = None
        stopped = bool(isinstance(current, dict)
                       and current.get("session_id") == session_id
                       and current.get("stopped_at"))
    if stopped:
        raise _session_refusal(
            errors.SESSION_STOPPED, f"session {session_id} has stopped",
            "Start a new session on this device; a stopped session never drives it.",
            session_id)
    record_target = record.get("target_id") or record.get("serial")
    record_platform = record.get("platform") or ANDROID
    if record_target != target.target_id or record_platform != target.platform:
        raise _session_refusal(
            errors.SESSION_TARGET_MISMATCH,
            f"session {session_id} belongs to {record_target}, not {target.target_id}",
            "Bind the request to the session of this device, or drop the binding.",
            session_id, target_id=record_target)
    try:
        current_id = session._current_id(None)
    except Exception:  # noqa: BLE001 - an unreadable current.json names nothing
        current_id = None
    if session_id != current_id:
        session.select(session_id)
    else:
        session.select(None)
    return record


def parse_claim(value: Any) -> control.Claim | None:
    """The request's `control` claim: None when absent, else a `Claim`.
    A malformed object is the bridge's invalid-message refusal."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _invalid("Canvas request control must be an object "
                       "{holder_id, generation} or null")
    holder_id = value.get("holder_id")
    if not isinstance(holder_id, str) or not control.HOLDER_ID_RE.fullmatch(holder_id):
        raise _invalid("Canvas request control.holder_id must be 1-128 characters of "
                       "A-Z a-z 0-9 . _ : @ -")
    generation = value.get("generation")
    if generation is not None and (
            not isinstance(generation, int) or isinstance(generation, bool)
            or not 1 <= generation <= control.MAX_GENERATION):
        raise _invalid("Canvas request control.generation must be a positive integer or null")
    return control.Claim(holder_id, generation)


def admit_input(target: Target, claim: control.Claim | None) -> dict[str, Any] | None:
    """Admit one device input on the target's lease (raises on refusal).
    Returns the attribution `{generation, operation_id}` when the claim holds
    the lease, else None (no lease involved: today's answer)."""
    admitted = control.admit(target.platform, target.target_id, claim)
    if not admitted.get("leased"):
        return None
    return {"generation": admitted["generation"], "operation_id": admitted["operation_id"]}


def _payload_flag(payload: dict[str, Any], name: str, default: bool = False) -> bool:
    value = payload.get(name, default)
    if not isinstance(value, bool):
        raise _invalid(f"Canvas {name} must be true or false")
    return value


def control_op(target: Target, operation: str, payload: Any) -> dict[str, Any]:
    """The Canvas's lease ops on this bridge's target. They journal nothing."""
    if not isinstance(payload, dict):
        raise _invalid(f"Canvas {operation} payload must be an object")
    platform, target_id = target.platform, target.target_id
    if operation == "control-acquire":
        return control.acquire(
            platform, target_id, holder_id=payload.get("holder_id"),
            kind=payload.get("kind", "human"), label=payload.get("label"),
            ttl_s=payload.get("ttl_s"), takeover=_payload_flag(payload, "takeover"),
            session_id=payload.get("session_id"))
    if operation == "control-release":
        return control.release(platform, target_id, holder_id=payload.get("holder_id"),
                               generation=payload.get("generation"))
    if operation == "control-renew":
        return control.renew(platform, target_id, holder_id=payload.get("holder_id"),
                             generation=payload.get("generation"), ttl_s=payload.get("ttl_s"))
    entries = control.status(platform, target_id)
    return {"ok": True, "lease": entries[0] if entries else None}


def dispatch(target: Target, message: dict[str, Any]) -> dict[str, Any]:
    operation = message.get("op")
    payload = message.get("payload") or {}
    origin = message.get("origin")
    if origin not in ORIGINS:
        raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                  "Canvas action origin is invalid")
    claim = parse_claim(message.get("control"))
    session_id = message.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise _invalid("Canvas request session_id must be a session id string or null")
    if operation in CONTROL_OPS:
        return control_op(target, operation, payload)
    record = bound_session(target, session_id)
    if operation == "record":
        return record_completed(record, origin, payload)
    if operation == "screen-size":
        size = ui.screen_size(target)
        return {"ok": True, "display": (
            {"width": size[0], "height": size[1]} if size else None)}
    if operation not in INPUT_OPS:
        raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                  f"unsupported Canvas operation {operation!r}")
    detail_payload = {"kind": operation, "origin": origin,
                      "canvas": True}
    try:
        with input_scope(origin):
            result, final, admitted = _actuate(target, record, operation, payload,
                                               origin, detail_payload, claim)
    except errors.AutonomError as exc:
        if exc.code in GUARD_REFUSALS:
            # Only an active pre-test refuses with these codes: the refused
            # input is journaled with its code (tp-006); nothing reached the device.
            journal.record_action(
                record, verb=f"ui {operation}", argv=["ui", str(operation), "<canvas>"],
                payload=None, ok=False, error_code=exc.code, origin=origin,
            )
        raise
    if final:
        return result
    if admitted is not None:
        detail_payload["control"] = dict(admitted)
        result["control"] = dict(admitted)
    detail, evidence_warning = actions.record_detail_checked(
        record, f"canvas-{operation}", detail_payload)
    if detail:
        result["detail"] = detail
    if evidence_warning:
        result.setdefault("warnings", []).append(evidence_warning)
    journal.record_action(
        record, verb=f"ui {operation}", argv=["ui", str(operation), "<canvas>"],
        payload=result, ok=True, origin=origin,
    )
    return result


def request_stream(*, binary: bool = False) -> Any:
    """Keep the request pipe for this process; point descriptor 0 at the null device.

    Every child process (adb, idb, xcrun) inherits descriptor 0, and `adb shell`
    forwards what it reads there to the device: a request written while one ran
    (a lease acquire behind a screen-size) was read by adb and never answered.
    Call it before anything reads stdin.

    When `sys.stdin` is not the process's descriptor 0 (an in-process caller
    replaced it, as tests that drive `main()` do), no child can inherit it:
    that stream is returned as it is and descriptor 0 is left alone.
    """
    stream = sys.stdin
    try:
        own_descriptor = stream.fileno() == 0
    except (AttributeError, OSError, ValueError):
        own_descriptor = False
    if not own_descriptor:
        return getattr(stream, "buffer", stream) if binary else stream
    descriptor = os.dup(0)
    null = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(null, 0)
    finally:
        os.close(null)
    if binary:
        return os.fdopen(descriptor, "rb")
    return os.fdopen(descriptor, "r", encoding="utf-8", errors="replace")


def main() -> int:
    args = parser().parse_args()
    target = Target(args.platform, args.target, args.tool,
                    {"serial": args.target} if args.platform == ANDROID
                    else {"udid": args.target})
    for line in request_stream():
        try:
            message = json.loads(line)
            result = dispatch(target, message)
            response = {"id": message.get("id"), "ok": True, "result": result}
        except errors.AutonomError as exc:
            response = {"id": locals().get("message", {}).get("id"),
                        **exc.as_dict()}
        except Exception as exc:  # transport boundary: one request must not kill bridge
            response = {"id": locals().get("message", {}).get("id"),
                        "ok": False, "error_code": errors.BACKEND_FAILED,
                        "error": str(exc)}
        print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
