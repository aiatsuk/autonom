"""`xcrun simctl` wrapper — iOS Simulator lifecycle for Autonom.

Everything here works without `idb`: booting, installing, launching, deep
links, permissions, location, media, screenshots, video, and logs. Only the
accessibility tree and gestures need the companion (see `ios_idb.py`), so an
operator without idb still gets a usable — if not clickable — iOS harness
(RISK-016).
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from . import errors

RUNTIME_PREFIX = "com.apple.CoreSimulator.SimRuntime."
_RUNTIME_NAME = re.compile(r"^([A-Za-z]+)-(\d+)(?:-(\d+))?(?:-(\d+))?$")


@dataclass(frozen=True)
class Simulator:
    udid: str
    name: str
    state: str
    runtime: str
    is_available: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": "ios",
            "target_id": self.udid,
            "udid": self.udid,
            "state": self.state,
            "running": self.state == "Booted",
            "name": self.name,
            "runtime": self.runtime,
            "properties": {"is_available": self.is_available},
        }


def find_simctl(explicit: str | None = None) -> str:
    """Resolve the `xcrun` driver. Order: flag, environment, PATH."""
    candidate = explicit or os.environ.get("AUTONOM_SIMCTL")
    if candidate:
        return candidate
    path = shutil.which("xcrun")
    if not path:
        raise errors.tool_missing("simctl")
    return path


def run_simctl(
    xcrun: str,
    args: Sequence[str],
    *,
    timeout: float | None = 60,
    check: bool = True,
    binary: bool = False,
) -> subprocess.CompletedProcess:
    command = [xcrun, "simctl", *args]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            text=not binary,
        )
    except FileNotFoundError as exc:
        raise errors.tool_missing("simctl") from exc
    if check and completed.returncode != 0:
        detail = completed.stderr if not binary else (completed.stderr or b"").decode("utf-8", "replace")
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            (detail or "").strip() or f"simctl {' '.join(args)} failed ({completed.returncode})",
            "Run 'autonom doctor' to check the Xcode toolchain.",
        )
    return completed


def runtime_display_name(runtime_identifier: str) -> str:
    """`…SimRuntime.iOS-26-0` -> `iOS 26.0`; unknown shapes pass through."""
    if not runtime_identifier.startswith(RUNTIME_PREFIX):
        return runtime_identifier
    tail = runtime_identifier[len(RUNTIME_PREFIX):]
    match = _RUNTIME_NAME.match(tail)
    if not match:
        return tail
    platform_name = match.group(1)
    version = ".".join(part for part in match.groups()[1:] if part)
    return f"{platform_name} {version}"


def parse_devices(payload: dict[str, Any]) -> list[Simulator]:
    simulators: list[Simulator] = []
    for runtime_identifier, entries in sorted((payload.get("devices") or {}).items()):
        runtime = runtime_display_name(runtime_identifier)
        for entry in entries or []:
            if not isinstance(entry, dict) or not entry.get("udid"):
                continue
            simulators.append(
                Simulator(
                    udid=entry["udid"],
                    name=entry.get("name") or "",
                    state=entry.get("state") or "Unknown",
                    runtime=runtime,
                    is_available=bool(entry.get("isAvailable", True)),
                )
            )
    return simulators


def list_devices(xcrun: str, *, available_only: bool = True) -> list[Simulator]:
    args = ["list", "devices"]
    if available_only:
        args.append("available")
    args.append("--json")
    completed = run_simctl(xcrun, args, timeout=60)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"could not parse simctl device list: {exc}",
            "Check that 'xcrun simctl list devices available --json' works.",
        ) from exc
    return parse_devices(payload)


def find_simulator(xcrun: str, udid: str) -> Simulator | None:
    for simulator in list_devices(xcrun, available_only=False):
        if simulator.udid == udid:
            return simulator
    return None


# --- lifecycle ---------------------------------------------------------------


def boot(xcrun: str, udid: str, *, timeout: float = 120) -> bool:
    """Boot and wait. Returns True when this call performed the boot.

    `bootstatus -b` boots if needed and blocks until the device is usable, which
    is the behavior CAP-IOSS-001 specifies. One retry absorbs the transient
    CoreSimulator failures that make cold boots flaky (RISK-012).
    """
    simulator = find_simulator(xcrun, udid)
    if simulator and simulator.state == "Booted":
        return False

    detail = ""
    for _attempt in range(2):
        completed = run_simctl(xcrun, ["bootstatus", udid, "-b"], timeout=timeout, check=False)
        # `bootstatus -b` exits 0 even when the boot failed — observed on a
        # simulator whose data directory had been deleted, where it printed
        # "Unable to boot device because it cannot be located on disk" and still
        # returned 0. The device state is the only trustworthy oracle.
        simulator = find_simulator(xcrun, udid)
        if simulator and simulator.state == "Booted":
            return True
        detail = ((completed.stderr or "") + (completed.stdout or "")).strip()

    raise errors.AutonomError(
        errors.IOS_BOOT_FAILED,
        detail or f"simulator {udid} did not reach the Booted state within {timeout:.0f}s",
        "If the device data is missing, run 'xcrun simctl erase <udid>'. Otherwise try "
        "'xcrun simctl shutdown all' and retry, or pick another simulator with 'autonom devices'.",
    )


def shutdown(xcrun: str, udid: str) -> None:
    run_simctl(xcrun, ["shutdown", udid], timeout=60, check=False)


def install(xcrun: str, udid: str, app_path: Path) -> None:
    app_path = app_path.expanduser()
    if not app_path.exists():
        raise errors.AutonomError(
            errors.INSTALL_PATH_NOT_FOUND,
            f"app bundle not found: {app_path}",
            "Pass --install with a path to a built .app bundle (see DerivedData or build/ios).",
        )
    run_simctl(xcrun, ["install", udid, str(app_path)], timeout=180)


def uninstall(xcrun: str, udid: str, bundle_id: str, *, check: bool = False) -> bool:
    """Returns True when simctl reported success.

    The return code used to be dropped, so a failed uninstall (a bundle id
    that is not installed, a device that is not booted) answered `ok: true`.
    With ``check`` the failure raises instead, carrying simctl's own words.
    """
    completed = run_simctl(xcrun, ["uninstall", udid, bundle_id], timeout=60, check=False)
    if completed.returncode == 0:
        return True
    if check:
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            (completed.stderr or "").strip()
            or f"simctl uninstall {bundle_id} failed ({completed.returncode})",
            "Check the bundle id with 'xcrun simctl listapps <udid>' and that the "
            "simulator is booted.",
            bundle_id=bundle_id,
        )
    return False


def list_apps(xcrun: str, udid: str) -> str:
    return run_simctl(xcrun, ["listapps", udid], timeout=60, check=False).stdout or ""


def is_installed(xcrun: str, udid: str, bundle_id: str) -> bool:
    return bundle_id in list_apps(xcrun, udid)


def launch(
    xcrun: str,
    udid: str,
    bundle_id: str,
    *,
    args: Sequence[str] = (),
    env: dict[str, str] | None = None,
) -> int | None:
    """Launch and return the pid when simctl reports one.

    Child environment travels through `SIMCTL_CHILD_*`, which is also the
    mechanism CAP-ATTACH-004 uses for the per-process iOS proxy.
    """
    if not is_installed(xcrun, udid, bundle_id):
        raise errors.AutonomError(
            errors.APP_NOT_INSTALLED,
            f"{bundle_id} is not installed on {udid}",
            "Install it first with 'autonom session start --install <path>.app'.",
        )
    command_env = dict(os.environ)
    for key, value in (env or {}).items():
        command_env[f"SIMCTL_CHILD_{key}"] = value
    completed = subprocess.run(
        [xcrun, "simctl", "launch", udid, bundle_id, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=60,
        env=command_env,
    )
    if completed.returncode != 0:
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            (completed.stderr or "").strip() or f"launch {bundle_id} failed",
            "Check the bundle id with 'xcrun simctl listapps <udid>'.",
        )
    match = re.search(r":\s*(\d+)", completed.stdout or "")
    return int(match.group(1)) if match else None


def terminate(xcrun: str, udid: str, bundle_id: str) -> bool:
    """Returns True when the app was running. Stopping a stopped app is success."""
    completed = run_simctl(xcrun, ["terminate", udid, bundle_id], timeout=30, check=False)
    return completed.returncode == 0


def app_container(xcrun: str, udid: str, bundle_id: str, kind: str = "data") -> Path | None:
    completed = run_simctl(xcrun, ["get_app_container", udid, bundle_id, kind], timeout=30, check=False)
    if completed.returncode != 0:
        return None
    value = (completed.stdout or "").strip()
    # simctl prints the literal "(null)" with exit 0 for an app that has no
    # such container — system apps have no data container — and that string
    # used to become a Path that "exists" nowhere, read as an empty listing.
    if not value or value == "(null)":
        return None
    return Path(value)


# --- the app's executable and installed bundle ------------------------------------
#
# The log predicate matches the app's process by image path. The bundle id's
# last component is only a guess at that name, and for a Flutter app it is
# always wrong: every Flutter iOS app's executable is `Runner`, so a live
# Flutter session (`com.example.knit`) filtered on "knit" and returned no
# line at all. The executable name alone is not enough either — every other
# Flutter app on the simulator is a `Runner` too — so where the app's
# installed bundle is known the predicate matches that path exactly.
#
# The executable is CFBundleExecutable in the app's Info.plist: read from the
# recorded install .app when one is given (a local file, never cached), else
# from `simctl appinfo`, else from the installed bundle. The installed bundle
# path is appinfo's `Path`, else `get_app_container <udid> <bundle> app`. The
# recorded install path is the build output, NOT where the process runs
# from, so it never feeds the path match.
#
# Every lookup here is best-effort and never raises: a timeout, a missing
# tool, or a malformed plist degrades the filter, it must never fail (or roll
# back) the verb that asked.

_EXECUTABLE_CACHE: dict[tuple[str, str, str], str] = {}
_BUNDLE_PATH_CACHE: dict[tuple[str, str, str], str] = {}
PROBE_TIMEOUT = 30


def _appinfo_pattern(key: str) -> re.Pattern[str]:
    return re.compile(
        r'^\s*"?' + re.escape(key) + r'"?\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;\s]+))\s*;', re.M)


_APPINFO_EXECUTABLE = _appinfo_pattern("CFBundleExecutable")
_APPINFO_IDENTIFIER = _appinfo_pattern("CFBundleIdentifier")
_APPINFO_PATH = _appinfo_pattern("Path")


def _probe(xcrun: str, args: Sequence[str], *,
           timeout: float | None = None) -> tuple[subprocess.CompletedProcess | None, str | None]:
    """Run a read-only simctl query without letting it raise.

    Returns ``(completed, None)`` on success, else ``(completed or None,
    reason)``: a timeout (``PROBE_TIMEOUT`` seconds unless given), a missing
    tool, or a non-zero exit, in words a payload can carry."""
    timeout = PROBE_TIMEOUT if timeout is None else timeout
    try:
        completed = run_simctl(xcrun, list(args), timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None, f"simctl {args[0]} timed out after {timeout:g}s"
    except errors.AutonomError as exc:
        return None, exc.message
    except OSError as exc:
        return None, f"simctl {args[0]} could not run: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()[-200:]
        return completed, detail or f"simctl {args[0]} failed ({completed.returncode})"
    return completed, None


def _sane_executable(value: Any) -> str | None:
    """A CFBundleExecutable fit for a predicate: a plain file name."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name) > 255 or "/" in name or any(ch in name for ch in "\r\n\0"):
        return None
    return name


