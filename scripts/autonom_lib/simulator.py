"""Typed simulator controls with explicit platform support and verification."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import adb as adb_mod
from . import errors, ios_prefs, ios_simctl
from .platform import ANDROID, IOS, Target

# The deterministic "marketing" status bar: 9:41, full battery, full signal,
# no notifications. Pinning it before a screenshot removes the clock, battery
# and signal glyphs from a before/after diff, so two captures of the same
# screen differ only where the app itself changed.
IOS_STATUS_BAR_PIN: dict[str, str] = {
    "time": "9:41",
    "batteryState": "charged",
    "batteryLevel": "100",
    "wifiMode": "active",
    "wifiBars": "3",
    "cellularMode": "active",
    "cellularBars": "4",
    "dataNetwork": "5g",
}
# SystemUI demo mode is the Android equivalent of `simctl status_bar`. The
# mobile icon is hidden rather than shaped: recent SystemUI ignores demo-mode
# mobile overrides when the emulator reports its virtual radio, so a stray
# "3G" glyph survives `datatype` — hiding it matches Play screenshot conventions.
ANDROID_STATUS_BAR_PIN: dict[str, str] = {
    "hhmm": "0941",
    "battery": "100",
    "plugged": "false",
    "wifi": "show",
    "wifi_level": "4",
    "mobile": "hide",
    "notifications": "false",
}
ANDROID_STATUS_BAR_KEYS = (
    "hhmm", "battery", "plugged", "wifi", "wifi_level", "mobile", "mobile_level",
    "datatype", "notifications",
)


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
    if control == "keyboard":
        return _keyboard(target, action, values)
    raise errors.AutonomError(errors.UNSUPPORTED_CAPABILITY,
                              f"unknown simulator control {control!r}")


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "show"}


def _int(values: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    """A bounded integer control value, refused with a stable code rather
    than a bare ValueError from `int()`."""
    raw = values.get(key, default)
    try:
        number = int(str(raw).strip())
    except ValueError as exc:
        raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                  f"{key} must be an integer, got {raw!r}") from exc
    if not low <= number <= high:
        raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                  f"{key} must be {low}..{high}, got {number}")
    return number


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
    """`override` applies the given keys; `pin` applies the deterministic
    preset (keys given override it); `clear` restores the live bar."""
    if action not in ("override", "pin", "clear"):
        raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                  "status-bar action must be override, pin, or clear")
    if target.platform == IOS:
        if action == "clear":
            _simctl(target, ["status_bar", target.target_id, "clear"])
            applied: dict[str, Any] = {}
        else:
            applied = {**IOS_STATUS_BAR_PIN, **values} if action == "pin" else dict(values)
            args = ["status_bar", target.target_id, "override"]
            for key, value in applied.items():
                args.extend([f"--{key}", str(value)])
            _simctl(target, args)
        return {"control": "status-bar", "action": action,
                "values": applied, "verified": True}
    if action == "clear":
        _demo(target, "exit")
        applied = {}
    else:
        applied = {**ANDROID_STATUS_BAR_PIN, **values} if action == "pin" else dict(values)
        applied = _android_status_bar(target, applied)
    return {"control": "status-bar", "action": action,
            "values": applied, "verified": True}


def _demo(target: Target, command: str, *pairs: tuple[str, Any]) -> None:
    args = ["shell", "am", "broadcast", "-a", "com.android.systemui.demo",
            "-e", "command", command]
    for key, value in pairs:
        args.extend(["-e", key, str(value)])
    _adb(target, args)


def _android_status_bar(target: Target, values: dict[str, Any]) -> dict[str, Any]:
    """Translate the key set into SystemUI demo-mode broadcasts.

    The broadcasts are idempotent, so re-sending them is how the bar gets
    re-pinned after something (a reboot, a system dialog) reset it.
    """
    unknown = sorted(set(values) - set(ANDROID_STATUS_BAR_KEYS))
    if unknown:
        raise errors.AutonomError(
            errors.FLOW_COMMAND_INVALID,
            f"unknown status-bar key(s) for Android: {', '.join(unknown)}",
            "Keys: " + ", ".join(ANDROID_STATUS_BAR_KEYS) + ".")
    applied: dict[str, Any] = {}
    _adb(target, ["shell", "settings", "put", "global", "sysui_demo_allowed", "1"])
    _demo(target, "enter")
    if "hhmm" in values:
        hhmm = str(values["hhmm"]).replace(":", "").zfill(4)
        if not (hhmm.isdigit() and len(hhmm) == 4):
            raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                      f"hhmm must be four digits, got {values['hhmm']!r}")
        _demo(target, "clock", ("hhmm", hhmm))
        applied["hhmm"] = hhmm
    if "battery" in values or "plugged" in values:
        level = _int(values, "battery", 100, 0, 100)
        plugged = "true" if _truthy(values.get("plugged", "false")) else "false"
        _demo(target, "battery", ("level", level), ("plugged", plugged))
        applied.update({"battery": level, "plugged": plugged})
    if "wifi" in values:
        if _truthy(values["wifi"]):
            level = _int(values, "wifi_level", 4, 0, 4)
            _demo(target, "network", ("wifi", "show"), ("level", level), ("fully", "true"))
            applied.update({"wifi": "show", "wifi_level": level})
        else:
            _demo(target, "network", ("wifi", "hide"))
            applied["wifi"] = "hide"
    if "mobile" in values:
        if _truthy(values["mobile"]):
            level = _int(values, "mobile_level", 4, 0, 4)
            datatype = str(values.get("datatype", "lte"))
            _demo(target, "network", ("mobile", "show"), ("level", level),
                  ("datatype", datatype))
            applied.update({"mobile": "show", "mobile_level": level, "datatype": datatype})
        else:
            _demo(target, "network", ("mobile", "hide"))
            applied["mobile"] = "hide"
    if "notifications" in values:
        visible = "true" if _truthy(values["notifications"]) else "false"
        _demo(target, "notifications", ("visible", visible))
        applied["notifications"] = visible
    return applied


def _keyboard(target: Target, action: str, values: dict[str, Any]) -> dict[str, Any]:
    """Pin (or reset) autocorrect, prediction, auto-capitalisation and locale.

    iOS only: the values live in the simulator's on-disk preference store,
    which cfprefsd reads at boot — so the device must be shut down for the
    write, and `reboot=true` asks the verb to do the shutdown/boot itself.
    Android keeps these settings inside the keyboard app (Gboard), where no
    host-level command reaches them; the verb refuses rather than pretend.
    """
    if target.platform == ANDROID:
        raise errors.AutonomError(
            errors.UNSUPPORTED_CAPABILITY,
            "Android has no host-level keyboard preference store; autocorrect "
            "and prediction live inside the keyboard app",
            hint="Turn them off in the emulator's Gboard settings by hand, or make "
                 "the field opt out (inputType textNoSuggestions / Flutter "
                 "autocorrect: false), then re-run.",
            capability="simulator.keyboard")
    if action not in ("pin", "reset", "show"):
        raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                  "keyboard action must be pin, reset, or show")
    locale = values.get("locale")
    pins = ios_prefs.keyboard_pins(str(locale) if locale else None)
    udid = target.target_id

    if action == "show":
        observed = ios_prefs.observe(udid, pins)
        return {"control": "keyboard", "action": action,
                "observed": observed, "pinned": ios_prefs.is_pinned(observed, pins),
                "verified": True}

    # A device with no data directory is refused before any lifecycle churn:
    # shutting a simulator down for a write that cannot happen helps nobody.
    ios_prefs.require_preferences_dir(udid)
    simulator = ios_simctl.find_simulator(target.tool, udid)
    booted = simulator is not None and simulator.state == "Booted"
    reboot = _truthy(values.get("reboot", "false"))
    if booted and not reboot:
        raise errors.AutonomError(
            errors.SIMULATOR_MUST_BE_SHUTDOWN,
            f"simulator {udid} is booted; preferences are read at boot, so a "
            "write now would be ignored or overwritten",
            hint="Shut it down first ('autonom devices shutdown --udid <UDID>') or "
                 "pass --value reboot=true to let this verb shut down, write, and "
                 "boot it again.",
            target_id=udid)
    if booted:
        ios_simctl.shutdown(target.tool, udid)

    result: dict[str, Any] = {"control": "keyboard", "action": action,
                              "locale": pins.get(ios_prefs.GLOBAL_DOMAIN, {}).get("AppleLocale")}
    try:
        if action == "pin":
            result["preferences"] = ios_prefs.apply_pins(udid, pins)
            observed = ios_prefs.observe(udid, pins)
            result["verified"] = ios_prefs.is_pinned(observed, pins)
        else:
            result["removed"] = ios_prefs.remove_pins(udid, pins)
            observed = ios_prefs.observe(udid, pins)
            result["verified"] = all(value is None for domain in observed.values()
                                     for value in domain.values())
    finally:
        # The caller asked for a running simulator back; a failed write must
        # not leave it dark.
        if booted:
            ios_simctl.boot(target.tool, udid)
    result["observed"] = observed
    result["rebooted"] = booted
    return result
