"""One point-in-time load summary per platform (§2.2).

Honesty rules baked into the payload shape:

- `metric_semantics` names what was measured, and the two platforms are
  deliberately different constants — Android reads guest PSS accounting,
  the iOS Simulator is measured as a **host** process (`ps`), which is
  spelled out in `limitations` on every iOS snapshot.
- Partial data prefers `ok: true` + `warnings[]` (cpu missing but memory
  present); `ok: false` is reserved for "no useful signal at all".
"""
from __future__ import annotations

import subprocess
import time
from typing import Any

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


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def take(target: Target, app_id: str) -> tuple[dict[str, Any], str | None]:
    """-> (payload, raw_meminfo_text). The raw dump travels beside the payload,
    never inside it: the payload goes to stdout and the journal, and the
    package rule is that neither ever carries full dump text."""
    if target.platform == ANDROID:
        payload, raw = _android(target, app_id)
    else:
        payload, raw = _ios(target, app_id), None
    payload["captured_at"] = _now()
    payload["app_id"] = app_id
    return payload, raw


def _android(target: Target, app_id: str) -> tuple[dict[str, Any], str]:
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

    cpu: dict[str, Any] = {"available": False}
    cpuinfo = adb_mod.run_adb(target.tool, ["shell", "dumpsys", "cpuinfo"],
                              serial=target.target_id, check=False, timeout=60)
    percent = (meminfo_mod.parse_cpuinfo(cpuinfo.stdout or "", app_id)
               if cpuinfo.returncode == 0 else None)
    if percent is not None:
        cpu = {"available": True, "process_percent": percent,
               "note": "dumpsys cpuinfo averages over its sampling window; "
                       "use a series under a fixed flow for claims"}
    else:
        warnings.append({
            "code": "cpu_unavailable",
            "error": "dumpsys cpuinfo had no line for the app process",
            "hint": "CPU load is best-effort; memory metrics are unaffected.",
        })

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
