from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from . import adb as adb_mod
from . import ios_simctl
from .platform import ANDROID, Target


def tail_logcat(
    adb: str,
    serial: str,
    *,
    package: str | None = None,
    since_seconds: float | None = 30,
    max_lines: int = 200,
    grep: str | None = None,
) -> list[dict[str, Any]]:
    args = ["logcat", "-d", "-v", "threadtime"]
    cutoff = None
    if since_seconds is not None and since_seconds > 0:
        cutoff = _device_cutoff(adb, serial, since_seconds)
        if cutoff:
            # Let logcat do the windowing, in its own clock. Dumping the whole
            # buffer and filtering here was both slow (80k+ lines on a busy
            # emulator) and wrong whenever the two clocks had drifted.
            args += ["-t", cutoff]
    completed = adb_mod.run_adb(adb, args, serial=serial, timeout=20, check=True)
    assert isinstance(completed.stdout, str)
    lines = completed.stdout.splitlines()
    if since_seconds is not None and since_seconds > 0 and not cutoff:
        # Degraded: the device clock was unreadable, so fall back to comparing
        # against the host's. Accurate only while the two agree.
        lines = _filter_recent(lines, since_seconds)
    if package:
        pid = _pid_for_package(adb, serial, package)
        if pid:
            lines = [line for line in lines if f" {pid} " in f" {line} "]
    if grep:
        pattern = re.compile(grep, re.IGNORECASE)
        lines = [line for line in lines if pattern.search(line)]
    if max_lines > 0:
        lines = lines[-max_lines:]
    return [{"line": line} for line in lines]


def _pid_for_package(adb: str, serial: str, package: str) -> str | None:
    completed = adb_mod.run_adb(
        adb,
        ["shell", "pidof", "-s", package],
        serial=serial,
        timeout=10,
        check=False,
    )
    assert isinstance(completed.stdout, str)
    pid = completed.stdout.strip().split()
    return pid[0] if pid else None


_TS = re.compile(r"^(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})")

# No space, so `adb shell date <fmt>` cannot be split into two arguments by the
# device shell — which silently truncated the answer to "08-06".
_DEVICE_CLOCK_FORMAT = "+%m-%d_%H:%M:%S"


def _device_cutoff(adb: str, serial: str, since_seconds: float) -> str | None:
    """A `logcat -t` cutoff expressed in the **device's** wall clock.

    logcat stamps every line with the device clock, so a window computed from
    the host clock is wrong by however far the two have drifted. An emulator
    that outlived a host sleep is routinely tens of seconds behind — enough to
    empty a `--since 30` window entirely while the log is busy, which is
    exactly what happened: `logs tail` reported one irrelevant line while 320
    matching ones sat in the buffer.

    Returns None when the device clock cannot be read; the caller then falls
    back to host-clock filtering and says so.
    """
    completed = adb_mod.run_adb(
        adb, ["shell", "date", _DEVICE_CLOCK_FORMAT],
        serial=serial, timeout=10, check=False,
    )
    text = (completed.stdout or "").strip().replace("_", " ")
    try:
        stamp = time.strptime(f"{time.localtime().tm_year} {text}", "%Y %m-%d %H:%M:%S")
    except ValueError:
        return None
    # Parsing and formatting both go through the host timezone, so it cancels
    # out and what comes back is the device's own wall clock.
    return time.strftime(
        "%m-%d %H:%M:%S.000", time.localtime(time.mktime(stamp) - since_seconds)
    )


def _filter_recent(lines: list[str], since_seconds: float) -> list[str]:
    """Host-clock fallback. Returns exactly what matched — including nothing.

    The previous version substituted `lines[-200:]` when the window came up
    empty, so a filter that had failed returned a plausible-looking tail from
    an unrelated moment and no way to tell. An empty window is an answer.
    """
    cutoff = time.time() - since_seconds
    kept: list[str] = []
    year = time.localtime().tm_year
    for line in lines:
        match = _TS.match(line)
        if not match:
            continue
        try:
            ts = time.strptime(f"{year} {match.group(1)}", "%Y %m-%d %H:%M:%S.%f")
        except ValueError:
            kept.append(line)
            continue
        if time.mktime(ts) >= cutoff:
            kept.append(line)
    return kept


