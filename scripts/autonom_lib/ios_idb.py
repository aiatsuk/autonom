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

import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime
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

# Xcode 27 moved SimulatorKit.framework to <Xcode.app>/Contents/SharedFrameworks.
# idb_companion before 1.6.2 (upstream commit 9811012, built after 2026-06-09)
# still loads it from <DEVELOPER_DIR>/Library/PrivateFrameworks, so every HID
# verb (tap, swipe, text, key, button) fails while describe-all keeps working.
SIMULATORKIT_MARKER = "simulatorkit is required for hid"
COMPANION_FIXED_VERSION = (1, 6, 2)
COMPANION_FIXED_DATE = date(2026, 6, 9)
HID_UPGRADE_FIX = "brew update && brew upgrade idb-companion (and pipx upgrade fb-idb)"
HID_FRAMEWORK_HINT = (
    f"idb_companion cannot load SimulatorKit from this Xcode. Upgrade it: "
    f"{HID_UPGRADE_FIX}. Or install AXe (brew install cameroncooke/axe/axe) and "
    "set AUTONOM_IOS_HID=axe; the accessibility tree keeps working either way."
)
# Where the fb-idb client records local companions. Pruning is visible there
# as entries disappearing; AUTONOM_IDB_STATE_FILE points tests elsewhere.
IDB_STATE_FILE = "/tmp/idb/state"


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


def target_companion(target: Target | None) -> str | None:
    """The companion endpoint for ``target``: an ``idb_companion`` alias the
    CLI placed on the Target, else AUTONOM_IDB_COMPANION (which
    ``--idb-host/--idb-port`` are promoted to)."""
    aliases = getattr(target, "aliases", None) or {}
    return aliases.get("idb_companion") or companion_endpoint()


def idb_is_overridden(explicit: str | None = None) -> bool:
    """True when the idb client was pinned by flag or AUTONOM_IDB."""
    return bool(explicit or os.environ.get("AUTONOM_IDB"))


def _is_connection_refused(message: str) -> bool:
    lowered = message.lower()
    return ("connection refused" in lowered or "errno 61" in lowered
            or "econnrefused" in lowered)


def is_hid_framework_failure(message: str) -> bool:
    return SIMULATORKIT_MARKER in (message or "").lower()


def run_idb(
    idb: str,
    args: Sequence[str],
    *,
    udid: str | None = None,
    timeout: float | None = 30,
    check: bool = True,
    binary: bool = False,
    companion: str | None = None,
    _retry: bool = True,
) -> subprocess.CompletedProcess:
    # `--companion` is a top-level idb option, so it goes before the verb.
    endpoint = companion or companion_endpoint()
    command = [idb, *(["--companion", endpoint] if endpoint else []), *args]
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
        if is_hid_framework_failure(message):
            # Checked first: the message also says "companion"-ish things, and
            # a retry cannot fix a framework the companion cannot load.
            raise errors.AutonomError(
                errors.IOS_HID_FRAMEWORK_MISSING, message, HID_FRAMEWORK_HINT,
                fix=HID_UPGRADE_FIX,
            )
        code = (
            errors.IDB_COMPANION_UNAVAILABLE
            if "companion" in message.lower() or "connect" in message.lower()
            else errors.BACKEND_FAILED
        )
        if (code == errors.IDB_COMPANION_UNAVAILABLE and _retry and not endpoint
                and _is_connection_refused(message) and _prune_companions(idb)):
            # Seen on a real Mac: the client kept a companion entry from an
            # earlier boot (a dead pid and its socket), so every verb died with
            # "Connection refused" until `idb list-targets` noticed and dropped
            # it. Prune the same way, once, and re-dispatch — but only for a
            # refused connection (the verb never reached the companion), and,
            # when the client's registry is readable, only if an entry was
            # really dropped. A reset or disconnect may arrive after the action
            # was delivered, and `ui text` sent twice types the text twice.
            return run_idb(idb, args, udid=udid, timeout=timeout, check=check,
                           binary=binary, companion=companion, _retry=False)
        raise errors.AutonomError(
            code,
            message,
            "Check 'idb list-targets'; run 'autonom doctor' for the whole environment.",
        )
    return completed


