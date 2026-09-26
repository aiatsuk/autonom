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


# Short names an agent reaches for first (they are the iOS service names too),
# mapped to the Android runtime permissions they unambiguously mean. `location`
# is a permission group: the system's own dialog grants fine and coarse
# together, and API 31+ refuses fine without coarse.
ANDROID_PERMISSION_ALIASES: dict[str, tuple[str, ...]] = {
    "camera": ("android.permission.CAMERA",),
    "microphone": ("android.permission.RECORD_AUDIO",),
    "location": ("android.permission.ACCESS_FINE_LOCATION",
                 "android.permission.ACCESS_COARSE_LOCATION"),
    "contacts": ("android.permission.READ_CONTACTS",),
    "photos": ("android.permission.READ_MEDIA_IMAGES",),
    "notifications": ("android.permission.POST_NOTIFICATIONS",),
}

_PERMISSION_NAME_HINT = (
    "Android takes full permission names, e.g. android.permission.CAMERA, "
    "android.permission.RECORD_AUDIO, android.permission.ACCESS_FINE_LOCATION, "
    "android.permission.POST_NOTIFICATIONS. Short names understood: "
    + ", ".join(sorted(ANDROID_PERMISSION_ALIASES)) + "."
)


def android_permission_names(service: str) -> list[str]:
    """The Android permission(s) a `permissions` service argument names."""
    alias = ANDROID_PERMISSION_ALIASES.get((service or "").strip().lower())
    return list(alias) if alias else [service]


# `dumpsys package <pkg>` lists each runtime permission as
# `android.permission.CAMERA: granted=true, flags=[ USER_SET ]` under a
# `runtime permissions:` heading, once per user.
_RUNTIME_PERMISSION = re.compile(r"^\s*([A-Za-z0-9_.]+):\s*granted=(true|false)")


def granted_runtime_permissions(dumpsys_text: str) -> list[str]:
    """Runtime permissions `dumpsys package` reports as granted, in order.

    Only the `runtime permissions:` sections count: `install permissions`
    (INTERNET and friends) are granted at install time and cannot be revoked.
    """
    granted: list[str] = []
    in_runtime = False
    for line in (dumpsys_text or "").splitlines():
        stripped = line.strip()
        if stripped.endswith(":") and "permissions" in stripped:
            in_runtime = stripped == "runtime permissions:"
            continue
        if not in_runtime:
            continue
        match = _RUNTIME_PERMISSION.match(line)
        if match is None:
            in_runtime = False  # the section ended at the first non-permission line
            continue
        if match.group(2) == "true" and match.group(1) not in granted:
            granted.append(match.group(1))
    return granted


def _raise_for_pm_output(output: str, app_id: str, permission: str) -> None:
    """`pm grant|revoke` failures are Java exceptions on stdout; name them."""
    text = (output or "").strip()
    if not text:
        return
    lowered = text.lower()
    if ("unknown package" in lowered or "package " + app_id.lower() + " not found" in lowered
            or "no such package" in lowered):
        raise errors.AutonomError(
            errors.APP_NOT_INSTALLED, f"{app_id} is not installed on the device",
            "Check the package id with 'adb shell pm list packages'.",
        )
    if "unknown permission" in lowered or "illegalargumentexception" in lowered:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"unknown Android permission: {permission}",
            _PERMISSION_NAME_HINT,
        )
    if "has not requested permission" in lowered or "not a changeable permission" in lowered:
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            f"{permission} cannot be changed for {app_id}: {text.splitlines()[-1][:200]}",
            "Only runtime permissions the app declares in its manifest can be granted "
            "or revoked; install-time permissions are fixed.",
        )
    if "exception" in lowered or lowered.startswith(("error", "failure")):
        raise adb_mod.AdbError(text.splitlines()[-1][:400])


def _pm_permission(target: Target, action: str, app_id: str, permission: str) -> None:
    completed = adb_mod.run_adb(
        target.tool, ["shell", "pm", action, app_id, permission],
        serial=target.target_id, timeout=30, check=False,
    )
    output = completed.stdout if isinstance(completed.stdout, str) else ""
    _raise_for_pm_output(output, app_id, permission)
    if completed.returncode != 0:
        raise adb_mod.AdbError(output.strip() or f"pm {action} {app_id} {permission} failed")


