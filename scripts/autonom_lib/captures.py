"""The Canvas captures store: screenshots and screen recordings per device.

Captures live where a person finds them: `<root>/<device folder>/`, the root
being `--captures-dir`, else `$AUTONOM_CAPTURES_DIR`, else `~/Downloads/Autonom`.
File names read well in Finder: `<Folder name> YYYY-MM-DD HH.MM.SS.png|mp4` in
local time, with ` (2)` ... ` (999)` on a clash.

A hidden index (`.autonom-captures.json`, written atomically, mode 0600) lists
the captures this store made. The store lists, serves, deletes and prunes only
files in its index: a file the user put in the folder is never touched, and an
index entry whose file was removed in Finder is dropped on the next list.

Limits per device folder: when a commit would pass `MAX_ITEMS` files or
`MAX_BYTES` bytes, the oldest indexed captures are deleted until the new one
fits. Their names are returned as `pruned`, and `last_pruned` says so to the
gallery.

A capture is written to a `.partial-<12 hex>.<ext>` file first and renamed into
place only once it is complete, so a half-written file never shows under a
capture name; `sweep` removes partial files a stopped process left behind.
"""
from __future__ import annotations

import errno
import json
import os
import re
import secrets
import stat
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import errors

ROOT_ENV = "AUTONOM_CAPTURES_DIR"
DEFAULT_ROOT = Path.home() / "Downloads" / "Autonom"
MAX_ITEMS = 200
MAX_BYTES = 2 * 1024**3
SCREENSHOT_MAX = 32 * 1024**2
VIDEO_MAX = 1024**3
INDEX = ".autonom-captures.json"
LIST_DEFAULT = 200
LIST_MAX = 500
MAX_CLASH = 999
FOLDER_NAME_MAX = 60
PARTIAL_PREFIX = ".partial-"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
KINDS = {"screenshot": ("png", "image/png"), "video": ("mp4", "video/mp4")}
CONTENT_TYPES = {ext: content_type for ext, content_type in KINDS.values()}
_UNSAFE = re.compile(r"[^A-Za-z0-9 ._()-]")
_PARTIAL = re.compile(r"\.partial-[0-9a-f]{12}\.(png|mp4)")


def root(explicit: str | None = None) -> Path:
    """`--captures-dir`, else `$AUTONOM_CAPTURES_DIR`, else `~/Downloads/Autonom`."""
    value = explicit or os.environ.get(ROOT_ENV)
    if value:
        return Path(value).expanduser()
    return DEFAULT_ROOT


def _clean(text: str | None) -> str:
    value = _UNSAFE.sub("_", text or "").strip(" ._")
    return value[:FOLDER_NAME_MAX].strip(" ._")


def folder_name(device_name: str | None, target_id: str) -> str:
    """The device folder's name: characters outside `[A-Za-z0-9 ._()-]` become
    `_`, the ends are trimmed, at most 60 characters; an empty result takes
    the same rule on the target id (and `device` when that is empty too)."""
    return _clean(device_name) or _clean(target_id) or "device"


def device_dir(base: Path, device_name: str | None, target_id: str) -> Path:
    """`<root>/<folder name>`, created with mode 0700 when missing."""
    directory = Path(base) / folder_name(device_name, target_id)
    if not directory.is_dir():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    return directory


def device_path(base: Path, device_name: str | None, target_id: str) -> Path:
    """The device folder's path, without creating anything."""
    return Path(base) / folder_name(device_name, target_id)


def temp_path(directory: Path, ext: str) -> Path:
    """A fresh `.partial-<12 hex>.<ext>` path in `directory`."""
    if ext not in CONTENT_TYPES:
        raise ValueError(f"unknown capture extension {ext!r}")
    return Path(directory) / f"{PARTIAL_PREFIX}{secrets.token_hex(6)}.{ext}"


