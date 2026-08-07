from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Sequence

from . import errors


class AdbError(errors.AutonomError):
    """Kept as a distinct type for callers that catch it by name.

    It subclasses `AutonomError` so the CLI's single formatter renders it with a
    stable `error_code`, without breaking `except adb.AdbError` anywhere.
    """

    def __init__(self, message: str, code: str = errors.BACKEND_FAILED, hint: str | None = None) -> None:
        super().__init__(code, message, hint)


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    properties: dict[str, str]

    def as_dict(self) -> dict[str, object]:
        # `serial` is permanent (DEC-004); `target_id` and `name` are additive.
        return {
            "serial": self.serial,
            "target_id": self.serial,
            "state": self.state,
            "running": self.state == "device",
            "platform": "android",
            "name": self.properties.get("model") or self.properties.get("product") or self.serial,
            "properties": self.properties,
        }


def find_adb(explicit: str | None = None) -> str:
    """Resolve the adb binary. Order: flag, environment, PATH."""
    candidate = explicit or os.environ.get("AUTONOM_ADB")
    if candidate:
        return candidate
    path = shutil.which("adb")
    if not path:
        raise errors.tool_missing("adb")
    return path


def run_adb(
    adb: str,
    args: Sequence[str],
    *,
    serial: str | None = None,
    timeout: float | None = 30,
    check: bool = True,
    binary: bool = False,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    command = [adb]
    if serial:
        command.extend(["-s", serial])
    command.extend(args)
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if not binary else subprocess.PIPE,
            check=False,
            timeout=timeout,
            text=not binary,
        )
    except FileNotFoundError as exc:
        # An explicit --adb/AUTONOM_ADB pointing at nothing must still produce a
        # machine-readable failure rather than a bare OS error (INV-08).
        raise errors.tool_missing("adb") from exc
    if check and completed.returncode != 0:
        detail = completed.stdout if not binary else (completed.stderr or b"").decode("utf-8", "replace")
        raise AdbError(detail.strip() or f"adb {' '.join(args)} failed ({completed.returncode})")
    return completed


def list_devices(adb: str) -> list[Device]:
    completed = run_adb(adb, ["devices", "-l"], check=True)
    assert isinstance(completed.stdout, str)
    devices: list[Device] = []
    for line in completed.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        props: dict[str, str] = {}
        for token in parts[2:]:
            if ":" in token:
                key, value = token.split(":", 1)
                props[key] = value
        devices.append(Device(serial=serial, state=state, properties=props))
    return devices


def resolve_serial(adb: str, serial: str | None) -> str:
    if serial:
        return serial
    ready = [device for device in list_devices(adb) if device.state == "device"]
    if len(ready) == 1:
        return ready[0].serial
    if not ready:
        raise AdbError(
            "no authorized adb device is connected",
            errors.NO_TARGET,
            "Start an emulator or connect a device, then run 'autonom devices'.",
        )
    raise AdbError(
        "multiple adb devices are connected; pass --serial",
        errors.AMBIGUOUS_TARGET,
        "Pass --target <id> (or --serial <id>); run 'autonom devices' to list them.",
    )
