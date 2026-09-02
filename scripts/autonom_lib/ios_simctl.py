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
import re
import shutil
import subprocess
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


def uninstall(xcrun: str, udid: str, bundle_id: str) -> None:
    run_simctl(xcrun, ["uninstall", udid, bundle_id], timeout=60, check=False)


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


# --- device state ------------------------------------------------------------

PRIVACY_SERVICES = (
    "all", "calendar", "contacts-limited", "contacts", "location", "location-always",
    "photos-add", "photos", "media-library", "microphone", "motion", "reminders",
    "siri", "camera", "userTracking",
)


def openurl(xcrun: str, udid: str, url: str) -> None:
    run_simctl(xcrun, ["openurl", udid, url], timeout=30)


def privacy(xcrun: str, udid: str, action: str, service: str, bundle_id: str | None = None) -> None:
    if service not in PRIVACY_SERVICES:
        raise errors.AutonomError(
            errors.UNKNOWN_PRIVACY_SERVICE,
            f"unknown privacy service: {service}",
            "Valid services: " + ", ".join(PRIVACY_SERVICES),
        )
    args = ["privacy", udid, action, service]
    if bundle_id:
        args.append(bundle_id)
    run_simctl(xcrun, args, timeout=30)


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
