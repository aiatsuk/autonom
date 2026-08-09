#!/usr/bin/env python3
"""Deterministic stand-in for `idb`, used by Autonom tests.

The argv log this writes is the oracle for VER-004: it records what was
*actually dispatched*, so a tap that the CLI reports as successful but sends to
the wrong coordinates still fails the test.

Recording alone was not enough. This fake used to return 0 for any argv it did
not recognise, which meant it proved *what was dispatched* but never *that the
dispatch names a command idb has*. `ui pinch|rotate|shake` were built, logged,
and passed here for several releases while failing on every real machine, since
fb-idb has no such subcommands. So the command surface below is an allowlist
taken from a real `idb --help`, and an unknown command fails the way the real
one does. A fake that accepts more than the tool it stands in for is not a
cheaper tool, it is a hole in the suite.
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


# `idb --help` and `idb ui --help`, fb-idb 1.1.7.
COMMANDS = frozenset({
    "add-media", "approve", "boot", "clear-keychain", "clone", "companion",
    "connect", "contacts", "crash", "create", "daemon", "dap", "debugserver",
    "delete", "delete-all", "describe", "disconnect", "dsym", "dylib", "erase",
    "file", "focus", "framework", "get", "install", "instruments", "kill",
    "launch", "list", "list-apps", "list-targets", "log", "open", "record",
    "screenshot", "send-notification", "set", "set-location", "shell",
    "shutdown", "terminate", "ui", "uninstall", "video", "video-stream",
    "xctest", "xctrace",
})
UI_SUBCOMMANDS = frozenset({
    "describe-all", "describe-point", "tap", "button", "text", "key",
    "key-sequence", "swipe",
})


def reject(group: str, value: str, choices: frozenset[str]) -> int:
    """Refuse an unknown command the way argparse does inside real idb."""
    sys.stderr.write(
        f"usage: idb {group}\nidb {group}: error: argument {group}: invalid choice: "
        f"{value!r} (choose from {', '.join(sorted(choices))})\n"
    )
    return 2


def check_surface(argv: list[str]) -> int | None:
    positional = [arg for arg in argv if not arg.startswith("-")]
    if not positional:
        return None
    command = positional[0]
    if command not in COMMANDS:
        return reject("idb", command, COMMANDS)
    if command == "ui":
        if len(positional) < 2:
            return reject("ui", "", UI_SUBCOMMANDS)
        if positional[1] not in UI_SUBCOMMANDS:
            return reject("ui", positional[1], UI_SUBCOMMANDS)
    return None


def main(argv: list[str]) -> int:
    record(argv)
    state = load_state()

    if argv[:1] != ["--version"]:
        refusal = check_surface(argv)
        if refusal is not None:
            return refusal

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
