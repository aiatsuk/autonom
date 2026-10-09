#!/usr/bin/env python3
"""Deterministic stand-in for the Android SDK `emulator` binary.

Shares the fake-tool protocol of ``fake_adb.py``: every invocation is appended
to ``$AUTONOM_FAKE_LOG``, canned behavior comes from ``$AUTONOM_FAKE_STATE``.

State keys (all optional):

``avds``         list of AVD names for ``-list-avds`` (default ["Pixel_9"])
``boot_serial``  serial the booted AVD appears under (default emulator-5556);
                 ``boot_serials`` (AVD name -> serial) overrides it per AVD,
                 and ``-port N`` overrides both with ``emulator-N`` (a ``-port``
                 whose ``emulator-N`` is already listed fails as a real
                 emulator does: exit 1, no row)
``boot_first``   ``[serial, avd]``: another AVD's emulator that appears
                 first (a parallel boot), before this boot's own device
``boot_hang``    when true, ``-avd`` starts nothing — reproduces a hung boot
``boot_delay``   seconds the boot waits before the device appears (default 0)
``boot_no_name`` when true, the booted AVD is not entered in ``avd_names``
                 (a console that does not answer ``emu avd name`` yet)

A boot appends the device row and records the AVD under its serial in the
fake adb's ``avd_names`` (what ``adb emu avd name`` answers). Boots running
at the same moment (``-port`` boots release the boot lock right after the
launch) update the state under an exclusive lock (the directory
``<state>.lock.d``, removed again), so no boot's row is lost to another's
read-modify-write.

The state file is shared with fakes running at the same moment: a booting
emulator writes it while ``boot_avd`` polls ``fake_adb.py devices``. So a
write swaps a complete file in (``write_state``), and a read waits out a
writer caught mid-write (``load_state``): no reader parses a torn document.
"""
from __future__ import annotations

import contextlib
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
# How long a boot waits for another boot's state update to finish.
LOCK_PATIENCE_SECONDS = 10.0


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


@contextlib.contextmanager
def state_lock():
    """Exclusive lock for a read-modify-write of the state file: a sibling
    directory created with `mkdir` (atomic) and removed afterwards, so the
    state's directory holds nothing extra once the boot is done."""
    path = os.environ.get("AUTONOM_FAKE_STATE")
    if not path:
        yield
        return
    lock = path + ".lock.d"
    deadline = time.monotonic() + LOCK_PATIENCE_SECONDS
    held = False
    while not held:
        try:
            os.mkdir(lock)
            held = True
        except FileExistsError:
            if time.monotonic() >= deadline:
                break  # a lock left by a killed fake: go ahead rather than hang
            time.sleep(0.01)
    try:
        yield
    finally:
        if held:
            with contextlib.suppress(OSError):
                os.rmdir(lock)


def option(argv: list[str], flag: str) -> str | None:
    if flag in argv:
        index = argv.index(flag)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


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
        avd = argv[1]
        port = option(argv, "-port")
        first = state.get("boot_first")
        if first:
            with state_lock():
                state = load_state()
                state.setdefault("devices", []).append([first[0], "device", f"avd:{first[1]}"])
                state.setdefault("avd_names", {})[first[0]] = first[1]
                write_state(state)
        delay = float(state.get("boot_delay") or 0)
        if delay > 0:
            time.sleep(delay)
        with state_lock():
            state = load_state()
            serial = (f"emulator-{port}" if port else
                      (state.get("boot_serials") or {}).get(avd)
                      or state.get("boot_serial", "emulator-5556"))
            rows = state.setdefault("devices", [])
            if port and any(row and row[0] == serial for row in rows):
                # Like a real emulator whose console port is taken: it
                # cannot bind and exits without a device of its own.
                sys.stderr.write(f"emulator: ERROR: port {port} is already in use\n")
                return 1
            rows.append([serial, "device", f"avd:{avd}"])
            if not state.get("boot_no_name"):
                state.setdefault("avd_names", {})[serial] = avd
            write_state(state)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
