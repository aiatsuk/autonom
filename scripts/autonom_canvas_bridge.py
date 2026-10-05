#!/usr/bin/env python3
"""Persistent NDJSON action bridge used by Mobile Canvas.

Canvas never actuates adb/idb directly.  Every input crosses this process,
uses the same platform-neutral action functions as the CLI, and is journaled
with an explicit human/agent/replay/system origin.  Journaling goes into the
current Autonom session only when that session is on this Canvas's own
target; a session on another device never receives this Canvas's actions.

The exceptions are the streamed transports: on scrcpy (Android) Canvas
writes streamed input to the scrcpy control socket itself, and on idb (the iOS
Simulator) touches, the wheel and Home/Power go to the HID stream of the
idb_companion it owns, because a process hop per pointer move is too slow.
It then sends one `record` per completed action, which only journals and
never actuates.  Text on idb still comes here as a `text` operation.  Display
size changes (`wm size`/`wm density`) follow the record path on every Android
transport: Canvas runs them on its own serial, then sends one `display` record.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autonom_lib import actions, errors, journal, session, ui  # noqa: E402
from autonom_lib.platform import ANDROID, IOS, Target  # noqa: E402

ORIGINS = ("human", "agent", "replay", "system")
# The page's buttons that iOS has a hardware button for.
IOS_BUTTON_FOR_KEY = {"KEYCODE_HOME": "HOME", "KEYCODE_POWER": "LOCK"}
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
CONTROL_MODES = ("pause", "resume", "takeover", "release")
CONTROL_OWNERS = ("shared",) + ORIGINS
KEY_NAME = re.compile(r"[A-Za-z0-9_]{1,64}")
MAX_COUNT = 1_000_000
MAX_PIXEL = 100_000
MAX_SCROLL = 1_000_000.0
# Control messages are at most 64 KiB, so no single text entry can be longer.
MAX_TEXT = 64 * 1024


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
                     payload: Any) -> dict[str, Any]:
    """Journal one action Canvas already performed. Never touches the device."""
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
    detail = actions.record_detail(record, f"canvas-{kind}", detail_payload)
    if detail:
        result["detail"] = detail
    journal.record_action(
        record, verb=f"ui {kind}", argv=["ui", kind, "<canvas>"],
        payload=result, ok=True, origin=origin,
    )
    return result


def journal_session(target: Target) -> dict[str, Any] | None:
    """The current session when it is on this Canvas's target, else None.

    Canvas serves one target while the current session may be on another
    device; journaling there would put this Canvas's actions into that other
    device's record.  With None the action still runs and nothing is journaled.
    """
    record = session.load_current()
    if not record or record.get("target_id") != target.target_id:
        return None
    platform = record.get("platform")
    if platform and platform != target.platform:
        return None
    return record


def dispatch(target: Target, message: dict[str, Any]) -> dict[str, Any]:
    operation = message.get("op")
    payload = message.get("payload") or {}
    origin = message.get("origin")
    if origin not in ORIGINS:
        raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                  "Canvas action origin is invalid")
    record = journal_session(target)
    if operation == "record":
        return record_completed(record, origin, payload)
    detail_payload = {"kind": operation, "origin": origin,
                      "canvas": True}
    if operation == "screen-size":
        size = ui.screen_size(target)
        return {"ok": True, "display": (
            {"width": size[0], "height": size[1]} if size else None)}
    if operation == "tap":
        x, y = int(payload["x"]), int(payload["y"])
        ui.tap(target, x, y)
        detail_payload.update({"coordinate": True, "x": x, "y": y})
        result = {"ok": True, "x": x, "y": y}
    elif operation == "swipe":
        x1, y1 = int(payload["x1"]), int(payload["y1"])
        x2, y2 = int(payload["x2"]), int(payload["y2"])
        duration_ms = max(1, min(5000, int(payload.get("duration", 250))))
        ui.swipe(target, x1, y1, x2, y2, duration_ms / 1000)
        detail_payload.update({"from": [x1, y1], "to": [x2, y2],
                               "duration_ms": duration_ms})
        result = {"ok": True, "x1": x1, "y1": y1,
                  "x2": x2, "y2": y2, "duration": duration_ms}
    elif operation == "key":
        key = str(payload["key"])
        if target.platform == IOS:
            # The page's Home and Power buttons send Android names; iOS has buttons.
            key = IOS_BUTTON_FOR_KEY.get(key, key)
        ui.press_key(target, key)
        detail_payload["key"] = key
        result = {"ok": True, "key": key}
    elif operation == "text":
        text = str(payload.get("text", ""))
        sensitive = bool(payload.get("sensitive", False))
        ui.type_text(target, text)
        detail_payload.update({"sensitive": sensitive,
                               "text": None if sensitive else text,
                               "text_len": len(text)})
        # Text typed from the idb transport's control socket says so; any other
        # value is dropped rather than copied into the journal.
        if payload.get("transport") in RECORD_TRANSPORTS:
            detail_payload["transport"] = payload["transport"]
        result = {"ok": True, "typed": f"<{len(text)} chars>"}
    else:
        raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                  f"unsupported Canvas operation {operation!r}")
    detail = actions.record_detail(record, f"canvas-{operation}", detail_payload)
    if detail:
        result["detail"] = detail
    journal.record_action(
        record, verb=f"ui {operation}", argv=["ui", str(operation), "<canvas>"],
        payload=result, ok=True, origin=origin,
    )
    return result


def main() -> int:
    args = parser().parse_args()
    target = Target(args.platform, args.target, args.tool,
                    {"serial": args.target} if args.platform == ANDROID
                    else {"udid": args.target})
    for line in sys.stdin:
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
