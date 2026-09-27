"""One point-in-time load summary per platform (§2.2).

Honesty rules baked into the payload shape:

- `metric_semantics` names what was measured, and the two platforms are
  deliberately different constants — Android reads guest PSS accounting,
  the iOS Simulator is measured as a **host** process (`ps`), which is
  spelled out in `limitations` on every iOS snapshot.
- Partial data prefers `ok: true` + `warnings[]` (cpu missing but memory
  present); `ok: false` is reserved for "no useful signal at all".
- Android CPU is measured now: two `/proc/<pid>/stat` + `/proc/stat` reads
  a short interval apart. `dumpsys cpuinfo` is only the fallback, because
  its averaging window can have closed minutes before the snapshot; its
  window is reported and an old one is marked stale. How the figure was
  obtained lives in `cpu_sampling`, beside `cpu`, so series math (which
  flattens `cpu`) never treats window bookkeeping as a metric.
- Both Android sources report `process_percent` on the **one-core** scale
  that cpuinfo's per-process lines, toybox `top` and host `ps` all use: CPU
  time over wall time, so a process busy on two cores reads 200. Only
  cpuinfo's TOTAL line is a share of all cores, and it is never read here.
"""
from __future__ import annotations

import subprocess
import time
from typing import Any, Callable

from .. import adb as adb_mod
from .. import errors, ios_simctl
from ..platform import ANDROID, Target
from . import meminfo as meminfo_mod
from . import process as process_mod

ANDROID_SEMANTICS = "android_dumpsys_meminfo_v1"
IOS_SEMANTICS = "ios_simulator_host_process_v1"

IOS_LIMITATIONS = [
    "RSS is the host view of the Simulator process, not guest jetsam accounting",
    "Not comparable 1:1 to Android total_pss_kb",
]

# The fresh sample's interval: 0.5 s is 50 wall ticks at the usual 100 Hz
# clock, so one process tick is 2 points on the one-core scale.
CPU_SAMPLE_S = 0.5
# A cpuinfo window that closed longer ago than this no longer describes
# "now" (ActivityManager refreshes its tracker at most every 5 s, and only
# when something asks).
CPU_STALE_AFTER_MS = 5000
# CPU time over wall time, x100: what ProcessCpuTracker prints per process
# (it divides by the process' wall uptime), what `top` and `ps` print.
CPU_SCALE = "percent_of_one_core"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def take(target: Target, app_id: str, *,
         sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic,
         ) -> tuple[dict[str, Any], str | None]:
    """-> (payload, raw_meminfo_text). The raw dump travels beside the payload,
    never inside it: the payload goes to stdout and the journal, and the
    package rule is that neither ever carries full dump text."""
    if target.platform == ANDROID:
        payload, raw = _android(target, app_id, sleep=sleep, clock=clock)
    else:
        payload, raw = _ios(target, app_id), None
    payload["captured_at"] = _now()
    payload["app_id"] = app_id
    return payload, raw


def _cpu_ticks(target: Target, pid: int) -> tuple[int, dict[str, int]] | str:
    """One `/proc/<pid>/stat` + `/proc/stat` read -> (process ticks, machine
    ticks); a string explains why the read is unusable. A hung or failing
    adb is an unusable read too — the snapshot falls back, it is not lost."""
    try:
        completed = adb_mod.run_adb(
            target.tool, ["shell", "cat", f"/proc/{pid}/stat", "/proc/stat"],
            serial=target.target_id, check=False, timeout=15)
    except subprocess.TimeoutExpired:
        return f"reading /proc/{pid}/stat timed out after 15 s"
    except (adb_mod.AdbError, OSError) as exc:
        return f"reading /proc/{pid}/stat failed: {str(exc)[:200]}"
    text = completed.stdout or ""
    process = meminfo_mod.parse_proc_pid_stat(text, pid)
    machine = meminfo_mod.parse_proc_stat(text)
    if completed.returncode != 0 or process is None or machine is None:
        return f"/proc/{pid}/stat and /proc/stat were not readable as stat lines"
    return process, machine


