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

Agent screenshots (AS-03): `screenshot --agent [--max-edge N]`, and a plain
`screenshot` in an agent-profile session on Android, ask the on-device
helper for a JPEG (quality 80) whose long edge is at most N pixels (1000 by
default; a smaller screen is never upscaled), written with a `.jpg` name. The
reply adds `format`, `screen` (the screen size), `scale` (screen pixels per
image pixel) and `coordinates` (how an image pixel becomes a `ui tap
--x/--y`). A JPEG carries no `iTXt` chunks, so its metadata lives in the
index alone (`metadata_embedded: false`); its index row adds `format`,
`screen` and `scale`. Without the helper it is today's full-size PNG with
`scale` 1 and `fallback_reason`; a `--out` asked as `.jpg`/`.jpeg` is then
written with `.png` (warning `out_renamed`), so no image carries another
format's suffix. Every other capture (Flow
`takeScreenshot` included) is the PNG it always was, with the same reply
and index row.
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
JPEG_SIGNATURE = b"\xff\xd8"
METADATA_PREFIX = "autonom:"
# The file names a capture may have: the PNG of every capture, the JPEG of an
# agent screenshot. `next_index` counts both, so numbering never repeats.
SHOT_SUFFIXES = (".png", ".jpg")
# The `--out` names an agent screenshot (a JPEG) may be written under.
AGENT_OUT_SUFFIXES = (".jpg", ".jpeg")


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


# --- geometry -----------------------------------------------------------------


def png_size(path: Path) -> tuple[int, int] | None:
    """Pixel width and height from the IHDR chunk, without a PNG library.

    An agent that plans a tap from a screenshot needs to know the image's
    coordinate space: an iOS capture is in pixels while the accessibility tree
    speaks points, and an Android emulator's capture size depends on the AVD.
    The size is always the first chunk, so 24 bytes answer the question.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(24)
    except OSError:
        return None
    if len(head) < 24 or not head.startswith(PNG_SIGNATURE) or head[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", head[16:24])
    return width, height


# The JPEG start-of-frame markers (SOF0..SOF15) that carry the image size;
# 0xC4 (DHT), 0xC8 (JPG) and 0xCC (DAC) share the range but are not frames.
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# Markers that stand alone, with no length after them.
_JPEG_STANDALONE = frozenset({0x01, *range(0xD0, 0xD8)})
# How far into a file the frame header is looked for: the APPn and table
# segments before it are small; a file that hides it further is not read.
_JPEG_SCAN_LIMIT = 1 << 20


def jpeg_size(path: Path) -> tuple[int, int] | None:
    """Pixel width and height from a JPEG's start-of-frame (SOF) segment,
    without an image library: the segments before it are skipped by their
    lengths. None for anything that is not a JPEG with a readable frame."""
    try:
        with path.open("rb") as handle:
            data = handle.read(_JPEG_SCAN_LIMIT)
    except OSError:
        return None
    if not data.startswith(JPEG_SIGNATURE):
        return None
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            return None
        marker = data[offset + 1]
        if marker == 0xFF:  # fill byte before a marker
            offset += 1
            continue
        if marker in _JPEG_STANDALONE:
            offset += 2
            continue
        if marker in (0xD9, 0xDA):  # end of image, start of scan: no frame seen
            return None
        length = struct.unpack(">H", data[offset + 2:offset + 4])[0]
        if length < 2:
            return None
        if marker in _JPEG_SOF:
            if offset + 9 > len(data) or length < 7:
                return None
            height, width = struct.unpack(">HH", data[offset + 5:offset + 9])
            return (width, height) if width and height else None
        offset += 2 + length
    return None


def image_size(path: Path) -> tuple[int, int] | None:
    """`png_size` for a PNG, `jpeg_size` for a JPEG (an agent screenshot)."""
    return png_size(path) or jpeg_size(path)


# --- naming -------------------------------------------------------------------


def slugify(text: str | None, limit: int = 48) -> str:
    """Readable, not ASCII-only: Unicode labels stay legible in `ls`."""
    cleaned = re.sub(r"[^\w\-]+", "-", (text or "").strip(), flags=re.UNICODE)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-").lower()
    return cleaned[:limit] or "shot"


def next_index(directory: Path) -> int:
    """Sequence per directory, so a listing sorts in capture order. PNG and
    JPEG shots share it: a number is never used twice."""
    highest = 0
    if directory.is_dir():
        for suffix in SHOT_SUFFIXES:
            for entry in directory.glob(f"*{suffix}"):
                head = entry.name.split("_", 1)[0]
                if head.isdigit():
                    highest = max(highest, int(head))
    return highest + 1


def build_filename(index: int, label: str | None, moment: float | None = None,
                   suffix: str = ".png") -> str:
    stamp = time.strftime("%H%M%S", time.localtime(moment if moment else time.time()))
    return f"{index:04d}_{stamp}_{slugify(label)}{suffix}"


# --- metadata -----------------------------------------------------------------


def _itxt_chunk(keyword: str, value: str) -> bytes:
    """A UTF-8 PNG text chunk.

    `tEXt` is Latin-1 only, which would mangle many Unicode labels; `iTXt`
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
    """Read back what `embed_metadata` wrote, without a PNG library. Only a
    PNG carries it: anything else (an agent JPEG) reads as empty."""
    found: dict[str, str] = {}
    try:
        data = path.read_bytes()
    except OSError:
        return found
    if not data.startswith(PNG_SIGNATURE):
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