def bundle_executable(app_path: Path | str, bundle_id: str | None = None) -> str | None:
    """CFBundleExecutable from ``<app>/Info.plist`` (XML or binary, via
    plistlib), or None. With ``bundle_id``, the plist must name exactly that
    identifier: another app's bundle, or one that names none, lends nothing.
    Any read or parse failure is None — a malformed XML plist raises
    ExpatError, which is none of plistlib's own exception types."""
    try:
        with (Path(app_path).expanduser() / "Info.plist").open("rb") as handle:
            data = plistlib.load(handle)
    except Exception:  # noqa: BLE001 - never raises, see the section note
        return None
    if not isinstance(data, dict):
        return None
    if bundle_id and data.get("CFBundleIdentifier") != bundle_id:
        return None
    return _sane_executable(data.get("CFBundleExecutable"))


def _appinfo_value(text: str, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(text or "")
    if not match:
        return None
    return match.group(1) if match.group(1) is not None else match.group(2)


def parse_appinfo_executable(text: str, bundle_id: str | None = None) -> str | None:
    """CFBundleExecutable from `simctl appinfo`'s OpenStep-style listing."""
    if bundle_id:
        ident = _appinfo_value(text, _APPINFO_IDENTIFIER)
        if ident is not None and ident != bundle_id:
            return None
    return _sane_executable(_appinfo_value(text, _APPINFO_EXECUTABLE))


def _installed_bundle(value: str | None) -> str | None:
    """An installed .app path worth matching: absolute, and a directory on
    this host (simulator containers live on the host's disk)."""
    if not value:
        return None
    value = value.strip()
    if not value or value == "(null)" or not value.startswith("/"):
        return None
    path = value.rstrip("/")
    try:
        return path if os.path.isdir(path) else None
    except (OSError, ValueError):
        return None


def _lookup_installed(xcrun: str, udid: str, bundle_id: str) -> tuple[str | None, str | None]:
    """(executable, installed bundle path) from the device, best-effort."""
    executable = path = None
    completed, reason = _probe(xcrun, ["appinfo", udid, bundle_id])
    if reason is None and completed is not None:
        text = completed.stdout or ""
        ident = _appinfo_value(text, _APPINFO_IDENTIFIER)
        if ident is None or ident == bundle_id:
            executable = parse_appinfo_executable(text, bundle_id)
            path = _installed_bundle(_appinfo_value(text, _APPINFO_PATH))
    # A simulator that timed out once is not asked twice in a row.
    if not path and "timed out" not in (reason or ""):
        completed, reason = _probe(xcrun, ["get_app_container", udid, bundle_id, "app"])
        if reason is None and completed is not None:
            path = _installed_bundle(completed.stdout)
    if path and not executable:
        executable = bundle_executable(path, bundle_id)
    return executable, path


def app_image(xcrun: str, udid: str, bundle_id: str, *,
              app_path: Path | str | None = None) -> tuple[str | None, str | None]:
    """``(executable, installed bundle path)`` for ``bundle_id``, either
    possibly None. Never raises.

    Device answers are cached per (xcrun, udid, bundle id) once found; a
    miss is not cached (an app installed later is still found), and a cached
    bundle path that no longer exists — a reinstall moves the app to a new
    container — is looked up again. ``app_path`` is read every time."""
    if not bundle_id:
        return None, None
    key = (str(xcrun), udid, bundle_id)
    executable = bundle_executable(app_path, bundle_id) if app_path else None
    executable = executable or _EXECUTABLE_CACHE.get(key)
    path = _BUNDLE_PATH_CACHE.get(key)
    if path and _installed_bundle(path) is None:
        _BUNDLE_PATH_CACHE.pop(key, None)
        path = None
    if executable and path:
        return executable, path
    found_executable, found_path = _lookup_installed(xcrun, udid, bundle_id)
    if found_executable:
        _EXECUTABLE_CACHE[key] = found_executable
    if found_path:
        _BUNDLE_PATH_CACHE[key] = found_path
    return executable or found_executable, path or found_path


def app_executable(xcrun: str, udid: str, bundle_id: str, *,
                   app_path: Path | str | None = None) -> str | None:
    """The app's executable name (``Runner`` for any Flutter app), or None.
    Never raises; see ``app_image`` for the sources and the cache."""
    if not bundle_id:
        return None
    if app_path:
        name = bundle_executable(app_path, bundle_id)
        if name:
            return name
    cached = _EXECUTABLE_CACHE.get((str(xcrun), udid, bundle_id))
    if cached:
        return cached
    return app_image(xcrun, udid, bundle_id)[0]


def app_bundle_path(xcrun: str, udid: str, bundle_id: str) -> str | None:
    """Where the installed app lives on this host (``…/Containers/Bundle/
    Application/<uuid>/Runner.app``), or None. Never raises."""
    return app_image(xcrun, udid, bundle_id)[1]


def reset_caches() -> None:
    """Forget the per-process app and privacy-service probes (tests, or
    after an Xcode switch)."""
    _EXECUTABLE_CACHE.clear()
    _BUNDLE_PATH_CACHE.clear()
    _PRIVACY_CACHE.clear()


# --- device state ------------------------------------------------------------

# The fallback when the running simctl's own list cannot be read. The live
# list wins: Xcode 27 has no `camera` or `userTracking` service, and sending
# one answered backend_failed with simctl's usage text.
PRIVACY_SERVICES = (
    "all", "calendar", "contacts-limited", "contacts", "location", "location-always",
    "photos-add", "photos", "media-library", "microphone", "motion", "reminders",
    "siri", "camera", "userTracking",
)
_PRIVACY_CACHE: dict[str, tuple[str, ...] | None] = {}
_PRIVACY_ITEM = re.compile(r"^\s+([A-Za-z][A-Za-z0-9._+-]*)\s+-\s+\S")
_PRIVACY_SECTIONS = {"action", "service", "bundle identifier"}


def parse_privacy_services(help_text: str) -> tuple[str, ...]:
    """The service names under `simctl help privacy`'s ``service`` heading
    (the ``action`` heading's grant/revoke/reset have the same shape, so the
    section is tracked, never pattern-matched alone)."""
    services: list[str] = []
    section = None
    for line in (help_text or "").splitlines():
        stripped = line.strip()
        if stripped in _PRIVACY_SECTIONS:
            section = stripped
            continue
        if not line[:1].isspace() and stripped:
            section = None  # "Examples:", "Warning:", the usage line
            continue
        if section == "service":
            match = _PRIVACY_ITEM.match(line)
            if match and match.group(1) not in services:
                services.append(match.group(1))
    return tuple(services)


def privacy_services(xcrun: str) -> tuple[tuple[str, ...], str]:
    """``(services, source)``: this simctl's own list (``simctl``), parsed
    once per process, or the static fallback (``static``). `simctl help`
    prints to stderr and touches no device."""
    key = str(xcrun)
    if key not in _PRIVACY_CACHE:
        parsed: tuple[str, ...] | None = None
        # A timeout or a missing tool falls back to the static list (and is
        # remembered, so a wedged simctl is not waited on twice).
        completed, reason = _probe(xcrun, ["help", "privacy"])
        if reason is None and completed is not None:
            parsed = parse_privacy_services((completed.stdout or "") + "\n"
                                            + (completed.stderr or "")) or None
        _PRIVACY_CACHE[key] = parsed
    live = _PRIVACY_CACHE[key]
    return (live, "simctl") if live else (PRIVACY_SERVICES, "static")


def check_privacy_service(xcrun: str, service: str) -> str:
    """Refuse a service before the device is touched: one this simctl does
    not have (but the static list knows) is ``invalid_value``; a name nobody
    knows keeps ``unknown_privacy_service``. Both list what is supported."""
    supported, source = privacy_services(xcrun)
    if service in supported:
        return source
    hint = "Supported services: " + ", ".join(supported) + "."
    if service in PRIVACY_SERVICES:
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            f"privacy service {service!r} is not supported by this Xcode's simctl",
            hint, service=service, supported_services=list(supported))
    raise errors.AutonomError(
        errors.UNKNOWN_PRIVACY_SERVICE,
        f"unknown privacy service: {service}",
        hint, service=service, supported_services=list(supported))


