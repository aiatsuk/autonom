"""Session-scoped recovery of sparse Flutter semantics on Android emulators."""
from __future__ import annotations

import re
from typing import Any

from . import adb, errors, session
from .platform import ANDROID, Target

SERVICE = ("com.android.systemui.accessibility.accessibilitymenu/"
           "com.android.systemui.accessibility.accessibilitymenu.AccessibilityMenuService")
KEYS = ("enabled_accessibility_services", "accessibility_enabled")


def sparse(nodes: list[dict[str, Any]]) -> bool:
    return (len(nodes) <= 8 and
            not any(node.get("text") or node.get("desc") or node.get("clickable")
                    for node in nodes))


def flutter_shell(nodes: list[dict[str, Any]]) -> bool:
    """An unlabelled View inside Android containers is the observed shape."""
    return sparse(nodes) and any(node.get("class") == "android.view.View"
                                 for node in nodes)


def _shell(target: Target, *words: str) -> str:
    result = adb.run_adb(target.tool, ["shell", *words], serial=target.target_id)
    return str(result.stdout).strip()


def _settings(target: Target) -> dict[str, str]:
    return {key: _shell(target, "settings", "get", "secure", key) for key in KEYS}


def _put(target: Target, key: str, value: str) -> None:
    if value == "null":
        _shell(target, "settings", "delete", "secure", key)
    else:
        _shell(target, "settings", "put", "secure", key, value)


def _emulator(target: Target) -> bool:
    return (target.platform == ANDROID and target.target_id.startswith("emulator-")
            and _shell(target, "getprop", "ro.kernel.qemu") == "1")


def _service_available(target: Target) -> bool:
    result = _shell(target, "pm", "query-services", "-a",
                    "android.accessibilityservice.AccessibilityService")
    package, name = SERVICE.split("/", 1)
    return f"packageName={package}" in result and f"name={name}" in result


def foreground_app(target: Target) -> str | None:
    activity = _shell(target, "dumpsys", "activity", "activities")
    match = re.search(r"(?:topResumedActivity|mResumedActivity|ResumedActivity)"
                      r"[^\n]*?\s([A-Za-z][\w.]*)/", activity)
    return match.group(1) if match else None


def eligible(target: Target, record: dict[str, Any] | None,
             nodes: list[dict[str, Any]]) -> bool:
    """Require a whole sparse tree for the foreground app in this session."""
    if not record or not sparse(nodes):
        return False
    app_id = record.get("app_id")
    if not app_id or record.get("platform") != ANDROID or \
            record.get("target_id") != target.target_id:
        return False
    return all(node.get("package") == app_id for node in nodes)


def status(target: Target, record: dict[str, Any] | None) -> dict[str, Any]:
    state = (record or {}).get("accessibility") or {}
    return {"managed": bool(state), "service": SERVICE,
            "enabled_by_autonom": bool(state.get("enabled_by_autonom")),
            "current": _settings(target) if target.platform == ANDROID else None}


def enable(target: Target, record: dict[str, Any]) -> dict[str, Any]:
    """Persist the original settings before any write; verify every change."""
    if not _emulator(target):
        raise errors.AutonomError(errors.EMULATOR_ONLY,
                                  "accessibility recovery requires an Android emulator")
    if record.get("platform") != ANDROID or record.get("target_id") != target.target_id:
        raise errors.AutonomError(errors.NO_ACTIVE_SESSION,
                                  "an Android session on this emulator is required")
    if record.get("accessibility"):
        return status(target, record)
    if not _service_available(target):
        raise errors.AutonomError(errors.TOOL_MISSING,
                                  "system Accessibility Menu is not installed")
    before = _settings(target)
    services = [item for item in before[KEYS[0]].split(":") if item != "null" and item]
    already_on = SERVICE in services and before[KEYS[1]] == "1"
    after = dict(before)
    if not already_on:
        if SERVICE not in services:
            services.append(SERVICE)
        after = {KEYS[0]: ":".join(services), KEYS[1]: "1"}
    record["accessibility"] = {
        "service": SERVICE, "previous": before, "applied": after,
        "enabled_by_autonom": not already_on,
    }
    session.save(record)
    try:
        if not already_on:
            _put(target, KEYS[0], after[KEYS[0]])
            _put(target, KEYS[1], after[KEYS[1]])
        if _settings(target) != after:
            raise errors.AutonomError(errors.BACKEND_FAILED,
                                      "accessibility settings did not match read-back")
    except Exception:
        if not already_on:
            try:
                for key in KEYS:
                    _put(target, key, before[key])
                if _settings(target) == before:
                    record.pop("accessibility", None)
                    session.save(record)
            except Exception:
                pass  # retain the saved snapshot for explicit reset
        raise
    return status(target, record)


def restore(target: Target, record: dict[str, Any]) -> dict[str, Any]:
    state = record.get("accessibility")
    if not state:
        return {"restored": False, "reason": "not_managed"}
    if not _emulator(target) or record.get("target_id") != target.target_id:
        raise errors.AutonomError(errors.NO_TARGET,
                                  "original emulator is unavailable; settings remain saved")
    before, applied = state["previous"], state["applied"]
    current = _settings(target)
    if any(current[key] not in (before[key], applied[key]) for key in KEYS):
        raise errors.AutonomError(errors.BACKEND_FAILED,
                                  "accessibility settings changed outside Autonom; reset refused",
                                  "Inspect secure settings on the emulator before restoring them.")
    if state["enabled_by_autonom"]:
        for key in KEYS:
            _put(target, key, before[key])
        if _settings(target) != before:
            raise errors.AutonomError(errors.BACKEND_FAILED,
                                      "accessibility settings did not restore")
    record.pop("accessibility", None)
    session.save(record)
    return {"restored": True, "previous": before}
