"""Android emulator (AVD) lifecycle: discover the binary, list, boot, kill.

The `emulator` binary ships with the Android SDK but is rarely on PATH.
Discovery mirrors how the SDK is actually installed: an explicit override
first, then the SDK the local adb belongs to, then the well-known SDK roots,
then PATH. A missing binary is an expected condition (`emulator_not_found`),
not a crash — `devices` simply omits the AVD list then.

Shutdown refuses anything that is not an `emulator-<port>` serial: `adb emu
kill` on hardware is impossible, and powering off a person's phone would be
the wrong kind of surprise anyway.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import adb as adb_mod
from . import errors
from . import processes

AVD_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
EMULATOR_SERIAL = re.compile(r"^emulator-\d+$")


def find_emulator(explicit: str | None = None, *, adb_path: str | None = None) -> str:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("AUTONOM_EMULATOR")
    if env:
        candidates.append(env)
    if adb_path:
        sdk = Path(adb_path).resolve().parent.parent
        candidates.append(str(sdk / "emulator" / "emulator"))
    for root_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(root_var)
        if root:
            candidates.append(str(Path(root) / "emulator" / "emulator"))
    candidates.append(str(Path.home() / "Library/Android/sdk/emulator/emulator"))
    on_path = shutil.which("emulator")
    if on_path:
        candidates.append(on_path)
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise errors.AutonomError(
        errors.EMULATOR_NOT_FOUND,
        "Android `emulator` binary not found",
        "Install the SDK 'emulator' package (Android Studio does), or set AUTONOM_EMULATOR.",
    )


def list_avds(emulator_bin: str) -> list[str]:
    completed = subprocess.run(
        [emulator_bin, "-list-avds"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    names: list[str] = []
    for line in (completed.stdout or "").splitlines():
        line = line.strip()
        # The emulator interleaves INFO chatter with names; AVD names cannot
        # contain spaces, so anything else is noise.
        if line and AVD_NAME.match(line):
            names.append(line)
    return names


# --- AVD hardware profiles --------------------------------------------------
#
# `-list-avds` gives names only. Which of them is the phone with the 1080x2400
# screen, or the tablet, lives in each AVD's `config.ini` — and an agent asked
# to "test on a phone-sized emulator" has to read it, or guess from the name.

_INI_LINE = re.compile(r"^\s*([^#=\s][^=]*?)\s*=\s*(.*?)\s*$")


def avd_home() -> Path:
    """Where AVDs live: `$ANDROID_AVD_HOME`, else `~/.android/avd`."""
    override = os.environ.get("ANDROID_AVD_HOME")
    return Path(override).expanduser() if override else Path.home() / ".android" / "avd"


def _read_ini(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _INI_LINE.match(line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def _as_int(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def describe_avd(name: str) -> dict[str, Any]:
    """The hardware profile behind one AVD, from its ini files.

    `<home>/<name>.ini` holds the API target and the path of the `.avd`
    directory (AVDs can live outside the home); `<dir>/config.ini` holds the
    `hw.*` profile. Anything unreadable is reported as null, never guessed.
    """
    home = avd_home()
    pointer = _read_ini(home / f"{name}.ini")
    directory = Path(pointer["path"]).expanduser() if pointer.get("path") else home / f"{name}.avd"
    config = _read_ini(directory / "config.ini")
    width = _as_int(config.get("hw.lcd.width"))
    height = _as_int(config.get("hw.lcd.height"))
    api = None
    target = pointer.get("target") or config.get("image.sysdir.1") or ""
    match = re.search(r"android-(\d+)", target)
    if match:
        api = int(match.group(1))
    return {
        "name": name,
        "device": config.get("hw.device.name") or None,
        "screen": (
            {"width": width, "height": height,
             "density": _as_int(config.get("hw.lcd.density"))}
            if width and height else None
        ),
        "api": api,
        "abi": config.get("abi.type") or None,
        "path": str(directory) if config else None,
    }


def describe_avds(names: list[str]) -> list[dict[str, Any]]:
    return [describe_avd(name) for name in names]


def running_avd_name(adb_path: str, serial: str) -> str | None:
    """The AVD a running emulator was booted from, via its console.

    `adb emu avd name` answers only on `emulator-<port>` serials; hardware is
    skipped without an adb call. Best-effort: None when the console is
    unreachable, so the inventory never fails on it.
    """
    if not EMULATOR_SERIAL.match(serial):
        return None
    try:
        completed = adb_mod.run_adb(
            adb_path, ["emu", "avd", "name"], serial=serial, timeout=10, check=False,
        )
    except errors.AutonomError:
        return None
    output = completed.stdout if isinstance(completed.stdout, str) else ""
    for line in output.splitlines():
        line = line.strip()
        if line and line not in ("OK", "KO") and not line.startswith("KO:") and AVD_NAME.match(line):
            return line
    return None


def boot_avd(
    emulator_bin: str,
    adb_path: str,
    name: str,
    *,
    wait: bool = True,
    timeout: float = 180.0,
) -> dict[str, Any]:
    known = list_avds(emulator_bin)
    if name not in known:
        raise errors.AutonomError(
            errors.AVD_NOT_FOUND,
            f"AVD '{name}' does not exist",
            f"Available: {', '.join(known) if known else 'none — create one in Android Studio'}.",
        )
    before = {device.serial for device in adb_mod.list_devices(adb_path)}
    child = subprocess.Popen(
        [emulator_bin, "-avd", name],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    processes.register("emulator", child.pid, avd=name)
    detail: dict[str, Any] = {"avd": name, "pid": child.pid}
    if not wait:
        return {**detail, "booted": False, "waited": False}

    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        serial = next(
            (
                device.serial
                for device in adb_mod.list_devices(adb_path)
                if device.serial not in before and device.state == "device"
            ),
            None,
        )
        if serial:
            completed = adb_mod.run_adb(
                adb_path, ["shell", "getprop", "sys.boot_completed"], serial=serial, check=False
            )
            if (completed.stdout or "").strip() == "1":
                return {**detail, "booted": True, "waited": True,
                        "serial": serial, "target_id": serial}
        if child.poll() is not None and serial is None and time.monotonic() - started > 3:
            raise errors.AutonomError(
                errors.BACKEND_FAILED,
                f"emulator exited with code {child.returncode} before a device appeared",
                f"Run '{emulator_bin} -avd {name}' by hand to see its output.",
            )
        time.sleep(1.0)
    raise errors.AutonomError(
        errors.BOOT_TIMEOUT,
        f"AVD '{name}' did not reach boot_completed within {int(timeout)}s",
        "It may still be booting — check 'autonom devices', or re-run with a larger --timeout.",
    )


def kill_emulator(adb_path: str, serial: str, *, timeout: float = 15.0) -> dict[str, Any]:
    if not EMULATOR_SERIAL.match(serial):
        raise errors.AutonomError(
            errors.EMULATOR_ONLY,
            f"'{serial}' is not an emulator; refusing to power off hardware",
            "Only emulator-<port> targets can be shut down; unplug physical devices by hand.",
        )
    adb_mod.run_adb(adb_path, ["emu", "kill"], serial=serial, check=False)
    deadline = time.monotonic() + timeout
    gone = False
    while time.monotonic() < deadline:
        if all(device.serial != serial for device in adb_mod.list_devices(adb_path)):
            gone = True
            break
        time.sleep(0.5)
    return {"serial": serial, "target_id": serial, "stopped": True, "gone": gone}
