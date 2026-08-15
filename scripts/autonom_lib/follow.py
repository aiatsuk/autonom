"""Live session observation: bounded follows of append-only files (§2L).

Three follow shapes share one NDJSON line protocol on stdout:

- ``follow_file``    — tail a file under the session artifacts dir;
- ``follow_process`` — stream a device-log subprocess (adb logcat, log stream);
- ``follow_poll``    — poll a store and emit only items not seen before.

Every follow is bounded by ``--max-seconds`` / ``--max-lines`` and always ends
with one ``{"kind": "eof", "reason": …}`` line, so an agent in CI can never
hang on observation. Files are confined to the session's ``artifacts_dir`` —
the follow verbs read evidence, they are not a general file tailer.
"""
from __future__ import annotations

import os
import re
import selectors
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from . import errors

# Directories scanned when a session predates the streams[] registry (or a
# writer forgot to register). Kind mirrors the registered vocabulary.
_SCAN_DIRS = (("output", "output"), ("logs", "device_log"), ("network", "network"))
_SCAN_SUFFIXES = {".log", ".ndjson", ".jsonl", ".txt"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def confine(artifacts_dir: Path, raw: str) -> Path:
    """Resolve `raw` (relative or absolute) and refuse anything outside the
    session artifacts dir. Symlinks are resolved before the check."""
    base = artifacts_dir.resolve()
    candidate = Path(raw) if os.path.isabs(raw) else base / raw
    resolved = candidate.resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        raise errors.AutonomError(
            errors.PATH_FORBIDDEN,
            f"path escapes the session artifacts dir: {raw}",
            "Follow verbs only read files under the session's artifacts_dir; "
            "list them with 'autonom session outputs'.",
        )
    return resolved


def _entry(base: Path, *, stream_id: str, kind: str, rel: str,
           label: str | None = None, pid: int | None = None) -> dict[str, Any]:
    path = base / rel
    entry: dict[str, Any] = {
        "id": stream_id,
        "kind": kind,
        "path": rel,
        "abs_path": str(path),
        "exists": path.is_file(),
    }
    if entry["exists"]:
        stat = path.stat()
        entry["bytes"] = stat.st_size
        entry["mtime"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime))
    if label:
        entry["label"] = label
    if pid:
        entry["pid"] = pid
    if kind == "journal":
        entry["follow_hint"] = "autonom journal --follow"
    else:
        entry["follow_hint"] = f"autonom logs follow --path {rel}"
    entry["shell_hint"] = f"tail -f '{path}'"
    return entry


