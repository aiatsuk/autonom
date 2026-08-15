"""Screen capture, and the evidence trail around it.

Capturing the pixels is the easy half. The half that decides whether a
screenshot is worth anything a week later is what is recorded *with* it:

- a name a human can read, so a directory listing is a story rather than
  `shot1.png … shot9.png`;
- the session, target and app it belongs to, so shots from three runs cannot be
  confused for each other;
- what was on screen — the foreground activity;
- **whether any mock was active.** A screenshot of fabricated data is
  indistinguishable from the real thing once it leaves the terminal. Recording
  that fact is the difference between evidence and a convincing lie, and it is
  the single most important field here.

Metadata is written twice, on purpose. It goes into the PNG itself as `iTXt`
chunks, so it survives being copied into a ticket, and into a per-session
`index.jsonl`, so it can be searched without opening every file.
"""
from __future__ import annotations

import json
import os
import re
import struct
import time
import zlib
from pathlib import Path
from typing import Any

from . import adb as adb_mod
from . import errors, ios_simctl
from .platform import ANDROID, Target

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
METADATA_PREFIX = "autonom:"


def capture(adb: str, serial: str, output: Path) -> Path:
    """Android screencap. Unchanged 0.4.0 entry point."""
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    completed = adb_mod.run_adb(
        adb,
        ["exec-out", "screencap", "-p"],
        serial=serial,
        timeout=30,
        check=True,
        binary=True,
    )
    assert isinstance(completed.stdout, bytes)
    data = completed.stdout
    # Some adb versions CRLF-mangle PNG; fix common case
    if data.startswith(b"\x89PNG") is False and b"\r\n" in data[:20]:
        data = data.replace(b"\r\n", b"\n")
    output.write_bytes(data)
    return output


def capture_target(target: Target, output: Path) -> Path:
    """Dispatch by platform.

    iOS goes through `simctl io screenshot`, not idb, so visual evidence stays
    available exactly when the companion is down and an agent most needs it
    (CAP-IOSUI-007, RISK-016).
    """
    if target.platform == ANDROID:
        return capture(target.tool, target.target_id, output)
    try:
        return ios_simctl.screenshot(target.tool, target.target_id, output)
    except errors.AutonomError:
        from . import ios_idb

        return ios_idb.screenshot(target, output)


# --- naming -------------------------------------------------------------------


def slugify(text: str | None, limit: int = 48) -> str:
    """Readable, not ASCII-only: an accented label stays legible in `ls`."""
    cleaned = re.sub(r"[^\w\-]+", "-", (text or "").strip(), flags=re.UNICODE)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-").lower()
    return cleaned[:limit] or "shot"


def next_index(directory: Path) -> int:
    """Sequence per directory, so a listing sorts in capture order."""
    highest = 0
    if directory.is_dir():
        for entry in directory.glob("*.png"):
            head = entry.name.split("_", 1)[0]
            if head.isdigit():
                highest = max(highest, int(head))
    return highest + 1


def build_filename(index: int, label: str | None, moment: float | None = None) -> str:
    stamp = time.strftime("%H%M%S", time.localtime(moment if moment else time.time()))
    return f"{index:04d}_{stamp}_{slugify(label)}.png"


# --- metadata -----------------------------------------------------------------


