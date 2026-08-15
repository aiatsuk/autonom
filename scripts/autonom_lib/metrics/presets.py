"""What heavier profilers this host can actually run (§2.7).

`available` is a host-tool answer, not a promise: device-side checks (is
simpleperf on this image?) happen at trace time with their own error codes.
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
        rows.append({"id": preset["id"], "available": tools[preset["tool"]],
                     "tool": preset["tool"],
                     **({} if tools[preset["tool"]] else
                        {"reason": f"{preset['tool']}_missing"})})
    return {"presets": rows, "tools": tools}
