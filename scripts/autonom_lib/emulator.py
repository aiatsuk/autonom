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