def catalog(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Followable streams: registered first, then a conventional directory
    scan for anything a writer did not register, then the journal."""
    base = Path(record["artifacts_dir"])
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for stream in record.get("streams") or []:
        rel = stream.get("path")
        if not rel or rel in seen:
            continue
        seen.add(rel)
        entries.append(_entry(base, stream_id=stream.get("id") or rel,
                              kind=stream.get("kind") or "output", rel=rel,
                              label=stream.get("label"), pid=stream.get("pid")))
    for dirname, kind in _SCAN_DIRS:
        directory = base / dirname
        if not directory.is_dir():
            continue
        for file in sorted(directory.iterdir()):
            rel = f"{dirname}/{file.name}"
            if (not file.is_file() or file.suffix not in _SCAN_SUFFIXES
                    or rel in seen):
                continue
            seen.add(rel)
            entries.append(_entry(base, stream_id=rel.replace("/", ":", 1),
                                  kind=kind, rel=rel))
    if "journal.ndjson" not in seen and (base / "journal.ndjson").is_file():
        entries.append(_entry(base, stream_id="journal", kind="journal",
                              rel="journal.ndjson"))
    return entries


def resolve_source(record: dict[str, Any], source: str) -> Path:
    """Map a --source value (stream id or dir:name form) to a confined path."""
    base = Path(record["artifacts_dir"])
    for stream in record.get("streams") or []:
        if stream.get("id") == source and stream.get("path"):
            return confine(base, stream["path"])
    if ":" in source:
        dirname, name = source.split(":", 1)
        if dirname in {d for d, _ in _SCAN_DIRS}:
            return confine(base, f"{dirname}/{name}")
    if source == "journal":
        return base / "journal.ndjson"
    known = ", ".join(sorted(e["id"] for e in catalog(record))) or "none"
    raise errors.AutonomError(
        errors.STREAM_NOT_FOUND,
        f"no session stream named {source!r} (known: {known})",
        "List followable streams with 'autonom session outputs', or pass "
        "--path relative to the artifacts dir.",
    )


def _compile(grep: str | None) -> re.Pattern[str] | None:
    if not grep:
        return None
    try:
        return re.compile(grep)
    except re.error as exc:
        raise errors.AutonomError(
            errors.BACKEND_FAILED, f"invalid --grep regex: {exc}",
            "The filter is a Python regular expression.",
        )


def follow_file(
    path: Path,
    *,
    source: str,
    emit: Callable[[Any], None],
    from_start: bool = False,
    max_seconds: float = 0.0,
    max_lines: int = 0,
    grep: str | None = None,
    poll_ms: int = 250,
    raw: bool = False,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Tail one file. A missing file is polled for until the deadline — a
    registered stream may not have been written to yet. Rotation (the file
    shrank or was replaced) reopens from the start of the new file."""
    pattern = _compile(grep)
    deadline = clock() + max_seconds if max_seconds > 0 else None
    emitted = 0
    handle = None
    inode = None
    buffer = ""

    def _eof(reason: str) -> dict[str, Any]:
        if handle is not None:
            handle.close()
        payload = {"kind": "eof", "reason": reason, "lines": emitted,
                   "source": source}
        emit(payload)
        return payload

    if not path.is_file():
        from_start = True  # a file born after the follow began is all new

    while True:
        if handle is None and path.is_file():
            handle = path.open("r", encoding="utf-8", errors="replace")
            inode = os.fstat(handle.fileno()).st_ino
            buffer = ""
            if not from_start:
                handle.seek(0, os.SEEK_END)
            from_start = True  # a rotated replacement is always read fully
        if handle is not None:
            try:
                stat = path.stat()
                rotated = stat.st_ino != inode or stat.st_size < handle.tell()
            except OSError:
                rotated = True  # deleted; wait for the writer to recreate it
            if rotated:
                handle.close()
                handle = None
                continue
            chunk = handle.read()
            if chunk:
                buffer += chunk
                *complete, buffer = buffer.split("\n")
                for line in complete:
                    if pattern and not pattern.search(line):
                        continue
                    if raw:
                        emit(line)
                    else:
                        emit({"kind": "line", "source": source, "ts": _now(),
                              "text": line})
                    emitted += 1
                    if max_lines and emitted >= max_lines:
                        return _eof("max_lines")
                continue  # drain to EOF before checking the clock
        if deadline is not None and clock() >= deadline:
            return _eof("max_seconds")
        sleep(max(poll_ms, 20) / 1000.0)


def follow_process(
    argv: list[str],
    *,
    source: str,
    emit: Callable[[Any], None],
    max_seconds: float = 0.0,
    max_lines: int = 0,
    grep: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Stream a log subprocess's stdout line by line until a bound is hit or
    the process ends. The child is always reaped."""
    pattern = _compile(grep)
    deadline = clock() + max_seconds if max_seconds > 0 else None
    emitted = 0
    try:
        process = subprocess.Popen(  # noqa: S603 - argv built by the caller
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise errors.AutonomError(
            errors.BACKEND_FAILED, f"could not start {argv[0]}: {exc}")
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    buffer = b""
    reason = "stream_ended"
    try:
        while True:
            timeout = 0.25
            if deadline is not None:
                timeout = min(timeout, max(0.0, deadline - clock()))
            ready = selector.select(timeout)
            if ready:
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break  # process closed stdout
                buffer += chunk
                *complete, buffer = buffer.split(b"\n")
                for raw_line in complete:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r")
                    if pattern and not pattern.search(line):
                        continue
                    emit({"kind": "line", "source": source, "ts": _now(),
                          "text": line})
                    emitted += 1
                    if max_lines and emitted >= max_lines:
                        reason = "max_lines"
                        raise _Stop
            if deadline is not None and clock() >= deadline:
                reason = "max_seconds"
                raise _Stop
    except _Stop:
        pass
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()
    payload = {"kind": "eof", "reason": reason, "lines": emitted, "source": source}
    emit(payload)
    return payload


class _Stop(Exception):
    """Internal: unwind the follow loop when a bound is reached."""


def follow_poll(
    fetch_new: Callable[[], list[dict[str, Any]]],
    *,
    emit: Callable[[Any], None],
    interval: float = 1.0,
    max_seconds: float = 0.0,
    max_items: int = 0,
    item_kind: str = "flow",
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll `fetch_new` (which owns dedup) and emit each new item once."""
    deadline = clock() + max_seconds if max_seconds > 0 else None
    emitted = 0

    def _eof(reason: str) -> dict[str, Any]:
        payload = {"kind": "eof", "reason": reason, "count": emitted}
        emit(payload)
        return payload

    while True:
        for item in fetch_new():
            emit({"kind": item_kind, item_kind: item})
            emitted += 1
            if max_items and emitted >= max_items:
                return _eof("max")
        if deadline is not None and clock() >= deadline:
            return _eof("max_seconds")
        sleep(max(interval, 0.05))
