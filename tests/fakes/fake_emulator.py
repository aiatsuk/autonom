#!/usr/bin/env python3
"""Deterministic stand-in for the Android SDK `emulator` binary.

Shares the fake-tool protocol of ``fake_adb.py``: every invocation is appended
to ``$AUTONOM_FAKE_LOG``, canned behavior comes from ``$AUTONOM_FAKE_STATE``.

State keys (all optional):

``avds``         list of AVD names for ``-list-avds`` (default ["Pixel_9"])
``boot_serial``  serial the booted AVD appears under (default emulator-5556)
``boot_hang``    when true, ``-avd`` starts nothing — reproduces a hung boot

The state file is shared with fakes running at the same moment: a booting
emulator writes it while ``boot_avd`` polls ``fake_adb.py devices``. So a
write swaps a complete file in (``write_state``), and a read waits out a
writer caught mid-write (``load_state``): no reader parses a torn document.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

# How long a read retries a document that does not parse. A writer that
# rewrites the file in place (a test's plain ``write_text``) leaves it empty
# or half-written for microseconds; a document still unparsable after this
# long is genuinely corrupt and is raised, never read as an empty state.
READ_PATIENCE_SECONDS = 2.0
READ_RETRY_INTERVAL = 0.01


def read_state(path: Path, *, patience: float | None = None) -> dict:
    """The state document at ``path``; ``{}`` when there is none."""
    budget = READ_PATIENCE_SECONDS if patience is None else patience
    deadline = time.monotonic() + budget
    while True:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(READ_RETRY_INTERVAL)


def load_state() -> dict:
    path = os.environ.get("AUTONOM_FAKE_STATE")
    if not path:
        return {}
    return read_state(Path(path))


def record(argv: list[str]) -> None:
    path = os.environ.get("AUTONOM_FAKE_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"tool": "emulator", "argv": argv}) + "\n")


def write_state(state: dict) -> None:
    path = os.environ.get("AUTONOM_FAKE_STATE")
    if not path:
        return
    target = Path(path)
    # A sibling temp file, then an atomic rename over the target: a reader
    # opens either the previous document or this one — never a file that
    # `open(..., "w")` has truncated and the write has not yet refilled.
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, indent=2) + "\n")
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


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
