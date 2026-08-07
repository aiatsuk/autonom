#!/usr/bin/env python3
"""Deterministic stand-in for `idb`, used by Autonom tests.

The argv log this writes is the oracle for VER-004: it records what was
*actually dispatched*, so a tap that the CLI reports as successful but sends to
the wrong coordinates still fails the test.
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
        handle.write(json.dumps({"tool": "idb", "argv": argv}) + "\n")


def main(argv: list[str]) -> int:
    record(argv)
    state = load_state()

    for prefix, outcome in (state.get("idb_fail") or {}).items():
        if " ".join(argv).startswith(prefix):
            code, message = outcome
            sys.stderr.write(message + "\n")
            return int(code)

    if argv[:1] == ["--version"]:
        sys.stdout.write(state.get("idb_version", "1.1.7") + "\n")
        return 0

    if argv[:2] == ["ui", "describe-all"]:
        dump = state.get("idb_describe_all")
        if dump:
            sys.stdout.write(Path(dump).read_text(encoding="utf-8"))
        else:
            sys.stdout.write("[]")
        return 0

    if argv[:1] == ["screenshot"]:
        Path(argv[1]).write_bytes(PNG)
        return 0

    if argv[:2] == ["crash", "list"]:
        sys.stdout.write(state.get("idb_crash_list", ""))
        return 0

    # ui tap/swipe/text/button/key, connect, file … all succeed silently.
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