def _now_iso(moment: float) -> str:
    return (datetime.fromtimestamp(moment, timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    """One writer of a folder's index at a time (two Canvases may share a
    device folder). The lock is on the folder itself, so no extra file shows."""
    descriptor = None
    try:
        import fcntl

        descriptor = os.open(str(directory), os.O_RDONLY)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except (ImportError, OSError):
        if descriptor is not None:
            os.close(descriptor)
        descriptor = None
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)  # closing releases the lock


def _index_file(directory: Path) -> Path:
    return Path(directory) / INDEX


def _load(directory: Path) -> dict[str, Any]:
    """The index, or an empty one when it is missing or unreadable."""
    try:
        data = json.loads(_index_file(directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"captures": [], "last_pruned": None}
    if not isinstance(data, dict):
        return {"captures": [], "last_pruned": None}
    entries = [entry for entry in data.get("captures") or []
               if isinstance(entry, dict) and _plain_name(entry.get("name"))]
    pruned = data.get("last_pruned")
    return {"captures": entries,
            "last_pruned": pruned if isinstance(pruned, dict) else None}


def _save(directory: Path, index: dict[str, Any]) -> None:
    """Atomic: a temporary file beside it, then a rename; mode 0600."""
    descriptor, temporary = tempfile.mkstemp(prefix=".autonom-captures.", suffix=".tmp",
                                             dir=str(directory))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(index, handle, indent=1)
            handle.write("\n")
        os.replace(temporary, _index_file(directory))
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _plain_name(name: Any) -> bool:
    """A capture name is one plain, visible file name."""
    return (isinstance(name, str) and 0 < len(name) <= 255 and "/" not in name
            and "\\" not in name and "\x00" not in name and not name.startswith(".")
            and name not in (".", ".."))


def _regular(path: Path) -> os.stat_result | None:
    """The file's stat when it is a regular file and not a symlink."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return info if stat.S_ISREG(info.st_mode) else None


def _remove(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _claim(directory: Path, base: str, ext: str) -> Path:
    """The first free `<base>.<ext>`, `<base> (2).<ext>` ... claimed with O_EXCL."""
    for number in range(1, MAX_CLASH + 1):
        name = f"{base}.{ext}" if number == 1 else f"{base} ({number}).{ext}"
        path = Path(directory) / name
        try:
            descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(descriptor)
        return path
    raise errors.AutonomError(
        errors.BACKEND_FAILED, f"no free capture name for {base!r} in {directory}",
        "Delete or rename older captures in that folder.")


def _check(temp: Path, kind: str) -> int:
    """The finished file's size; refuses an empty, malformed or oversized one."""
    info = _regular(temp)
    if info is None:
        raise errors.AutonomError(errors.BACKEND_FAILED,
                                  f"the {kind} was not written", None)
    size = info.st_size
    limit = SCREENSHOT_MAX if kind == "screenshot" else VIDEO_MAX
    if size > limit:
        raise errors.AutonomError(
            errors.CAPTURE_TOO_LARGE,
            f"the {kind} is {size} bytes; at most {limit} are kept",
            "Nothing was saved. Record a shorter video." if kind == "video"
            else "Nothing was saved.", size=size, limit=limit)
    if kind == "screenshot":
        with open(temp, "rb") as handle:
            head = handle.read(len(PNG_SIGNATURE))
        if head != PNG_SIGNATURE:
            raise errors.AutonomError(
                errors.BACKEND_FAILED, "the screenshot is not a PNG image",
                "Check the device is on and unlocked, then try again.")
    elif size == 0:
        raise errors.AutonomError(
            errors.BACKEND_FAILED, "the recording is empty",
            "Record for at least a second, then stop.")
    return size


def _png_size(path: Path) -> tuple[int | None, int | None]:
    try:
        with open(path, "rb") as handle:
            head = handle.read(24)
    except OSError:
        return None, None
    if len(head) < 24 or not head.startswith(PNG_SIGNATURE) or head[12:16] != b"IHDR":
        return None, None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def _alive_entries(directory: Path, index: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Index entries whose file is still there, and whether any was dropped."""
    kept = [entry for entry in index["captures"]
            if _regular(Path(directory) / entry["name"]) is not None]
    return kept, len(kept) != len(index["captures"])


def commit(temp: Path, directory: Path, kind: str, *, meta: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Move a finished `temp` file into `directory` under its capture name,
    index it and prune the oldest indexed captures past the limits.

    `meta`: `platform`, `target_id`, `device_name`, `duration_ms`, and
    optionally `created` (epoch seconds, default now). Returns the Capture and
    the names of the captures pruned. On any refusal the temp file is removed
    and nothing is saved.
    """
    if kind not in KINDS:
        _remove(Path(temp))
        raise ValueError(f"unknown capture kind {kind!r}")
    temp = Path(temp)
    directory = Path(directory)
    try:
        size = _check(temp, kind)
    except BaseException:
        _remove(temp)
        raise
    ext, _content_type = KINDS[kind]
    moment = float(meta.get("created") or time.time())
    stamp = time.strftime("%Y-%m-%d %H.%M.%S", time.localtime(moment))
    base = f"{directory.name} {stamp}"
    with _locked(directory):
        try:
            final = _claim(directory, base, ext)
            os.replace(temp, final)
        except BaseException:
            _remove(temp)
            raise
        width, height = _png_size(final) if kind == "screenshot" else (None, None)
        capture = {
            "name": final.name,
            "kind": kind,
            "platform": meta.get("platform"),
            "target_id": meta.get("target_id"),
            "device_name": meta.get("device_name"),
            "size": size,
            "created_at": _now_iso(moment),
            "duration_ms": meta.get("duration_ms") if kind == "video" else None,
            "width": width,
            "height": height,
        }
        index = _load(directory)
        entries, _dropped = _alive_entries(directory, index)
        entries = [entry for entry in entries if entry["name"] != final.name]
        pruned: list[str] = []
        total = sum(int(entry.get("size") or 0) for entry in entries)
        # Oldest first: the index keeps commit order.
        while entries and (len(entries) + 1 > MAX_ITEMS or total + size > MAX_BYTES):
            oldest = entries.pop(0)
            total -= int(oldest.get("size") or 0)
            _remove(Path(directory) / oldest["name"])
            pruned.append(oldest["name"])
        entries.append(capture)
        index["captures"] = entries
        if pruned:
            index["last_pruned"] = {"at": _now_iso(time.time()), "count": len(pruned)}
        _save(directory, index)
    return capture, pruned


def list_entries(directory: Path, target_id: str | None, limit: int = LIST_DEFAULT) -> dict[str, Any]:
    """The device's indexed captures, newest first, at most `limit`.

    Entries whose file is gone are dropped from the index here. `count` and
    `total_bytes` cover every capture of this device in the folder. A folder
    that does not exist yet lists as empty and is not created.
    """
    directory = Path(directory)
    result: dict[str, Any] = {
        "captures": [], "count": 0, "total_bytes": 0,
        "limits": {"max_items": MAX_ITEMS, "max_bytes": MAX_BYTES},
        "dir": str(directory), "last_pruned": None,
    }
    if not directory.is_dir():
        return result
    with _locked(directory):
        index = _load(directory)
        entries, dropped = _alive_entries(directory, index)
        if dropped:
            index["captures"] = entries
            _save(directory, index)
    mine = [entry for entry in entries
            if target_id is None or entry.get("target_id") == target_id]
    result["captures"] = list(reversed(mine))[:max(0, int(limit))]
    result["count"] = len(mine)
    result["total_bytes"] = sum(int(entry.get("size") or 0) for entry in mine)
    result["last_pruned"] = index.get("last_pruned")
    return result


def _not_found(name: Any) -> errors.AutonomError:
    shown = name if isinstance(name, str) and len(name) <= 255 else "(invalid)"
    return errors.AutonomError(
        errors.CAPTURE_NOT_FOUND, f"no capture named {shown!r} on this device",
        "Refresh the gallery: the capture may have been deleted or moved in Finder.")


def resolve(directory: Path, name: str) -> Path:
    """The path of an indexed capture: a plain name in the index, a regular
    file and not a symlink, directly inside `directory`; else
    `capture_not_found`."""
    directory = Path(directory)
    if not _plain_name(name) or not directory.is_dir():
        raise _not_found(name)
    index = _load(directory)
    if not any(entry["name"] == name for entry in index["captures"]):
        raise _not_found(name)
    path = directory / name
    if _regular(path) is None:
        raise _not_found(name)
    if os.path.realpath(path.parent) != os.path.realpath(directory):
        raise _not_found(name)
    return path


def entry(directory: Path, name: str) -> dict[str, Any]:
    """`resolve` plus what the file route needs: path, content type and size."""
    path = resolve(directory, name)
    info = _regular(path)
    if info is None:
        raise _not_found(name)
    ext = path.suffix.lstrip(".").lower()
    content_type = CONTENT_TYPES.get(ext)
    if content_type is None:
        raise _not_found(name)
    return {"name": name, "path": str(path), "content_type": content_type,
            "size": info.st_size}


def delete(directory: Path, name: str) -> None:
    """Remove one indexed capture and its index entry."""
    directory = Path(directory)
    with _locked(directory) if directory.is_dir() else _nothing():
        path = resolve(directory, name)
        _remove(path)
        index = _load(directory)
        index["captures"] = [item for item in index["captures"] if item["name"] != name]
        _save(directory, index)


@contextmanager
def _nothing() -> Iterator[None]:
    yield


def sweep(directory: Path, older_than_s: float = 60) -> int:
    """Remove `.partial-*` files older than `older_than_s` (a stopped tools
    process left them). Returns how many were removed. Never creates the folder."""
    directory = Path(directory)
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    cutoff = time.time() - older_than_s
    removed = 0
    for name in names:
        if not _PARTIAL.fullmatch(name):
            continue
        path = directory / name
        info = _regular(path)
        if info is None or info.st_mtime > cutoff:
            continue
        try:
            os.unlink(path)
            removed += 1
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                continue
    return removed
