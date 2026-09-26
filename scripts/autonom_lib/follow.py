"""Live session observation: bounded follows of append-only files (§2L).

Three follow shapes share one NDJSON line protocol on stdout:

- ``follow_file``    — tail a file under the session artifacts dir;
- ``follow_process`` — stream a device-log subprocess (adb logcat, log stream);
  ``follow_processes`` merges several (a uid logcat plus a lifecycle one);
- ``follow_poll``    — poll a store and emit only items not seen before.

Every follow is bounded by ``--max-seconds`` / ``--max-lines`` and always ends
with one ``{"kind": "eof", "reason": …}`` line, so an agent in CI can never
hang on observation. Files are confined to the session's ``artifacts_dir`` —
the follow verbs read evidence, they are not a general file tailer.

Files are read in binary and split on ``\\n`` before decoding: byte offsets
stay exact for the rotation check (text-mode ``tell()`` returns an opaque
cookie once the incremental decoder holds state), and a multibyte character
split across writes is only decoded when its line completes.
"""
from __future__ import annotations

import os
import re
import selectors
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import errors

# Directories scanned when a session predates the streams[] registry (or a
# writer forgot to register). Kind mirrors the registered vocabulary.
_SCAN_DIRS = (("output", "output"), ("logs", "device_log"), ("network", "network"))
_SCAN_SUFFIXES = {".log", ".ndjson", ".jsonl", ".txt"}
# Evidence a session writes but nobody tails: metric snapshots, heap dumps,
# trace bundles (an Instruments `.trace` is a directory) and screen
# recordings. `session outputs` used to omit them, so a run's heaviest
# evidence was invisible to the verb that lists a session's outputs. Every
# entry is listed; only text files are marked followable.
_ARTIFACT_DIRS = (("metrics", "metrics"), ("recordings", "recording"))

_READ_CHUNK = 1 << 20  # drain in bounded slices, never the whole file at once


def _opener() -> str | None:
    """The desktop "open this file" command for this host, if any: `open`
    on macOS, `xdg-open` where it is installed, nothing otherwise."""
    if sys.platform == "darwin":
        return "open"
    return "xdg-open" if shutil.which("xdg-open") else None


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
           label: str | None = None, pid: int | None = None,
           followable: bool = True) -> dict[str, Any]:
    path = base / rel
    entry: dict[str, Any] = {
        "id": stream_id,
        "kind": kind,
        "path": rel,
        "abs_path": str(path),
        "exists": path.is_file() or (not followable and path.exists()),
    }
    if path.is_file():
        stat = path.stat()
        entry["bytes"] = stat.st_size
        entry["mtime"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime))
    elif entry["exists"]:  # a bundle directory such as an Instruments .trace
        entry["directory"] = True
        entry["mtime"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime))
    if label:
        entry["label"] = label
    if pid:
        entry["pid"] = pid
    entry["followable"] = followable
    if not followable:
        opener = _opener()
        if opener:  # no hint where no opener is known; abs_path is the answer
            entry["shell_hint"] = f"{opener} {shlex.quote(str(path))}"
        return entry
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
    for dirname, kind in _ARTIFACT_DIRS:
        directory = base / dirname
        if not directory.is_dir():
            continue
        for item in sorted(directory.iterdir()):
            rel = f"{dirname}/{item.name}"
            if rel in seen or item.name.startswith("."):
                continue
            seen.add(rel)
            text = item.is_file() and item.suffix in _SCAN_SUFFIXES
            entries.append(_entry(base, stream_id=rel.replace("/", ":", 1),
                                  kind=kind, rel=rel, followable=text))
    return entries


def resolve_source(record: dict[str, Any], source: str) -> Path:
    """Map a --source value (stream id or dir:name form) to a confined path."""
    base = Path(record["artifacts_dir"])
    for stream in record.get("streams") or []:
        if stream.get("id") == source and stream.get("path"):
            return confine(base, stream["path"])
    if ":" in source:
        dirname, name = source.split(":", 1)
        if dirname in {d for d, _ in _SCAN_DIRS + _ARTIFACT_DIRS}:
            return confine(base, f"{dirname}/{name}")
    if source == "journal":
        return confine(base, "journal.ndjson")
    # `device` (the live device log) is always followable; it is answered
    # by the CLI before this lookup, but an agent reading the list must see it.
    known = ", ".join(sorted({e["id"] for e in catalog(record)} | {"device"}))
    raise errors.AutonomError(
        errors.STREAM_NOT_FOUND,
        f"no session stream named {source!r} (known: {known})",
        "List followable streams with 'autonom session outputs', or pass "
        "--path relative to the artifacts dir.",
    )


