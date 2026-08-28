"""Heavy profiles: explicit duration, always an artifact (§2.6).

v1 success is defined as *artifact on disk + journal entry*, not parsed
stacks — `.trace` bundles open in Instruments, `perf.data` in simpleperf's
report tools. Tool absence is `tool_missing` with an install hint; a
profiler that ran and failed is `trace_failed` with its stderr tail.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from .. import adb as adb_mod
from .. import errors
from ..platform import ANDROID, Target
from . import artifacts as artifacts_mod
from . import frames as frames_mod
from . import presets as presets_mod
from . import process as process_mod

XCTRACE_TEMPLATES = {
    "allocations": "Allocations",
    "time-profiler": "Time Profiler",
    "leaks": "Leaks",
    "hitches": "Animation Hitches",
}
ANDROID_PRESETS = {"simpleperf", "gfxinfo-flow"}


def run_preset(target: Target, app_id: str, preset: str, *, duration: float,
               out_dir: Path, label: str,
               sleep=time.sleep) -> dict[str, Any]:
    android = target.platform == ANDROID
    if preset in ANDROID_PRESETS and not android:
        raise errors.AutonomError(
            errors.PRESET_UNAVAILABLE, f"{preset} is Android-only",
            "See 'autonom metrics list-presets' for this target.")
    if preset in XCTRACE_TEMPLATES and android:
        raise errors.AutonomError(
            errors.PRESET_UNAVAILABLE, f"{preset} is iOS-only (xctrace)",
            "See 'autonom metrics list-presets' for this target.")
    out_dir.mkdir(parents=True, exist_ok=True)
    if preset == "simpleperf":
        return _simpleperf(target, app_id, duration, out_dir, label)
    if preset == "gfxinfo-flow":
        return _gfxinfo_flow(target, app_id, duration, out_dir, label, sleep)
    if preset in XCTRACE_TEMPLATES:
        return _xctrace(target, app_id, preset, duration, out_dir, label)
    raise errors.AutonomError(
        errors.PRESET_UNAVAILABLE, f"unknown preset {preset!r}",
        "Run 'autonom metrics list-presets'.")


def _simpleperf(target: Target, app_id: str, duration: float, out_dir: Path,
                label: str) -> dict[str, Any]:
    which = adb_mod.run_adb(target.tool, ["shell", "which", "simpleperf"],
                            serial=target.target_id, check=False)
    if "simpleperf" not in (which.stdout or ""):
        raise errors.AutonomError(
            errors.TOOL_MISSING, "simpleperf is not on this device image",
            "Modern emulator images ship /system/bin/simpleperf; otherwise "
            "push the NDK's simpleperf for your ABI to /data/local/tmp.",
            tool="simpleperf")
    pid = process_mod.resolve(target, app_id)["pid"]
    remote = f"/data/local/tmp/autonom-{artifacts_mod.stamp()}-perf.data"
    try:
        record = adb_mod.run_adb(
            target.tool,
            ["shell", "simpleperf", "record", "-p", str(pid), "-o", remote,
             "--duration", str(int(max(duration, 1)))],
            serial=target.target_id, check=False, timeout=duration + 120)
        if record.returncode != 0:
            raise errors.AutonomError(
                errors.TRACE_FAILED,
                f"simpleperf record failed: {(record.stdout or '').strip()[:300]}",
                "Profiling may need a debuggable app or a userdebug image.")
        local = out_dir / (artifacts_mod.unique_stem(
            out_dir, f"{artifacts_mod.stamp()}-{label}", "-perf.data")
            + "-perf.data")
        pull = adb_mod.run_adb(target.tool, ["pull", remote, str(local)],
                               serial=target.target_id, check=False, timeout=300)
        if pull.returncode != 0 or not local.is_file():
            raise errors.AutonomError(
                errors.TRACE_FAILED, "could not pull perf.data from the device")
        local.chmod(0o600)
    finally:
        adb_mod.run_adb(target.tool, ["shell", "rm", "-f", remote],
                        serial=target.target_id, check=False)
    return {"ok": True, "preset": "simpleperf", "pid": pid,
            "duration_s": duration, "artifacts": [str(local)],
            "hint": "Inspect with the NDK's report.py / report_html.py."}


def _gfxinfo_flow(target: Target, app_id: str, duration: float, out_dir: Path,
                  label: str, sleep) -> dict[str, Any]:
    frames_mod.reset(target, app_id)
    sleep(max(duration, 0.0))  # the agent drives the UI meanwhile
    raw, summary = frames_mod.capture(target, app_id)
    stem = artifacts_mod.unique_stem(
        out_dir, f"{artifacts_mod.stamp()}-{label}", "-gfxinfo.txt")
    artifact = out_dir / f"{stem}-gfxinfo.txt"
    artifacts_mod.write_text(artifact, raw)
    return {"ok": True, "preset": "gfxinfo-flow", "duration_s": duration,
            "summary": summary, "artifacts": [str(artifact)],
            "note": "reset → window → framestats; drive the flow during the window"}


def _xctrace(target: Target, app_id: str, preset: str, duration: float,
             out_dir: Path, label: str) -> dict[str, Any]:
    if not presets_mod.xctrace_available(target.tool):
        raise errors.AutonomError(
            errors.TOOL_MISSING, "xctrace is not available on this host",
            "Install full Xcode (not just the CLT) and run "
            "'xcode-select --switch /Applications/Xcode.app'.", tool="xctrace")
    pid = process_mod.resolve(target, app_id)["pid"]
    stem = artifacts_mod.unique_stem(
        out_dir, f"{artifacts_mod.stamp()}-{label}", f"-{preset}.trace")
    output = out_dir / f"{stem}-{preset}.trace"
    argv = [target.tool, "xctrace", "record",
            "--template", XCTRACE_TEMPLATES[preset],
            "--device", target.target_id,
            "--attach", str(pid),
            "--time-limit", f"{int(max(duration, 1))}s",
            "--output", str(output)]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv above
            argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=duration + 180)
    except subprocess.TimeoutExpired:
        raise errors.AutonomError(
            errors.TRACE_FAILED,
            f"xctrace did not finish within {int(duration + 180)}s")
    except OSError as exc:
        raise errors.AutonomError(errors.TOOL_MISSING, str(exc), tool="xctrace")
    if completed.returncode != 0 or not output.exists():
        tail = (completed.stderr or completed.stdout or "").strip()[-400:]
        raise errors.AutonomError(
            errors.TRACE_FAILED, f"xctrace record failed: {tail}",
            "SIP or a privacy prompt can block recording; run once "
            "interactively and approve the prompt.")
    return {"ok": True, "preset": preset, "pid": pid, "duration_s": duration,
            "artifacts": [str(output)],
            "hint": "Open the .trace bundle in Instruments."}
