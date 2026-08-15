"""Parse `dumpsys meminfo <package>` text into stable numeric metrics.

The pattern table is the library twin of the android-memory-leaks skill's
standalone `analyze_meminfo_series.py`; `tests/test_metrics.py` pins both to
the same fixtures so they cannot drift apart.
"""
from __future__ import annotations

import re

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


def parse_meminfo(text: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for name, patterns in _COMPILED.items():
        for pattern in patterns:
            hit = pattern.search(text)
            if hit:
                found[name] = int(hit.group(1).replace(",", ""))
                break
    return found


def parse_proc_status(text: str) -> dict[str, int]:
    """Threads / VmRSS / VmSize from /proc/<pid>/status (values in kB)."""
    out: dict[str, int] = {}
    for key, name in (("Threads", "threads"), ("VmRSS", "vm_rss_kb"),
                      ("VmSize", "vm_size_kb")):
        hit = re.search(rf"^{key}:\s*(\d+)", text, re.MULTILINE)
        if hit:
            out[name] = int(hit.group(1))
    return out


def parse_cpuinfo(text: str, package: str) -> float | None:
    """The process' load percent from `dumpsys cpuinfo`, e.g.
    ` 12% 4321/com.example.app: 8% user + 4% kernel`."""
    pattern = re.compile(
        rf"^\s*([\d.]+)%\s+\d+/{re.escape(package)}(?::|\b)", re.MULTILINE)
    hit = pattern.search(text)
    return float(hit.group(1)) if hit else None
