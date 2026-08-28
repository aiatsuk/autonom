"""Series math: first/last/delta/slope over numeric metrics, leads only.

The summarize algorithm is the proven one from the android-memory-leaks
skill's `analyze_meminfo_series.py` (pinned against it in tests). Its
interpretation line is part of the contract: a directional lead is never
reported as a leak.
"""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import errors

INTERPRETATION = (
    "Directional trend only. A leak requires a retained object/root path or a "
    "repeatable accumulation pattern under an equivalent flow."
)


def linear_slope(values: list[float]) -> float:
    """Ordinary least-squares slope of values vs sample index."""
    n = len(values)
    if n < 2:
        return 0.0
    xs = range(n)
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(values)
    variance_x = sum((x - mean_x) ** 2 for x in xs)
    if variance_x == 0:
        return 0.0
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values))
    return covariance / variance_x


def summarize(samples: list[dict[str, Any]], min_growth_kb: int) -> dict[str, Any]:
    names = sorted({
        key
        for sample in samples
        if isinstance(sample.get("metrics"), Mapping)
        for key in sample["metrics"]
    })
    metrics: dict[str, dict[str, Any]] = {}
    for name in names:
        values = [float(sample["metrics"][name]) for sample in samples
                  if isinstance(sample.get("metrics"), Mapping)
                  and name in sample["metrics"]]
        if not values:
            continue
        decreases = sum(later < earlier for earlier, later in zip(values, values[1:]))
        delta = values[-1] - values[0]
        slope = linear_slope(values)
        max_decreases = max(1, math.floor((len(values) - 1) * 0.25))
        directional = (
            len(values) >= 3
            and delta >= min_growth_kb
            and slope > 0
            and decreases <= max_decreases
        )
        metrics[name] = {
            "samples": len(values),
            "first": values[0],
            "last": values[-1],
            "delta": delta,
            "minimum": min(values),
            "maximum": max(values),
            "slope_per_capture": slope,
            "decreases": decreases,
            "directional_growth": directional,
        }

    leads = [name for name, info in metrics.items() if info["directional_growth"]]
    return {
        "sample_count": len(samples),
        "metrics": metrics,
        "directional_growth_leads": leads,
        "interpretation": INTERPRETATION,
    }


def flatten_snapshot(payload: dict[str, Any]) -> dict[str, float]:
    """Numeric metrics from one snapshot payload, flat for series math."""
    flat: dict[str, float] = {}
    for section in ("memory", "cpu", "proc", "disk"):
        block = payload.get(section)
        if not isinstance(block, Mapping):
            continue
        for key, value in block.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                flat[key] = float(value)
    return flat


def capture(snapshot_fn: Callable[[], dict[str, Any]], *, count: int,
            interval: float,
            sleep: Callable[[float], None]) -> list[dict[str, Any]]:
    """Take `count` snapshots spaced by `interval` seconds (passive: the agent
    drives UI between its own calls, never this loop)."""
    samples: list[dict[str, Any]] = []
    for index in range(count):
        payload = snapshot_fn()
        samples.append({
            "captured_at": payload.get("captured_at"),
            "artifact": (payload.get("artifacts") or [None])[0],
            "metrics": flatten_snapshot(payload),
        })
        if index + 1 < count:
            sleep(max(interval, 0.0))
    return samples


def from_dir(directory: Path, glob: str) -> list[dict[str, Any]]:
    """Offline samples from previously captured snapshot JSON files."""
    files = sorted(directory.glob(glob),
                   key=lambda p: (p.stat().st_mtime_ns, p.name))
    if not files:
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"no snapshot files match {glob!r} under {directory}",
            "Capture some with 'autonom metrics snapshot' first.",
        )
    samples = []
    for file in files:
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise errors.AutonomError(
                errors.BACKEND_FAILED, f"unreadable snapshot {file}: {exc}")
        samples.append({
            "path": str(file),
            "captured_at": payload.get("captured_at"),
            "metrics": flatten_snapshot(payload),
        })
    return samples
