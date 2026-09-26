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
    ` 12% 4321/com.example.app: 8% user + 4% kernel`.

    The package must end exactly at `:` or whitespace — a bare word boundary
    would let `com.example.app` claim `com.example.app.dev`'s line."""
    # Recent SystemUI prefixes the percentage of a process that was not in
    # the previous window with a sign (` +0% 12772/ru.skywool.knix: …`, seen
    # on an API-37 emulator); the old pattern demanded a digit first and
    # reported the app as absent from cpuinfo.
    pattern = re.compile(
        rf"^\s*[+-]?([\d.]+)%\s+\d+/{re.escape(package)}(?=[:\s]|$)", re.MULTILINE)
    hit = pattern.search(text)
    return float(hit.group(1)) if hit else None


_CPUINFO_WINDOW = re.compile(
    r"CPU usage from\s+(\d+)(ms|s)\s+to\s+(\d+)(ms|s)\s+(ago|later)")


def parse_cpuinfo_window(text: str) -> dict[str, int] | None:
    """The averaging window `dumpsys cpuinfo` printed its figures over.

    ProcessCpuTracker prints `CPU usage from 221640ms to 186792ms ago (…)`:
    the window opened 221.6 s and closed 186.8 s before the dump, so every
    percentage below it describes load from three minutes earlier. Returns
    {cpu_window_ms, cpu_window_age_ms}; a `later` window (the tracker's
    clock ahead of the dump) has age 0."""
    hit = _CPUINFO_WINDOW.search(text)
    if not hit:
        return None
    scale = {"ms": 1, "s": 1000}
    start = int(hit.group(1)) * scale[hit.group(2)]
    end = int(hit.group(3)) * scale[hit.group(4)]
    if hit.group(5) == "ago":
        return {"cpu_window_ms": max(start - end, 0), "cpu_window_age_ms": end}
    return {"cpu_window_ms": max(end - start, 0), "cpu_window_age_ms": 0}


def parse_proc_pid_stat(text: str, pid: int) -> int | None:
    """utime + stime (clock ticks) from the `/proc/<pid>/stat` line of `pid`.

    `comm` sits in parentheses and may itself hold spaces or `)`, so the
    fields are counted after the **last** `)`: state is field 3, utime 14
    and stime 15 (proc(5))."""
    for line in text.splitlines():
        head, sep, rest = line.strip().rpartition(")")
        if not sep or not head.startswith(f"{pid} ("):
            continue
        fields = rest.split()
        if len(fields) < 13:
            return None
        try:
            return int(fields[11]) + int(fields[12])
        except ValueError:
            return None
    return None


def parse_proc_stat(text: str) -> dict[str, int] | None:
    """Machine-wide ticks from `/proc/stat`, plus the online core count.

    The aggregate `cpu` line summed over user+nice+system+idle+iowait+irq+
    softirq+steal counts ticks on **all** cores together; divided by the
    number of per-core `cpuN` lines it is wall time in ticks. That division
    is what puts a process' tick delta on cpuinfo's per-process one-core
    scale (CPU time over wall time, cpuinfo dividing by wall uptime) instead
    of a share of the whole machine.

    `steal` is wall time a VM guest (e.g. an emulator on a loaded host) was
    not given; leaving it out would shrink the denominator and read high.
    `guest`/`guest_nice` stay out: the kernel already counts them in
    user/nice. Old kernels print fewer than eight values; whatever is
    present is summed."""
    total: int | None = None
    cores = 0
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "cpu" and total is None:
            values = fields[1:9]  # user nice system idle iowait irq softirq steal
            if not values:
                return None
            try:
                total = sum(int(value) for value in values)
            except ValueError:
                return None
        elif re.fullmatch(r"cpu\d+", fields[0]):
            cores += 1
    if total is None:
        return None
    return {"total_ticks": total, "cpu_count": cores}