def default_source(record: dict[str, Any]) -> str | None:
    """The stream `logs follow` follows when given neither --source nor --path.

    When the session has exactly one device-log stream there is nothing to
    choose between, so it is the answer; otherwise None and the caller asks.
    """
    device_streams = [entry["id"] for entry in catalog(record)
                      if entry.get("kind") == "device_log"]
    return device_streams[0] if len(device_streams) == 1 else None


def _compile(grep: str | None) -> re.Pattern[str] | None:
    if not grep:
        return None
    try:
        # IGNORECASE matches the twins: `logs tail --grep` and `journal --grep`.
        return re.compile(grep, re.IGNORECASE)
    except re.error as exc:
        # `logs follow` has always answered backend_failed here and callers
        # pin it; the code stays (`logs tail --grep` uses invalid_value).
        raise errors.AutonomError(
            errors.BACKEND_FAILED, f"invalid --grep regex {grep!r}: {exc}",
            "The filter is a Python regular expression; escape literal "
            "characters such as ( [ * with a backslash.",
        ) from exc


def _decode(raw_line: bytes) -> str:
    return raw_line.decode("utf-8", errors="replace").rstrip("\r")


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
    line_filter: Callable[[str], bool] | None = None,
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
    position = 0
    buffer = b""

    if not path.is_file():
        from_start = True  # a file born after the follow began is all new

    try:
        while True:
            if handle is None and path.is_file():
                handle = path.open("rb")
                inode = os.fstat(handle.fileno()).st_ino
                buffer = b""
                if not from_start:
                    handle.seek(0, os.SEEK_END)
                position = handle.tell()
                from_start = True  # a rotated replacement is always read fully
            if handle is not None:
                try:
                    stat = path.stat()
                    rotated = stat.st_ino != inode or stat.st_size < position
                except OSError:
                    rotated = True  # deleted; wait for the writer to recreate it
                if rotated:
                    handle.close()
                    handle = None
                    continue
                chunk = handle.read(_READ_CHUNK)
                if chunk:
                    position += len(chunk)
                    buffer += chunk
                    *complete, buffer = buffer.split(b"\n")
                    for raw_line in complete:
                        line = _decode(raw_line)
                        if pattern and not pattern.search(line):
                            continue
                        if line_filter and not line_filter(line):
                            continue
                        if raw:
                            emit(line)
                        else:
                            emit({"kind": "line", "source": source,
                                  "ts": _now(), "text": line})
                        emitted += 1
                        if max_lines and emitted >= max_lines:
                            return _eof_line(emit, "max_lines", emitted, source)
                    continue  # drain to EOF before checking the clock
            if deadline is not None and clock() >= deadline:
                return _eof_line(emit, "max_seconds", emitted, source)
            pause = max(poll_ms, 20) / 1000.0
            if deadline is not None:
                pause = min(pause, max(0.0, deadline - clock()))
            sleep(pause)
    finally:
        if handle is not None:
            handle.close()


def _eof_line(emit: Callable[[Any], None], reason: str, emitted: int,
              source: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"kind": "eof", "reason": reason, "lines": emitted,
               "source": source}
    for key, value in (detail or {}).items():
        payload.setdefault(key, value)
    emit(payload)
    return payload


