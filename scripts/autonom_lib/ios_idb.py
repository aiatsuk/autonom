"""`idb` wrapper — the iOS Development Bridge (DEC-002).

idb is to iOS roughly what adb is to Android: a companion process talks to the
simulator or device, and a thin client drives it — optionally from another
machine, which is what makes a remote Mac farm possible (CAP-IOSS-006).

idb reaches its capabilities through Apple private frameworks, so it is the part
of the stack most likely to break on an Xcode upgrade (RISK-006). Every piece of
knowledge about idb's command line is therefore confined to this module; the
parser above it is fixture-driven and shape-tolerant, so a drift costs one
adapter rather than a rewrite.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

from . import errors
from .platform import Target

BUTTONS = ("APPLE_PAY", "HOME", "LOCK", "SIDE_BUTTON", "SIRI")

# What `idb ui` actually accepts. Kept here, next to the code that builds those
# argv lists, because this list is the thing that goes stale on an idb upgrade.
UI_SUBCOMMANDS = (
    "describe-all", "describe-point", "tap", "button", "text", "key",
    "key-sequence", "swipe",
)
GESTURES = ("swipe",)
# Offered by the CLI, backed by nothing. idb has no pinch/rotate/shake — not
# under `idb ui`, not at the top level (verified against fb-idb 1.1.7) — so
# these were dispatched as commands that do not exist and came back as
# `backend_failed` carrying idb's own argparse usage text. Refusing them the way
# Android gestures are already refused is the honest shape: a typed code, and a
# hint that names the alternative instead of sending the reader to `doctor`.
UNBACKED_GESTURES = ("pinch", "rotate", "shake")


def find_idb(explicit: str | None = None) -> str:
    """Resolve the idb client. Order: flag, environment, PATH."""
    candidate = explicit or os.environ.get("AUTONOM_IDB")
    if candidate:
        return candidate
    path = shutil.which("idb")
    if not path:
        raise errors.tool_missing("idb")
    return path


def companion_endpoint(host: str | None = None, port: int | None = None) -> str | None:
    """`host:port` for a remote companion, or None for a local one."""
    if host:
        return f"{host}:{port or 10882}"
    configured = os.environ.get("AUTONOM_IDB_COMPANION")
    return configured or None


def run_idb(
    idb: str,
    args: Sequence[str],
    *,
    udid: str | None = None,
    timeout: float | None = 30,
    check: bool = True,
    binary: bool = False,
) -> subprocess.CompletedProcess:
    command = [idb, *args]
    if udid:
        command += ["--udid", udid]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            text=not binary,
        )
    except FileNotFoundError as exc:
        raise errors.tool_missing("idb") from exc
    if check and completed.returncode != 0:
        detail = completed.stderr if not binary else (completed.stderr or b"").decode("utf-8", "replace")
        message = (detail or "").strip() or f"idb {' '.join(args)} failed ({completed.returncode})"
        code = (
            errors.IDB_COMPANION_UNAVAILABLE
            if "companion" in message.lower() or "connect" in message.lower()
            else errors.BACKEND_FAILED
        )
        raise errors.AutonomError(
            code,
            message,
            "Check 'idb list-targets'; run 'autonom doctor' for the whole environment.",
        )
    return completed


def version(idb: str) -> str | None:
    """Liveness + identity probe.

    `idb` has no `--version` flag (it errors with "unrecognized arguments"), so
    readiness is established by a command that actually exercises the client and
    its companion. `list-targets` is the cheapest one that does both.
    """
    completed = subprocess.run(
        [idb, "list-targets", "--json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=False, timeout=30,
    )
    if completed.returncode != 0:
        return None
    targets = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    return f"idb client ok ({len(targets)} target(s))"


def companion_version(companion: str = "idb_companion") -> str | None:
    completed = subprocess.run(
        [companion, "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, check=False, timeout=20,
    )
    if completed.returncode != 0:
        return None
    return (completed.stdout or "").strip() or None


def connect(idb: str, endpoint: str | None, udid: str) -> None:
    """Attach the client to a companion.

    `idb connect <host> <port>` for a remote companion; for a local one idb
    spawns/needs `idb_companion` itself, so connecting by udid is enough.
    """
    if endpoint:
        host, _, port = endpoint.partition(":")
        run_idb(idb, ["connect", host, port or "10882"], timeout=30, check=False)
        return
    run_idb(idb, ["connect", udid], timeout=30, check=False)


def _client(target: Target, args: Any = None) -> str:
    idb = find_idb(getattr(args, "idb", None) if args else None)
    return idb


def probe(idb_path: str | None = None, endpoint: str | None = None) -> dict[str, Any]:
    """Readiness snapshot cached in the session record (CAP-IOSS-006)."""
    import time

    try:
        idb = find_idb(idb_path)
    except errors.AutonomError as exc:
        return {"state": "missing", "version": None, "companion": endpoint or "local",
                "error": exc.message, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    resolved = version(idb)
    return {
        "state": "ready" if resolved else "error",
        "version": resolved,
        "path": idb,
        "companion": endpoint or "local",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# --- UI ----------------------------------------------------------------------


def describe_all(target: Target, *, idb_path: str | None = None) -> str:
    idb = find_idb(idb_path)
    completed = run_idb(idb, ["ui", "describe-all", "--json"], udid=target.target_id, timeout=20)
    return completed.stdout or ""


def tap(target: Target, x: int, y: int, *, idb_path: str | None = None) -> None:
    idb = find_idb(idb_path)
    run_idb(idb, ["ui", "tap", str(x), str(y)], udid=target.target_id, timeout=10)


def swipe(target: Target, x1: int, y1: int, x2: int, y2: int, duration: float,
          *, idb_path: str | None = None) -> None:
    idb = find_idb(idb_path)
    run_idb(
        idb,
        ["ui", "swipe", str(x1), str(y1), str(x2), str(y2), "--duration", str(duration)],
        udid=target.target_id,
        timeout=20,
    )


def text(target: Target, value: str, *, idb_path: str | None = None) -> None:
    idb = find_idb(idb_path)
    run_idb(idb, ["ui", "text", value], udid=target.target_id, timeout=20)


def button(target: Target, name: str, *, idb_path: str | None = None) -> None:
    idb = find_idb(idb_path)
    run_idb(idb, ["ui", "button", name], udid=target.target_id, timeout=10)


def key(target: Target, keycode: str, *, idb_path: str | None = None) -> None:
    idb = find_idb(idb_path)
    run_idb(idb, ["ui", "key", keycode], udid=target.target_id, timeout=10)


def gesture(target: Target, name: str, *, idb_path: str | None = None, **kwargs: Any) -> None:
    if name in UNBACKED_GESTURES:
        raise errors.AutonomError(
            errors.UNSUPPORTED_ON_PLATFORM,
            f"idb provides no '{name}' command, so it cannot be sent to a simulator",
            "Use 'autonom ui swipe' for anything reachable by a drag. Rotation and "
            "shake have no Autonom path; rotate the window by hand in Simulator "
            "(Device > Rotate) if the test needs it.",
            gesture=name,
        )
    raise errors.AutonomError(
        errors.UNSUPPORTED_ON_PLATFORM,
        f"unknown gesture: {name}",
        "Supported gestures: " + ", ".join(GESTURES),
    )


def screenshot(target: Target, output: Path, *, idb_path: str | None = None) -> Path:
    idb = find_idb(idb_path)
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    run_idb(idb, ["screenshot", str(output)], udid=target.target_id, timeout=60)
    return output


# --- diagnostics -------------------------------------------------------------


def crash_list(target: Target, *, idb_path: str | None = None) -> str:
    idb = find_idb(idb_path)
    return run_idb(idb, ["crash", "list"], udid=target.target_id, timeout=30, check=False).stdout or ""


def crash_show(target: Target, name: str, *, idb_path: str | None = None) -> str:
    idb = find_idb(idb_path)
    return run_idb(idb, ["crash", "show", name], udid=target.target_id, timeout=30).stdout or ""


def file_pull(target: Target, bundle_id: str, remote: str, local: Path,
              *, idb_path: str | None = None) -> Path:
    idb = find_idb(idb_path)
    local.parent.mkdir(parents=True, exist_ok=True)
    run_idb(
        idb,
        ["file", "pull", "--bundle-id", bundle_id, remote, str(local)],
        udid=target.target_id,
        timeout=60,
    )
    return local