def openurl(xcrun: str, udid: str, url: str) -> None:
    run_simctl(xcrun, ["openurl", udid, url], timeout=30)


def privacy(xcrun: str, udid: str, action: str, service: str, bundle_id: str | None = None) -> None:
    check_privacy_service(xcrun, service)
    args = ["privacy", udid, action, service]
    if bundle_id:
        args.append(bundle_id)
    run_simctl(xcrun, args, timeout=30)


# --- read-backs for the simulator controls -----------------------------------------
#
# `simctl status_bar <udid> list` prints only the overrides that are set:
#
#     Current Status Bar Overrides:
#     =============================
#     Time: 9:41
#     DataNetworkType: 11
#     WiFi Mode: 3, WiFi Bars: 3
#     Cell Mode: 3, Cell Bars: 4
#     Operator Name: Carrier
#     Battery State: 2, Battery Level: 100, Not Charging: 0
#
# (format strings and the "print only when set" branches read from Xcode
# 27.0's simctl). The enumerations come back as the raw values simctl's own
# `override` parser assigns to each name, so they are mapped back here; a
# build that prints the name instead is accepted too.

IOS_STATUS_BAR_ENUMS: dict[str, dict[str, int]] = {
    "dataNetwork": {"hide": 0, "wifi": 1, "3g": 6, "4g": 7, "lte": 8, "lte-a": 9,
                    "lte+": 10, "5g": 11, "5g+": 12, "5g-uwb": 13, "5g-uc": 14},
    "wifiMode": {"searching": 1, "failed": 2, "active": 3},
    "cellularMode": {"notSupported": 0, "searching": 1, "failed": 2, "active": 3},
    "batteryState": {"discharging": 0, "charging": 1, "charged": 2},
}
_STATUS_BAR_HEADER = "Current Status Bar Overrides"
_STATUS_BAR_FIELDS = {
    "time": "time", "datanetworktype": "dataNetwork", "wifi mode": "wifiMode",
    "wifi bars": "wifiBars", "cell mode": "cellularMode", "cell bars": "cellularBars",
    "cellular mode": "cellularMode", "cellular bars": "cellularBars",
    "operator name": "operatorName", "battery state": "batteryState",
    "battery level": "batteryLevel", "not charging": "notCharging",
}
_UNSET = {"", "(null)", "null", "<null>"}


