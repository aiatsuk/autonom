"""Opt-in local screenshot service for Mac host video.

Merely shipping this code does not enable a listener. The user must configure
one AVD/serial/port explicitly in ~/.config/autonom/host-video.json.
The emulator creates a per-process gRPC bearer token in its private discovery
record. No TLS keys, JWT keys or signing settings are needed.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

from . import errors


def configured_device(name: str, port: int | None) -> dict | None:
    if sys.platform != "darwin":
        return None
    path = Path.home() / ".config" / "autonom" / "host-video.json"
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise errors.AutonomError(errors.INVALID_VALUE, "Invalid host-video configuration") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("devices"), list):
        raise errors.AutonomError(errors.INVALID_VALUE, "Invalid host-video configuration")
    matches = [d for d in data["devices"] if isinstance(d, dict) and d.get("avd") == name]
    if not matches:
        return None
    if len(matches) != 1:
        raise errors.AutonomError(errors.INVALID_VALUE, "Duplicate host-video AVD configuration")
    device = matches[0]
    if any(d is not device and isinstance(d, dict) and
           (d.get("grpcPort") == device.get("grpcPort") or d.get("serial") == device.get("serial"))
           for d in data["devices"]):
        raise errors.AutonomError(errors.INVALID_VALUE, "Duplicate host-video endpoint")
    match = re.fullmatch(r"emulator-(\d+)", str(device.get("serial", "")))
    console = int(match[1]) if match else 0
    grpc = device.get("grpcPort")
    if (console not in range(5554, 5683, 2) or type(grpc) is not int
            or not 1024 <= grpc <= 65535 or grpc in (console, console+1)
            or (port is not None and port != console)):
        raise errors.AutonomError(errors.INVALID_VALUE, "Host-video serial or port mismatch")
    return {"port": console, "grpcPort": grpc}