def _android_reset(target: Target, service: str, app_id: str) -> dict[str, Any]:
    """Reset one package's runtime permissions — never another app's.

    `pm reset-permissions` takes no package argument and resets EVERY app on
    the device (measured: resetting Chrome cost YouTube its RECORD_AUDIO
    grant). A reset here revokes the named permission, or with `all` each
    runtime permission `dumpsys package` reports as granted, for this package
    only, and clears the user-set/user-fixed flags so the app may ask again.
    """
    if (service or "").strip().lower() == "all":
        completed = adb_mod.run_adb(
            target.tool, ["shell", "dumpsys", "package", app_id],
            serial=target.target_id, timeout=30, check=False,
        )
        text = completed.stdout if isinstance(completed.stdout, str) else ""
        if "Unable to find package" in text or f"Package [{app_id}]" not in text:
            raise errors.AutonomError(
                errors.APP_NOT_INSTALLED, f"{app_id} is not installed on the device",
                "Check the package id with 'adb shell pm list packages'.",
            )
        permissions_to_reset = granted_runtime_permissions(text)
    else:
        permissions_to_reset = android_permission_names(service)
    revoked: list[str] = []
    for permission in permissions_to_reset:
        _pm_permission(target, "revoke", app_id, permission)
        adb_mod.run_adb(
            target.tool,
            ["shell", "pm", "clear-permission-flags", app_id, permission,
             "user-set", "user-fixed"],
            serial=target.target_id, timeout=30, check=False,
        )
        revoked.append(permission)
    return {"action": "reset", "service": service, "app_id": app_id,
            "scope": "package", "revoked": revoked}


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
        return _android_reset(target, service, app_id)
    if action not in {"grant", "revoke"}:
        raise errors.AutonomError(
            errors.UNKNOWN_PRIVACY_SERVICE, f"unknown action: {action}",
            "Valid actions: grant, revoke, reset.",
        )
    names = android_permission_names(service)
    for permission in names:
        _pm_permission(target, action, app_id, permission)
    return {"action": action, "service": service, "app_id": app_id, "permissions": names}


# --- location ----------------------------------------------------------------