def parse_status_bar_list(text: str) -> dict[str, str] | None:
    """The overrides `status_bar list` reports, keyed by the override flag
    name (``batteryLevel``, ``wifiMode``, ...), values as printed. None when
    the output is not a status-bar listing at all."""
    lines = (text or "").splitlines()
    if not any(_STATUS_BAR_HEADER in line for line in lines):
        return None
    overrides: dict[str, str] = {}
    for line in lines:
        if _STATUS_BAR_HEADER in line or set(line.strip()) <= {"="}:
            continue
        # "Time:" takes the whole rest of the line (a clock contains ':');
        # the other lines are comma-separated "Label: value" pairs.
        head, sep, rest = line.partition(":")
        if not sep:
            continue
        if head.strip().lower() in ("time", "operator name"):
            pairs = [(head, rest)]
        else:
            pairs = [part.partition(":")[::2] for part in line.split(",")]
        for label, value in pairs:
            key = _STATUS_BAR_FIELDS.get(label.strip().lower())
            if key is None:
                continue
            value = value.strip()
            # An explicit empty carrier name ('') is an override; "(null)"
            # is not, whatever the key.
            if value.lower() in _UNSET and not (key == "operatorName" and value == ""):
                continue
            overrides[key] = value
    return overrides


def status_bar_value_matches(key: str, expected: Any, observed: str | None) -> bool:
    """One override read back: ints compare as ints, the enumerations by
    name or by simctl's raw value, the free-text keys exactly."""
    if observed is None:
        return False
    wanted = str(expected).strip()
    if key in IOS_STATUS_BAR_ENUMS:
        table = IOS_STATUS_BAR_ENUMS[key]
        if observed.strip().lstrip("-").isdigit():
            return table.get(wanted) == int(observed)
        return observed.strip().lower() == wanted.lower()
    if key in ("batteryLevel", "wifiBars", "cellularBars"):
        try:
            return int(observed) == int(wanted)
        except ValueError:
            return False
    return observed == wanted


