#!/usr/bin/env python3
"""Deterministic stand-in for AXe (`axe`), used by Autonom tests.

Like `fake_idb.py`, the argv log is the oracle: it records what was actually
dispatched, and `type --stdin` also records the text read from stdin, so a
test can prove the words that would have been typed. The command surface is
an allowlist taken from `axe --help` (AXe 1.8.0); an unknown subcommand, or a
HID verb without `--udid`, fails the way the real tool does.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# `axe --help`, AXe 1.8.0.
COMMANDS = frozenset({
    "describe-ui", "list-simulators", "init", "tap", "slider", "type", "swipe",
    "drag", "button", "key", "key-sequence", "key-combo", "touch", "gesture",
    "stream-video", "record-video", "screenshot", "batch",
})
NEEDS_UDID = COMMANDS - {"list-simulators", "init"}
BUTTONS = frozenset({"apple-pay", "home", "lock", "side-button", "siri"})


def load_state() -> dict:
    path = os.environ.get("AUTONOM_FAKE_STATE")
    if not path or not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def record(argv: list[str], stdin: str | None) -> None:
    path = os.environ.get("AUTONOM_FAKE_LOG")
    if not path:
        return
    entry = {"tool": "axe", "argv": argv}
    if stdin is not None:
        entry["stdin"] = stdin
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def main(argv: list[str]) -> int:
    stdin = sys.stdin.read() if "--stdin" in argv else None
    record(argv, stdin)
    if argv[:1] == ["--version"]:
        sys.stdout.write(load_state().get("axe_version", "1.8.0") + "\n")
        return 0
    if not argv or argv[0] not in COMMANDS:
        sys.stderr.write(f"Error: Unknown subcommand '{argv[0] if argv else ''}'\n")
        return 64
    if argv[0] in NEEDS_UDID and "--udid" not in argv:
        sys.stderr.write("Error: Missing expected argument '--udid <udid>'\n")
        return 64
    if argv[0] == "button" and (len(argv) < 2 or argv[1] not in BUTTONS):
        sys.stderr.write("Error: The value is not a valid button type\n")
        return 64
    for prefix, outcome in (load_state().get("axe_fail") or {}).items():
        if " ".join(argv).startswith(prefix):
            code, message = outcome
            sys.stderr.write(message + "\n")
            return int(code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