# --- iOS ---------------------------------------------------------------------


def start_log_stream(target: Target, destination: Path, *, bundle_id: str | None = None) -> int | None:
    """Start a session-long `log stream` writing ndjson to the artifacts dir.

    Returns the pid so `session stop` can reap it (INV-10). Failure is not fatal:
    logs are supplementary evidence and must never block the UI loop.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    args = [target.tool, "simctl", "spawn", target.target_id, "log", "stream",
            "--style", "ndjson", "--level", "info"]
    if bundle_id:
        args += ["--predicate", _ios_predicate(bundle_id)]
    try:
        handle = open(destination, "ab")
        process = subprocess.Popen(  # noqa: S603 - argv is constructed, never shell
            args, stdout=handle, stderr=subprocess.DEVNULL, start_new_session=True
        )
        return process.pid
    except OSError:
        return None


def _ios_predicate(bundle_id: str) -> str:
    leaf = bundle_id.rsplit(".", 1)[-1]
    return (
        f'subsystem == "{bundle_id}" OR processImagePath CONTAINS "{leaf}" '
        f'OR senderImagePath CONTAINS "{leaf}"'
    )


def _read_tail(path: Path, max_lines: int) -> list[str]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        # Debug sessions produce modest files; a bounded deque keeps memory flat.
        from collections import deque

        return list(deque(handle, maxlen=max(1, max_lines)))


def tail_ios(
    target: Target,
    *,
    stream_path: Path | None = None,
    package: str | None = None,
    since_seconds: float | None = 30,
    max_lines: int = 200,
    grep: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    lines: list[str] = []

    if stream_path and stream_path.exists():
        lines = [line.rstrip("\n") for line in _read_tail(stream_path, max_lines * 4)]
    else:
        args = ["spawn", target.target_id, "log", "show",
                "--style", "ndjson", "--last", f"{int(since_seconds or 30)}s"]
        if package:
            args += ["--predicate", _ios_predicate(package)]
        completed = ios_simctl.run_simctl(target.tool, args, timeout=30, check=False)
        if completed.returncode != 0:
            warnings.append({
                "code": "log_backend_unavailable",
                "error": (completed.stderr or "").strip()[:400],
                "hint": "Start the session with --log-stream for a continuous buffer.",
            })
        else:
            lines = (completed.stdout or "").splitlines()

    if package and stream_path:
        lines = [line for line in lines if package.rsplit(".", 1)[-1] in line or package in line]
    if grep:
        pattern = re.compile(grep, re.IGNORECASE)
        lines = [line for line in lines if pattern.search(line)]
    if max_lines > 0:
        lines = lines[-max_lines:]
    return [{"line": _compact_ndjson(line)} for line in lines if line.strip()], warnings


def _compact_ndjson(line: str) -> str:
    """Render one ndjson log record as a compact human line; pass through on failure."""
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return line
    if not isinstance(record, dict):
        return line
    parts = [
        record.get("timestamp", ""),
        record.get("processImagePath", "").rsplit("/", 1)[-1],
        record.get("messageType", ""),
        record.get("subsystem", ""),
        record.get("eventMessage", ""),
    ]
    return " ".join(str(part) for part in parts if part).strip() or line


def tail(
    target: Target,
    *,
    stream_path: Path | None = None,
    package: str | None = None,
    since_seconds: float | None = 30,
    max_lines: int = 200,
    grep: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if target.platform == ANDROID:
        entries = tail_logcat(
            target.tool,
            target.target_id,
            package=package,
            since_seconds=since_seconds,
            max_lines=max_lines,
            grep=grep,
        )
        return entries, []
    return tail_ios(
        target,
        stream_path=stream_path,
        package=package,
        since_seconds=since_seconds,
        max_lines=max_lines,
        grep=grep,
    )