def pretest_attribution(session: dict[str, Any] | None) -> dict[str, str]:
    """``pretest_id``/``check_id`` of the session's active pre-test, else ``{}``."""
    if not session:
        return {}
    try:
        from .pretest import store as pretest_store

        return pretest_store.attribution(session)
    except Exception:  # noqa: BLE001 - attribution must never break a capture
        return {}


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


# --- agent screenshots (AS-03) -----------------------------------------------


def agent_scale(screen: tuple[int, int], image: tuple[int, int]) -> int | float:
    """Screen pixels per image pixel: the screen's long edge over the
    image's (1 for an image that was not scaled down)."""
    factor = round(max(screen) / max(image), 4)
    return int(factor) if factor == int(factor) else factor


def coordinates_note(scale: int | float) -> str:
    """How an image pixel becomes a tap, with the factor filled in."""
    factor = f"{scale:g}"
    return f"image pixel (x, y) → ui tap --x round(x*{factor}) --y round(y*{factor})"


def _write_image(output: Path, data: bytes) -> Path:
    """Write a capture made elsewhere (the helper's JPEG) where `capture`
    would have written the PNG."""
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    return output


def _agent_shot(target: Target, max_edge: int | None
                ) -> tuple[dict[str, Any] | None, tuple[str, str | None] | None]:
    """The helper's JPEG, or None and (fallback reason, hint)."""
    if target.platform != ANDROID:
        return None, ("agent screenshots are made by the Android on-device helper", None)
    from . import ui_android  # lazily: only an agent screenshot needs it

    shot, reason, hint = ui_android.helper_screenshot(
        target, max_edge if max_edge is not None else ui_android.AGENT_SHOT_MAX_EDGE)
    if shot is None:
        return None, (reason or "the helper did not make the screenshot", hint)
    return shot, None