def read_status_bar(xcrun: str, udid: str) -> tuple[dict[str, str] | None, str | None]:
    """``(overrides, None)``, or ``(None, reason)`` when they cannot be
    listed (a timeout, a failure, output that is not a listing). Never
    raises."""
    completed, reason = _probe(xcrun, ["status_bar", udid, "list"])
    if reason is not None or completed is None:
        return None, reason or "simctl status_bar list did not run"
    overrides = parse_status_bar_list(completed.stdout or "")
    if overrides is None:
        return None, "simctl status_bar list printed no override listing"
    return overrides, None


def status_bar_overrides(xcrun: str, udid: str) -> dict[str, str] | None:
    """The live overrides, or None when they cannot be listed."""
    return read_status_bar(xcrun, udid)[0]


def read_notify_state(xcrun: str, udid: str, name: str) -> tuple[int | None, str | None]:
    """``(state, None)`` for a Darwin notification inside the simulator
    (`notifyutil -g <name>` prints ``<name> <value>``), or ``(None,
    reason)``. Never raises."""
    completed, reason = _probe(xcrun, ["spawn", udid, "notifyutil", "-g", name])
    if reason is not None or completed is None:
        return None, reason or "notifyutil -g did not run"
    for line in (completed.stdout or "").splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == name and fields[-1].lstrip("-").isdigit():
            return int(fields[-1]), None
    return None, f"notifyutil -g printed no state for {name}"


