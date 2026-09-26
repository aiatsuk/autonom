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
    os._exit(0)
for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, stop)
out = open(dest, "ab")
size = out.tell()
for line in iter(child.stdout.readline, b""):
    line = line[:cap]
    if size + len(line) > cap:
        out.close()
        # Link, then atomically swap in an empty file: `dest` never vanishes,
        # so a reader polling it never sees a missing file.
        try:
            if os.path.lexists(dest + ".1"):
                os.remove(dest + ".1")
            os.link(dest, dest + ".1")
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


def log_predicate(bundle_id: str, *, executable: str | None = None) -> str:
    """The app's own subsystem, or a process/sender image named after it.

    The image-path clauses use the app's executable name when it is known,
    else the bundle id's last component — but only when that component is
    distinctive (``log_leaf``). For ``com.example.app`` with no known
    executable the predicate is the subsystem alone: exact, possibly
    narrower, never "every process whose path contains app".
    """
    clauses = [f'subsystem == "{_quoted(bundle_id)}"']
    name = executable or log_leaf(bundle_id)
    if name:
        clauses += [f'processImagePath CONTAINS "{_quoted(name)}"',
                    f'senderImagePath CONTAINS "{_quoted(name)}"']
    return " OR ".join(clauses)


def log_stream_argv(xcrun: str, udid: str, *, bundle_id: str | None = None,
                    executable: str | None = None) -> list[str]:
    argv = [xcrun, "simctl", "spawn", udid, "log", "stream",
            "--style", "ndjson", "--level", "info"]
    if bundle_id:
        argv += ["--predicate", log_predicate(bundle_id, executable=executable)]
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
                     max_bytes: int | None = None) -> int | None:
    """Session-long ndjson `log stream`, filtered to the app when known and
    capped on disk. Returns the writer pid (stopping it stops the stream), or
    None when it could not start — logs are supplementary evidence and must
    never block the UI loop."""
    try:
        process = spawn_bounded(log_stream_argv(xcrun, udid, bundle_id=bundle_id,
                                                executable=executable),
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
