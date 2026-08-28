#!/usr/bin/env python3
"""Persistent NDJSON action bridge used by Mobile Canvas.

Canvas never actuates adb/idb directly.  Every input crosses this process,
uses the same platform-neutral action functions as the CLI, and is journaled
with an explicit human/agent/replay/system origin.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autonom_lib import actions, errors, journal, session, ui  # noqa: E402
from autonom_lib.platform import ANDROID, IOS, Target  # noqa: E402


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--platform", choices=(ANDROID, IOS), required=True)
    value.add_argument("--target", required=True)
    value.add_argument("--tool", required=True)
    return value


def dispatch(target: Target, message: dict[str, Any]) -> dict[str, Any]:
    operation = message.get("op")
    payload = message.get("payload") or {}
    origin = message.get("origin")
    if origin not in ("human", "agent", "replay", "system"):
        raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                  "Canvas action origin is invalid")
    record = session.load_current()
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