def notify_state(xcrun: str, udid: str, name: str) -> int | None:
    """The notification's state value, or None."""
    return read_notify_state(xcrun, udid, name)[0]


def read_pasteboard(xcrun: str, udid: str) -> tuple[str | None, str | None]:
    """``(text, None)`` from `simctl pbpaste`, or ``(None, reason)``. Never
    raises."""
    completed, reason = _probe(xcrun, ["pbpaste", udid])
    if reason is not None or completed is None:
        return None, reason or "simctl pbpaste did not run"
    return completed.stdout or "", None


def pasteboard(xcrun: str, udid: str) -> str | None:
    """The simulator pasteboard's text, or None when it cannot be read."""
    return read_pasteboard(xcrun, udid)[0]


def set_location(xcrun: str, udid: str, latitude: float, longitude: float) -> None:
    run_simctl(xcrun, ["location", udid, "set", f"{latitude},{longitude}"], timeout=30)


def clear_location(xcrun: str, udid: str) -> None:
    run_simctl(xcrun, ["location", udid, "clear"], timeout=30, check=False)


def add_media(xcrun: str, udid: str, path: Path) -> None:
    if not path.exists():
        raise errors.AutonomError(
            errors.INSTALL_PATH_NOT_FOUND,
            f"media file not found: {path}",
            "Pass an existing image or video path.",
        )
    run_simctl(xcrun, ["addmedia", udid, str(path)], timeout=60)


def screenshot(xcrun: str, udid: str, output: Path) -> Path:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    run_simctl(xcrun, ["io", udid, "screenshot", str(output)], timeout=60)
    return output


# --- log stream ----------------------------------------------------------------

LOG_MAX_MB_ENV = "AUTONOM_IOS_LOG_MAX_MB"
DEFAULT_LOG_MAX_MB = 50

# The writer that sits between `log stream` and the artifacts file. An
# unfiltered simulator stream was measured at 806 MB in eight minutes, so the
# stream is never written straight to disk: this process owns the child, caps
# the file, and rotates once to `<file>.1`. It is argv-only stdlib Python run
# with the current interpreter, so no shell is involved. SIGTERM (what
# `session stop` sends to the recorded pid) stops the child with it.
_BOUNDED_WRITER = r"""
import os, signal, subprocess, sys
dest, cap, argv = sys.argv[1], max(1, int(sys.argv[2])), sys.argv[3:]
child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
def stop(*_):
    try:
        child.terminate()
    except OSError:
        pass
    # A stop between linking the rotation's temp name and renaming it over
    # `.1` leaves a second name for `dest`'s own bytes; drop it. Nothing is
    # lost: `dest` and the previous `.1` are both intact at that point.
    try:
        os.remove(dest + ".1.tmp")
    except OSError:
        pass
    os._exit(0)
for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, stop)
out = open(dest, "ab")
size = out.tell()
for line in iter(child.stdout.readline, b""):
    # `log stream` opens with a plain-text banner that quotes the predicate
    # (and so the bundle id): not a record, and a package filter over the
    # file would pass it as one. `log show` ends with a count trailer.
    head = line.lstrip()[:64]
    if head.startswith(b"Filtering the log data") or (
            head.startswith(b'{"count"') and b'"finished"' in line and len(line) < 80):
        continue
    line = line[:cap]
    if size + len(line) > cap:
        out.close()
        # Link `dest` to a temp name and rename that over `.1`, then swap in
        # an empty `dest`. Each step is atomic, so neither `dest` nor an
        # existing `.1` ever vanishes: a reader polling `dest` never sees a
        # missing file, and a SIGTERM (`session stop`) at any step keeps the
        # previous rotation. Removing `.1` before linking it used to lose a
        # full cap of evidence when the stop landed between the two calls.
        try:
            if os.path.lexists(dest + ".1.tmp"):
                os.remove(dest + ".1.tmp")
            os.link(dest, dest + ".1.tmp")
            os.replace(dest + ".1.tmp", dest + ".1")
            open(dest + ".new", "wb").close()
            os.replace(dest + ".new", dest)
        except OSError:
            os.replace(dest, dest + ".1")
        out = open(dest, "ab")
        size = 0
    out.write(line)
    out.flush()
    size += len(line)
out.close()
child.wait()
"""


def log_max_bytes() -> int:
    """The per-file cap, from AUTONOM_IOS_LOG_MAX_MB (fractions allowed)."""
    raw = os.environ.get(LOG_MAX_MB_ENV)
    try:
        megabytes = float(raw) if raw else DEFAULT_LOG_MAX_MB
    except ValueError:
        megabytes = DEFAULT_LOG_MAX_MB
    if megabytes <= 0:
        megabytes = DEFAULT_LOG_MAX_MB
    return max(1, int(megabytes * 1024 * 1024))


