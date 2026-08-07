#!/usr/bin/env python3
"""Deterministic stand-in for `xcrun` (simctl subset), used by Autonom tests.

Invoked exactly as the real driver is: ``<this> simctl <subcommand> …``.
Records every invocation to ``$AUTONOM_FAKE_LOG`` and reads canned state from
``$AUTONOM_FAKE_STATE``.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006"
    "00057dd8b7c40000000049454e44ae426082"
)

DEFAULT_DEVICES = {
    "devices": {
        "com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
            {
                "udid": "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB",
                "name": "iPhone 17 Pro",
                "state": "Shutdown",
                "isAvailable": True,
            }
        ]
    }
}


def load_state() -> dict:
    path = os.environ.get("AUTONOM_FAKE_STATE")
    if not path or not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_state(state: dict) -> None:
    path = os.environ.get("AUTONOM_FAKE_STATE")
    if path:
        Path(path).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def record(argv: list[str]) -> None:
    path = os.environ.get("AUTONOM_FAKE_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"tool": "simctl", "argv": argv}) + "\n")


def main(argv: list[str]) -> int:
    record(argv)
    state = load_state()

    for prefix, outcome in (state.get("simctl_fail") or {}).items():
        if " ".join(argv).startswith(prefix):
            code, message = outcome
            sys.stderr.write(message + "\n")
            return int(code)

    if argv[:1] != ["simctl"]:
        sys.stderr.write(f"fake xcrun: unsupported driver {argv[:1]}\n")
        return 1
    args = argv[1:]

    if args[:2] == ["list", "devices"]:
        sys.stdout.write(json.dumps(state.get("simctl_devices", DEFAULT_DEVICES)))
        return 0

    if args[:1] == ["bootstatus"]:
        devices = state.setdefault("simctl_devices", json.loads(json.dumps(DEFAULT_DEVICES)))
        for entries in devices["devices"].values():
            for entry in entries:
                if entry["udid"] == args[1]:
                    entry["state"] = "Booted"
        write_state(state)
        return 0

    if args[:1] == ["listapps"]:
        installed = state.get("installed", ["com.example.app"])
        sys.stdout.write("{" + " ".join(f'"{app}" = {{}};' for app in installed) + "}")
        return 0

    if args[:1] == ["install"]:
        installed = state.setdefault("installed", [])
        bundle = state.get("install_bundle_id", "com.example.app")
        if bundle not in installed:
            installed.append(bundle)
        write_state(state)
        return 0

    if args[:1] == ["uninstall"]:
        installed = state.setdefault("installed", [])
        if args[2] in installed:
            installed.remove(args[2])
        write_state(state)
        return 0

    if args[:1] == ["launch"]:
        sys.stdout.write(f"{args[2]}: 4242\n")
        return 0

    if args[:1] == ["terminate"]:
        return 0 if args[2] in state.get("running", ["com.example.app"]) else 1

    if args[:2] == ["io", args[1]] and len(args) >= 3 and args[2] == "screenshot":
        Path(args[3]).write_bytes(PNG)
        return 0

    if args[:1] == ["get_app_container"]:
        sys.stdout.write(state.get("container", "/tmp/fake-container") + "\n")
        return 0

    if args[:1] == ["spawn"] and "log" in args:
        for line in state.get("ios_log", []):
            sys.stdout.write(line + "\n")
        return 0

    # openurl / privacy / location / addmedia / shutdown succeed silently.
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