def follow_process(
    argv: list[str],
    *,
    source: str,
    emit: Callable[[Any], None],
    max_seconds: float = 0.0,
    max_lines: int = 0,
    grep: str | None = None,
    line_filter: Callable[[str], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    warnings: list[dict[str, Any]] | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stream a log subprocess's stdout line by line until a bound is hit or
    the process ends. The child is always reaped; when it closes its stream,
    a final unterminated line is still emitted — the writer is done, so the
    fragment is complete evidence."""
    return follow_processes([(argv, line_filter)], source=source, emit=emit,
                            max_seconds=max_seconds, max_lines=max_lines, grep=grep,
                            clock=clock, warnings=warnings, detail=detail)


_DEDUP_WINDOW = 512  # recent lines remembered per stream for cross-stream dedup


def follow_processes(
    streams: list[tuple[Any, ...]],
    *,
    source: str,
    emit: Callable[[Any], None],
    max_seconds: float = 0.0,
    max_lines: int = 0,
    grep: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    warnings: list[dict[str, Any]] | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Follow several log subprocesses as one stream (`logs follow --package`
    on API 31+ runs the app's uid logcat and a lifecycle-tag logcat side by
    side, because one logcat cannot OR the two filters).

    Each ``(argv, line_filter[, ended_warning])`` entry is one child. A line
    both children print — the app's own AndroidRuntime lines are in each — is
    emitted once. ``warnings`` are emitted first as ``{"kind": "warning",
    ...}`` lines, so a degraded filter is announced before its output; a
    child that ends while others still run emits its ``ended_warning`` the
    same way, so a follow never silently loses half its filter. ``detail`` is
    merged into the final eof line. The stream ends when every child has
    ended, or at the first bound.
    """
    pattern = _compile(grep)
    deadline = clock() + max_seconds if max_seconds > 0 else None
    for warning in warnings or []:
        emit({"kind": "warning", "source": source, **warning})
    emitted = 0
    processes: list[subprocess.Popen] = []
    selector = selectors.DefaultSelector()
    reason = None
    buffers: dict[int, bytes] = {}
    filters: dict[int, Callable[[str], bool] | None] = {}
    ended_warnings: dict[int, dict[str, Any] | None] = {}
    recent: dict[int, list[str]] = {}

    def push(index: int, line: str) -> bool:
        """Emit one line; True when the max_lines bound has been reached."""
        nonlocal emitted
        if pattern and not pattern.search(line):
            return False
        line_filter = filters.get(index)
        if line_filter and not line_filter(line):
            return False
        if len(processes) > 1:
            for other, lines in recent.items():
                if other != index and line in lines:
                    lines.remove(line)  # seen from the other child: once is enough
                    return False
            mine = recent.setdefault(index, [])
            mine.append(line)
            del mine[:-_DEDUP_WINDOW]
        emit({"kind": "line", "source": source, "ts": _now(), "text": line})
        emitted += 1
        return bool(max_lines and emitted >= max_lines)

    try:
        for index, spec in enumerate(streams):
            argv, line_filter = spec[0], spec[1]
            ended_warnings[index] = spec[2] if len(spec) > 2 else None
            try:
                process = subprocess.Popen(  # noqa: S603 - argv built by the caller
                    argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError as exc:
                raise errors.AutonomError(
                    errors.BACKEND_FAILED, f"could not start {argv[0]}: {exc}")
            assert process.stdout is not None
            processes.append(process)
            filters[index] = line_filter
            buffers[index] = b""
            selector.register(process.stdout, selectors.EVENT_READ, index)
        open_streams = len(processes)
        while reason is None and open_streams:
            timeout = 0.25
            if deadline is not None:
                timeout = min(timeout, max(0.0, deadline - clock()))
            for key, _events in selector.select(timeout):
                index = key.data
                chunk = os.read(key.fileobj.fileno(), 65536)  # type: ignore[union-attr]
                if not chunk:  # this child closed stdout: flush its tail
                    selector.unregister(key.fileobj)
                    open_streams -= 1
                    if buffers[index] and reason is None:
                        if push(index, _decode(buffers[index])):
                            reason = "max_lines"
                        buffers[index] = b""
                    if reason is None and open_streams and ended_warnings.get(index):
                        emit({"kind": "warning", "source": source,
                              **ended_warnings[index]})  # type: ignore[arg-type]
                    continue
                buffers[index] += chunk
                *complete, buffers[index] = buffers[index].split(b"\n")
                for raw_line in complete:
                    if push(index, _decode(raw_line)):
                        reason = "max_lines"
                        break
                if reason is not None:
                    break
            if reason is None and deadline is not None and clock() >= deadline:
                reason = "max_seconds"
    finally:
        selector.close()
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout is not None:
                process.stdout.close()
    payload = _eof_line(emit, reason or "stream_ended", emitted, source, detail)
    return payload


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
        pause = max(interval, 0.05)
        if deadline is not None:
            # never sleep past the deadline: --max-seconds is a hard bound
            pause = min(pause, max(0.05, deadline - clock()))
        sleep(pause)
