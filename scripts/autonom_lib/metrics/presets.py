"""What heavier profilers this host can actually run (§2.7).

`available` is a host-tool answer, not a promise: device-side checks (is
simpleperf on this image?) happen at trace time with their own error codes.
The one known target-side refusal is listed here too: every Autonom iOS
target is a Simulator, and Instruments refuses some templates there.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Any

PRESETS: tuple[dict[str, str], ...] = (
    {"id": "simpleperf", "platform": "android", "tool": "adb"},
    {"id": "gfxinfo-flow", "platform": "android", "tool": "adb"},
    {"id": "allocations", "platform": "ios", "tool": "xctrace"},
    {"id": "time-profiler", "platform": "ios", "tool": "xctrace"},
    {"id": "leaks", "platform": "ios", "tool": "xctrace"},
    {"id": "hitches", "platform": "ios", "tool": "xctrace"},
)

# preset id -> why it cannot run against the iOS Simulator
SIMULATOR_UNSUPPORTED: dict[str, str] = {
    "hitches": ("Instruments' Animation Hitches template is not supported on "
                "the iOS Simulator; record it in Instruments on a physical "
                "device, or use time-profiler for main-thread stalls here"),
}


def xctrace_available(xcrun: str | None) -> bool:
    if not xcrun:
        return False
    try:
        completed = subprocess.run(
            [xcrun, "xctrace", "version"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=20)
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def listing(platform: str | None, *, adb: str | None,
            xcrun: str | None) -> dict[str, Any]:
    tools = {
        "adb": adb is not None,
        "xctrace": xctrace_available(xcrun),
        "ps": shutil.which("ps") is not None,
        "du": shutil.which("du") is not None,
    }
    rows = []
    for preset in PRESETS:
        if platform and preset["platform"] != platform:
            rows.append({"id": preset["id"], "available": False,
                         "reason": f"{preset['platform']}_only",
                         "tool": preset["tool"]})
            continue
        if preset["id"] in SIMULATOR_UNSUPPORTED:
            rows.append({"id": preset["id"], "available": False,
                         "reason": "unsupported_on_simulator",
                         "note": SIMULATOR_UNSUPPORTED[preset["id"]],
                         "tool": preset["tool"]})
            continue
        rows.append({"id": preset["id"], "available": tools[preset["tool"]],
                     "tool": preset["tool"],
                     **({} if tools[preset["tool"]] else
                        {"reason": f"{preset['tool']}_missing"})})
    return {"presets": rows, "tools": tools}