def capture_evidence(
    target: Target,
    session: dict[str, Any] | None,
    *,
    label: str | None = None,
    task: str | None = None,
    out: Path | None = None,
    agent: bool = False,
    max_edge: int | None = None,
) -> dict[str, Any]:
    """Capture, annotate, and file the shot so it is findable later.

    With a session, the canonical copy always lands in the session's `shots/`
    even when `--out` is given: `--out` used to divert the only copy away from
    the run's own evidence directory, so the artifact archive of a session
    contained no pictures of it.

    ``agent`` (AS-03): the helper's JPEG, its long edge at most ``max_edge``
    (default 1000), named ``.jpg``, with ``format``, ``screen``, ``scale`` and
    ``coordinates`` in the result; without the helper today's PNG with
    ``scale`` 1 and ``fallback_reason``, and an ``out`` asked as ``.jpg`` or
    ``.jpeg`` is then written with ``.png`` instead (the result's paths name
    it, and a warning ``out_renamed`` gives ``requested`` and ``written``).
    Without ``agent`` nothing differs from before. An ``agent`` capture with
    an ``out`` not ending in ``.jpg``/``.jpeg`` is ``invalid_value`` before
    any capture. No image is ever written under a suffix that is not its
    format's.
    """
    if agent and out is not None and Path(out).suffix.lower() not in AGENT_OUT_SUFFIXES:
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            f"an agent screenshot is a JPEG; --out {Path(out).name} does not end in .jpg "
            "or .jpeg",
            "Use a .jpg name or drop --agent.", flag="--out")
    moment = time.time()
    # While a pre-test check is open, an unlabelled capture belongs to it
    # (tp-004). Without an active pre-test this is empty and nothing changes.
    attribution = pretest_attribution(session)
    if task is None and attribution.get("check_id"):
        task = attribution["check_id"]
    shot: dict[str, Any] | None = None
    fallback: tuple[str, str | None] | None = None
    if agent:
        shot, fallback = _agent_shot(target, max_edge)
    suffix = ".jpg" if shot is not None else ".png"
    renamed: dict[str, Any] | None = None
    if agent and shot is None and out is not None:
        # Without the helper the agent screenshot is today's PNG: never PNG
        # bytes under the `.jpg`/`.jpeg` name asked for, so it is written
        # under that name with `.png`, and the reply says so.
        requested = Path(out)
        out = requested.with_suffix(".png")
        renamed = {"code": "out_renamed", "requested": str(requested), "written": str(out),
                   "hint": "the helper was not available, so this is today's PNG"}
    if session:
        directory = shots_dir(session, task)
        destination = directory / build_filename(next_index(directory), label, moment, suffix)
    else:
        destination = out or Path(build_filename(1, label, moment, suffix))

    if shot is not None:
        path = _write_image(destination, shot["data"])
    else:
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
    meta.update(attribution)
    mocks = active_mocks()
    meta["mocks_active"] = mocks["active"]
    if mocks["active"]:
        meta["mocks"] = ",".join(str(item) for item in mocks["ids"])

    # A JPEG has no iTXt chunks: its metadata lives in the index alone.
    embedded = embed_metadata(path, meta) if shot is None else False

    copies = [str(path)]
    if session and out:
        extra = Path(out).expanduser()
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_bytes(path.read_bytes())
        copies.append(str(extra))

    if shot is not None:
        size = jpeg_size(path) or (shot["width"], shot["height"])
    else:
        size = png_size(path)
    result: dict[str, Any] = {"path": str(path), "copies": copies,
                              "metadata": meta, "metadata_embedded": embedded,
                              "width": size[0] if size else None,
                              "height": size[1] if size else None}
    agent_keys: dict[str, Any] = {}
    if shot is not None:
        screen = (int(shot["screen"][0]), int(shot["screen"][1]))
        scale = agent_scale(screen, size or (shot["width"], shot["height"]))
        agent_keys = {"format": "jpeg", "screen": list(screen), "scale": scale}
        result.update(agent_keys, coordinates=coordinates_note(scale))
    elif agent:
        # today's PNG: image pixels are screen pixels
        result.update(format="png", screen=list(size) if size else None, scale=1,
                      coordinates=coordinates_note(1))
        if fallback is not None:
            result["fallback_reason"] = fallback[0]
            if fallback[1]:
                result["fallback_hint"] = fallback[1]
    if session:
        # Resolve both sides: the capture path is resolved, but artifacts_dir is
        # stored as written, so under a symlinked root (macOS /var -> /private/var,
        # or a global session home on one) relative_to would wrongly reject it.
        artifacts = Path(session["artifacts_dir"]).resolve()
        # a PNG's row keeps exactly its keys; a JPEG's adds how to read it
        append_index(session, {**meta, "file": str(path.resolve().relative_to(artifacts)),
                               "width": result["width"], "height": result["height"],
                               **agent_keys})
    warnings: list[dict[str, Any]] = []
    if mocks["active"]:
        warnings.append({
            "code": "screenshot_shows_mocked_data",
            "error": f"{mocks['active']} mock rule(s) were active — this image may "
                     f"show fabricated responses",
            "hint": ("The fact is embedded in the PNG and the index. " if shot is None
                     else "The fact is recorded in the shots index. ")
                    + "Do not present this as evidence of real backend behaviour.",
        })
    if renamed is not None:
        warnings.append(renamed)
    if warnings:
        result["warnings"] = warnings
    return result
