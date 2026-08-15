"""Android memory evidence pack: the capture/analyze pair (§2.4).

The Python twin of the android-memory-leaks skill's
`capture_android_memory.sh` — same artifacts (metadata, meminfo, proc
status, gfxinfo, optional HPROF), no shell required, everything under the
session's `metrics/` dir with 0600 modes.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .. import adb as adb_mod
from .. import errors
from ..platform import Target
from . import meminfo as meminfo_mod
from . import process as process_mod
from . import series as series_mod


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def capture(target: Target, app_id: str, *, out_dir: Path, label: str,
            want_hprof: bool = True) -> dict[str, Any]:
    resolved = process_mod.resolve(target, app_id)
    pid = resolved["pid"]
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / f"{label}-{stamp}"
    files: list[str] = []
    warnings: list[dict[str, str]] = []

    def prop(name: str) -> str:
        completed = adb_mod.run_adb(target.tool, ["shell", "getprop", name],
                                    serial=target.target_id, check=False)
        return (completed.stdout or "").strip()

    files.append(_write(Path(f"{prefix}-metadata.txt"), (
        f"captured_at_utc={stamp}\n"
        f"serial={target.target_id}\n"
        f"package={app_id}\n"
        f"pid={pid}\n"
        f"android_release={prop('ro.build.version.release')}\n"
        f"android_sdk={prop('ro.build.version.sdk')}\n"
    )))

    meminfo = adb_mod.run_adb(target.tool,
                              ["shell", "dumpsys", "meminfo", app_id],
                              serial=target.target_id, check=False, timeout=60)
    files.append(_write(Path(f"{prefix}-meminfo.txt"), meminfo.stdout or ""))

    status = adb_mod.run_adb(target.tool,
                             ["shell", "cat", f"/proc/{pid}/status"],
                             serial=target.target_id, check=False)
    files.append(_write(Path(f"{prefix}-proc-status.txt"), status.stdout or ""))

    gfx = adb_mod.run_adb(target.tool, ["shell", "dumpsys", "gfxinfo", app_id],
                          serial=target.target_id, check=False, timeout=60)
    if gfx.returncode == 0:
        files.append(_write(Path(f"{prefix}-gfxinfo.txt"), gfx.stdout or ""))

    if want_hprof:
        remote = f"/data/local/tmp/autonom-{stamp}.hprof"
        try:
            dump = adb_mod.run_adb(
                target.tool, ["shell", "am", "dumpheap", "-g", app_id, remote],
                serial=target.target_id, check=False, timeout=300)
            failed = dump.returncode != 0 or "Error" in (dump.stdout or "")
            if not failed:
                local = Path(f"{prefix}.hprof")
                pull = adb_mod.run_adb(target.tool, ["pull", remote, str(local)],
                                       serial=target.target_id, check=False,
                                       timeout=300)
                failed = pull.returncode != 0 or not local.is_file()
                if not failed:
                    local.chmod(0o600)
                    files.append(str(local))
            if failed:
                raise errors.AutonomError(
                    errors.BACKEND_FAILED,
                    f"heap dump failed for {app_id}",
                    "HPROF needs a debuggable build; retry with --no-hprof "
                    "for the meminfo-only pack.",
                )
        finally:
            adb_mod.run_adb(target.tool, ["shell", "rm", "-f", remote],
                            serial=target.target_id, check=False)

    payload: dict[str, Any] = {"ok": True, "pid": pid, "label": label,
                               "prefix": str(prefix), "files": files}
    if warnings:
        payload["warnings"] = warnings
    return payload


def analyze(directory: Path, *, glob: str,
            min_growth_kb: int) -> dict[str, Any]:
    files = sorted(directory.glob(glob),
                   key=lambda p: (p.stat().st_mtime_ns, p.name))
    if not files:
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"no meminfo captures match {glob!r} under {directory}",
            "Capture some with 'autonom metrics memory capture' first.",
        )
    samples = []
    for file in files:
        metrics = meminfo_mod.parse_meminfo(
            file.read_text(encoding="utf-8", errors="replace"))
        if not metrics:
            raise errors.AutonomError(
                errors.BACKEND_FAILED,
                f"no supported meminfo metrics found in {file}",
                "The file must be a `dumpsys meminfo <package>` capture.",
            )
        samples.append({"path": str(file), "metrics": metrics})
    report = series_mod.summarize(samples, max(min_growth_kb, 0))
    report["samples"] = samples
    return report
