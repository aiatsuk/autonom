"""Frame statistics: Android gfxinfo and Flutter timing files (§2.5).

Android parsing is best-effort by design — gfxinfo's shape varies by API
level, so the raw text is always the artifact and the summary only claims
the fields it actually matched. The Flutter summary is the library twin of
the flutter-performance-audit skill's `frame_timings_summary.py`, pinned
by an equivalence test.
"""
from __future__ import annotations

import math
import re
import statistics
from typing import Any, Iterable, Sequence

from .. import adb as adb_mod
from ..platform import Target

_GFX_PATTERNS: tuple[tuple[str, str], ...] = (
    ("total_frames", r"Total frames rendered:\s*(\d+)"),
    ("janky_frames", r"Janky frames:\s*(\d+)"),
    ("percentile_50_ms", r"50th percentile:\s*(\d+)ms"),
    ("percentile_90_ms", r"90th percentile:\s*(\d+)ms"),
    ("percentile_95_ms", r"95th percentile:\s*(\d+)ms"),
    ("percentile_99_ms", r"99th percentile:\s*(\d+)ms"),
)


def reset(target: Target, app_id: str) -> None:
    adb_mod.run_adb(target.tool,
                    ["shell", "dumpsys", "gfxinfo", app_id, "reset"],
                    serial=target.target_id, check=False, timeout=60)


def capture(target: Target, app_id: str) -> tuple[str, dict[str, Any]]:
    completed = adb_mod.run_adb(
        target.tool, ["shell", "dumpsys", "gfxinfo", app_id, "framestats"],
        serial=target.target_id, check=False, timeout=60)
    raw = completed.stdout or ""
    return raw, parse_gfxinfo(raw)


def parse_gfxinfo(text: str) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for name, pattern in _GFX_PATTERNS:
        hit = re.search(pattern, text)
        if hit:
            summary[name] = int(hit.group(1))
    janky_pct = re.search(r"Janky frames:\s*\d+\s*\(([\d.]+)%\)", text)
    if janky_pct:
        summary["janky_percent"] = float(janky_pct.group(1))
    summary["parsed"] = bool(summary)
    if not summary["parsed"]:
        summary["note"] = ("gfxinfo shape not recognized on this API level; "
                           "the raw artifact holds the truth")
    return summary


# --- Flutter frame timings (twin of frame_timings_summary.py) ----------------

_BUILD_KEYS: Sequence[str] = (
    "frame_build_times", "frameBuildTimes",
    "build_times_millis", "buildDurationMillis",
)
_RASTER_KEYS: Sequence[str] = (
    "frame_rasterizer_times", "frame_raster_times", "frameRasterizerTimes",
    "raster_times_millis", "rasterDurationMillis",
)


def _numeric_list(value: Any) -> list[float] | None:
    if not isinstance(value, list) or not value:
        return None
    out: list[float] = []
    for item in value:
        if not isinstance(item, (int, float)) or isinstance(item, bool):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        out.append(number)
    return out


def _find_key(value: Any, keys: Iterable[str]) -> list[float] | None:
    wanted = frozenset(keys)
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key in wanted:
                if key in current:
                    parsed = _numeric_list(current[key])
                    if parsed is not None:
                        return parsed
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend(reversed(current))
    return None


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percent
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _lane(values: list[float], budget_ms: float) -> dict[str, float | int]:
    over = sum(1 for sample in values if sample > budget_ms)
    total = len(values)
    return {
        "frames": total,
        "average_ms": statistics.fmean(values),
        "p50_ms": percentile(values, 0.50),
        "p90_ms": percentile(values, 0.90),
        "p99_ms": percentile(values, 0.99),
        "worst_ms": max(values),
        "over_budget": over,
        "over_budget_percent": 100.0 * over / total,
    }


def flutter_summary(payload: Any, budget_ms: float) -> dict[str, Any] | None:
    """-> {budget_ms, build?, raster?}, or None when no timing arrays exist."""
    build = _find_key(payload, _BUILD_KEYS)
    raster = _find_key(payload, _RASTER_KEYS)
    if build is None and raster is None:
        return None
    result: dict[str, Any] = {"budget_ms": budget_ms}
    if build is not None:
        result["build"] = _lane(build, budget_ms)
    if raster is not None:
        result["raster"] = _lane(raster, budget_ms)
    return result
