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


def write_state(state: dict) -> None:
    path = os.environ.get("AUTONOM_FAKE_STATE")
    if path:
        Path(path).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


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

# `idb ui quiet` exists in newer fb-idb (1.6.5 on the bench Mac) and not in
# 1.1.7 above, so it is off unless a test turns it on with `idb_quiet`:
#   "quiet"    — prints settling, then quiet; exit 0
#   "busy"     — prints busy (with `idb_quiet_signals`), then timed_out; exit 1
#   "settling" — prints settling, then timed_out; exit 1
# Absent, or "unsupported", `ui quiet` is refused as an unknown choice, the way
# an idb without the command refuses it — so every existing settle test keeps
# the poll path. The output is real idb's: one JSON object per line with
# sorted keys under `--json`, its text lines otherwise. Arguments are checked
# the way idb's argparse checks them (a float timeout, integer windows).
QUIET_MODES = ("quiet", "busy", "settling")
QUIET_PID = 4242


def quiet_mode(state: dict) -> str | None:
    mode = state.get("idb_quiet")
    return mode if mode in QUIET_MODES else None


def usage_error(message: str) -> int:
    sys.stderr.write(f"usage: idb ui quiet\nidb ui quiet: error: {message}\n")
    return 2


def answer_quiet(state: dict, argv: list[str]) -> int:
    """`ui quiet [timeout] [--json] [--watch] [--quiet-window-ms N]
    [--busy-threshold-ms N] [--pid P | --bundle-id B] [--udid U]`."""
    timeout: float | None = None
    as_json = False
    rest = argv[2:]
    index = 0
    while index < len(rest):
        arg = rest[index]
        if arg in ("--quiet-window-ms", "--busy-threshold-ms", "--pid"):
            if index + 1 >= len(rest):
                return usage_error(f"argument {arg}: expected one argument")
            try:
                int(rest[index + 1])
            except ValueError:
                return usage_error(f"argument {arg}: invalid int value: {rest[index + 1]!r}")
            index += 2
        elif arg in ("--bundle-id", "--udid"):
            index += 2
        elif arg == "--json":
            as_json = True
            index += 1
        elif arg == "--watch":
            index += 1
        elif arg.startswith("-"):
            return usage_error(f"unrecognized arguments: {arg}")
        elif timeout is None:
            try:
                timeout = float(arg)
            except ValueError:
                return usage_error(f"argument timeout: invalid float value: {arg!r}")
            index += 1
        else:
            return usage_error(f"unrecognized arguments: {arg}")

    def emit(line: str, output: dict) -> None:
        sys.stdout.write((json.dumps(output, sort_keys=True) if as_json else line) + "\n")

    def state_event(name: str, signals: list[str] | None = None) -> None:
        output = {"event": "state", "state": name, "pid": QUIET_PID}
        text = f"{name} (pid {QUIET_PID})"
        if name == "busy":
            output["signals"] = sorted(signals or [])
            text = f"busy ({', '.join(sorted(signals or []))}) (pid {QUIET_PID})"
        emit(text, output)

    mode = quiet_mode(state)
    if mode == "quiet":
        state_event("settling")
        state_event("quiet")
        return 0
    if timeout is None:
        # no timeout: idb reports the state now, busy or quiet, and stops
        state_event("busy", state.get("idb_quiet_signals") or ["run_loop_idle"])
        return 1
    if mode == "busy":
        state_event("busy", state.get("idb_quiet_signals")
                    or ["animations_inactive", "run_loop_idle"])
    else:
        state_event("settling")
    emit(f"not quiet within {timeout}s", {"event": "timed_out", "timeout": timeout})
    return 1


def reject(group: str, value: str, choices: frozenset[str]) -> int:
    """Refuse an unknown command the way argparse does inside real idb."""
    sys.stderr.write(
        f"usage: idb {group}\nidb {group}: error: argument {group}: invalid choice: "
        f"{value!r} (choose from {', '.join(sorted(choices))})\n"
    )
    return 2


# Top-level options real idb accepts before the verb (`idb --companion h:p ui
# tap ...`). They are stripped before dispatch so the verb is found the way
# idb's own argparse finds it; the log keeps the full argv.
GLOBAL_WITH_VALUE = frozenset({"--companion", "--companion-path", "--log", "--compression"})
GLOBAL_FLAGS = frozenset({"--companion-tls", "--no-prune-dead-companion"})


