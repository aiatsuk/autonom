"""Typed simulator controls with explicit platform support and verification.

`verified` convention (every control, both platforms):

- `True`  — the value was read back from the device after the change and it
  equals what was asked for, exactly (a parsed value, never a substring).
- `False` — anything else: the read-back disagreed, the device answered that
  the command is unsupported, or no read-back exists for the control (a
  notification post, an emulator-console stimulus, a simctl override that
  cannot be listed). `verification` then says which: `read_back`,
  `mismatch`, `unsupported`, or `unavailable`.

A `False` is therefore not a failure by itself; it means "do not trust the
exit code, compare a screenshot or tree before and after".

Every control validates its action and every value before the first device
command, so a refused call never leaves a half-applied state behind.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from . import adb as adb_mod
from . import errors, ios_prefs, ios_simctl
from .platform import ANDROID, IOS, Target

# The deterministic status bar: full battery, full signal, no notifications.
# Pinning it before a screenshot removes the battery and signal glyphs from a
# before/after diff, so two captures of the same screen differ only where the
# app itself changed. The clock is deliberately NOT part of the preset — a
# tester wants the real time in evidence — and is pinned only on request:
# `time=9:41` on iOS, `hhmm=0941` on Android (the marketing convention).
IOS_STATUS_BAR_PIN: dict[str, str] = {
    "batteryState": "charged",
    "batteryLevel": "100",
    "wifiMode": "active",
    "wifiBars": "3",
    "cellularMode": "active",
    "cellularBars": "4",
    "dataNetwork": "5g",
}
# The `simctl status_bar override` flags and the values each accepts; `None`
# means free text (the clock and the carrier name).
IOS_STATUS_BAR_VALUES: dict[str, tuple[str, ...] | None] = {
    "time": None,
    "operatorName": None,
    "dataNetwork": ("hide", "wifi", "3g", "4g", "lte", "lte-a", "lte+", "5g", "5g+",
                    "5g-uwb", "5g-uc"),
    "wifiMode": ("searching", "failed", "active"),
    "wifiBars": ("0", "1", "2", "3"),
    "cellularMode": ("notSupported", "searching", "failed", "active"),
    "cellularBars": ("0", "1", "2", "3", "4"),
    "batteryState": ("charging", "charged", "discharging"),
    "batteryLevel": tuple(str(level) for level in range(101)),
}
# Android status-bar keys given to an iOS target, with the iOS spelling. They
# are refused rather than forwarded: simctl would print its usage, or worse,
# accept `--mode live` as the start of another flag.
IOS_EQUIVALENT_OF_ANDROID_KEY: dict[str, str] = {
    "mode": "iOS overrides never freeze the clock; drop mode= (pin a clock with time=9:41)",
    "hhmm": "time=9:41",
    "wifi": "wifiMode=active (or searching / failed)",
    "wifi_level": "wifiBars=0..3",
    "mobile": "cellularMode=active (or notSupported / searching / failed)",
    "mobile_level": "cellularBars=0..4",
    "notifications": "no equivalent; simctl cannot hide notification icons, drop the key",
    "battery": "batteryLevel=0..100",
    "plugged": "batteryState=charging (or charged / discharging)",
    "datatype": "dataNetwork=5g (or lte / 4g / 3g / wifi / hide)",
}
# Android has two ways to pin the bar, and they differ on the clock.
#
# SystemUI *demo mode* is the Android equivalent of `simctl status_bar`, but
# entering it freezes the clock at that moment (verified on a real emulator:
# a capture taken 73 s after `enter` still showed the entry minute). It is
# therefore used only when the caller asks for a fixed clock (`hhmm=`) or for
# a glyph only demo mode can shape (`wifi`, `mobile`, `datatype`), and by
# `override`. The mobile icon is hidden there rather than shaped: recent
# SystemUI ignores demo-mode mobile overrides when the emulator reports its
# virtual radio, so a stray "3G" glyph survives `datatype`.
ANDROID_STATUS_BAR_PIN: dict[str, str] = {
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
# The data-type glyphs SystemUI demo mode knows (`DemoStatusIcons` / the
# mobile signal controller's demo handler).
ANDROID_DEMO_DATATYPES = ("1x", "3g", "4g", "4g+", "5g", "5ge", "5g+", "e", "g", "h",
                          "h+", "lte", "lte+", "dis", "not")
# The default `pin` is the *live* mode: the real, ticking clock, with the
# battery pinned through the battery service, the cellular bars through the
# emulator console, and notification icons hidden through StatusBarManager.
# Wi-Fi bars cannot be shaped here; the emulator's virtual Wi-Fi is full.
ANDROID_LIVE_KEYS = ("battery", "plugged", "mobile_level", "notifications")
ANDROID_DEMO_ONLY_KEYS = ("hhmm", "wifi", "wifi_level", "mobile", "datatype")
ANDROID_DEFAULT_SIGNAL_PROFILE = "4"  # what a fresh emulator boots with
# The cellular bars are pinned twice over the emulator console. `gsm
# signal-profile <0..4>` alone did not hold on a real API 36 emulator: four
# bars fell to two within 65 s, because the guest modem keeps re-reporting
# its own strength on its periodic update. `gsm signal <rssi> <ber>` sets an
# explicit RSSI, the value the modem reports on every later update, so it is
# sent after the profile with an RSSI inside the band Android maps to the
# requested level (GSM ASU: <=2 none, 3-4 poor, 5-7 moderate, 8-11 good,
# >=12 great). Neither command can be read back from the host, and holding
# for minutes is not proven on every emulator build, hence the
# `signal_unstable` warning on each live pin until it is.
ANDROID_SIGNAL_RSSI = {0: 1, 1: 4, 2: 7, 3: 10, 4: 31}
# Even both commands do not hold: on a real API 36 emulator the bars fell to
# none and SystemUI stopped drawing the battery within 80 s of a live pin
# (and with no pin at all), while `dumpsys battery` still read the pinned
# level. No one-shot host command holds the bar, so a live pin records what
# it pinned and `reassert_pins` re-sends it right before every capture, then
# waits this long for SystemUI to redraw.
REPIN_SETTLE_SECONDS = 0.4
# Espresso's setup guide: turn these three off on a test device, or an
# animation in flight races the next assertion.
ANDROID_ANIMATION_SCALES = (
    "window_animation_scale", "transition_animation_scale", "animator_duration_scale",
)
ANDROID_DEFAULT_ANIMATION_SCALE = "1"  # the unset (null) value reads as 1.0
# `simctl ui <udid> content_size` categories, smallest to largest.
IOS_CONTENT_SIZES = (
    "extra-small", "small", "medium", "large", "extra-large", "extra-extra-large",
    "extra-extra-extra-large", "accessibility-medium", "accessibility-large",
    "accessibility-extra-large", "accessibility-extra-extra-large",
    "accessibility-extra-extra-extra-large",
)
IOS_BATTERY_STATES = ("charging", "charged", "discharging")

# The actions every control accepts. `text-size` takes its value as the
# action (a content-size category on iOS, a font scale on Android) and is
# checked by `_text_size_value`.
CONTROL_ACTIONS: dict[str, tuple[str, ...]] = {
    "battery": ("set", "reset"),
    "network": ("online", "offline", "shape"),
    "push": ("send",),
    "sms": ("send",),
    "call": ("incoming", "cancel"),
    "biometric": ("enroll", "unenroll", "match", "nonmatch"),
    "clipboard": ("set",),
    "appearance": ("light", "dark"),
    "status-bar": ("override", "pin", "clear"),
    "keyboard": ("pin", "reset", "show"),
    "animations": ("pin", "reset", "show"),
}
CONTROLS = (*CONTROL_ACTIONS, "text-size")

_BOOLEAN_WORDS = {"1": True, "true": True, "yes": True, "on": True, "show": True,
                  "0": False, "false": False, "no": False, "off": False, "hide": False}


def _require_simulator(target: Target) -> None:
    if target.platform == ANDROID and not target.target_id.startswith("emulator-"):
        raise errors.AutonomError(
            errors.UNSUPPORTED_CAPABILITY,
            "simulator controls cannot mutate a physical Android device",
            capability="simulator.controls", target_id=target.target_id)


def _require_action(control: str, action: str) -> None:
    valid = CONTROL_ACTIONS.get(control)
    if valid is not None and action not in valid:
        raise errors.AutonomError(
            errors.INVALID_SIMULATOR_ACTION,
            f"unknown {control} action {action!r}",
            f"Valid {control} actions: " + ", ".join(valid) + ".",
            control=control, valid_actions=list(valid))


def _adb(target: Target, args: list[str]) -> str:
    return (adb_mod.run_adb(target.tool, args, serial=target.target_id,
                            timeout=30, check=True).stdout or "").strip()


def _simctl(target: Target, args: list[str]) -> str:
    return (ios_simctl.run_simctl(target.tool, args, timeout=30,
                                  check=True).stdout or "").strip()


def apply(target: Target, control: str, action: str,
          values: dict[str, Any]) -> dict[str, Any]:
    if control not in CONTROLS:
        raise errors.AutonomError(errors.UNSUPPORTED_CAPABILITY,
                                  f"unknown simulator control {control!r}")
    _require_simulator(target)
    _require_action(control, action)
    if control in ("status-bar", "animations") and target.platform == ANDROID:
        # Before anything reads or writes the record: one taken on another
        # device must neither be merged into a new pin nor restored here.
        stale = drop_stale_record(target)
        result = _status_bar(target, action, values) if control == "status-bar" \
            else _animations(target, action)
        if stale:
            result["stale_pin_dropped"] = True
            result.setdefault("warnings", []).append(_stale_warning())
        recorded = read_snapshot(target)
        if recorded and not (recorded.get("identity") or {}).get("boot"):
            result.setdefault("warnings", []).append({
                "code": "pin_not_device_bound",
                "error": "the device's boot identity could not be read, so this pin "
                         "is not re-asserted before captures and clear/reset cannot "
                         "restore what it replaced",
                "hint": "Check 'adb -s <serial> shell cat " + _BOOT_ID_PATH + "'.",
            })
        return result
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
    if control == "animations":
        return _animations(target, action)
    return _keyboard(target, action, values)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "show"}


def _bool(values: dict[str, Any], key: str, default: bool) -> bool:
    """A boolean control value; an unrecognised word is refused, not read as
    false (`plugged=maybe` used to unplug silently)."""
    if key not in values:
        return default
    word = str(values[key]).strip().lower()
    if word not in _BOOLEAN_WORDS:
        raise errors.AutonomError(
            errors.FLOW_COMMAND_INVALID,
            f"{key} must be true or false, got {values[key]!r}",
            "Booleans: " + ", ".join(sorted(_BOOLEAN_WORDS)) + ".")
    return _BOOLEAN_WORDS[word]


def _int(values: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    """A bounded integer control value, refused with a stable code rather
    than a bare ValueError from `int()`."""
    raw = values.get(key, default)
    try:
        number = int(str(raw).strip())
    except ValueError:
        raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                  f"{key} must be an integer, got {raw!r}") from None
    if not low <= number <= high:
        raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                  f"{key} must be {low}..{high}, got {number}")
    return number


def _choice(values: dict[str, Any], key: str, default: str,
            allowed: tuple[str, ...]) -> str:
    value = str(values.get(key, default)).strip()
    if value not in allowed:
        raise errors.AutonomError(
            errors.FLOW_COMMAND_INVALID,
            f"{key} must be one of {', '.join(allowed)}, got {value!r}")
    return value


def _unverified(result: dict[str, Any], reason: str = "unavailable") -> dict[str, Any]:
    result["verified"] = False
    result["verification"] = reason
    return result


def _checked(result: dict[str, Any], ok: bool) -> dict[str, Any]:
    result["verified"] = ok
    result["verification"] = "read_back" if ok else "mismatch"
    return result


# --- per-target snapshot store ------------------------------------------------
#
# A pin changes device state a human may have set on purpose (a battery
# override for a low-battery test, a custom animation scale). The first pin
# records what it is about to replace under
# $AUTONOM_HOME/simulator-state/<target>.json and the matching reset/clear
# restores exactly that; a second pin merges only what is not recorded yet,
# so the snapshot always describes the device before the FIRST pin.


def snapshot_path(target: Target) -> Path:
    return _serial_snapshot_path(target.target_id)


def _serial_snapshot_path(target_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", target_id)
    return ios_prefs.state_root() / "simulator-state" / f"{safe}.json"


# --- device identity ------------------------------------------------------------
#
# An adb serial names a port, not a device: `emulator-5554` is whichever
# emulator took console port 5554 last. A record kept under that name and
# replayed before every capture would, once the pinned emulator is killed
# without a clear, land on the next emulator that takes the port — possibly
# someone's own AVD. So every Android record carries the identity of the
# device it was taken on (the AVD name and the kernel's per-boot random id)
# and is used only while the device answering on the serial has the same
# identity; anything else — another AVD, a reboot, an identity that cannot be
# read — drops the record without sending the device a single command.
# iOS records need no guard: they are keyed by the simulator UDID, which is
# never reused.

_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
# Read once per process per serial: a capture re-checks on every call, and
# a device cannot change identity under a serial without going away first
# (which `devices shutdown`/`devices boot` forget explicitly).
_IDENTITY_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


def _read_prop(target: Target, args: list[str]) -> str | None:
    try:
        completed = adb_mod.run_adb(target.tool, args, serial=target.target_id,
                                    timeout=10, check=False)
    except errors.AutonomError:
        return None
    output = completed.stdout if isinstance(completed.stdout, str) else ""
    if completed.returncode != 0:
        return None
    line = output.strip().splitlines()[0].strip() if output.strip() else ""
    return line or None


def device_identity(target: Target) -> dict[str, Any]:
    """The identity of the Android device on `target`'s serial now:
    `{"avd": <AVD name or None>, "boot": <boot identity or None>}`. The boot
    identity is the kernel boot_id; where that is unreadable, the device
    serial number plus the boot timestamp (`ro.runtime.firstboot`). An
    identity with neither value is unverifiable and matches nothing."""
    key = (str(target.tool), target.target_id)
    cached = _IDENTITY_CACHE.get(key)
    if cached is not None:
        return dict(cached)
    avd = _read_prop(target, ["emu", "avd", "name"])
    if avd is not None and (avd in ("OK", "KO") or avd.startswith("KO")):
        avd = None
    boot = _read_prop(target, ["shell", "cat", _BOOT_ID_PATH])
    if boot is not None and not re.fullmatch(r"[0-9A-Fa-f-]{8,64}", boot):
        boot = None
    if boot is not None:
        boot = f"boot_id:{boot.lower()}"
    else:
        serialno = _read_prop(target, ["shell", "getprop", "ro.boot.serialno"])
        firstboot = _read_prop(target, ["shell", "getprop", "ro.runtime.firstboot"])
        if firstboot:
            boot = f"firstboot:{serialno or ''}:{firstboot}"
    identity = {"avd": avd, "boot": boot}
    _IDENTITY_CACHE[key] = identity
    return dict(identity)


def _identity_matches(recorded: Any, current: dict[str, Any]) -> bool:
    if not isinstance(recorded, dict) or not current.get("boot"):
        return False
    return recorded.get("avd") == current.get("avd") and recorded.get("boot") == current.get("boot")


def forget_device(target_id: str) -> bool:
    """Drop every record kept for `target_id` and its cached identity. Called
    when the device on that serial goes away (`devices shutdown`) or a new
    one takes it (`devices boot`). Returns True when a record existed."""
    for key in [key for key in _IDENTITY_CACHE if key[1] == target_id]:
        del _IDENTITY_CACHE[key]
    path = _serial_snapshot_path(target_id)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


def drop_stale_record(target: Target) -> bool:
    """Delete the Android record for `target` unless it was taken on the
    device answering on the serial now; True when one was dropped. Nothing
    is sent to the device beyond the identity read."""
    if target.platform != ANDROID:
        return False
    snapshot = read_snapshot(target)
    if not snapshot:
        return False
    if _identity_matches(snapshot.get("identity"), device_identity(target)):
        return False
    try:
        snapshot_path(target).unlink()
    except OSError:
        pass
    return True


def restore_before_shutdown(target: Target) -> dict[str, Any]:
    """Undo the Android pins recorded for `target` before its emulator is
    killed: `{"pins_restored": [...], "warnings": [...], "record_found": bool}`.

    Emulators quickboot: `emu kill` saves a snapshot, so a battery override,
    hidden notification icons, or zeroed animation scales left on the device
    come back on its next boot, where no record remains to restore them. So
    a record taken on the device answering on the serial now is restored
    exactly as `status-bar clear` and `animations reset` would, status bar
    first. A record from another device (or boot) sends nothing and warns
    `stale_pin_dropped`; a failed restore warns `pin_restore_failed`. The
    record itself is left for the caller to drop; with none, nothing is sent
    to the device at all."""
    result: dict[str, Any] = {"pins_restored": [], "warnings": [], "record_found": False}
    if target.platform != ANDROID:
        return result
    snapshot = read_snapshot(target)
    # A full restore removes the record itself; the caller still reports it.
    result["record_found"] = bool(snapshot)
    controls = [control for control, section in (("status-bar", "status_bar"),
                                                 ("animations", "animations"))
                if section in snapshot]
    if not controls:
        return result
    if not _identity_matches(snapshot.get("identity"), device_identity(target)):
        result["warnings"].append(_stale_warning())
        return result
    for control in controls:
        try:
            if control == "status-bar":
                _android_status_bar_clear(target)
            else:
                _animations(target, "reset")
        except errors.AutonomError as exc:
            undo = "clear" if control == "status-bar" else "reset"
            result["warnings"].append({
                "code": "pin_restore_failed",
                "error": f"the {control} pin could not be restored before shutdown: "
                         f"{exc.message}",
                "hint": f"Boot the emulator and run 'autonom simulator {control} {undo}'; "
                        "the pin survives a quickboot snapshot.",
            })
            continue
        result["pins_restored"].append(control)
    return result


def _stale_warning() -> dict[str, Any]:
    return {
        "code": "stale_pin_dropped",
        "error": "the recorded pin belonged to another device (or an earlier boot) "
                 "on this serial; it was dropped, not replayed or restored",
        "hint": "Pin again on this device if it needs a deterministic status bar.",
    }


def read_snapshot(target: Target) -> dict[str, Any]:
    path = snapshot_path(target)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_snapshot(target: Target, snapshot: dict[str, Any]) -> None:
    path = snapshot_path(target)
    if not snapshot:
        try:
            path.unlink()
        except OSError:
            pass
        return
    if target.platform == ANDROID and "identity" not in snapshot:
        # Bound at the first write; `drop_stale_record` ran before it, so a
        # record without an identity is always a fresh one for this device.
        snapshot["identity"] = device_identity(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _remember(target: Target, section: str, entries: dict[str, Any]) -> dict[str, Any]:
    """Merge `entries` into a snapshot section without overwriting a key
    already recorded; returns the section as stored."""
    snapshot = read_snapshot(target)
    stored = snapshot.setdefault(section, {})
    changed = False
    for key, value in entries.items():
        if key not in stored:
            stored[key] = value
            changed = True
    if changed:
        _write_snapshot(target, snapshot)
    return stored


def _forget(target: Target, section: str) -> None:
    snapshot = read_snapshot(target)
    if section in snapshot:
        del snapshot[section]
        if set(snapshot) <= {"identity"}:
            snapshot = {}
        _write_snapshot(target, snapshot)


# --- Android battery service ---------------------------------------------------

_BATTERY_LEVEL = re.compile(r"^\s*level:\s*(-?\d+)\s*$", re.M)
_BATTERY_AC = re.compile(r"^\s*AC powered:\s*(true|false)\s*$", re.M)
_BATTERY_USB = re.compile(r"^\s*USB powered:\s*(true|false)\s*$", re.M)


def parse_battery(text: str) -> dict[str, Any]:
    """The fields of `dumpsys battery` a pin changes. `level` is the exact
    `level:` line (not `scale:`, not a substring: `level: 100` is not 10)."""
    level = _BATTERY_LEVEL.search(text or "")
    ac = _BATTERY_AC.search(text or "")
    usb = _BATTERY_USB.search(text or "")
    return {
        # The battery service prints this banner while `set` overrides are live.
        "overridden": "UPDATES STOPPED" in (text or ""),
        "level": int(level.group(1)) if level else None,
        "ac": ac.group(1) == "true" if ac else None,
        "usb": usb.group(1) == "true" if usb else None,
    }


def _read_battery(target: Target) -> tuple[dict[str, Any], str]:
    observed = _adb(target, ["shell", "dumpsys", "battery"])
    return parse_battery(observed), observed


def _battery(target: Target, action: str, values: dict[str, Any]) -> dict[str, Any]:
    if target.platform == IOS:
        if action == "reset":
            _simctl(target, ["status_bar", target.target_id, "clear"])
            return _unverified({"control": "battery", "action": action})
        level = _int(values, "level", 100, 0, 100)
        state = _choice(values, "state", "charged", IOS_BATTERY_STATES)
        _simctl(target, ["status_bar", target.target_id, "override",
                         "--batteryLevel", str(level), "--batteryState", state])
        # simctl has no read-back for an override; the glyph is the evidence.
        return _unverified({"control": "battery", "level": level, "state": state})
    if action == "reset":
        _adb(target, ["shell", "dumpsys", "battery", "reset"])
        parsed, _ = _read_battery(target)
        return _checked({"control": "battery", "action": action}, not parsed["overridden"])
    level = _int(values, "level", 100, 0, 100)
    _adb(target, ["shell", "dumpsys", "battery", "set", "level", str(level)])
    parsed, observed = _read_battery(target)
    return _checked({"control": "battery", "level": level, "observed": observed[-500:],
                     "observed_level": parsed["level"]}, parsed["level"] == level)


_NETWORK_SPEEDS = ("gsm", "hscsd", "gprs", "edge", "umts", "hsdpa", "lte", "evdo", "full")
_NETWORK_DELAYS = ("gprs", "edge", "umts", "none")
_NUMERIC_RANGE = re.compile(r"^\d+(?::\d+)?$")


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
        return _unverified({"control": "network", "action": action})
    speed = str(values.get("speed", "full")).strip()
    delay = str(values.get("delay", "none")).strip()
    for key, value, names in (("speed", speed, _NETWORK_SPEEDS),
                              ("delay", delay, _NETWORK_DELAYS)):
        if value not in names and not _NUMERIC_RANGE.match(value):
            raise errors.AutonomError(
                errors.FLOW_COMMAND_INVALID,
                f"network {key} must be one of {', '.join(names)} or <n>[:<n>], "
                f"got {value!r}")
    _adb(target, ["emu", "network", "speed", speed])
    _adb(target, ["emu", "network", "delay", delay])
    return _unverified({"control": "network", "action": "shape", "speed": speed,
                        "delay": delay})


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
        # simctl accepted the payload; whether the app showed it is not known.
        return _unverified({"control": "push", "app_id": app_id})
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
        return _unverified({"control": control, "number": number})
    command = "call" if action == "incoming" else "cancel"
    _adb(target, ["emu", "gsm", command, number])
    return _unverified({"control": control, "action": action, "number": number})


# `xcrun simctl` has no biometric subcommand (checked on Xcode 26: "Unrecognized
# subcommand: biometric"), so this control had never worked on iOS. The
# Simulator's Face ID / Touch ID menu is driven by Darwin notifications, which
# `simctl spawn <udid> notifyutil` can post from the host. Face ID devices
# listen on the `pearl` names, Touch ID devices on `fingerTouch`; posting the
# wrong family is silently ignored, so the device type picks the family.
IOS_BIOMETRIC_ENROLLMENT = "com.apple.BiometricKit.enrollmentChanged"
IOS_BIOMETRIC_MATCH = {
    "face": {"match": "com.apple.BiometricKit_Sim.pearl.match",
             "nonmatch": "com.apple.BiometricKit_Sim.pearl.nomatch"},
    "touch": {"match": "com.apple.BiometricKit_Sim.fingerTouch.match",
              "nonmatch": "com.apple.BiometricKit_Sim.fingerTouch.nomatch"},
}
# Kept for callers that read the table: the Touch ID names, as before.
IOS_BIOMETRIC_NOTIFICATIONS = {
    "enroll": (IOS_BIOMETRIC_ENROLLMENT, "1"),
    "unenroll": (IOS_BIOMETRIC_ENROLLMENT, "0"),
    "match": (IOS_BIOMETRIC_MATCH["touch"]["match"], None),
    "nonmatch": (IOS_BIOMETRIC_MATCH["touch"]["nonmatch"], None),
}


def biometry_kind(device_type: str, name: str) -> str | None:
    """`face`, `touch`, or None when the device type does not say.

    iPhone X and every numbered iPhone from 11 on have Face ID; the SE and
    iPhone 8 and older have Touch ID. iPad Pro 11-inch / 13-inch and 12.9-inch
    from the 3rd generation have Face ID; every other iPad has Touch ID.
    """
    text = f"{device_type} {name}".lower().replace("-", " ")
    if "iphone" in text:
        if re.search(r"iphone se\b", text):
            return "touch"
        if re.search(r"iphone x", text):
            return "face"
        match = re.search(r"iphone (\d+)", text)
        if match:
            return "face" if int(match.group(1)) >= 11 else "touch"
        return None
    if "ipad" in text:
        if "ipad pro" in text:
            if re.search(r"\b(11|13) inch", text):
                return "face"
            generation = re.search(r"12\.9 inch.*?(\d+)(?:st|nd|rd|th) generation", text)
            if generation:
                return "face" if int(generation.group(1)) >= 3 else "touch"
            if "12.9 inch" in text or "9.7 inch" in text or "10.5 inch" in text:
                return "touch"
            return None
        return "touch"
    return None


def _ios_biometry(target: Target) -> str | None:
    try:
        listing = json.loads(_simctl(target, ["list", "devices", "--json"]) or "{}")
    except (errors.AutonomError, json.JSONDecodeError):
        return None
    for entries in (listing.get("devices") or {}).values():
        for entry in entries or []:
            if isinstance(entry, dict) and entry.get("udid") == target.target_id:
                return biometry_kind(str(entry.get("deviceTypeIdentifier") or ""),
                                     str(entry.get("name") or ""))
    return None


def _biometric(target: Target, action: str) -> dict[str, Any]:
    if target.platform == IOS:
        if action in ("enroll", "unenroll"):
            kind = None
            posts = [(IOS_BIOMETRIC_ENROLLMENT, "1" if action == "enroll" else "0")]
        else:
            kind = _ios_biometry(target)
            families = [kind] if kind else ["face", "touch"]
            posts = [(IOS_BIOMETRIC_MATCH[family][action], None) for family in families]
        for name, state in posts:
            args = ["spawn", target.target_id, "notifyutil"]
            if state is not None:
                args += ["-s", name, state]
            args += ["-p", name]
            _simctl(target, args)
        result: dict[str, Any] = {"control": "biometric", "action": action,
                                  "notification": posts[0][0],
                                  "notifications": [name for name, _ in posts]}
        if action in ("match", "nonmatch"):
            result["biometry"] = kind or "unknown"
        # notifyutil posts and returns; nothing reports whether a prompt took it.
        return _unverified(result)
    if action != "match":
        raise errors.AutonomError(
            errors.UNSUPPORTED_CAPABILITY,
            "Android emulator only exposes a fingerprint touch stimulus")
    _adb(target, ["emu", "finger", "touch", "1"])
    return _unverified({"control": "biometric", "action": action})


def shell_quote(text: str) -> str:
    """Quote one argument for the device shell. `adb shell a b` joins its
    argv with spaces and hands the line to `sh`, so an unquoted `it's` or
    `$HOME; x` would be parsed there, not delivered."""
    return "'" + text.replace("'", "'\\''") + "'"


def _clipboard(target: Target, action: str, values: dict[str, Any]) -> dict[str, Any]:
    text = str(values.get("text") or "")
    result: dict[str, Any] = {"control": "clipboard", "action": "set", "length": len(text)}
    if target.platform == IOS:
        completed = subprocess.run(
            [target.tool, "simctl", "pbcopy", target.target_id], input=text,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=False)
        if completed.returncode:
            raise errors.AutonomError(errors.BACKEND_FAILED,
                                      completed.stderr.strip() or "simctl pbcopy failed")
        pasted = ios_simctl.run_simctl(target.tool, ["pbpaste", target.target_id],
                                       timeout=30, check=False)
        if pasted.returncode:
            return _unverified(result)
        return _checked(result, (pasted.stdout or "") == text)
    completed = adb_mod.run_adb(
        target.tool, ["shell", "cmd", "clipboard", "set", "text", shell_quote(text)],
        serial=target.target_id, timeout=30, check=False)
    output = (completed.stdout or "") if isinstance(completed.stdout, str) else ""
    # API 36 answers "No shell command implementation." and exits 0.
    if (completed.returncode or "No shell command implementation" in output
            or "nknown command" in output):
        result["supported"] = False
        result["warnings"] = [{
            "code": "clipboard_unsupported",
            "error": "this Android build has no 'cmd clipboard' shell command; "
                     "the clipboard was not changed",
            "hint": "Type the text with 'autonom ui type' instead, or seed it from "
                    "the app under test.",
        }]
        return _unverified(result, "unsupported")
    result["supported"] = True
    # `cmd clipboard` has no portable read-back.
    return _unverified(result)


def _appearance(target: Target, action: str) -> dict[str, Any]:
    result: dict[str, Any] = {"control": "appearance", "value": action}
    if target.platform == IOS:
        _simctl(target, ["ui", target.target_id, "appearance", action])
        observed = _simctl(target, ["ui", target.target_id, "appearance"]).lower()
        result["observed"] = observed
        return _checked(result, observed == action)
    _adb(target, ["shell", "cmd", "uimode", "night", "yes" if action == "dark" else "no"])
    observed = _adb(target, ["shell", "cmd", "uimode", "night"])
    match = re.search(r"Night mode:\s*(\w+)", observed)
    mode = match.group(1).lower() if match else None
    result["observed"] = mode
    return _checked(result, mode == ("yes" if action == "dark" else "no"))


def _text_size_value(target: Target, action: str) -> float | str:
    if target.platform == IOS:
        if action not in IOS_CONTENT_SIZES:
            raise errors.AutonomError(
                errors.INVALID_SIMULATOR_ACTION,
                f"unknown iOS content size {action!r}",
                "Valid text-size values: " + ", ".join(IOS_CONTENT_SIZES) + ".",
                control="text-size", valid_actions=list(IOS_CONTENT_SIZES))
        return action
    try:
        scale = float(action)
    except ValueError:
        scale = float("nan")
    if not 0.5 <= scale <= 2.0:
        raise errors.AutonomError(
            errors.INVALID_SIMULATOR_ACTION,
            f"Android font scale must be a number 0.5..2.0, got {action!r}",
            "Pass the scale as the action, e.g. 'simulator text-size 1.3'.",
            control="text-size", valid_actions=["0.5..2.0"])
    return scale


def _text_size(target: Target, action: str) -> dict[str, Any]:
    value = _text_size_value(target, action)
    result: dict[str, Any] = {"control": "text-size", "value": action}
    if target.platform == IOS:
        _simctl(target, ["ui", target.target_id, "content_size", action])
        observed = _simctl(target, ["ui", target.target_id, "content_size"])
        observed = observed.splitlines()[-1].strip().lower() if observed else ""
        result["observed"] = observed
        return _checked(result, observed == action)
    _adb(target, ["shell", "settings", "put", "system", "font_scale", str(value)])
    observed = _adb(target, ["shell", "settings", "get", "system", "font_scale"])
    result["observed"] = observed
    try:
        matches = float(observed) == value
    except ValueError:
        matches = False
    return _checked(result, matches)


# --- status bar -----------------------------------------------------------------


def _status_bar(target: Target, action: str,
                values: dict[str, Any]) -> dict[str, Any]:
    """`override` applies the given keys; `pin` applies the deterministic
    preset (keys given override it); `clear` restores the live bar."""
    if target.platform == IOS:
        return _ios_status_bar(target, action, values)
    if action == "clear":
        return _android_status_bar_clear(target)
    mode = str(values.pop("mode", "")).lower() or None
    if mode not in (None, "live", "demo"):
        raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                  "status-bar mode must be live or demo")
    demo_keys = sorted(set(values) & set(ANDROID_DEMO_ONLY_KEYS))
    if action == "pin" and mode == "live" and demo_keys:
        raise errors.AutonomError(
            errors.FLOW_COMMAND_INVALID,
            f"{', '.join(demo_keys)} need demo mode, which freezes the clock",
            "Drop mode=live, or drop those keys to keep the real clock.")
    if action == "pin" and mode != "demo" and not demo_keys:
        return _android_live_pin(target, values)
    applied = {**ANDROID_STATUS_BAR_PIN, **values} if action == "pin" else dict(values)
    broadcasts, applied = _android_demo_plan(applied)
    # Validated in full; only now does anything reach the device.
    _android_demo_apply(target, broadcasts)
    # Demo mode holds on its own; a live pin recorded earlier must not be
    # re-sent over it before a capture.
    _record_live_pin(target, None)
    return _unverified({"control": "status-bar", "action": action,
                        "values": {"mode": "demo", **applied}})


def _ios_status_bar(target: Target, action: str, values: dict[str, Any]) -> dict[str, Any]:
    if action == "clear":
        _simctl(target, ["status_bar", target.target_id, "clear"])
        return _unverified({"control": "status-bar", "action": action, "values": {}})
    android_keys = [key for key in values if key in IOS_EQUIVALENT_OF_ANDROID_KEY]
    if android_keys:
        raise errors.AutonomError(
            errors.FLOW_COMMAND_INVALID,
            f"Android-only status-bar key(s) on iOS: {', '.join(android_keys)}",
            "iOS equivalents: " + "; ".join(
                f"{key} -> {IOS_EQUIVALENT_OF_ANDROID_KEY[key]}" for key in android_keys) + ".",
            platform=IOS, keys=android_keys)
    unknown = sorted(set(values) - set(IOS_STATUS_BAR_VALUES))
    if unknown:
        raise errors.AutonomError(
            errors.FLOW_COMMAND_INVALID,
            f"unknown status-bar key(s) for iOS: {', '.join(unknown)}",
            "Keys: " + ", ".join(IOS_STATUS_BAR_VALUES) + ".")
    applied = {**IOS_STATUS_BAR_PIN, **values} if action == "pin" else dict(values)
    for key, value in applied.items():
        allowed = IOS_STATUS_BAR_VALUES[key]
        if allowed is not None and str(value).strip() not in allowed:
            shown = "0..100" if key == "batteryLevel" else ", ".join(allowed)
            raise errors.AutonomError(
                errors.FLOW_COMMAND_INVALID,
                f"status-bar {key} must be {shown}, got {value!r}")
    args = ["status_bar", target.target_id, "override"]
    for key, value in applied.items():
        args.extend([f"--{key}", str(value).strip()])
    _simctl(target, args)
    return _unverified({"control": "status-bar", "action": action, "values": applied})


def _android_signal(target: Target, level: int) -> None:
    """Pin the cellular bars: the profile first, then an explicit RSSI in the
    band Android maps to `level` (see ANDROID_SIGNAL_RSSI)."""
    _adb(target, ["emu", "gsm", "signal-profile", str(level)])
    _adb(target, ["emu", "gsm", "signal", str(ANDROID_SIGNAL_RSSI[level]), "0"])


def _android_live_pin(target: Target, values: dict[str, Any]) -> dict[str, Any]:
    """Pin battery, cellular bars, and notification icons; leave the clock alone."""
    unknown = sorted(set(values) - set(ANDROID_LIVE_KEYS))
    if unknown:
        raise errors.AutonomError(
            errors.FLOW_COMMAND_INVALID,
            f"unknown live status-bar key(s): {', '.join(unknown)}",
            "Live keys: " + ", ".join(ANDROID_LIVE_KEYS) + ". Demo-mode keys ("
            + ", ".join(ANDROID_DEMO_ONLY_KEYS) + ") switch to demo mode and "
            "freeze the clock.")
    level = _int(values, "battery", 100, 0, 100)
    plugged = _bool(values, "plugged", False)
    signal = _int(values, "mobile_level", 4, 0, 4)
    hide = not _bool(values, "notifications", False)

    # What the pin is about to replace, recorded once for `clear`.
    before, _ = _read_battery(target)
    _remember(target, "status_bar", {"battery": before,
                                     "signal": {"profile": None, "readable": False}})
    # A demo pin earlier left SystemUI in demo mode, where the clock stays
    # frozen whatever the live pin does; leaving it is idempotent.
    _demo(target, "exit")
    _adb(target, ["shell", "dumpsys", "battery", "set", "level", str(level)])
    if plugged:
        _adb(target, ["shell", "dumpsys", "battery", "set", "ac", "1"])
    else:
        _adb(target, ["shell", "dumpsys", "battery", "unplug"])
    _android_signal(target, signal)
    warnings: list[dict[str, Any]] = []
    completed = adb_mod.run_adb(
        target.tool, ["shell", "cmd", "statusbar", "send-disable-flag",
                      "notification-icons" if hide else "none"],
        serial=target.target_id, timeout=30, check=False)
    output = (completed.stdout or "") if isinstance(completed.stdout, str) else ""
    icons_ok = completed.returncode == 0 and "rror" not in output and "nknown" not in output
    if not icons_ok:
        warnings.append({
            "code": "status_bar_notification_icons_unsupported",
            "error": "this SystemUI has no 'cmd statusbar send-disable-flag'; "
                     "notification icons stay visible",
            "hint": "Pass hhmm=<HHMM> to use demo mode instead (fixed clock).",
        })
    warnings.append({
        "code": errors.SIGNAL_UNSTABLE,
        "error": "the emulator's modem may re-report its own signal strength; the "
                 "cellular bars are pinned but cannot be read back from the host",
        "hint": "The bars and battery are re-asserted automatically right before "
                "each 'autonom screenshot' and flow capture; pass hhmm=<HHMM> "
                "(demo mode, fixed clock) to hide the mobile icon instead.",
    })
    _record_live_pin(target, {"battery": level, "plugged": plugged,
                              "mobile_level": signal,
                              "hide_notification_icons": hide and icons_ok})
    parsed, observed = _read_battery(target)
    applied: dict[str, Any] = {
        "mode": "live", "battery": level, "plugged": "true" if plugged else "false",
        "mobile_level": signal,
        "notifications": ("hidden" if hide and icons_ok else "visible"),
    }
    result: dict[str, Any] = {"control": "status-bar", "action": "pin", "values": applied,
                              "observed_battery": parsed["level"],
                              "verified_keys": ["battery"]}
    _checked(result, parsed["level"] == level)
    result["warnings"] = warnings
    return result


def _record_live_pin(target: Target, pinned: dict[str, Any] | None) -> None:
    """Store (or drop, with None) the values the last live pin sent, under
    `status_bar.pinned`. Unlike the pre-pin keys it is overwritten by every
    pin: it describes the bar to hold, not the device to restore."""
    snapshot = read_snapshot(target)
    section = snapshot.get("status_bar")
    if pinned is None:
        if isinstance(section, dict) and "pinned" in section:
            del section["pinned"]
            _write_snapshot(target, snapshot)
        return
    snapshot.setdefault("status_bar", {})["pinned"] = {"mode": "live", **pinned}
    _write_snapshot(target, snapshot)


def reassert_pins(target: Target) -> dict[str, Any] | None:
    """Re-send the recorded live Android status-bar pin, or do nothing.

    The emulator's modem re-reports its own signal and SystemUI drops the
    battery icon within about a minute, so a pin is only as good as its last
    re-send. This re-sends exactly what the last live pin sent (battery
    level and plug state, cellular profile and RSSI, the notification-icons
    disable flag) and never touches the pre-pin snapshot `clear` restores.
    Returns `{"repinned": True, "controls": [...]}`, or None when nothing is
    pinned, the pin is a demo-mode one, or the target is iOS (simctl
    overrides persist on their own). A record taken on another device (or
    an earlier boot) is dropped unsent:
    `{"repinned": False, "controls": [], "stale_pin_dropped": True}`."""
    if target.platform != ANDROID:
        return None
    snapshot = read_snapshot(target)
    section = snapshot.get("status_bar")
    pinned = section.get("pinned") if isinstance(section, dict) else None
    if not isinstance(pinned, dict) or pinned.get("mode") != "live":
        return None
    # The serial may now belong to another emulator (the pinned one killed
    # without a clear, another AVD booted on its port) or to a reboot of it:
    # replay nothing and drop the record, pre-pin snapshot included, since it
    # describes a device that is no longer there.
    if drop_stale_record(target):
        return {"repinned": False, "controls": [], "stale_pin_dropped": True}
    controls: list[str] = []
    level = pinned.get("battery")
    if isinstance(level, int) and 0 <= level <= 100:
        _adb(target, ["shell", "dumpsys", "battery", "set", "level", str(level)])
        if pinned.get("plugged"):
            _adb(target, ["shell", "dumpsys", "battery", "set", "ac", "1"])
        else:
            _adb(target, ["shell", "dumpsys", "battery", "unplug"])
        controls.append("battery")
    signal = pinned.get("mobile_level")
    if signal in ANDROID_SIGNAL_RSSI:
        _android_signal(target, signal)
        controls.append("mobile_level")
    if pinned.get("hide_notification_icons"):
        adb_mod.run_adb(target.tool, ["shell", "cmd", "statusbar", "send-disable-flag",
                                      "notification-icons"],
                        serial=target.target_id, timeout=30, check=False)
        controls.append("notifications")
    if not controls:
        return None
    return {"repinned": True, "controls": controls}


def repin_before_capture(target: Target,
                         sleep: Any = None) -> dict[str, Any] | None:
    """`reassert_pins`, then the settle wait, for a capture verb. A failed
    re-send never costs the capture: it is reported as `repinned: False`."""
    try:
        result = reassert_pins(target)
    except errors.AutonomError as exc:
        return {"repinned": False, "controls": [], "repin_error": exc.message}
    if result and result.get("repinned"):
        (sleep or time.sleep)(REPIN_SETTLE_SECONDS)
    return result


def _android_status_bar_clear(target: Target) -> dict[str, Any]:
    """Undo both modes: leave demo mode, put the battery and signal back to
    what the first pin recorded, and restore every status-bar component.
    Without a snapshot (a pin from another machine, a lost state root) the
    battery service is reset and the emulator's default signal profile
    restored — the best honest guess."""
    snapshot = read_snapshot(target).get("status_bar")
    _demo(target, "exit")
    result: dict[str, Any] = {"control": "status-bar", "action": "clear", "values": {},
                              "snapshot": snapshot is not None}
    expected: dict[str, Any] | None = None
    if snapshot is None:
        _adb(target, ["shell", "dumpsys", "battery", "reset"])
        _adb(target, ["emu", "gsm", "signal-profile", ANDROID_DEFAULT_SIGNAL_PROFILE])
        expected = {"overridden": False}
    else:
        battery = snapshot.get("battery")
        if isinstance(battery, dict):
            expected = _restore_battery(target, battery)
        if "signal" in snapshot:
            # The pre-pin profile cannot be read from the host; a fresh
            # emulator reports full bars, which is what it is put back to.
            _android_signal(target, int(ANDROID_DEFAULT_SIGNAL_PROFILE))
        if "demo_allowed" in snapshot:
            previous = snapshot["demo_allowed"]
            if previous in (None, "", "null"):
                _adb(target, ["shell", "settings", "delete", "global", "sysui_demo_allowed"])
            else:
                _adb(target, ["shell", "settings", "put", "global", "sysui_demo_allowed",
                              str(previous)])
        result["restored"] = snapshot
    adb_mod.run_adb(target.tool, ["shell", "cmd", "statusbar", "send-disable-flag", "none"],
                    serial=target.target_id, timeout=30, check=False)
    _forget(target, "status_bar")
    if expected is None:
        return _unverified(result)
    parsed, _ = _read_battery(target)
    ok = parsed["overridden"] == expected["overridden"] and (
        "level" not in expected or parsed["level"] == expected["level"])
    return _checked(result, ok)


def _restore_battery(target: Target, before: dict[str, Any]) -> dict[str, Any]:
    """Put the battery service back as it was before the first pin: a live
    service is reset, a pre-existing override is re-applied."""
    _adb(target, ["shell", "dumpsys", "battery", "reset"])
    if not before.get("overridden"):
        return {"overridden": False}
    expected: dict[str, Any] = {"overridden": True}
    if before.get("level") is not None:
        _adb(target, ["shell", "dumpsys", "battery", "set", "level", str(before["level"])])
        expected["level"] = before["level"]
    for key in ("ac", "usb"):
        if before.get(key) is not None:
            _adb(target, ["shell", "dumpsys", "battery", "set", key,
                          "1" if before[key] else "0"])
    return expected


def _demo(target: Target, command: str, *pairs: tuple[str, Any]) -> None:
    args = ["shell", "am", "broadcast", "-a", "com.android.systemui.demo",
            "-e", "command", command]
    for key, value in pairs:
        args.extend(["-e", key, str(value)])
    _adb(target, args)


def _android_demo_plan(values: dict[str, Any]) -> tuple[list[tuple[Any, ...]], dict[str, Any]]:
    """Validate the key set and translate it into SystemUI demo-mode
    broadcasts, without touching the device."""
    unknown = sorted(set(values) - set(ANDROID_STATUS_BAR_KEYS))
    if unknown:
        raise errors.AutonomError(
            errors.FLOW_COMMAND_INVALID,
            f"unknown status-bar key(s) for Android: {', '.join(unknown)}",
            "Keys: " + ", ".join(ANDROID_STATUS_BAR_KEYS) + ".")
    applied: dict[str, Any] = {}
    broadcasts: list[tuple[Any, ...]] = []
    if "hhmm" in values:
        hhmm = str(values["hhmm"]).replace(":", "").zfill(4)
        if not (hhmm.isdigit() and len(hhmm) == 4
                and int(hhmm[:2]) < 24 and int(hhmm[2:]) < 60):
            raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                      f"hhmm must be a 24-hour HHMM time, got {values['hhmm']!r}")
        broadcasts.append(("clock", ("hhmm", hhmm)))
        applied["hhmm"] = hhmm
    if "battery" in values or "plugged" in values:
        level = _int(values, "battery", 100, 0, 100)
        plugged = "true" if _bool(values, "plugged", False) else "false"
        broadcasts.append(("battery", ("level", level), ("plugged", plugged)))
        applied.update({"battery": level, "plugged": plugged})
    if "wifi_level" in values and "wifi" not in values:
        raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                  "wifi_level needs wifi=show")
    if "wifi" in values:
        if _bool(values, "wifi", True):
            level = _int(values, "wifi_level", 4, 0, 4)
            broadcasts.append(("network", ("wifi", "show"), ("level", level),
                               ("fully", "true")))
            applied.update({"wifi": "show", "wifi_level": level})
        else:
            broadcasts.append(("network", ("wifi", "hide")))
            applied["wifi"] = "hide"
    if ("mobile_level" in values or "datatype" in values) and "mobile" not in values:
        raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                  "mobile_level and datatype need mobile=show")
    if "mobile" in values:
        if _bool(values, "mobile", False):
            level = _int(values, "mobile_level", 4, 0, 4)
            datatype = _choice(values, "datatype", "lte", ANDROID_DEMO_DATATYPES)
            broadcasts.append(("network", ("mobile", "show"), ("level", level),
                               ("datatype", datatype)))
            applied.update({"mobile": "show", "mobile_level": level, "datatype": datatype})
        else:
            broadcasts.append(("network", ("mobile", "hide")))
            applied["mobile"] = "hide"
    if "notifications" in values:
        visible = "true" if _bool(values, "notifications", False) else "false"
        broadcasts.append(("notifications", ("visible", visible)))
        applied["notifications"] = visible
    return broadcasts, applied


def _android_demo_apply(target: Target, broadcasts: list[tuple[Any, ...]]) -> None:
    """Send a validated plan. The broadcasts are idempotent, so re-sending
    them is how the bar gets re-pinned after something (a reboot, a system
    dialog) reset it."""
    previous = _adb(target, ["shell", "settings", "get", "global", "sysui_demo_allowed"])
    _remember(target, "status_bar", {"demo_allowed": previous or None})
    _adb(target, ["shell", "settings", "put", "global", "sysui_demo_allowed", "1"])
    _demo(target, "enter")
    for command, *pairs in broadcasts:
        _demo(target, command, *pairs)


# --- animations -------------------------------------------------------------------


def _read_scales(target: Target) -> dict[str, str]:
    return {key: _adb(target, ["shell", "settings", "get", "global", key])
            for key in ANDROID_ANIMATION_SCALES}


def _scale_is(value: str | None, expected: str | None) -> bool:
    if expected in (None, "null"):
        return value in (None, "", "null")
    try:
        return float(str(value)) == float(expected)
    except ValueError:
        return False


def _animations(target: Target, action: str) -> dict[str, Any]:
    """Turn the three Android animation scales off (`pin`), put back what
    the first pin replaced (`reset`), or read them (`show`)."""
    if target.platform == IOS:
        raise errors.AutonomError(
            errors.UNSUPPORTED_CAPABILITY,
            "the iOS Simulator has no host-level switch for system animations",
            hint="Disable animations in the app under test (UIView.setAnimationsEnabled"
                 "(false) in a UI-test launch argument).",
            capability="simulator.animations")
    result: dict[str, Any] = {"control": "animations", "action": action}
    if action == "show":
        observed = _read_scales(target)
        result.update({"observed": observed,
                       "pinned": all(_scale_is(value, "0") for value in observed.values()),
                       "snapshot": "animations" in read_snapshot(target)})
        return _checked(result, True)
    if action == "pin":
        _remember(target, "animations", _read_scales(target))
        expected: dict[str, str | None] = {key: "0" for key in ANDROID_ANIMATION_SCALES}
        for key in ANDROID_ANIMATION_SCALES:
            _adb(target, ["shell", "settings", "put", "global", key, "0"])
    else:
        snapshot = read_snapshot(target).get("animations")
        result["snapshot"] = snapshot is not None
        expected = {}
        for key in ANDROID_ANIMATION_SCALES:
            previous = (snapshot or {}).get(key, ANDROID_DEFAULT_ANIMATION_SCALE)
            if previous in (None, "", "null"):
                _adb(target, ["shell", "settings", "delete", "global", key])
                expected[key] = None
            else:
                _adb(target, ["shell", "settings", "put", "global", key, str(previous)])
                expected[key] = str(previous)
        _forget(target, "animations")
        result["restored"] = expected
    observed = _read_scales(target)
    result["observed"] = observed
    return _checked(result, all(_scale_is(observed[key], expected[key])
                                for key in ANDROID_ANIMATION_SCALES))


# --- keyboard -----------------------------------------------------------------------


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
    locale = values.get("locale")
    pins = ios_prefs.keyboard_pins(str(locale) if locale else None)
    udid = target.target_id

    if action == "show":
        observed = ios_prefs.observe(udid, pins)
        return {"control": "keyboard", "action": action,
                "observed": observed, "pinned": ios_prefs.is_pinned(observed, pins),
                "backup": ios_prefs.read_backup(udid) is not None,
                "verified": True}

    reboot = _bool(values, "reboot", False)
    # A device with no data directory is refused before any lifecycle churn:
    # shutting a simulator down for a write that cannot happen helps nobody.
    ios_prefs.require_preferences_dir(udid)
    simulator = ios_simctl.find_simulator(target.tool, udid)
    state = simulator.state if simulator is not None else None
    if state in ("Booting", "Shutting Down"):
        raise errors.AutonomError(
            errors.SIMULATOR_MUST_BE_SHUTDOWN,
            f"simulator {udid} is {state}; the preference store is in flux",
            hint="Wait until 'autonom devices' shows it Booted or Shutdown, then retry.",
            target_id=udid, state=state)
    booted = state == "Booted"
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
        # `simctl shutdown` can exit without the device going down; the
        # device list is the only oracle, so nothing is written until it
        # reads Shutdown.
        ios_simctl.shutdown(target.tool, udid)
        after = ios_simctl.find_simulator(target.tool, udid)
        after_state = after.state if after is not None else None
        if after_state != "Shutdown":
            raise errors.AutonomError(
                errors.SIMULATOR_MUST_BE_SHUTDOWN,
                f"simulator {udid} is still {after_state or 'unknown'} after shutdown; "
                "nothing was written",
                hint="Shut it down by hand ('xcrun simctl shutdown <UDID>') and retry "
                     "without reboot=true.",
                target_id=udid, state=after_state)

    result: dict[str, Any] = {"control": "keyboard", "action": action,
                              "locale": pins.get(ios_prefs.GLOBAL_DOMAIN, {}).get("AppleLocale")}
    try:
        if action == "pin":
            backup = ios_prefs.record_backup(udid, pins)
            result["preferences"] = ios_prefs.apply_pins(udid, pins)
            result["backup"] = str(backup) if backup else str(ios_prefs.backup_path(udid))
            observed = ios_prefs.observe(udid, pins)
            result["verified"] = ios_prefs.is_pinned(observed, pins)
        else:
            undone = ios_prefs.remove_pins(udid, pins)
            result.update(undone)
            observed = ios_prefs.observe(udid, pins)
            # Verified means the store now reads exactly what reset intended:
            # restored keys hold their previous value, removed keys are gone.
            result["verified"] = all(
                observed.get(domain, {}).get(key) == value
                for domain, keys in undone["restored"].items()
                for key, value in keys.items()) and all(
                observed.get(domain, {}).get(key) is None
                for domain, keys in undone["removed"].items() for key in keys)
    except BaseException:
        # The caller asked for a running simulator back, but a boot failure
        # here must not replace the error that actually stopped the write.
        if booted:
            try:
                ios_simctl.boot(target.tool, udid)
            except Exception:  # noqa: BLE001 - the original failure is re-raised
                pass
        raise
    if booted:
        ios_simctl.boot(target.tool, udid)
    result["observed"] = observed
    result["rebooted"] = booted
    return result