# Last bundle-id components that name a kind of thing, not an app: a
# `CONTAINS "app"` predicate matches nearly every process image path on the
# simulator (every `*.app` bundle), so such a leaf is never used alone.
GENERIC_BUNDLE_LEAVES = frozenset({
    "app", "apps", "ios", "iosapp", "mobile", "client", "main", "prod",
    "release", "debug", "dev", "staging", "beta", "test", "demo", "application",
})


def log_leaf(bundle_id: str) -> str | None:
    """The bundle id's last component when it is distinctive enough to match
    a process image by substring; None when it is generic (``app``, ``ios``,
    ``mobile``, ...) or shorter than 3 characters."""
    leaf = bundle_id.rsplit(".", 1)[-1]
    if len(leaf) < 3 or leaf.lower() in GENERIC_BUNDLE_LEAVES:
        return None
    return leaf


def _quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def bundle_prefixes(bundle_path: str) -> list[str]:
    """``<bundle>/`` as given and, when it differs, with symlinks resolved
    (`/tmp` is `/private/tmp` on macOS): the forms a record's image path may
    take. The trailing slash keeps `Runner.app` from matching `Runner.app2`."""
    base = str(bundle_path).rstrip("/")
    forms = [base]
    try:
        real = os.path.realpath(base)
    except (OSError, ValueError):
        real = base
    if real != base:
        forms.append(real)
    return [form + "/" for form in forms]


def log_predicate(bundle_id: str, *, executable: str | None = None,
                  bundle_path: str | None = None) -> str:
    """The app's own subsystem, or a process/sender image that is the app.

    The image clauses are as exact as what is known: the installed bundle
    (``BEGINSWITH "<bundle>/"``: the app's own process and the frameworks it
    ships, and no other app, even another Flutter `Runner`), else the
    executable (``ENDSWITH "/Runner"``: any app with that executable name),
    else the bundle id's last component — only when it is distinctive
    (``log_leaf``). For ``com.example.app`` with nothing known the predicate
    is the subsystem alone: exact, possibly narrower, never "every process
    whose path contains app"."""
    clauses = [f'subsystem == "{_quoted(bundle_id)}"']
    if bundle_path:
        for prefix in bundle_prefixes(bundle_path):
            clauses += [f'processImagePath BEGINSWITH "{_quoted(prefix)}"',
                        f'senderImagePath BEGINSWITH "{_quoted(prefix)}"']
    elif executable:
        clauses += [f'processImagePath ENDSWITH "/{_quoted(executable)}"',
                    f'senderImagePath ENDSWITH "/{_quoted(executable)}"']
    else:
        leaf = log_leaf(bundle_id)
        if leaf:
            clauses += [f'processImagePath CONTAINS "{_quoted(leaf)}"',
                        f'senderImagePath CONTAINS "{_quoted(leaf)}"']
    return " OR ".join(clauses)


def app_log_predicate(xcrun: str, udid: str, bundle_id: str, *,
                      app_path: Path | str | None = None) -> str:
    """``log_predicate`` for the installed app, resolved now (``app_image``).
    Never raises; with nothing resolvable it is the subsystem/leaf form."""
    executable, bundle_path = app_image(xcrun, udid, bundle_id, app_path=app_path)
    return log_predicate(bundle_id, executable=executable, bundle_path=bundle_path)


_LOG_BANNER = "Filtering the log data"


def is_log_noise(line: str) -> bool:
    """True for the two lines `log` prints that are not log records: the
    `log stream` banner (``Filtering the log data using "<predicate>"``)
    and the `log show --style ndjson` trailer (``{"count":0,"finished":1}``).
    Returning either as a log line answered "one line" for an app that had
    logged nothing."""
    text = (line or "").strip()
    if text.startswith(_LOG_BANNER):
        return True
    if not (text.startswith("{") and '"finished"' in text):
        return False
    try:
        record = json.loads(text)
    except ValueError:
        return False
    return isinstance(record, dict) and set(record) <= {"count", "finished"}


def log_line_matches(line: str, bundle_id: str, *, executable: str | None = None,
                     bundle_path: str | None = None) -> bool:
    """Client-side filter mirroring ``log_predicate``: the app's subsystem or
    full bundle id, then its image — under the installed bundle when that is
    known (another app's `/Runner.app/Runner` is rejected), else named
    ``executable``, else containing the distinctive last component."""
    if is_log_noise(line):
        return False
    if bundle_id in line:
        return True
    try:
        record = json.loads(line)
    except ValueError:
        record = None
    if isinstance(record, dict):
        if record.get("subsystem") == bundle_id:
            return True
        images = [str(record.get(key) or "") for key in ("processImagePath", "senderImagePath")]
        if bundle_path:
            prefixes = bundle_prefixes(bundle_path)
            return any(image.startswith(prefix) for image in images for prefix in prefixes)
        if executable:
            return any(image.endswith("/" + executable) for image in images)
        leaf = log_leaf(bundle_id)
        return bool(leaf) and any(leaf in image for image in images)
    # Not ndjson: only a substring can decide.
    if bundle_path:
        return any(prefix in line for prefix in bundle_prefixes(bundle_path))
    if executable:
        return executable in line
    leaf = log_leaf(bundle_id)
    return bool(leaf) and leaf in line