def strip_globals(argv: list[str]) -> list[str]:
    index = 0
    while index < len(argv):
        if argv[index] in GLOBAL_WITH_VALUE:
            index += 2
        elif argv[index] in GLOBAL_FLAGS:
            index += 1
        else:
            break
    return argv[index:]


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def prune_state_file() -> None:
    """What real `list-targets` does to /tmp/idb/state: drop companions whose
    process is gone. Only the file AUTONOM_IDB_STATE_FILE names is touched."""
    path = os.environ.get("AUTONOM_IDB_STATE_FILE")
    if not path or not Path(path).exists():
        return
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    kept = [entry for entry in entries
            if not entry.get("stale") and _pid_alive(entry.get("pid"))]
    Path(path).write_text(json.dumps(kept), encoding="utf-8")


def udid_of(argv: list[str]) -> str | None:
    if "--udid" in argv and argv.index("--udid") + 1 < len(argv):
        return argv[argv.index("--udid") + 1]
    return None


def maybe_spawn_companion(state: dict, argv: list[str]) -> None:
    """`idb_spawn_companion: {"path": ..., "pid_file": ...}`: what real idb
    does for a simulator its registry does not list — start a companion
    (here the program at ``path``, run as ``path --udid <udid>`` in its own
    session, so it outlives this call) and register it. The pid goes to
    ``pid_file`` so the test can stop it. A listed simulator, live or not,
    gets no new companion: idb connects to the registered one."""
    spawn = state.get("idb_spawn_companion")
    udid = udid_of(argv)
    path = os.environ.get("AUTONOM_IDB_STATE_FILE")
    if not spawn or not udid or not path or argv[:1] == ["list-targets"]:
        return
    registry = Path(path)
    entries = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else []
    if any(entry.get("udid") == udid for entry in entries):
        return
    import subprocess
    process = subprocess.Popen(
        [spawn["path"], "--udid", udid], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    Path(spawn["pid_file"]).write_text(str(process.pid), encoding="utf-8")
    entries.append({"udid": udid, "is_local": True, "pid": process.pid,
                    "path": f"/tmp/idb/{udid}_companion.sock"})
    registry.write_text(json.dumps(entries), encoding="utf-8")


def check_surface(argv: list[str], state: dict | None = None) -> int | None:
    positional = [arg for arg in argv if not arg.startswith("-")]
    if not positional:
        return None
    command = positional[0]
    if command not in COMMANDS:
        return reject("idb", command, COMMANDS)
    if command == "ui":
        choices = UI_SUBCOMMANDS | ({"quiet"} if quiet_mode(state or {}) else set())
        if len(positional) < 2:
            return reject("ui", "", choices)
        if positional[1] not in choices:
            return reject("ui", positional[1], choices)
    return None


def main(argv: list[str]) -> int:
    record(argv)
    argv = strip_globals(argv)
    state = load_state()

    if argv[:1] != ["--version"]:
        refusal = check_surface(argv, state)
        if refusal is not None:
            return refusal

    for prefix, outcome in (state.get("idb_fail") or {}).items():
        if " ".join(argv).startswith(prefix):
            code, message = outcome
            sys.stderr.write(message + "\n")
            return int(code)

    # A failure on the first matching call only — a connection that broke
    # once and works on the next call, with or without a prune in between.
    once_only = state.get("idb_fail_once") or {}
    for prefix, outcome in list(once_only.items()):
        if " ".join(argv).startswith(prefix):
            once_only.pop(prefix)
            write_state(state)
            code, message = outcome
            sys.stderr.write(message + "\n")
            return int(code)

    maybe_spawn_companion(state, argv)

    # A failure that clears itself once `list-targets` has run — the stale
    # companion registration seen on a real Mac, which `list-targets` prunes.
    once = state.get("idb_fail_until_pruned") or {}
    if argv[:1] == ["list-targets"]:
        prune_state_file()
    if argv[:1] == ["list-targets"] and once:
        state.pop("idb_fail_until_pruned", None)
        write_state(state)
    else:
        for prefix, outcome in once.items():
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

    if argv[:2] == ["ui", "quiet"]:
        return answer_quiet(state, argv)

    if argv[:1] == ["screenshot"]:
        Path(argv[1]).write_bytes(PNG)
        return 0

    if argv[:2] == ["crash", "list"]:
        # `idb_crash_list_stderr`: a complaint printed with exit 0 and no
        # listing (a companion lost mid-call); `idb_fail` covers a non-zero
        # exit.
        if state.get("idb_crash_list_stderr"):
            sys.stderr.write(state["idb_crash_list_stderr"] + "\n")
        sys.stdout.write(state.get("idb_crash_list", ""))
        return 0

    # ui tap/swipe/text/button/key, connect, file … all succeed silently.
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