def _fresh_cpu(target: Target, pid: int, *, sleep: Callable[[float], None],
               clock: Callable[[], float]) -> tuple[dict[str, Any], dict[str, Any]] | str:
    """-> (cpu, cpu_sampling) from two reads CPU_SAMPLE_S apart, on the
    one-core scale of cpuinfo's process lines; a string says why not.

    The machine-tick delta summed over all cores, divided by the core count,
    is the wall time of the window in ticks, so
    `100 * d_process * cores / d_machine` is CPU time over wall time — no
    CLK_TCK or host clock needed. Without a core count that division is
    impossible, and the sample is refused rather than guessed."""
    first = _cpu_ticks(target, pid)
    if isinstance(first, str):
        return first
    opened = clock()
    sleep(CPU_SAMPLE_S)
    second = _cpu_ticks(target, pid)
    closed = clock()
    if isinstance(second, str):
        return second  # e.g. the process died or restarted in between
    cores = second[1]["cpu_count"]
    if not cores or cores != first[1]["cpu_count"]:
        return "/proc/stat listed no stable per-core cpuN lines to scale by"
    process_delta = second[0] - first[0]
    machine_delta = second[1]["total_ticks"] - first[1]["total_ticks"]
    if machine_delta <= 0:
        return "the two /proc samples showed no elapsed machine time"
    if process_delta < 0:
        # the pid was reused by a new process between the reads
        return "the process' CPU ticks went backwards between the two /proc samples"
    # all cores busy is the ceiling; anything above it is read jitter
    percent = round(min(100.0 * cores,
                        100.0 * process_delta * cores / machine_delta), 1)
    window_ms = int(round((closed - opened) * 1000))
    cpu = {"available": True, "process_percent": percent,
           "note": f"measured now over {window_ms} ms from /proc tick deltas, "
                   "as percent of one core (cpuinfo's per-process scale; "
                   "a process busy on two cores reads 200); use a series "
                   "under a fixed flow for claims"}
    sampling: dict[str, Any] = {
        "source": "proc_stat_delta",
        "cpu_window_ms": window_ms,
        "cpu_window_age_ms": int(round(max(clock() - closed, 0.0) * 1000)),
        "stale": False,
        "scale": CPU_SCALE,
        "cpu_count": cores,
    }
    return cpu, sampling