def parse_coordinates(value: str) -> tuple[float, float]:
    parts = [part.strip() for part in (value or "").split(",")]
    if len(parts) != 2:
        raise errors.AutonomError(
            errors.INVALID_COORDINATES, f"expected 'lat,lon', got {value!r}",
            "Pass latitude,longitude, e.g. 'autonom location set 55.751244,37.618423'.",
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


def set_location(target: Target, value: str,
                 session: dict[str, Any] | None = None) -> dict[str, Any]:
    latitude, longitude = parse_coordinates(value)
    if session is not None:
        # Remembered so `location get` can say "you asked for X, the system
        # reports Y" instead of leaving the mismatch to look like a failed set.
        session["location_requested"] = {
            "latitude": latitude, "longitude": longitude,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
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


def _with_delivery(observed: dict[str, Any],
                   session: dict[str, Any] | None) -> dict[str, Any]:
    """Annotate an observed fix with what this session asked for, if anything."""
    requested = (session or {}).get("location_requested")
    if not requested:
        return observed
    close = (
        observed.get("latitude") is not None
        and abs(observed["latitude"] - requested["latitude"]) < 1e-4
        and abs(observed["longitude"] - requested["longitude"]) < 1e-4
    )
    annotated = {**observed, "requested": requested, "delivered": close}
    if not close:
        annotated["note"] = ("The system's last known location differs from the fix this "
                             "session set; the emulator delivers a fix only to an app that "
                             "subscribes to location updates.")
    return annotated


def get_location(target: Target, session: dict[str, Any] | None = None) -> dict[str, Any]:
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
            return _with_delivery(fixes[provider], session)
    if fixes:
        return _with_delivery(next(iter(fixes.values())), session)
    return _with_delivery({"latitude": None, "longitude": None, "provider": None,
                           "note": "no last known location on the device"}, session)


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


_PATH_EXAMPLES = {
    IOS: "Documents/state.json",
    ANDROID: "files/state.json or shared_prefs/<name>.xml",
}


def _path_hint(platform: str | None) -> str:
    example = _PATH_EXAMPLES.get(platform or "") or (
        f"{_PATH_EXAMPLES[IOS]} (iOS) or {_PATH_EXAMPLES[ANDROID]} (Android)")
    return f"Pass a container-relative path such as {example}."


def safe_relative(remote: str, platform: str | None = None) -> str:
    """Reject anything that escapes the app container after normalization (INV-09).

    Only a `..` path *component* escapes; a file name that merely contains two
    dots (`a..b`) is an ordinary name and stays allowed."""
    candidate = (remote or "").strip()
    hint = _path_hint(platform)
    if not candidate:
        raise errors.AutonomError(
            errors.PATH_OUTSIDE_CONTAINER, "an empty path is not inside the container", hint,
        )
    if candidate.startswith(("/", "~")):
        raise errors.AutonomError(
            errors.PATH_OUTSIDE_CONTAINER, f"absolute paths are not allowed: {remote}", hint,
        )
    normalized = os.path.normpath(candidate)
    if normalized == ".." or normalized.startswith("../") or normalized.startswith("/"):
        raise errors.AutonomError(
            errors.PATH_OUTSIDE_CONTAINER, f"path escapes the app container: {remote}", hint,
        )
    return normalized


def _path_not_found(app_id: str, relative: str, platform: str) -> errors.AutonomError:
    """A container path that does not exist.

    Emitted as `body_file_not_found` — the code `file ls` has always used here,
    kept because codes are never repurposed or removed — with `detail:
    path_not_found` so a caller can tell it from a missing network-mock body.
    """
    return errors.AutonomError(
        errors.BODY_FILE_NOT_FOUND,
        f"no such path in the {app_id} container: {relative}",
        f"List the container first: 'autonom file ls --app-id {app_id}'. "
        + _path_hint(platform),
        detail="path_not_found",
    )


def _is_a_directory(app_id: str, relative: str) -> errors.AutonomError:
    return errors.AutonomError(
        errors.INVALID_VALUE,
        f"{relative} in the {app_id} container is a directory, not a file",
        f"'file pull' copies one file; list the directory with "
        f"'autonom file ls {relative} --app-id {app_id}' and pull a file from it.",
        detail="is_a_directory",
    )


def _shell_complaint(tool: str, relative: str, output: str) -> str | None:
    """What `cat`/`ls` said about `relative`, when that is all the output is.

    `exec-out` merges stderr into stdout and exits 0, so a missing file comes
    back as the one line `cat: files/x: No such file or directory` — which
    `file pull` used to write to disk as if it were the file.
    """
    first = (output or "").lstrip().split("\n", 1)[0].strip()
    if not first.startswith(f"{tool}: ") or relative not in first:
        return None
    lowered = first.lower()
    if "no such file or directory" in lowered:
        return "missing"
    if "is a directory" in lowered:
        return "directory"
    return None


def file_pull(target: Target, app_id: str, remote: str, destination: Path) -> dict[str, Any]:
    relative = safe_relative(remote, target.platform)
    destination = destination.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)

    if target.platform == IOS:
        container = ios_simctl.app_container(target.tool, target.target_id, app_id, "data")
        if container and container.is_dir():
            source = container / relative
            if source.is_dir():
                raise _is_a_directory(app_id, relative)
            if not source.exists():
                raise _path_not_found(app_id, relative, IOS)
            destination.write_bytes(source.read_bytes())
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
        # Only the head can be a complaint; never decode a whole binary file.
        head = completed.stdout[:512].decode("utf-8", "replace")
        _raise_if_run_as_refused(app_id, stderr or head)
        complaint = _shell_complaint("cat", relative, stderr or head)
        if complaint == "missing":
            raise _path_not_found(app_id, relative, ANDROID)
        if complaint == "directory":
            raise _is_a_directory(app_id, relative)
        if completed.returncode != 0:
            raise adb_mod.AdbError(stderr.strip() or f"run-as {app_id} cat {relative} failed")
        destination.write_bytes(completed.stdout)

    # Contents are deliberately not echoed: pulled app data can hold PII.
    return {"path": str(destination), "bytes": destination.stat().st_size, "remote": relative}


def file_ls(target: Target, app_id: str, remote: str = ".") -> list[str]:
    relative = safe_relative(remote, target.platform) if remote not in (".", "") else "."
    if target.platform == IOS:
        container = ios_simctl.app_container(target.tool, target.target_id, app_id, "data")
        if not container:
            raise errors.AutonomError(
                errors.APP_NOT_INSTALLED, f"no data container for {app_id}",
                "Either the app is not installed or it has no data container. System "
                "apps vary (com.apple.Maps has one, com.apple.Preferences does not); "
                "check the bundle id with 'xcrun simctl listapps <udid>'.",
            )
        if not container.is_dir():
            raise errors.AutonomError(
                errors.APP_NOT_INSTALLED,
                f"data container for {app_id} is not on disk: {container}",
                "The simulator may need a boot, or the app a reinstall.",
            )
        base = container if relative == "." else container / relative
        if not base.exists():
            # safe_relative already refused every escaping path, so a name
            # like `a..b` is simply missing, not outside the container.
            raise _path_not_found(app_id, relative, IOS)
        if not base.is_dir():
            return [base.name]
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
    if _shell_complaint("ls", relative, text) == "missing":
        raise _path_not_found(app_id, relative, ANDROID)
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
    first_line = output.strip().splitlines()[0][:160] if output.strip() else ""
    if "unknown package" in lowered:
        # Not a debuggability question: the package is not on the device.
        raise errors.AutonomError(
            errors.APP_NOT_INSTALLED,
            f"{app_id} is not installed on the device: {first_line}",
            "Check the package id with 'adb shell pm list packages', or install the app.",
        )
    if any(marker.lower() in lowered for marker in _RUN_AS_REFUSALS):
        raise errors.AutonomError(
            errors.APP_NOT_DEBUGGABLE,
            f"run-as refused {app_id}: {first_line}",
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
