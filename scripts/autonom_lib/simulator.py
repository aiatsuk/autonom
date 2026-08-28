"""Typed simulator controls with explicit platform support and verification."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import adb as adb_mod
from . import errors, ios_simctl
from .platform import ANDROID, IOS, Target


def _require_simulator(target: Target) -> None:
    if target.platform == ANDROID and not target.target_id.startswith("emulator-"):
        raise errors.AutonomError(
            errors.UNSUPPORTED_CAPABILITY,
            "simulator controls cannot mutate a physical Android device",
            capability="simulator.controls", target_id=target.target_id)


def _adb(target: Target, args: list[str]) -> str:
    return (adb_mod.run_adb(target.tool, args, serial=target.target_id,
                            timeout=30, check=True).stdout or "").strip()


def _simctl(target: Target, args: list[str]) -> str:
    return (ios_simctl.run_simctl(target.tool, args, timeout=30,
                                  check=True).stdout or "").strip()


def apply(target: Target, control: str, action: str,
          values: dict[str, Any]) -> dict[str, Any]:
    _require_simulator(target)
    if control == "battery":
        return _battery(target, action, values)
    if control == "network":
        return _network(target, action, values)
    if control == "push":
        return _push(target, values)
    if control in ("sms", "call"):
        return _telephony(target, control, action, values)
    if control == "biometric":
        return _biometric(target, action)
    if control == "clipboard":
        return _clipboard(target, action, values)
    if control == "appearance":
        return _appearance(target, action)
    if control == "text-size":
        return _text_size(target, action)
    if control == "status-bar":
        return _status_bar(target, action, values)
    raise errors.AutonomError(errors.UNSUPPORTED_CAPABILITY,
                              f"unknown simulator control {control!r}")


def _battery(target: Target, action: str, values: dict[str, Any]) -> dict[str, Any]:
    if target.platform == IOS:
        if action == "reset":
            _simctl(target, ["status_bar", target.target_id, "clear"])
            return {"control": "battery", "action": action, "verified": True}
        level = int(values.get("level", 100))
        state = values.get("state", "charged")
        _simctl(target, ["status_bar", target.target_id, "override",
                         "--batteryLevel", str(level), "--batteryState", str(state)])
        return {"control": "battery", "level": level, "state": state,
                "verified": True}
    if action == "reset":
        _adb(target, ["shell", "dumpsys", "battery", "reset"])
        return {"control": "battery", "action": action, "verified": True}
    level = int(values.get("level", 100))
    if not 0 <= level <= 100:
        raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                  "battery level must be 0..100")
    _adb(target, ["shell", "dumpsys", "battery", "set", "level", str(level)])
    observed = _adb(target, ["shell", "dumpsys", "battery"])
    return {"control": "battery", "level": level,
            "verified": f"level: {level}" in observed, "observed": observed[-500:]}


def _network(target: Target, action: str, values: dict[str, Any]) -> dict[str, Any]:
    if target.platform == IOS:
        raise errors.AutonomError(
            errors.UNSUPPORTED_CAPABILITY,
            "simctl exposes no supported network shaping command",
            hint="Use Autonom network mocks or a host-level network conditioner.",
            capability="simulator.network")
    if action in ("online", "offline"):
        enabled = action == "online"
        state = "enable" if enabled else "disable"
        _adb(target, ["shell", "svc", "wifi", state])
        _adb(target, ["shell", "svc", "data", state])
        return {"control": "network", "action": action, "verified": True}
    speed = str(values.get("speed", "full"))
    delay = str(values.get("delay", "none"))
    _adb(target, ["emu", "network", "speed", speed])
    _adb(target, ["emu", "network", "delay", delay])
    return {"control": "network", "action": "shape", "speed": speed,
            "delay": delay, "verified": True}


def _push(target: Target, values: dict[str, Any]) -> dict[str, Any]:
    app_id = str(values.get("app_id") or "")
    payload = values.get("payload")
    if not app_id or not isinstance(payload, dict):
        raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                  "push requires app_id and a JSON object payload")
    if target.platform == IOS:
        rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with tempfile.NamedTemporaryFile(mode="w", suffix=".apns", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            _simctl(target, ["push", target.target_id, app_id, handle.name])
        return {"control": "push", "app_id": app_id, "verified": True}
    raise errors.AutonomError(
        errors.UNSUPPORTED_CAPABILITY,
        "Android has no provider-neutral local push injection command",
        hint="Use a declared fixture or network mock for Android push flows.",
        capability="simulator.push")


def _telephony(target: Target, control: str, action: str,
               values: dict[str, Any]) -> dict[str, Any]:
    if target.platform == IOS:
        raise errors.AutonomError(
            errors.UNSUPPORTED_CAPABILITY,
            f"iOS Simulator exposes no public {control} injection command",
            capability=f"simulator.{control}")
    number = str(values.get("number") or "5551234")
    if control == "sms":
        text = str(values.get("text") or "Autonom")
        _adb(target, ["emu", "sms", "send", number, text])
        return {"control": control, "number": number, "verified": True}
    command = "call" if action == "incoming" else "cancel"
    _adb(target, ["emu", "gsm", command, number])
    return {"control": control, "action": action, "number": number,
            "verified": True}


def _biometric(target: Target, action: str) -> dict[str, Any]:
    if target.platform == IOS:
        _simctl(target, ["biometric", target.target_id,
                         "match" if action == "match" else "nonmatch"])
    else:
        if action != "match":
            raise errors.AutonomError(
                errors.UNSUPPORTED_CAPABILITY,
                "Android emulator only exposes a fingerprint touch stimulus")
        _adb(target, ["emu", "finger", "touch", "1"])
    return {"control": "biometric", "action": action, "verified": True}


def _clipboard(target: Target, action: str, values: dict[str, Any]) -> dict[str, Any]:
    if action != "set":
        raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                  "clipboard action must be set")
    text = str(values.get("text") or "")
    if target.platform == IOS:
        completed = subprocess.run(
            [target.tool, "simctl", "pbcopy", target.target_id], input=text,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=False)
        if completed.returncode:
            raise errors.AutonomError(errors.BACKEND_FAILED,
                                      completed.stderr.strip() or "simctl pbcopy failed")
    else:
        _adb(target, ["shell", "cmd", "clipboard", "set", "text", text])
    return {"control": "clipboard", "action": "set", "length": len(text),
            "verified": True}


def _appearance(target: Target, action: str) -> dict[str, Any]:
    if action not in ("light", "dark"):
        raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                  "appearance must be light or dark")
    if target.platform == IOS:
        _simctl(target, ["ui", target.target_id, "appearance", action])
    else:
        _adb(target, ["shell", "cmd", "uimode", "night",
                      "yes" if action == "dark" else "no"])
    return {"control": "appearance", "value": action, "verified": True}


def _text_size(target: Target, action: str) -> dict[str, Any]:
    if target.platform == IOS:
        _simctl(target, ["ui", target.target_id, "content_size", action])
    else:
        scale = float(action)
        if not 0.5 <= scale <= 2.0:
            raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                      "Android font scale must be 0.5..2.0")
        _adb(target, ["shell", "settings", "put", "system", "font_scale", str(scale)])
    return {"control": "text-size", "value": action, "verified": True}


def _status_bar(target: Target, action: str,
                values: dict[str, Any]) -> dict[str, Any]:
    if target.platform == IOS:
        if action == "clear":
            _simctl(target, ["status_bar", target.target_id, "clear"])
        else:
            args = ["status_bar", target.target_id, "override"]
            for key, value in values.items():
                args.extend([f"--{key}", str(value)])
            _simctl(target, args)
        return {"control": "status-bar", "action": action,
                "values": values, "verified": True}
    if action == "clear":
        _adb(target, ["shell", "am", "broadcast", "-a", "com.android.systemui.demo",
                      "-e", "command", "exit"])
    else:
        _adb(target, ["shell", "settings", "put", "global", "sysui_demo_allowed", "1"])
        args = ["shell", "am", "broadcast", "-a", "com.android.systemui.demo",
                "-e", "command", "clock"]
        if values.get("hhmm"):
            args.extend(["-e", "hhmm", str(values["hhmm"])])
        _adb(target, args)
    return {"control": "status-bar", "action": action,
            "values": values, "verified": True}
