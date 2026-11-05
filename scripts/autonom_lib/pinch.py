"""A bounded two-finger pinch, dispatched by the existing Canvas input transports."""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import errors, ios_idb
from .platform import Target

MIN_SCALE = 0.1
MAX_SCALE = 10.0
MIN_DURATION = 0.1
MAX_DURATION = 2.0
BRIDGE = Path(__file__).resolve().parents[1] / "pinch_bridge.mjs"
BRIDGE_TIMEOUT = 120
CLEANUP_TIMEOUT = 40


def _run_bridge(command: list[str], request: str, *, timeout: float = BRIDGE_TIMEOUT,
                cleanup_timeout: float = CLEANUP_TIMEOUT) -> subprocess.CompletedProcess:
    """Terminate gracefully first so the bridge can reap the transport it owns."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(input=request, timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        cleanup_confirmed = False
        try:
            _, stderr = process.communicate(timeout=cleanup_timeout)
            try:
                cleanup_confirmed = json.loads(stderr).get("cleanup_confirmed") is True
            except (ValueError, AttributeError):
                pass
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise errors.AutonomError(
            errors.UI_ACTION_UNCERTAIN, "pinch transport timed out; input may have been sent",
            "Inspect the screen before repeating the gesture. If cleanup was not confirmed, "
            "check the temporary transport before retrying.", sent="unknown",
            cleanup_confirmed=cleanup_confirmed) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def check_values(scale: float, duration: float) -> None:
    for name, value, low, high in (("scale", scale, MIN_SCALE, MAX_SCALE),
                                   ("duration", duration, MIN_DURATION, MAX_DURATION)):
        if not math.isfinite(value) or not low <= value <= high:
            raise errors.AutonomError(
                errors.INVALID_VALUE, f"--{name} must be between {low:g} and {high:g}",
                "Use --scale 2 to zoom in or --scale 0.5 to zoom out; "
                "--duration is seconds.", **{name: value if math.isfinite(value) else str(value)})


def radius_for(x: int, y: int, width: int, height: int, scale: float) -> int:
    """Start radius along the horizontal axis; both paths stay inside the screen."""
    if not (0 <= x < width and 0 <= y < height):
        raise errors.AutonomError(
            errors.COORDINATE_SPACE_MISMATCH,
            f"point ({x}, {y}) is outside the {width}x{height} screen",
            "Use Android pixels or iOS points, and choose a point on the screen.",
            point=[x, y], screen=[width, height])
    available = min(x, width - 1 - x)
    # Android's standard scale recognizer has a minimum starting span in dp.
    # A fixed 80 pixels can be too narrow, especially on a dense display, and
    # an inward gesture would never enter the recognizer. Scale the default to
    # the display while keeping the entire gesture inside its horizontal edge.
    preferred = max(10, int(min(width, height) * 0.42))
    radius = min(preferred, int(available / max(1.0, scale)))
    if radius < 10 or radius * scale < 2:
        raise errors.AutonomError(
            errors.INVALID_COORDINATES, "the pinch has too little room around its center",
            "Choose a center farther from the left and right edges, or use a smaller scale.",
            point=[x, y], screen=[width, height])
    return radius


def dispatch(target: Target, *, x: int, y: int, scale: float, duration: float,
             radius: int, width: int, height: int) -> str:
    node = shutil.which("node")
    if not node:
        raise errors.AutonomError(errors.TOOL_MISSING, "pinch needs Node.js",
                                  "Install Node.js to use the pinch input transport.",
                                  tool="node", capability="ui.pinch")
    request: dict[str, Any] = {"platform": target.platform, "target": target.target_id,
                               "x": x, "y": y, "scale": scale, "duration": duration,
                               "radius": radius, "width": width, "height": height}
    if target.platform == "android":
        request["adb"] = target.tool
    else:
        request["companion"] = ios_idb.target_companion(target)
    try:
        result = _run_bridge([node, str(BRIDGE)], json.dumps(request))
    except OSError as exc:
        raise errors.AutonomError(errors.BACKEND_FAILED, f"could not start pinch transport: {exc}",
                                  "Check the Node.js installation.") from exc
    if result.returncode:
        try:
            payload = json.loads(result.stderr)
            if (not isinstance(payload, dict) or payload.get("ok") is not False or
                    any(not isinstance(payload.get(key), str) or not payload[key]
                        for key in ("error_code", "error", "hint"))):
                raise ValueError
        except (ValueError, TypeError):
            raise errors.AutonomError(
                errors.UI_ACTION_UNCERTAIN,
                "pinch transport failed without a structured result; input may have been sent",
                "Inspect the screen before repeating the gesture.", sent="unknown") from None
        reserved = {"ok", "error_code", "error", "hint"}
        raise errors.AutonomError(payload.get("error_code", errors.BACKEND_FAILED),
                                  payload.get("error", "pinch transport failed"),
                                  payload.get("hint", "Inspect the target and try again."),
                                  **{k: v for k, v in payload.items() if k not in reserved})
    try:
        payload = json.loads(result.stdout)
        if payload.get("ok") is not True or payload.get("backend") not in ("scrcpy", "idb"):
            raise ValueError
        return payload["backend"]
    except (ValueError, TypeError, AttributeError, KeyError):
        raise errors.AutonomError(
            errors.UI_ACTION_UNCERTAIN, "pinch transport returned an invalid result",
            "Inspect the screen before repeating the gesture.", sent="unknown") from None
