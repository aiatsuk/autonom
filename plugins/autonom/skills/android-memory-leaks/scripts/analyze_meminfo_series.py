#!/usr/bin/env python3
"""Compare a time series of Android dumpsys meminfo captures.

Flags directional growth without asserting a leak — retained-path proof is
still required by the memory investigation skill.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Iterable, Mapping

# Ordered pattern candidates per metric. First successful match wins.
_METRIC_SPECS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "total_pss_kb",
        (
            r"TOTAL\s+PSS:\s*([\d,]+)",
            r"^\s*TOTAL\s+([\d,]+)\s+",
        ),
    ),
    ("total_rss_kb", (r"TOTAL\s+RSS:\s*([\d,]+)",)),
    ("java_heap_kb", (r"^\s*Java Heap:\s*([\d,]+)",)),
    ("native_heap_kb", (r"^\s*Native Heap:\s*([\d,]+)",)),
    ("graphics_kb", (r"^\s*Graphics:\s*([\d,]+)",)),
    ("private_other_kb", (r"^\s*Private Other:\s*([\d,]+)",)),
    ("system_kb", (r"^\s*System:\s*([\d,]+)",)),
    ("activities", (r"\bActivities:\s*(\d+)",)),
    ("views", (r"\bViews:\s*(\d+)",)),
    ("view_root_impl", (r"\bViewRootImpl:\s*(\d+)",)),
    ("app_contexts", (r"\bAppContexts:\s*(\d+)",)),
    ("webviews", (r"\bWebViews:\s*(\d+)",)),
)

_COMPILED: dict[str, tuple[re.Pattern[str], ...]] = {
    name: tuple(re.compile(p, re.MULTILINE | re.IGNORECASE) for p in patterns)
    for name, patterns in _METRIC_SPECS
}


def parse_number(value: str) -> int:
    return int(value.replace(",", ""))


def parse_meminfo(text: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for name, patterns in _COMPILED.items():
        for pattern in patterns:
            hit = pattern.search(text)
            if hit:
                found[name] = parse_number(hit.group(1))
                break
    return found


def linear_slope(values: list[int]) -> float:
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


def _series_for_metric(
    samples: list[dict[str, object]], name: str
) -> list[int]:
    series: list[int] = []
    for sample in samples:
        metrics = sample.get("metrics")
        if not isinstance(metrics, Mapping):
            continue
        if name in metrics:
            series.append(int(metrics[name]))  # type: ignore[index]
    return series


def summarize(samples: list[dict[str, object]], min_growth_kb: int) -> dict[str, object]:
    names = sorted(
        {
            key
            for sample in samples
            if isinstance(sample.get("metrics"), Mapping)
            for key in sample["metrics"]  # type: ignore[index]
        }
    )
    metrics: dict[str, dict[str, object]] = {}
    for name in names:
        values = _series_for_metric(samples, name)
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
        "samples": samples,
        "metrics": metrics,
        "directional_growth_leads": leads,
        "interpretation": (
            "Directional trend only. A leak requires a retained object/root path or a "
            "repeatable accumulation pattern under an equivalent flow."
        ),
    }


def resolve_inputs(values: Iterable[str], pattern: str) -> list[Path]:
    paths: list[Path] = []
    for raw in values:
        candidate = Path(raw)
        if candidate.is_dir():
            paths.extend(candidate.glob(pattern))
        elif candidate.is_file():
            paths.append(candidate)
        else:
            paths.extend(Path().glob(raw))
    unique = {path.resolve() for path in paths}
    return sorted(unique, key=lambda p: (p.stat().st_mtime_ns, p.name))


def render_text(report: dict[str, object]) -> str:
    out = [f"Samples: {report['sample_count']}"]
    metrics = report["metrics"]
    assert isinstance(metrics, dict)
    for name, raw in metrics.items():
        assert isinstance(raw, dict)
        mark = "  LEAD" if raw["directional_growth"] else ""
        unit = " KB" if str(name).endswith("_kb") else ""
        out.append(
            f"{name}: {raw['first']} -> {raw['last']} "
            f"(delta {raw['delta']:+}{unit}, slope {raw['slope_per_capture']:.1f}/capture)"
            f"{mark}"
        )
    leads = report["directional_growth_leads"]
    if leads:
        out.append("Directional growth leads: " + ", ".join(leads))  # type: ignore[arg-type]
    else:
        out.append("Directional growth leads: none")
    out.append(str(report["interpretation"]))
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare a series of Android dumpsys meminfo captures"
    )
    parser.add_argument("inputs", nargs="+", help="files, directories, or glob patterns")
    parser.add_argument(
        "--glob",
        default="*-meminfo.txt",
        help="glob applied when an input is a directory",
    )
    parser.add_argument(
        "--min-growth-kb",
        type=int,
        default=1024,
        help="minimum first→last growth (KB metrics) to flag a lead",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    paths = resolve_inputs(args.inputs, args.glob)
    if not paths:
        print("error: no meminfo captures found", file=sys.stderr)
        return 2

    samples: list[dict[str, object]] = []
    for path in paths:
        try:
            metrics = parse_meminfo(path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            print(f"error: {path}: {exc}", file=sys.stderr)
            return 2
        if not metrics:
            print(f"error: no supported meminfo metrics found in {path}", file=sys.stderr)
            return 3
        samples.append({"path": str(path), "metrics": metrics})

    report = summarize(samples, max(0, args.min_growth_kb))
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