def _state_path() -> Path | None:
    """The fb-idb client's companion registry, when it is knowably this idb's.

    AUTONOM_IDB_STATE_FILE wins. The default /tmp/idb/state belongs to the
    PATH-resolved client; a pinned client (flag or AUTONOM_IDB) may keep its
    registry elsewhere — or be a test fake — so its state is unknown.
    """
    configured = os.environ.get("AUTONOM_IDB_STATE_FILE")
    if configured:
        return Path(configured)
    return None if idb_is_overridden() else Path(IDB_STATE_FILE)


def _state_entries() -> int | None:
    """How many companions the idb client has registered, or None if unknown."""
    path = _state_path()
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return len(payload) if isinstance(payload, list) else None


def _prune_companions(idb: str) -> bool:
    """`idb list-targets` drops companion registrations whose process is gone.

    Returns whether a retry is justified. When the client's registry is
    readable, only an entry that actually disappeared counts: it used to be
    enough for `list-targets` to exit 0, so a companion that was simply down
    got every verb dispatched twice. When the registry cannot be read the
    answer is unknown and the single retry stays allowed — the caller only
    gets here for a refused connection, which never reached the companion, so
    re-sending cannot repeat an action.
    """
    before = _state_entries()
    try:
        completed = subprocess.run(
            [idb, "list-targets"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0:
        return False
    if before is None:
        return True
    after = _state_entries()
    return after is not None and after < before


def version(idb: str) -> str | None:
    """Liveness + identity probe.

    `idb` has no `--version` flag (it errors with "unrecognized arguments"), so
    readiness is established by a command that actually exercises the client and
    its companion. `list-targets` is the cheapest one that does both.
    """
    endpoint = companion_endpoint()
    completed = subprocess.run(
        [idb, *(["--companion", endpoint] if endpoint else []), "list-targets", "--json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=False, timeout=30,
    )
    if completed.returncode != 0:
        return None
    targets = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    return f"idb client ok ({len(targets)} target(s))"


_SEMVER = re.compile(r"\b(\d+)\.(\d+)(?:\.(\d+))?\b")


def companion_info(companion: str = "idb_companion") -> dict[str, Any] | None:
    """`idb_companion --version`, reduced to known fields.

    The companion can print its whole environment (tokens included) when it
    starts, so nothing it prints is passed through verbatim: only the
    version and build date are kept. stderr is never read.
    """
    try:
        completed = subprocess.run(
            [companion, "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, text=True, check=False, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    info: dict[str, Any] = {"version": None, "build_date": None, "build_time": None}
    for line in (completed.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            for key in info:
                value = payload.get(key)
                if isinstance(value, str) and len(value) <= 64:
                    info[key] = value
            break
    if info["version"] is None:
        match = _SEMVER.search((completed.stdout or "").splitlines()[0]
                               if completed.stdout else "")
        if match:
            info["version"] = match.group(0)
    if not any(info.values()):
        return None
    return info


def companion_version(companion: str = "idb_companion") -> str | None:
    """One-line summary of `companion_info` (kept for existing callers)."""
    info = companion_info(companion)
    if not info:
        return None
    parts = []
    if info.get("version"):
        parts.append(info["version"])
    if info.get("build_date"):
        parts.append(f"built {info['build_date']}")
    return " ".join(parts) or None


def _parse_build_date(value: str | None) -> date | None:
    if not value:
        return None
    text = " ".join(value.split())
    for fmt in ("%b %d %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def companion_predates_xcode27(info: dict[str, Any] | None) -> bool | None:
    """True when this companion only knows the pre-Xcode-27 SimulatorKit path.

    A version string decides when present; otherwise the build date does.
    None when neither can be read.
    """
    if not info:
        return None
    match = _SEMVER.search(info.get("version") or "")
    if match:
        parsed = tuple(int(part or 0) for part in match.groups())
        return parsed < COMPANION_FIXED_VERSION
    built = _parse_build_date(info.get("build_date"))
    if built:
        return built < COMPANION_FIXED_DATE
    return None


def hid_assess(xcode: dict[str, Any], info: dict[str, Any] | None) -> dict[str, Any]:
    """Pure verdict from `ios_simctl.xcode_info` and `companion_info`.

    ``ready`` is False only when the failure is determined: the Developer dir
    lacks the legacy SimulatorKit, Xcode is 27+ or has the new location, and
    the companion predates the fix.
    """
    stale = companion_predates_xcode27(info)
    moved = (not xcode.get("simulatorkit_legacy")
             and bool(xcode.get("simulatorkit_shared") or (xcode.get("xcode_major") or 0) >= 27))
    ready = not (moved and stale)
    return {
        "ready": ready,
        "reason": None if ready else (
            "idb_companion loads SimulatorKit from Library/PrivateFrameworks, which "
            f"Xcode {xcode.get('xcode_version') or '27+'} no longer has"),
        "companion_predates_fix": stale,
        "fix": None if ready else HID_UPGRADE_FIX,
    }


def hid_status(companion_path: str | None = None, *,
               developer: str | None = None) -> dict[str, Any]:
    """Can idb deliver HID events on this machine? Read from disk and
    `idb_companion --version` only; never touches a simulator."""
    from . import ios_simctl

    xcode = ios_simctl.xcode_info(developer)
    path = companion_path or shutil.which("idb_companion")
    info = companion_info(path) if path else None
    return {**hid_assess(xcode, info), "xcode": xcode, "companion_path": path,
            "companion": info}


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
    snapshot: dict[str, Any] = {
        "state": "ready" if resolved else "error",
        "version": resolved,
        "path": idb,
        "companion": endpoint or "local",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # A pinned client (flag or AUTONOM_IDB) may not pair with the companion on
    # PATH, so the HID check only speaks for the PATH-resolved install; a
    # remote companion is on another machine's Xcode entirely.
    if resolved and not endpoint and not idb_is_overridden(idb_path):
        status = hid_status()
        snapshot["hid"] = {"ready": status["ready"], "reason": status["reason"],
                           "fix": status["fix"]}
    return snapshot


# --- UI ----------------------------------------------------------------------


def describe_all(target: Target, *, idb_path: str | None = None) -> str:
    idb = find_idb(idb_path)
    completed = run_idb(idb, ["ui", "describe-all", "--json"], udid=target.target_id,
                        timeout=20, companion=target_companion(target))
    return completed.stdout or ""


def tap(target: Target, x: int, y: int, *, duration: float | None = None,
        idb_path: str | None = None) -> None:
    idb = find_idb(idb_path)
    argv = ["ui", "tap", str(x), str(y)]
    if duration is not None:
        argv += ["--duration", str(duration)]  # seconds; idb's long press
    run_idb(idb, argv, udid=target.target_id, timeout=20,
            companion=target_companion(target))


def swipe(target: Target, x1: int, y1: int, x2: int, y2: int, duration: float,
          *, idb_path: str | None = None) -> None:
    idb = find_idb(idb_path)
    run_idb(
        idb,
        ["ui", "swipe", str(x1), str(y1), str(x2), str(y2), "--duration", str(duration)],
        udid=target.target_id,
        timeout=20,
        companion=target_companion(target),
    )


def text(target: Target, value: str, *, idb_path: str | None = None) -> None:
    idb = find_idb(idb_path)
    run_idb(idb, ["ui", "text", value], udid=target.target_id, timeout=20,
            companion=target_companion(target))


def button(target: Target, name: str, *, idb_path: str | None = None) -> None:
    idb = find_idb(idb_path)
    run_idb(idb, ["ui", "button", name], udid=target.target_id, timeout=10,
            companion=target_companion(target))


def key(target: Target, keycode: str, *, idb_path: str | None = None) -> None:
    idb = find_idb(idb_path)
    run_idb(idb, ["ui", "key", keycode], udid=target.target_id, timeout=10,
            companion=target_companion(target))


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
    run_idb(idb, ["screenshot", str(output)], udid=target.target_id, timeout=60,
            companion=target_companion(target))
    return output


# --- diagnostics -------------------------------------------------------------


def crash_list(target: Target, *, idb_path: str | None = None) -> str:
    idb = find_idb(idb_path)
    return run_idb(idb, ["crash", "list"], udid=target.target_id, timeout=30, check=False,
                   companion=target_companion(target)).stdout or ""


def crash_show(target: Target, name: str, *, idb_path: str | None = None) -> str:
    idb = find_idb(idb_path)
    return run_idb(idb, ["crash", "show", name], udid=target.target_id, timeout=30,
                   companion=target_companion(target)).stdout or ""


def file_pull(target: Target, bundle_id: str, remote: str, local: Path,
              *, idb_path: str | None = None) -> Path:
    idb = find_idb(idb_path)
    local.parent.mkdir(parents=True, exist_ok=True)
    run_idb(
        idb,
        ["file", "pull", "--bundle-id", bundle_id, remote, str(local)],
        udid=target.target_id,
        timeout=60,
        companion=target_companion(target),
    )
    return local