def app_log_filter(xcrun: str, udid: str, bundle_id: str, *,
                   app_path: Path | str | None = None) -> Any:
    """A ``line -> bool`` filter for the installed app, resolved once now
    (``app_image``). Never raises."""
    executable, bundle_path = app_image(xcrun, udid, bundle_id, app_path=app_path)

    def matches(line: str) -> bool:
        return log_line_matches(line, bundle_id, executable=executable,
                                bundle_path=bundle_path)

    return matches


def log_stream_argv(xcrun: str, udid: str, *, bundle_id: str | None = None,
                    executable: str | None = None,
                    bundle_path: str | None = None) -> list[str]:
    argv = [xcrun, "simctl", "spawn", udid, "log", "stream",
            "--style", "ndjson", "--level", "info"]
    if bundle_id:
        argv += ["--predicate", log_predicate(bundle_id, executable=executable,
                                              bundle_path=bundle_path)]
    return argv


def spawn_bounded(argv: Sequence[str], destination: Path, *,
                  max_bytes: int | None = None) -> subprocess.Popen:
    """Run ``argv`` with its stdout written to ``destination``, capped.

    The file never exceeds ``max_bytes``; when the next line would push it
    over, the file is moved to ``<destination>.1`` (replacing any earlier
    rotation) and a fresh one starts, so at most twice the cap is on disk.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    cap = max_bytes if max_bytes is not None else log_max_bytes()
    return subprocess.Popen(  # noqa: S603 - argv is constructed, never shell
        [sys.executable, "-c", _BOUNDED_WRITER, str(destination), str(int(cap)), *argv],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def start_log_stream(xcrun: str, udid: str, destination: Path, *,
                     bundle_id: str | None = None,
                     executable: str | None = None,
                     app_path: Path | str | None = None,
                     bundle_path: str | None = None,
                     max_bytes: int | None = None) -> int | None:
    """Session-long ndjson `log stream`, filtered to the app when known and
    capped on disk. Returns the writer pid (stopping it stops the stream), or
    None when it could not start — logs are supplementary evidence and must
    never block the UI loop, so nothing here raises.

    What is not passed is resolved (``app_image``; ``app_path`` only helps
    find the executable name): with the installed bundle known, the stream
    is exactly this app. The predicate is fixed when the stream starts, so a
    reinstall (which moves the app to a new container) needs a new stream
    to keep the path clauses; the subsystem clause keeps matching."""
    if bundle_id and not bundle_path:
        found_executable, bundle_path = app_image(xcrun, udid, bundle_id, app_path=app_path)
        executable = executable or found_executable
    try:
        process = spawn_bounded(log_stream_argv(xcrun, udid, bundle_id=bundle_id,
                                                executable=executable,
                                                bundle_path=bundle_path),
                                destination, max_bytes=max_bytes)
    except OSError:
        return None
    return process.pid


# --- toolchain ---------------------------------------------------------------

# Xcode 27 moved SimulatorKit out of the Developer directory. idb_companion
# builds before 1.6.2 only know the legacy location and fail every HID call.
SIMULATORKIT_LEGACY = Path("Library/PrivateFrameworks/SimulatorKit.framework")
SIMULATORKIT_SHARED = Path("SharedFrameworks/SimulatorKit.framework")


def developer_dir() -> str | None:
    """DEVELOPER_DIR when set, else `xcode-select -p`; None when neither answers."""
    configured = os.environ.get("DEVELOPER_DIR")
    if configured:
        return configured
    try:
        completed = subprocess.run(
            ["xcode-select", "-p"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (completed.stdout or "").strip()
    return value if completed.returncode == 0 and value else None


def _major(version: str | None) -> int | None:
    match = re.match(r"(\d+)", version or "")
    return int(match.group(1)) if match else None


def xcode_info(developer: str | None = None) -> dict[str, Any]:
    """Where Xcode is, which version it is, and where SimulatorKit lives.

    Read from disk only (version.plist via plistlib), so it is cheap and never
    touches a simulator.
    """
    developer = developer if developer is not None else developer_dir()
    info: dict[str, Any] = {
        "developer_dir": developer, "xcode_version": None, "xcode_build": None,
        "xcode_major": None, "simulatorkit_path": None,
        "simulatorkit_legacy": False, "simulatorkit_shared": False,
    }
    if not developer:
        return info
    dev = Path(developer)
    contents = dev.parent  # <Xcode.app>/Contents/Developer -> Contents
    plist = contents / "version.plist"
    try:
        with plist.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        data = {}
    if isinstance(data, dict):
        info["xcode_version"] = data.get("CFBundleShortVersionString") or None
        info["xcode_build"] = data.get("ProductBuildVersion") or None
    info["xcode_major"] = _major(info["xcode_version"])
    legacy = dev / SIMULATORKIT_LEGACY
    shared = contents / SIMULATORKIT_SHARED
    info["simulatorkit_legacy"] = legacy.is_dir()
    info["simulatorkit_shared"] = shared.is_dir()
    if legacy.is_dir():
        info["simulatorkit_path"] = str(legacy)
    elif shared.is_dir():
        info["simulatorkit_path"] = str(shared)
    return info
