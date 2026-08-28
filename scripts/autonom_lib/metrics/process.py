"""Resolve the app's process id — the identity every metric hangs off.

Failure always reports `sources_tried`, so an agent can see exactly which
resolution paths came up empty instead of guessing (§3.7, D2).

Deliberately NO free-text fallback like ``pgrep -f <bundle-id>``: the CLI's
own command line contains the bundle id (``--app-id com.example.app``), so a
substring match can "resolve" the dead app to the autonom process itself and
silently measure the wrong program. A missing pid must stay a refusal.
"""
from __future__ import annotations

import re

from .. import errors, ios_simctl, logs as logs_mod
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
    pid = logs_mod.pid_for_package(target.tool, target.target_id, app_id)
    if pid and pid.isdigit():
        return {"pid": int(pid), "sources_tried": tried}
    raise _not_running(app_id, tried)


def _ios(target: Target, app_id: str) -> dict[str, object]:
    tried = ["simctl spawn launchctl list"]
    completed = ios_simctl.run_simctl(
        target.tool, ["spawn", target.target_id, "launchctl", "list"],
        check=False)
    pattern = re.compile(
        rf"^(\d+)\s+\S+\s+UIKitApplication:{re.escape(app_id)}\[", re.MULTILINE)
    hit = pattern.search(completed.stdout or "")
    if hit:
        return {"pid": int(hit.group(1)), "sources_tried": tried}
    raise _not_running(app_id, tried)