def _itxt_chunk(keyword: str, value: str) -> bytes:
    """A UTF-8 PNG text chunk.

    `tEXt` is Latin-1 only, which would mangle any non-Latin label; `iTXt`
    carries UTF-8 and is just as widely readable (`exiftool`, Preview, PIL).
    """
    payload = (
        keyword.encode("ascii", "replace") + b"\x00"
        + b"\x00\x00"   # uncompressed, compression method 0
        + b"\x00"       # language tag: none
        + b"\x00"       # translated keyword: none
        + value.encode("utf-8")
    )
    body = b"iTXt" + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def embed_metadata(path: Path, fields: dict[str, Any]) -> bool:
    """Insert metadata before IEND. Returns False if the file is not a PNG.

    Never raises: losing the annotation must not lose the screenshot.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if not data.startswith(PNG_SIGNATURE):
        return False
    marker = data.rfind(b"IEND")
    if marker < 4:
        return False
    insert_at = marker - 4  # start of IEND's length field
    chunks = b"".join(
        _itxt_chunk(METADATA_PREFIX + key, str(value))
        for key, value in fields.items()
        if value is not None
    )
    try:
        path.write_bytes(data[:insert_at] + chunks + data[insert_at:])
    except OSError:
        return False
    return True


def read_metadata(path: Path) -> dict[str, str]:
    """Read back what `embed_metadata` wrote, without a PNG library."""
    found: dict[str, str] = {}
    try:
        data = path.read_bytes()
    except OSError:
        return found
    offset = len(PNG_SIGNATURE)
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        if kind == b"iTXt":
            keyword, _, rest = payload.partition(b"\x00")
            name = keyword.decode("ascii", "replace")
            if name.startswith(METADATA_PREFIX):
                found[name[len(METADATA_PREFIX):]] = rest[3:].lstrip(b"\x00").decode(
                    "utf-8", "replace"
                )
        if kind == b"IEND":
            break
        offset += 12 + length
    return found


# --- context ------------------------------------------------------------------


def foreground(target: Target) -> str | None:
    """Best-effort "what was on screen". Never fails a capture."""
    if target.platform != ANDROID:
        return None
    try:
        completed = adb_mod.run_adb(
            target.tool, ["shell", "dumpsys", "activity", "activities"],
            serial=target.target_id, timeout=15, check=False,
        )
    except errors.AutonomError:
        return None
    text = completed.stdout if isinstance(completed.stdout, str) else ""
    match = re.search(r"topResumedActivity=ActivityRecord\{\S+ \S+ (\S+)", text)
    return match.group(1) if match else None


def active_mocks() -> dict[str, Any]:
    """Whether this screenshot may be showing fabricated data."""
    try:
        from .network import mocks as mocks_mod

        rules = mocks_mod.active()
    except Exception:  # noqa: BLE001 - annotation must never break a capture
        return {"active": 0, "ids": []}
    return {"active": len(rules), "ids": [rule.get("id") for rule in rules]}


# --- evidence-grade capture ---------------------------------------------------


def shots_dir(session: dict[str, Any], task: str | None = None) -> Path:
    path = Path(session["artifacts_dir"]) / "shots"
    if task:
        path = path / slugify(task, limit=32)
    path.mkdir(parents=True, exist_ok=True)
    return path


def index_path(session: dict[str, Any]) -> Path:
    return Path(session["artifacts_dir"]) / "shots" / "index.jsonl"


def append_index(session: dict[str, Any], entry: dict[str, Any]) -> None:
    path = index_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if not existed:
        os.chmod(path, 0o600)


def load_index(session: dict[str, Any]) -> list[dict[str, Any]]:
    path = index_path(session)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn final line is not a reason to lose the rest
    return entries


def capture_evidence(
    target: Target,
    session: dict[str, Any] | None,
    *,
    label: str | None = None,
    task: str | None = None,
    out: Path | None = None,
) -> dict[str, Any]:
    """Capture, annotate, and file the shot so it is findable later.

    With a session, the canonical copy always lands in the session's `shots/`
    even when `--out` is given: `--out` used to divert the only copy away from
    the run's own evidence directory, so the artifact archive of a session
    contained no pictures of it.
    """
    moment = time.time()
    if session:
        directory = shots_dir(session, task)
        destination = directory / build_filename(next_index(directory), label, moment)
    else:
        destination = out or Path(build_filename(1, label, moment))

    path = capture_target(target, destination)

    meta = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment)),
        "label": label,
        "task": task,
        "session_id": (session or {}).get("session_id"),
        "platform": target.platform,
        "target_id": target.target_id,
        "app_id": (session or {}).get("app_id"),
        "foreground": foreground(target),
    }
    mocks = active_mocks()
    meta["mocks_active"] = mocks["active"]
    if mocks["active"]:
        meta["mocks"] = ",".join(str(item) for item in mocks["ids"])

    embedded = embed_metadata(path, meta)

    copies = [str(path)]
    if session and out:
        extra = Path(out).expanduser()
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_bytes(path.read_bytes())
        copies.append(str(extra))

    result: dict[str, Any] = {"path": str(path), "copies": copies,
                              "metadata": meta, "metadata_embedded": embedded}
    if session:
        # Resolve both sides: the capture path is resolved, but artifacts_dir is
        # stored as written, so under a symlinked root (macOS /var -> /private/var,
        # or a global session home on one) relative_to would wrongly reject it.
        artifacts = Path(session["artifacts_dir"]).resolve()
        append_index(session, {**meta, "file": str(path.resolve().relative_to(artifacts))})
    if mocks["active"]:
        result["warnings"] = [{
            "code": "screenshot_shows_mocked_data",
            "error": f"{mocks['active']} mock rule(s) were active — this image may "
                     f"show fabricated responses",
            "hint": "The fact is embedded in the PNG and the index. Do not present "
                    "this as evidence of real backend behaviour.",
        }]
    return result
