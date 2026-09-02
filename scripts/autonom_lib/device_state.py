"""Device-state verbs: deep links, permissions, location, media, crashes, files, recording.

These put an app into a state an agent could not otherwise reach, and read back
the evidence of what happened. Where a platform has no equivalent the verb
refuses with `unsupported_on_platform` rather than silently doing nothing — a
no-op that reports success is worse than an error, because the agent then
believes the state was set.

None of these are certificate or network-configuration operations, so the C-05
consent gate does not apply; `network attach` is the verb that does (see
`consent.py`).
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from . import adb as adb_mod
from . import errors, ios_idb, ios_simctl
from .platform import ANDROID, IOS, Target

_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", re.ASCII)


def _unsupported(target: Target, verb: str, alternative: str = "") -> errors.AutonomError:
    return errors.AutonomError(
        errors.UNSUPPORTED_ON_PLATFORM,
        f"'{verb}' is not supported on {target.platform}",
        alternative or f"This verb is {IOS if target.platform == ANDROID else ANDROID}-only.",
    )


# --- deep links --------------------------------------------------------------


def open_url(target: Target, url: str) -> None:
    if not _URL.match(url or ""):
        raise errors.AutonomError(
            errors.INVALID_URL,
            f"not a URL: {url!r}",
            "Pass a full URL with a scheme, e.g. myapp://profile/42 or https://example.com.",
        )
    if target.platform == IOS:
        ios_simctl.openurl(target.tool, target.target_id, url)
        return
    adb_mod.run_adb(
        target.tool,
        ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", shlex.quote(url)],
        serial=target.target_id,
        timeout=30,
        check=True,
    )


# --- orientation -------------------------------------------------------------

_ORIENTATIONS = {
    "portrait": "0",
    "landscape": "1",
    "portrait-reversed": "2",
    "landscape-reversed": "3",
}


def set_orientation(target: Target, orientation: str) -> dict[str, Any]:
    """Force a device orientation (Android only).

    Disables the accelerometer rotation first — otherwise the sensor snaps
    the value straight back. iOS Simulators expose no orientation surface
    through simctl or idb, so the verb refuses there rather than pretending.
    """
    rotation = _ORIENTATIONS.get(orientation)
    if rotation is None:
        raise errors.AutonomError(
            errors.INVALID_COORDINATES,
            f"unknown orientation {orientation!r}",
            "Orientations: " + ", ".join(_ORIENTATIONS) + ".",
        )
    if target.platform == IOS:
        raise _unsupported(target, "setOrientation",
                           "simctl/idb expose no orientation control; rotate "
                           "in the Simulator app manually.")
    adb_mod.run_adb(
        target.tool,
        ["shell", "settings", "put", "system", "accelerometer_rotation", "0"],
        serial=target.target_id, timeout=10, check=True,
    )
    adb_mod.run_adb(
        target.tool,
        ["shell", "settings", "put", "system", "user_rotation", rotation],
        serial=target.target_id, timeout=10, check=True,
    )
    return {"orientation": orientation, "user_rotation": rotation}


# --- permissions -------------------------------------------------------------


def permissions(target: Target, action: str, service: str, app_id: str | None) -> dict[str, Any]:
    if target.platform == IOS:
        ios_simctl.privacy(target.tool, target.target_id, action, service, app_id)
        return {"action": action, "service": service, "app_id": app_id}
    if not app_id:
        raise errors.AutonomError(
            errors.UNKNOWN_PRIVACY_SERVICE,
            "Android permission changes need a package id",
            "Pass the package: 'autonom permissions grant android.permission.CAMERA com.example.app'.",
        )
    if action == "reset":
        adb_mod.run_adb(
            target.tool, ["shell", "pm", "reset-permissions", app_id],
            serial=target.target_id, timeout=30, check=True,
        )
        return {"action": "reset", "service": "all", "app_id": app_id}
    if action not in {"grant", "revoke"}:
        raise errors.AutonomError(
            errors.UNKNOWN_PRIVACY_SERVICE, f"unknown action: {action}",
            "Valid actions: grant, revoke, reset.",
        )
    adb_mod.run_adb(
        target.tool, ["shell", "pm", action, app_id, service],
        serial=target.target_id, timeout=30, check=True,
    )
    return {"action": action, "service": service, "app_id": app_id}


# --- location ----------------------------------------------------------------


def parse_coordinates(value: str) -> tuple[float, float]:
    parts = [part.strip() for part in (value or "").split(",")]
    if len(parts) != 2:
        raise errors.AutonomError(
            errors.INVALID_COORDINATES, f"expected 'lat,lon', got {value!r}",
            "Example: --at 55.751244,37.618423",
        )
    try:
        latitude, longitude = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise errors.AutonomError(
            errors.INVALID_COORDINATES, f"coordinates are not numeric: {value!r}",
            "Example: 55.751244,37.618423",
        ) from exc
    if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
        raise errors.AutonomError(
            errors.INVALID_COORDINATES,
            f"coordinates out of range: {latitude},{longitude}",
            "Latitude must be -90..90 and longitude -180..180.",
        )
    return latitude, longitude


_EMULATOR_SERIAL = re.compile(r"^emulator-\d+$")


def _require_android_emulator(target: Target, verb: str) -> None:
    """Location mocking on Android goes through the emulator console, which a
    physical device does not have. Refuse hardware with a concrete reason
    rather than a `geo fix` that would silently reach nothing."""
    if not _EMULATOR_SERIAL.match(target.target_id):
        raise errors.AutonomError(
            errors.UNSUPPORTED_ON_PLATFORM,
            f"'{verb}' on a physical Android device is out of scope",
            "Location mocking uses the emulator console (emulator-<port>); a real "
            "device needs a mock-location app plus developer settings.",
        )


def set_location(target: Target, value: str) -> dict[str, Any]:
    latitude, longitude = parse_coordinates(value)
    if target.platform == IOS:
        ios_simctl.set_location(target.tool, target.target_id, latitude, longitude)
        return {"latitude": latitude, "longitude": longitude, "via": "simctl"}
    _require_android_emulator(target, "location set")
    # The emulator console `geo fix` takes LONGITUDE first, then latitude — the
    # reverse of every "lat,lon" the rest of the CLI speaks. Getting this order
    # wrong drops the pin in the wrong hemisphere, silently.
    adb_mod.run_adb(
        target.tool,
        ["emu", "geo", "fix", f"{longitude:.7f}", f"{latitude:.7f}"],
        serial=target.target_id,
    )
    # Seen on a real API-37 emulator: the console answers OK, but the location
    # manager keeps reporting its last delivered fix and the GNSS provider
    # stays inactive until some app subscribes to location updates. Say so,
    # or `location get` right after `set` reads like a failure of `set`.
    return {"latitude": latitude, "longitude": longitude, "via": "emulator_console",
            "delivery": "on_subscription",
            "note": "The fix is injected into the emulator GNSS; the system's last known "
                    "location (what 'location get' reads) updates only once an app "
                    "requests location updates."}


_LAST_LOCATION = re.compile(
    r"last location=Location\[(\S+)\s+(-?\d+\.\d+),\s*(-?\d+\.\d+)"
    r"(?:[^\]]*?hAcc=(-?\d+\.\d+))?"
)
_PROVIDER_PRIORITY = ("fused", "gps", "network", "passive")


def get_location(target: Target) -> dict[str, Any]:
    """Read the current (last known) location.

    Android reads it from `dumpsys location`, preferring the fused provider.
    iOS has no read-back — `simctl` can set or clear a simulator's location but
    not report it — so it refuses rather than invent a value."""
    if target.platform == IOS:
        raise errors.AutonomError(
            errors.UNSUPPORTED_ON_PLATFORM,
            "reading the location back is not supported on iOS",
            "simctl can set or clear a simulator's location but not read it; "
            "track what you set with 'location set'.",
        )
    completed = adb_mod.run_adb(
        target.tool, ["shell", "dumpsys", "location"], serial=target.target_id
    )
    fixes: dict[str, dict[str, Any]] = {}
    for match in _LAST_LOCATION.finditer(completed.stdout or ""):
        provider = match.group(1)
        fixes.setdefault(provider, {
            "latitude": float(match.group(2)),
            "longitude": float(match.group(3)),
            "provider": provider,
            "accuracy_m": float(match.group(4)) if match.group(4) else None,
        })
    for provider in _PROVIDER_PRIORITY:
        if provider in fixes:
            return fixes[provider]
    if fixes:
        return next(iter(fixes.values()))
    return {"latitude": None, "longitude": None, "provider": None,
            "note": "no last known location on the device"}


def clear_location(target: Target) -> None:
    if target.platform == IOS:
        ios_simctl.clear_location(target.tool, target.target_id)
        return
    _require_android_emulator(target, "location clear")
    # The emulator console has no "revert to real GPS" — a simulator has no real
    # fix to return to. Report that honestly instead of pretending to clear.
    raise errors.AutonomError(
        errors.UNSUPPORTED_ON_PLATFORM,
        "the Android emulator has no location reset",
        "There is no real GPS to restore; set a new position with 'location set' instead.",
    )


# --- media -------------------------------------------------------------------


def add_media(target: Target, path: Path) -> dict[str, Any]:
    path = path.expanduser()
    if not path.exists():
        raise errors.AutonomError(
            errors.INSTALL_PATH_NOT_FOUND, f"media file not found: {path}",
            "Pass an existing image or video path.",
        )
    if target.platform == IOS:
        ios_simctl.add_media(target.tool, target.target_id, path)
        return {"added": str(path)}
    remote = f"/sdcard/Pictures/{path.name}"
    adb_mod.run_adb(target.tool, ["push", str(path), remote],
                    serial=target.target_id, timeout=120, check=True)
    adb_mod.run_adb(
        target.tool,
        ["shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
         "-d", f"file://{remote}"],
        serial=target.target_id, timeout=30, check=False,
    )
    return {"added": str(path), "remote": remote}


# --- crashes -----------------------------------------------------------------

_CRASH_LINE = re.compile(
    r"^(?P<name>\S+\.ips\S*)\s*$|"
    r"^(?P<date>\d{4}-\d{2}-\d{2}[^\s]*)\s+(?P<other>\S+)"
)


def crash_list(target: Target, app_id: str | None = None) -> list[dict[str, Any]]:
    """Structured crash entries.

    iOS reads idb's crash store. Android has no equivalent report directory, so
    the closest honest analogue is the dedicated `crash` logcat buffer.
    """
    if target.platform == IOS:
        raw = ios_idb.crash_list(target)
        entries: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.lower().startswith(("name", "----")):
                continue
            fields = line.split()
            name = fields[0]
            entry = {
                "name": name,
                "bundle_id": next((f for f in fields[1:] if "." in f and "/" not in f), None),
                "process": name.split("-")[0] if "-" in name else name,
                "date": " ".join(fields[-2:]) if len(fields) >= 3 else None,
                "raw": line,
            }
            entries.append(entry)
        if app_id:
            entries = [e for e in entries
                       if (e["bundle_id"] == app_id or app_id.rsplit(".", 1)[-1] in e["raw"])]
        return entries

    completed = adb_mod.run_adb(
        target.tool, ["logcat", "-b", "crash", "-d", "-v", "threadtime"],
        serial=target.target_id, timeout=30, check=False,
    )
    text = completed.stdout if isinstance(completed.stdout, str) else ""
    entries = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if app_id and app_id not in line and app_id.rsplit(".", 1)[-1] not in line:
            continue
        entries.append({"name": None, "bundle_id": app_id, "process": None,
                        "date": line.split()[0] if line.split() else None, "raw": line})
    return entries


def crash_show(target: Target, name: str) -> str:
    if target.platform == IOS:
        return ios_idb.crash_show(target, name)
    raise _unsupported(
        target, "crash show",
        "On Android use 'autonom crash list' (the crash logcat buffer) or a tombstone pull.",
    )


# --- app-container files -----------------------------------------------------


def safe_relative(remote: str) -> str:
    """Reject anything that escapes the app container after normalization (INV-09)."""
    candidate = (remote or "").strip()
    if not candidate:
        raise errors.AutonomError(
            errors.PATH_OUTSIDE_CONTAINER, "an empty path is not inside the container",
            "Pass a container-relative path such as Documents/state.json.",
        )
    if candidate.startswith(("/", "~")):
        raise errors.AutonomError(
            errors.PATH_OUTSIDE_CONTAINER, f"absolute paths are not allowed: {remote}",
            "Pass a container-relative path such as Documents/state.json.",
        )
    normalized = os.path.normpath(candidate)
    if normalized == ".." or normalized.startswith("../") or normalized.startswith("/"):
        raise errors.AutonomError(
            errors.PATH_OUTSIDE_CONTAINER, f"path escapes the app container: {remote}",
            "Pass a container-relative path such as Documents/state.json.",
        )
    return normalized


def file_pull(target: Target, app_id: str, remote: str, destination: Path) -> dict[str, Any]:
    relative = safe_relative(remote)
    destination = destination.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)

    if target.platform == IOS:
        container = ios_simctl.app_container(target.tool, target.target_id, app_id, "data")
        if container and (container / relative).exists():
            data = (container / relative).read_bytes()
            destination.write_bytes(data)
        else:
            ios_idb.file_pull(target, app_id, relative, destination)
    else:
        completed = adb_mod.run_adb(
            target.tool,
            ["exec-out", "run-as", app_id, "cat", relative],
            serial=target.target_id, timeout=60, check=False, binary=True,
        )
        assert isinstance(completed.stdout, bytes)
        stderr = (completed.stderr or b"").decode("utf-8", "replace")
        _raise_if_run_as_refused(app_id, stderr or completed.stdout.decode("utf-8", "replace"))
        if completed.returncode != 0:
            raise adb_mod.AdbError(stderr.strip() or f"run-as {app_id} cat {relative} failed")
        destination.write_bytes(completed.stdout)

    # Contents are deliberately not echoed: pulled app data can hold PII.
    return {"path": str(destination), "bytes": destination.stat().st_size, "remote": relative}


def file_ls(target: Target, app_id: str, remote: str = ".") -> list[str]:
    relative = safe_relative(remote) if remote not in (".", "") else "."
    if target.platform == IOS:
        container = ios_simctl.app_container(target.tool, target.target_id, app_id, "data")
        if not container:
            raise errors.AutonomError(
                errors.APP_NOT_INSTALLED, f"no data container for {app_id}",
                "Check the bundle id with 'xcrun simctl listapps <udid>'.",
            )
        base = container if relative == "." else container / relative
        if not base.exists():
            return []
        return sorted(entry.name + ("/" if entry.is_dir() else "") for entry in base.iterdir())
    completed = adb_mod.run_adb(
        target.tool, ["exec-out", "run-as", app_id, "ls", "-1", relative],
        serial=target.target_id, timeout=30, check=False,
    )
    text = completed.stdout if isinstance(completed.stdout, str) else ""
    # `exec-out` merges run-as's complaint into the listing, so a system app
    # used to come back as one "file" named `run-as: package not an
    # application` with ok: true. Refuse by name instead.
    _raise_if_run_as_refused(app_id, text)
    if completed.returncode != 0:
        raise adb_mod.AdbError(text.strip() or f"run-as {app_id} ls {relative} failed")
    return [line.strip() for line in text.splitlines() if line.strip()]


_RUN_AS_REFUSALS = (
    "package not an application", "not debuggable", "run-as: unknown package",
    "run-as: could not", "run-as: package", "run-as: Could not",
)


def _raise_if_run_as_refused(app_id: str, output: str) -> None:
    lowered = (output or "").lower()
    if not lowered.startswith("run-as:") and "run-as:" not in lowered[:200]:
        return
    if any(marker.lower() in lowered for marker in _RUN_AS_REFUSALS):
        raise errors.AutonomError(
            errors.APP_NOT_DEBUGGABLE,
            f"run-as refused {app_id}: {output.strip().splitlines()[0][:160]}",
            "Container files are readable only for debuggable builds (release and "
            "system apps refuse run-as). Install a debug build, or pull app data "
            "through the app's own export.",
        )


# --- screen recording --------------------------------------------------------


def record_start(target: Target, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if target.platform == IOS:
        argv = [target.tool, "simctl", "io", target.target_id, "recordVideo",
                "--codec", "h264", "--force", str(destination)]
    else:
        argv = [target.tool, "-s", target.target_id, "shell", "screenrecord",
                "/sdcard/autonom-recording.mp4"]
    process = subprocess.Popen(  # noqa: S603 - argv is constructed, never shell
        argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
    )
    return process.pid


def record_stop(target: Target, pid: int | None, destination: Path) -> dict[str, Any]:
    """Stop cleanly: both recorders finalize the container only on SIGINT."""
    stopped = False
    if pid:
        try:
            os.kill(pid, 2)  # SIGINT, so the mp4 moov atom is written
            stopped = True
        except (ProcessLookupError, PermissionError):
            stopped = False
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.1)

    if target.platform == ANDROID and stopped:
        time.sleep(1.0)  # screenrecord flushes after the signal
        adb_mod.run_adb(
            target.tool, ["pull", "/sdcard/autonom-recording.mp4", str(destination)],
            serial=target.target_id, timeout=120, check=False,
        )
        adb_mod.run_adb(
            target.tool, ["shell", "rm", "-f", "/sdcard/autonom-recording.mp4"],
            serial=target.target_id, timeout=30, check=False,
        )

    size = destination.stat().st_size if destination.exists() else 0
    return {"path": str(destination), "bytes": size, "was_recording": stopped}
