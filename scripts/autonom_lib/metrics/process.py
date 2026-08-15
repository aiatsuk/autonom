"""Resolve the app's process id — the identity every metric hangs off.

Failure always reports `sources_tried`, so an agent can see exactly which
resolution paths came up empty instead of guessing (§3.7, D2).
"""
from __future__ import annotations

import re
import subprocess

from .. import adb as adb_mod
from .. import errors, ios_simctl
from ..platform import ANDROID, Target


def resolve(target: Target, app_id: str) -> dict[str, object]:
    if target.platform == ANDROID:
        return _android(target, app_id)
    return _ios(target, app_id)


def _not_running(app_id: str, tried: list[str]) -> errors.AutonomError:
    return errors.AutonomError(
        errors.APP_NOT_RUNNING,
        f"no running process found for {app_id}",
        "Launch it first ('autonom session launch <app-id>'), then retry.",
        sources_tried=tried,
    )


def _android(target: Target, app_id: str) -> dict[str, object]:
    tried = ["adb shell pidof -s"]
    completed = adb_mod.run_adb(target.tool, ["shell", "pidof", "-s", app_id],
                                serial=target.target_id, check=False)
    pid = (completed.stdout or "").strip()
    if pid.isdigit():
        return {"pid": int(pid), "sources_tried": tried}
    raise _not_running(app_id, tried)


def _ios(target: Target, app_id: str) -> dict[str, object]:
    tried: list[str] = []
    tried.append("simctl spawn launchctl list")
    completed = ios_simctl.run_simctl(
        target.tool, ["spawn", target.target_id, "launchctl", "list"],
        check=False)
    pattern = re.compile(
        rf"^(\d+)\s+\S+\s+UIKitApplication:{re.escape(app_id)}\[", re.MULTILINE)
    hit = pattern.search(completed.stdout or "")
    if hit:
        return {"pid": int(hit.group(1)), "sources_tried": tried}
    tried.append("pgrep -f <bundle id>")
    try:
        pgrep = subprocess.run(["pgrep", "-n", "-f", app_id], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               check=False, timeout=15)
        pid = (pgrep.stdout or "").strip().splitlines()
        if pid and pid[0].isdigit():
            return {"pid": int(pid[0]), "sources_tried": tried}
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise _not_running(app_id, tried)