def _cpuinfo_cpu(target: Target, app_id: str, fresh_error: str,
                 warnings: list[dict[str, str]]
                 ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The dumpsys cpuinfo fallback, with its window and staleness spelled out.

    Its per-process lines are already on the one-core scale (CPU ms over the
    process' wall ms), the same as the fresh sample. A hung adb here costs
    the CPU figure only: memory was measured and the snapshot stands."""
    text, failure = "", "had no line for the app process"
    try:
        completed = adb_mod.run_adb(target.tool, ["shell", "dumpsys", "cpuinfo"],
                                    serial=target.target_id, check=False,
                                    timeout=60)
        if completed.returncode == 0:
            text = completed.stdout or ""
        else:
            failure = f"exited {completed.returncode}"
    except subprocess.TimeoutExpired:
        failure = "timed out after 60 s"
    except (adb_mod.AdbError, OSError) as exc:
        failure = f"failed: {str(exc)[:200]}"
    percent = meminfo_mod.parse_cpuinfo(text, app_id) if text else None
    if percent is None:
        warnings.append({
            "code": "cpu_unavailable",
            "error": (f"no fresh /proc sample ({fresh_error}) and dumpsys "
                      f"cpuinfo {failure}"),
            "hint": "CPU load is best-effort; memory metrics are unaffected.",
        })
        return {"available": False}, None
    window = meminfo_mod.parse_cpuinfo_window(text)
    sampling: dict[str, Any] = {"source": "dumpsys_cpuinfo", "scale": CPU_SCALE,
                                "stale_after_ms": CPU_STALE_AFTER_MS,
                                "fresh_sample_error": fresh_error}
    if window:
        sampling.update(window)
        stale = window["cpu_window_age_ms"] > CPU_STALE_AFTER_MS
        described = (f"averaged over {window['cpu_window_ms']} ms that ended "
                     f"{window['cpu_window_age_ms']} ms before this snapshot")
    else:
        stale = True  # no window printed: the figure's age cannot be vouched for
        described = "averaged over a window dumpsys cpuinfo did not print"
    sampling["stale"] = stale
    cpu = {"available": True, "process_percent": percent,
           "note": f"dumpsys cpuinfo figure {described}; use a series under a "
                   "fixed flow for claims"}
    if stale:
        warnings.append({
            "code": "cpu_stale",
            "error": (f"process_percent is a dumpsys cpuinfo figure {described}, "
                      "not the current load"),
            "hint": ("The fresh /proc sample failed "
                     f"({fresh_error}); treat this CPU figure as history."),
        })
    return cpu, sampling


def _android(target: Target, app_id: str, *, sleep: Callable[[float], None],
             clock: Callable[[], float]) -> tuple[dict[str, Any], str]:
    resolved = process_mod.resolve(target, app_id)
    pid = resolved["pid"]
    warnings: list[dict[str, str]] = []

    completed = adb_mod.run_adb(
        target.tool, ["shell", "dumpsys", "meminfo", app_id],
        serial=target.target_id, check=False, timeout=60)
    memory = meminfo_mod.parse_meminfo(completed.stdout or "")
    raw_meminfo = completed.stdout or ""
    if not memory:
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"dumpsys meminfo returned no parseable metrics for {app_id}",
            "The app may have just died; check 'autonom crash list'.",
        )

    proc: dict[str, Any] = {}
    status = adb_mod.run_adb(
        target.tool, ["shell", "cat", f"/proc/{pid}/status"],
        serial=target.target_id, check=False)
    if status.returncode == 0:
        proc = meminfo_mod.parse_proc_status(status.stdout or "")
    if not proc:
        warnings.append({
            "code": "proc_status_unavailable",
            "error": f"/proc/{pid}/status was not readable",
            "hint": "Thread and VmRSS enrichment is skipped; meminfo stands.",
        })

    fresh = _fresh_cpu(target, pid, sleep=sleep, clock=clock)
    sampling: dict[str, Any] | None
    if isinstance(fresh, str):
        cpu, sampling = _cpuinfo_cpu(target, app_id, fresh, warnings)
    else:
        cpu, sampling = fresh

    payload: dict[str, Any] = {
        "ok": True,
        "platform": "android",
        "pid": pid,
        "metric_semantics": ANDROID_SEMANTICS,
        "memory": memory,
        "cpu": cpu,
        "proc": proc,
        "limitations": [],
    }
    if sampling is not None:
        payload["cpu_sampling"] = sampling
    if warnings:
        payload["warnings"] = warnings
    return payload, raw_meminfo


def _ios(target: Target, app_id: str) -> dict[str, Any]:
    resolved = process_mod.resolve(target, app_id)
    pid = resolved["pid"]
    warnings: list[dict[str, str]] = []

    try:
        ps = subprocess.run(["ps", "-p", str(pid), "-o", "%cpu=,rss="],
                            text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise errors.AutonomError(
            errors.TOOL_MISSING, f"host 'ps' unavailable: {exc}", tool="ps")
    fields = (ps.stdout or "").split()
    if ps.returncode != 0 or len(fields) < 2:
        raise errors.AutonomError(
            errors.APP_NOT_RUNNING,
            f"pid {pid} vanished between resolution and measurement",
            "Relaunch the app and snapshot again.",
        )
    cpu_percent, rss_kb = float(fields[0]), int(fields[1])

    disk: dict[str, Any] = {}
    container = ios_simctl.app_container(target.tool, target.target_id,
                                         app_id, "data")
    if container:
        try:
            du = subprocess.run(["du", "-sk", str(container)], text=True,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, check=False,
                                timeout=60)
            size = (du.stdout or "").split()
            if du.returncode == 0 and size and size[0].isdigit():
                disk = {"data_container_bytes": int(size[0]) * 1024,
                        "source": "simctl_get_app_container+du"}
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not disk:
        warnings.append({
            "code": "disk_unavailable",
            "error": "the data container size could not be measured",
            "hint": "Memory and CPU stand; container lookup needs the app installed.",
        })

    payload: dict[str, Any] = {
        "ok": True,
        "platform": "ios",
        "pid": pid,
        "metric_semantics": IOS_SEMANTICS,
        "memory": {"rss_bytes": rss_kb * 1024, "source": "host_ps"},
        "cpu": {"process_percent": cpu_percent, "source": "host_ps",
                "available": True},
        "disk": disk,
        "limitations": list(IOS_LIMITATIONS),
    }
    if warnings:
        payload["warnings"] = warnings
    return payload
