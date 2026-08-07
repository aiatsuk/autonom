#!/usr/bin/env python3
"""Summarize Flutter integration-test frame build/raster timing arrays."""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

_BUILD_KEYS: Sequence[str] = (
    "frame_build_times",
    "frameBuildTimes",
    "build_times_millis",
    "buildDurationMillis",
)
_RASTER_KEYS: Sequence[str] = (
    "frame_rasterizer_times",
    "frame_raster_times",
    "frameRasterizerTimes",
    "raster_times_millis",
    "rasterDurationMillis",
)


def numeric_list(value: Any) -> list[float] | None:
    if not isinstance(value, list) or not value:
        return None
    out: list[float] = []
    for item in value:
        if not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        out.append(number)
    return out


def find_key(value: Any, keys: Iterable[str]) -> list[float] | None:
    """Depth-first search for the first numeric array under any of `keys`."""
    wanted = frozenset(keys)
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key in wanted:
                if key in current:
                    parsed = numeric_list(current[key])
                    if parsed is not None:
                        return parsed
            # continue into values (LIFO: reverse for stable-ish left-first)
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


def summarize(values: list[float], budget_ms: float) -> dict[str, float | int]:
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize Flutter integration-test frame timings"
    )
    parser.add_argument("input")
    parser.add_argument("--budget-ms", type=float, default=16.67)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    build = find_key(payload, _BUILD_KEYS)
    raster = find_key(payload, _RASTER_KEYS)
    if build is None and raster is None:
        print("error: no supported frame timing arrays found", file=sys.stderr)
        return 3

    result: dict[str, Any] = {"budget_ms": args.budget_ms}
    if build is not None:
        result["build"] = summarize(build, args.budget_ms)
    if raster is not None:
        result["raster"] = summarize(raster, args.budget_ms)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Frame budget: {args.budget_ms:.2f} ms")
        for lane in ("build", "raster"):
            if lane not in result:
                continue
            stats = result[lane]
            print(
                f"{lane}: n={stats['frames']} avg={stats['average_ms']:.2f} "
                f"p90={stats['p90_ms']:.2f} p99={stats['p99_ms']:.2f} "
                f"worst={stats['worst_ms']:.2f} over={stats['over_budget']} "
                f"({stats['over_budget_percent']:.1f}%)"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
