#!/usr/bin/env python3
"""Deterministic stand-in for the Android SDK `emulator` binary.

Shares the fake-tool protocol of ``fake_adb.py``: every invocation is appended
to ``$AUTONOM_FAKE_LOG``, canned behavior comes from ``$AUTONOM_FAKE_STATE``.

State keys (all optional):

``avds``         list of AVD names for ``-list-avds`` (default ["Pixel_9"])
``boot_serial``  serial the booted AVD appears under (default emulator-5556)
``boot_hang``    when true, ``-avd`` starts nothing — reproduces a hung boot
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def load_state() -> dict:
    path = os.environ.get("AUTONOM_FAKE_STATE")
    if not path or not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def record(argv: list[str]) -> None:
    path = os.environ.get("AUTONOM_FAKE_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"tool": "emulator", "argv": argv}) + "\n")


def write_state(state: dict) -> None:
    path = os.environ.get("AUTONOM_FAKE_STATE")
    if path:
        Path(path).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    record(argv)
    state = load_state()

    if argv[:1] == ["-list-avds"]:
        for name in state.get("avds", ["Pixel_9"]):
            sys.stdout.write(name + "\n")
        return 0

    if argv[:1] == ["-avd"] and len(argv) >= 2:
        if state.get("boot_hang"):
            return 0
        serial = state.get("boot_serial", "emulator-5556")
        rows = state.setdefault("devices", [])
        rows.append([serial, "device", f"avd:{argv[1]}"])
        write_state(state)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
