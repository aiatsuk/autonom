#!/usr/bin/env python3
"""Live acceptance of the Autonom MCP adapter on the two test devices (run ws-p3-mcp-adapter, sections 8 and 9).

Not part of the unit suite. It runs the real adapter server from
``integrations/chatgpt/autonom-chatgpt`` over stdio (started as its ``.mcp.json`` says) with
the real CLI of the checkout this script lives in (``AUTONOM_CLI=<repo>/scripts/autonom.py``,
never ``autonom`` from PATH), a temporary ``AUTONOM_HOME`` (so a temporary MCP state dir),
config ``flow_roots`` = a temporary folder with two generated Flow v1 files (open the
Settings app and scroll; a 60-step scroll flow for cancellation), ``allowed_targets`` = the
device under test and ``control_idle_s`` 30.

    python3 tests/live/chatgpt_mcp_live.py --android emulator-5584 [--ios UDID] --evidence-dir DIR
        [--measure-seconds N] [--boot]
    python3 tests/live/chatgpt_mcp_live.py --dry-run --android emulator-5584 --ios UDID
    python3 tests/live/chatgpt_mcp_live.py --self-test
    python3 tests/live/chatgpt_mcp_live.py check --results FILE...
    python3 tests/live/chatgpt_mcp_live.py check-chatgpt --results FILE --screenshots DIR
    python3 tests/live/chatgpt_mcp_live.py fill-doc --doc FILE --results FILE...
    python3 tests/live/chatgpt_mcp_live.py check-doc --doc FILE --results FILE...

``--results`` of ``check``, ``fill-doc`` and ``check-doc`` may also be repeated once per file
(``--results A --results B``); every file named either way is used.

Allowed devices only: Android emulator-5584 and the iOS Simulator
34A85293-39A2-436E-B669-12574874EC75. Any other device (emulator-5554 above all) is refused
before anything runs. ``--boot`` boots a target that is not running (Android: the AVD
AB_Bench_MCP_API36 on port 5584) and shuts it down again at the end; without it a missing
target makes its steps ``not_exercised``.

Steps per device, each a check ``{id, status, detail}``: ``hosts``, ``devices``, ``start``,
``foreign_host``, ``screen``, ``tree``, ``flow_passed``, ``tap_changes_screen``,
``op_reconciled``, ``pause_refuses_agent``, ``resume_reenables``, ``cancel_run``,
``media_focus``, ``media_thumb``, ``input_tap``, ``artifacts``, ``no_secrets``, ``stop``.
Output ``<evidence>/mcp-live-<platform>.json`` (owner-only) with the checks and the focus and
thumb measurements. Exit 0 when every check passed (or was not exercised), 1 otherwise, 2 on
bad arguments or a refused device (a JSON error with ``error_code`` and ``hint`` on stderr).

``check`` passes when every check of every results file is ``pass`` (or ``not_exercised``
with a reason). ``check-chatgpt`` validates the diagnostics file the workspace page saved in
ChatGPT (schema 1, the six page checks ``pass``) and requires ``chatgpt-chat.png``,
``chatgpt-global.png`` and ``chatgpt-thread.png`` in the screenshots folder. ``fill-doc``
writes everything the results files back as one generated block between the two marker lines
``<!-- autonom-results:begin -->`` and ``<!-- autonom-results:end -->`` of
``docs/plans/CHATGPT_P3_RESULTS.md``. ``check-doc`` renders that block again from the given
results files with the same renderer and requires exactly one begin and one end marker, in
order, each on its own line, and the bytes between them equal to the rendering; outside the
block a line with ``|`` (any table), a copy of a marker or a word ``pending`` is a problem, and
a bearer or ``token=`` value anywhere is one (``fill-doc`` refuses to write such a document).
It prints ``{"problems": [...]}`` (exit 1 when any; a difference names the first differing
line with the expected and the found text, both redacted; a malformed results field is a
problem, never a traceback). The results file is written owner-only into a folder only this
user can write (an existing folder of another user or one the group or everyone can write is
refused), through a new temporary file renamed into place (a link at the path is replaced, not
followed), with every Canvas token seen during the run and the temporary root paths removed
(``no_secrets`` names what it found). ``--dry-run`` prints
the plan and touches no device; ``--self-test`` checks the helpers and the refusals with failing device-tool stubs
first on PATH and runs every step against the fake CLI and the fake Canvas.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PLUGIN = ROOT / "integrations" / "chatgpt" / "autonom-chatgpt"
REPO_CLI = ROOT / "scripts" / "autonom.py"

_previous = sys.dont_write_bytecode
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
try:
    import chatgpt_mcp_conformance as conf  # noqa: E402  (same folder: the stdlib MCP client)
finally:
    sys.dont_write_bytecode = _previous

ANDROID_SERIAL = "emulator-5584"
IOS_UDID = "34A85293-39A2-436E-B669-12574874EC75"  # "AB Bench mobile-mcp", iOS 27.0
ALLOWED = {"android": (ANDROID_SERIAL,), "ios": (IOS_UDID,)}
ANDROID_AVD = ("AB_Bench_MCP_API36", 5584)
SETTINGS_APP = {"android": "com.android.settings", "ios": "com.apple.Preferences"}
RESUME_KEY = {"android": "BACK", "ios": "HOME"}
WORKSPACE = "chatgpt"
RESULTS_SCHEMA = "autonom.chatgpt-mcp-live/v1"
SHORT_FLOW = "settings-scroll.yaml"
LONG_FLOW = "settings-long-scroll.yaml"
LONG_FLOW_STEPS = 60

CHECK_IDS = ("hosts", "devices", "start", "foreign_host", "screen", "tree", "flow_passed",
             "tap_changes_screen", "op_reconciled", "pause_refuses_agent", "resume_reenables", "cancel_run",
             "media_focus", "media_thumb", "input_tap", "artifacts", "no_secrets", "stop")
PAGE_CHECK_IDS = ("bridge.list_sessions", "media.focus", "media.thumb", "input.tap", "control.pause",
                  "control.resume")
CHATGPT_ROWS = ("chat.list_sessions", "chat.start_session", "chat.read_screen", "chat.tap_element",
                "entry.global", "entry.thread") + PAGE_CHECK_IDS + ("chat.stop_session", "uninstall_restore")
SCREENSHOTS = ("chatgpt-chat.png", "chatgpt-global.png", "chatgpt-thread.png")
MEASURE_KEYS = ("frames_per_s", "bytes_per_s", "call_ms_median", "call_ms_p95", "largest_packet",
                "key_frame_bytes", "resyncs")
MEASURE_COLUMNS = ("frames/s", "bytes/s", "median call ms", "p95 call ms", "largest packet", "key frame bytes",
                   "resyncs")
MEASURE_ROWS = tuple((profile, platform) for platform in ("android", "ios", "chatgpt")
                     for profile in ("focus", "thumb"))
# What no results file holds: the coordinator lists these outside the generated block of the results document.
COORDINATOR_ROWS = tuple(check_id for check_id in CHATGPT_ROWS if check_id not in PAGE_CHECK_IDS)
ENVIRONMENT_ROWS = ("Date (UTC)", "Repository commit", "macOS", "Python (server)", "Python (CLI)", "Node",
                    "ChatGPT desktop")
CONTROL_CODES = frozenset({"control_held", "control_generation_stale", "control_lease_expired", "control_not_held"})
FATAL_STREAM_CODES = frozenset({"stream_not_found", "media_unavailable", "session_stopped", "session_not_found",
                                "host_not_found", "host_unavailable"})
HOST_ID_RE = re.compile(r"^h_[a-z2-7]{26}\Z")
DIAG_RUN_RE = re.compile(r"^diag-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}\Z")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
SAFE_LABELS = ("General", "Display", "Battery", "Accessibility", "Storage", "Sound", "Notifications",
               "About phone", "System", "Privacy")
RISKY_WORDS = ("erase", "reset", "delete", "remove", "sign", "airplane", "wi-fi", "wifi", "bluetooth",
               "cellular", "mobile data", "hotspot", "power", "shut", "restart", "turn off", "vpn")

CheckFailed = conf.CheckFailed
NotExercised = conf.NotExercised
UsageError = conf.UsageError
expect = conf.expect
short = conf.short


# ====================================================================== pure helpers

def refuse_device(platform: str, value: str) -> None:
    if value not in ALLOWED[platform]:
        raise UsageError("device_not_allowed", "%s %s is not an allowed test device" % (platform, value),
                         "Use --android %s and --ios %s only; emulator-5554 is never used."
                         % (ANDROID_SERIAL, IOS_UDID), refused_device=value)


def flow_text(platform: str, steps: int) -> str:
    """A Flow v1 file that opens the Settings app on its first screen (stopped
    first, so a page left open by an earlier run is not resumed) and scrolls
    ``steps`` times."""
    name = "Settings open and scroll" if steps <= 2 else "Settings long scroll (%d steps)" % steps
    lines = ["schema: autonom.dev/flow/v1", "appId: %s" % SETTINGS_APP[platform], "name: %s" % name, "---",
             "- stopApp", "- launchApp"]
    lines += ["- scroll"] * steps
    if steps <= 2:
        lines.append("- takeScreenshot")
    return "\n".join(lines) + "\n"


def tree_hash(nodes: Any) -> str:
    rows = []
    for node in nodes if isinstance(nodes, list) else []:
        if isinstance(node, dict):
            rows.append([node.get("text"), node.get("desc"), node.get("resource_id"), node.get("bounds")])
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def node_label(node: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    for key in ("text", "desc"):
        value = node.get(key)
        if isinstance(value, str) and value.strip() and len(value) <= 200:
            return key, value
    return None, None


def pick_tap_node(nodes: Any) -> Optional[Tuple[str, str]]:
    """A clickable, visible, non-toggle node with a unique text (or description) safe to tap: (key, value)."""
    nodes = [node for node in (nodes if isinstance(nodes, list) else []) if isinstance(node, dict)]
    counts: Dict[str, int] = {}
    for node in nodes:
        for key in ("text", "desc"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                counts[value] = counts.get(value, 0) + 1
    # A label inside a clickable row counts too (Android Settings puts the
    # text in a child of the clickable row; a semantic tap on it taps the row):
    # the flat list is in document order with `depth`, so the open ancestors
    # are a stack.
    in_clickable: List[bool] = []
    in_toggle: List[bool] = []
    stack: List[Tuple[int, bool, bool]] = []
    for node in nodes:
        depth = node.get("depth") if isinstance(node.get("depth"), int) else 0
        while stack and stack[-1][0] >= depth:
            stack.pop()
        in_clickable.append(any(clickable for _, clickable, _ in stack))
        in_toggle.append(any(checkable for _, _, checkable in stack))
        stack.append((depth, bool(node.get("clickable")), bool(node.get("checkable"))))
    eligible = []
    for index, node in enumerate(nodes):
        key, value = node_label(node)
        if key is None or counts.get(value or "", 0) != 1:
            continue
        if not (node.get("clickable") or in_clickable[index]):
            continue
        if node.get("enabled") is False or node.get("visible") is False:
            continue
        if node.get("checkable") or in_toggle[index] or any(word in (value or "").lower() for word in RISKY_WORDS):
            continue
        bounds = node.get("bounds")
        if isinstance(bounds, list) and len(bounds) == 4 and (bounds[2] <= bounds[0] or bounds[3] <= bounds[1]):
            continue
        eligible.append((key, value))
    for label in SAFE_LABELS:
        for key, value in eligible:
            if value == label:
                return key, value
    return eligible[0] if eligible else None


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    data = sorted(value for value in values if isinstance(value, (int, float)))
    if not data:
        return None
    if len(data) == 1:
        return float(data[0])
    rank = (len(data) - 1) * p / 100.0
    low = int(rank)
    high = min(low + 1, len(data) - 1)
    return data[low] + (data[high] - data[low]) * (rank - low)


def round1(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 1)


class Meter:
    """Measurements of one stream's pull loop (section 9 step 8)."""

    def __init__(self, profile: str) -> None:
        self.profile = profile
        self.started: Optional[float] = None
        self.ended: Optional[float] = None
        self.calls = 0
        self.call_ms: List[float] = []
        self.frames = 0
        self.key_frames = 0
        self.non_key = 0
        self.bytes = 0
        self.largest = 0
        self.key_bytes = 0
        self.resyncs = 0
        self.dropped = 0
        self.gaps = 0
        self.errors: List[str] = []
        self.deliveries: List[float] = []

    def record(self, structured: Dict[str, Any], frames: List[Dict[str, Any]], call_ms: float, after_seq: int,
               now: float) -> None:
        self.calls += 1
        self.call_ms.append(call_ms)
        if structured.get("ok") is not True:
            self.errors.append(str(structured.get("error_code")))
            return
        resync = structured.get("resync") is True
        if resync and after_seq > 0:
            self.resyncs += 1
        self.dropped += int(structured.get("dropped") or 0)
        seqs = [frame.get("seq") for frame in frames]
        if frames:
            self.deliveries.append(now)
        if frames and self.profile == "focus":  # a thumb stream skips packets by design
            if not resync and after_seq > 0 and seqs[0] != after_seq + 1:
                self.gaps += 1
            if seqs != list(range(seqs[0], seqs[0] + len(seqs))):
                self.gaps += 1
        for frame in frames:
            size = len(base64.b64decode(frame.get("data_b64") or ""))
            self.frames += 1
            self.bytes += size
            self.largest = max(self.largest, size)
            if frame.get("key") is True:
                self.key_frames += 1
                self.key_bytes = max(self.key_bytes, size)
            else:
                self.non_key += 1

    @property
    def seconds(self) -> float:
        if self.started is None:
            return 0.0
        return max(0.0, (self.ended if self.ended is not None else time.monotonic()) - self.started)

    def intervals(self) -> List[float]:
        return [later - earlier for earlier, later in zip(self.deliveries, self.deliveries[1:])]

    def summary(self) -> Dict[str, Any]:
        seconds = self.seconds
        per = (lambda n: round1(n / seconds) if seconds > 0 else None)
        intervals = self.intervals()
        return {
            "profile": self.profile, "seconds": round1(seconds), "calls": self.calls, "frames": self.frames,
            "key_frames": self.key_frames, "frames_per_s": per(self.frames), "bytes": self.bytes,
            "bytes_per_s": per(self.bytes), "call_ms_median": round1(percentile(self.call_ms, 50)),
            "call_ms_p95": round1(percentile(self.call_ms, 95)), "largest_packet": self.largest,
            "key_frame_bytes": self.key_bytes, "resyncs": self.resyncs, "dropped": self.dropped,
            "gaps": self.gaps, "errors": self.errors[:10],
            "delivery_interval_s_median": round1(percentile(intervals, 50)),
            "delivery_interval_s_max": round1(max(intervals)) if intervals else None,
        }


def focus_verdict(meter: Meter) -> Tuple[bool, str]:
    summary = meter.summary()
    ok = (summary["frames_per_s"] or 0) >= 5 and meter.gaps == 0 and not meter.errors
    return ok, ("%s frames/s over %s s, %d resyncs, %d gaps, %d dropped%s"
                % (summary["frames_per_s"], summary["seconds"], meter.resyncs, meter.gaps, meter.dropped,
                   ", errors %s" % meter.errors[:3] if meter.errors else ""))


def thumb_verdict(meter: Meter, seconds: float) -> Tuple[bool, str]:
    intervals = meter.intervals()
    median = percentile(intervals, 50)
    need = max(2, int(seconds / 2.5))
    ok = (len(meter.deliveries) >= need and meter.non_key == 0 and not meter.errors
          and median is not None and 0.9 <= median <= 2.2)
    return ok, ("%d key frames in %.1f s (need %d), median interval %s s, %d non-key%s"
                % (len(meter.deliveries), meter.seconds, need, round1(median), meter.non_key,
                   ", errors %s" % meter.errors[:3] if meter.errors else ""))


def _token_list(tokens: Any) -> List[str]:
    """Every Canvas token seen (one string, None or an iterable of strings), longest first."""
    if tokens is None:
        return []
    items = [tokens] if isinstance(tokens, str) else list(tokens)
    return sorted({item for item in items if isinstance(item, str) and item}, key=len, reverse=True)


def secret_hits(texts: Sequence[str], tokens: Any, roots: Sequence[str]) -> List[str]:
    """Which forbidden things appear in ``texts`` (names only, never the values).

    ``tokens`` is every Canvas token seen during the run (or one, or None). The bearer and
    ``token=`` scan is the one ``check``, ``fill_doc`` and ``check-doc`` use.
    """
    hits = []
    joined = "\n".join(texts)
    lowered = joined.lower()
    if any(token in joined for token in _token_list(tokens)):
        hits.append("the Canvas token")
    if "bearer " in lowered:
        hits.append("a bearer value")
    if "token=" in lowered:
        hits.append("token=")
    for root in roots:
        if root and len(root) > 4 and root in joined:
            hits.append("the temporary root path")
            break
    return hits


def check_private_dir(directory: Path) -> None:
    """Refuse an evidence folder that is not a real folder of this user writable by nobody else."""
    try:
        info = os.lstat(str(directory))
    except OSError as exc:
        raise UsageError("evidence_dir_unsafe", "The evidence folder cannot be read (%s)" % type(exc).__name__,
                         "Create the folder first (mkdir -m 700).")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UsageError("evidence_dir_unsafe", "The evidence folder is not a plain folder",
                         "Name a real folder, not a link or a file.")
    if info.st_uid != os.getuid():
        raise UsageError("evidence_dir_unsafe", "The evidence folder belongs to another user",
                         "Use a folder you own (mkdir -m 700).")
    if info.st_mode & 0o022:
        raise UsageError("evidence_dir_unsafe", "The evidence folder is writable by the group or by everyone",
                         "chmod 700 the folder, or use a new one.")


def write_atomic(path: Path, text: str, mode: int) -> None:
    """Write ``path`` through a new temporary file in the same folder, then rename it into place.

    The temporary file is opened with O_CREAT|O_EXCL|O_NOFOLLOW, so no planted file or link is
    followed, and ``os.replace`` swaps the name itself: a link at ``path`` is replaced, never followed.
    """
    directory = path.parent
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    temp = directory / (".%s.%s.tmp" % (path.name, secrets.token_hex(8)))
    fd = os.open(str(temp), flags, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp), str(path))
    except BaseException:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise


def write_private(path: Path, text: str) -> None:
    """Write an owner-only (0600) evidence file into a folder only this user can write."""
    check_private_dir(path.parent)
    write_atomic(path, text, 0o600)


REDACTED_TOKEN = "<canvas token removed>"
REDACTED_ROOT = "<temporary root>"


def redact(value: Any, tokens: Any, roots: Sequence[str]) -> Any:
    """``value`` with every Canvas token seen and the temporary root paths replaced, in every string and key."""
    pairs = [(token, REDACTED_TOKEN) for token in _token_list(tokens)]
    pairs += [(root, REDACTED_ROOT) for root in sorted({root for root in roots if root and len(root) > 4},
                                                         key=len, reverse=True)]
    if not pairs:
        return value

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            for old, new in pairs:
                item = item.replace(old, new)
            return item
        if isinstance(item, (list, tuple)):
            return [walk(part) for part in item]
        if isinstance(item, dict):
            return {walk(key): walk(part) for key, part in item.items()}
        return item
    return walk(value)


def save_results(path: Path, payload: Dict[str, Any], tokens: Any, roots: Sequence[str]) -> Dict[str, Any]:
    """Write ``mcp-live-<platform>.json`` owner-only with every Canvas token and the temporary roots removed."""
    clean = redact(payload, tokens, roots)
    write_private(path, json.dumps(clean, indent=2, sort_keys=True) + "\n")
    return clean


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ====================================================================== result checks

def results_problems(data: Any) -> List[str]:
    """``check``: the problems of one mcp-live-<platform>.json."""
    if not isinstance(data, dict):
        return ["the results file is not a JSON object"]
    problems = []
    if data.get("schema") != RESULTS_SCHEMA:
        problems.append("schema is not %s" % RESULTS_SCHEMA)
    platform = data.get("platform")
    if not isinstance(platform, str):
        problems.append("platform is %s, not text" % _json_type(platform))
    elif platform not in ALLOWED:
        problems.append("unknown platform %r" % platform)
    elif data.get("target_id") not in ALLOWED[platform]:
        problems.append("target %r is not an allowed device" % data.get("target_id"))
    checks = data.get("checks")
    if not isinstance(checks, list):
        return problems + ["checks is not a list"]
    ids = [item.get("id") for item in checks if isinstance(item, dict)]
    for check_id in CHECK_IDS:
        if ids.count(check_id) != 1:
            problems.append("check %s appears %d times" % (check_id, ids.count(check_id)))
    for item in checks:
        if not isinstance(item, dict) or item.get("id") not in CHECK_IDS:
            problems.append("unexpected check %s" % short(item, 80))
            continue
        status, detail = item.get("status"), item.get("detail")
        if status == "pass":
            continue
        if status == "not_exercised" and isinstance(detail, str) and detail.strip():
            continue
        problems.append("%s is %s: %s" % (item.get("id"), status, short(detail or "no reason", 200)))
    if any(hit in ("a bearer value", "token=") for hit in secret_hits([json.dumps(data)], None, [])):
        problems.append("the results hold a bearer or token= string")
    return problems


def chatgpt_problems(data: Any, screenshots: Optional[Path]) -> List[str]:
    """``check-chatgpt``: the diagnostics file the page saved, and the three screenshots."""
    problems = []
    if not isinstance(data, dict):
        problems.append("the diagnostics file is not a JSON object")
        data = {}
    if data.get("schema_version") != 1 or isinstance(data.get("schema_version"), bool):
        problems.append("schema_version is not 1")
    if not (isinstance(data.get("run_id"), str) and DIAG_RUN_RE.match(data["run_id"])):
        problems.append("run_id is not a diagnostics run id")
    if not (isinstance(data.get("host_id"), str) and HOST_ID_RE.match(data["host_id"])):
        problems.append("host_id is missing or invalid")
    checks = data.get("checks")
    if not isinstance(checks, list):
        problems.append("checks is not a list")
        checks = []
    ids = [item.get("id") for item in checks if isinstance(item, dict)]
    for check_id in PAGE_CHECK_IDS:
        if ids.count(check_id) != 1:
            problems.append("page check %s appears %d times" % (check_id, ids.count(check_id)))
    for item in checks:
        if not isinstance(item, dict) or item.get("id") not in PAGE_CHECK_IDS:
            problems.append("unexpected page check %s" % short(item, 80))
        elif item.get("status") != "pass":
            problems.append("page check %s is %s" % (item.get("id"), item.get("status")))
    if any(hit in ("a bearer value", "token=") for hit in secret_hits([json.dumps(data)], None, [])):
        problems.append("the diagnostics hold a bearer or token= string")
    if screenshots is not None:
        for name in SCREENSHOTS:
            path = screenshots / name
            try:
                head = path.read_bytes()[:8] if path.is_file() else b""
            except OSError:
                head = b""
            if head != PNG_MAGIC:
                problems.append("screenshot %s is missing or not a PNG" % name)
    return problems


def classify_results(data: Any) -> Optional[str]:
    if isinstance(data, dict) and data.get("schema") == RESULTS_SCHEMA and isinstance(data.get("platform"), str) \
            and data["platform"] in ALLOWED:
        return data["platform"]
    if isinstance(data, dict) and data.get("schema_version") == 1 and isinstance(data.get("run_id"), str) \
            and DIAG_RUN_RE.match(data["run_id"]):
        return "chatgpt"
    return None


# ====================================================================== the results document
#
# Nothing in the document is parsed as Markdown. ``fill_doc`` writes every value the results
# files back as one generated block between two marker lines (``BLOCK_BEGIN``, ``BLOCK_END``);
# ``check-doc`` (``doc_problems``) renders the block again from the files it is given with the
# same renderer (``render_block``) and requires the bytes between the markers to equal it.
# Outside the block a line with a ``|`` (so a table of any form), a copy of a marker, a word
# ``pending`` and a bearer or ``token=`` value are refused; everything else there is free text
# the coordinator writes (the values no results file holds).

BLOCK_BEGIN = "<!-- autonom-results:begin -->"
BLOCK_END = "<!-- autonom-results:end -->"
MARKER_RE = re.compile(r"autonom-results:", re.IGNORECASE)
PENDING_RE = re.compile(r"\bpending\b", re.IGNORECASE)
PLATFORM_HEADINGS = (("android", "Android"), ("ios", "iOS"))
STATUS_HEADER = ("Check", "Status", "Detail")
RESULT_ENVIRONMENT_ROWS = ("Adapter version", "Android target", "iOS target")
KIND_ORDER = ("android", "ios", "chatgpt")


class DocRefused(Exception):
    """``fill_doc`` will not write the document; ``problems`` says why (no secret values)."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = list(problems)


def redact_text(value: Any, limit: int = 160) -> str:
    """``value`` as one short line with any bearer or ``token=`` value removed (for problem texts)."""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    text = re.sub(r"(?i)(bearer\s+)\S+", r"\1<removed>", text)
    text = re.sub(r"(?i)(token=)[^\s&|'\"`]+", r"\1<removed>", text)
    return short(text, limit)


def fmt_number(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return "n/a"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else "%.1f" % value
    return "n/a"


def _cell(value: Any) -> str:
    """The cell text the renderer writes for a value (one line, no pipe, no marker, at most 300 characters)."""
    text = " ".join(MARKER_RE.sub("autonom-results ", str(value)).replace("|", "/").split())[:300].strip()
    return text or "-"


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, list):
        return "a list"
    return "an object"


def _unique(items: Sequence[Any]) -> List[Any]:
    seen: List[Any] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _checks_by_id(data: Any) -> Dict[str, Dict[str, Any]]:
    checks = data.get("checks") if isinstance(data, dict) else None
    out: Dict[str, Dict[str, Any]] = {}
    for item in checks if isinstance(checks, list) else []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            out.setdefault(item["id"], item)
    return out


def _measurement_source(results: Dict[str, Any], platform: str, profile: str) -> Optional[Dict[str, Any]]:
    data = results.get(platform)
    block = data.get("measurements") if isinstance(data, dict) else None
    block = block.get(profile) if isinstance(block, dict) else None
    return block if isinstance(block, dict) else None


def results_field_problems(name: str, data: Dict[str, Any], kind: str) -> List[str]:
    """Malformed fields of one classified results file, each named with the file and the field."""
    label = "results %s" % name
    problems: List[str] = []
    known = CHECK_IDS if kind in ALLOWED else PAGE_CHECK_IDS
    checks = data.get("checks")
    if not isinstance(checks, list):
        problems.append("%s: checks is %s, not a list" % (label, _json_type(checks)))
    else:
        ids = []
        for index, item in enumerate(checks):
            at = "%s: checks[%d]" % (label, index)
            if not isinstance(item, dict):
                problems.append("%s is %s, not an object" % (at, _json_type(item)))
                continue
            if not isinstance(item.get("id"), str):
                problems.append("%s.id is %s, not text" % (at, _json_type(item.get("id"))))
            elif item["id"] not in known:
                problems.append("%s.id %s is not a known check" % (at, redact_text(item["id"], 60)))
            else:
                ids.append(item["id"])
            if item.get("status") not in conf.STATUSES:
                problems.append("%s.status is %s, not pass, fail or not_exercised"
                                % (at, redact_text(item.get("status"), 60)))
            if item.get("detail") is not None and not isinstance(item.get("detail"), str):
                problems.append("%s.detail is %s, not text" % (at, _json_type(item.get("detail"))))
        for check_id in known:
            if ids.count(check_id) == 0:
                problems.append("%s lacks the check %s" % (label, check_id))
            elif ids.count(check_id) > 1:
                problems.append("%s has the check %s %d times" % (label, check_id, ids.count(check_id)))
    measurements = data.get("measurements")
    if measurements is not None and not isinstance(measurements, dict):
        problems.append("%s: measurements is %s, not an object" % (label, _json_type(measurements)))
    elif isinstance(measurements, dict):
        for profile in ("focus", "thumb"):
            block = measurements.get(profile)
            if block is None:
                continue
            if not isinstance(block, dict):
                problems.append("%s: measurements.%s is %s, not an object" % (label, profile, _json_type(block)))
                continue
            for field in MEASURE_KEYS:
                value = block.get(field)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                    problems.append("%s: measurements.%s.%s is %s, not a number"
                                    % (label, profile, field, _json_type(value)))
    if kind in ALLOWED:
        if data.get("target_id") not in ALLOWED[kind]:
            problems.append("%s: target_id %s is not an allowed device"
                            % (label, redact_text(data.get("target_id"), 60)))
        if "adapter_version" not in data:
            problems.append("%s lacks adapter_version" % label)
    if "adapter_version" in data and not isinstance(data.get("adapter_version"), str):
        problems.append("%s: adapter_version is %s, not text" % (label, _json_type(data.get("adapter_version"))))
    if any(hit in ("a bearer value", "token=") for hit in secret_hits([json.dumps(data)], None, [])):
        problems.append("%s holds a bearer or token= string" % label)
    return problems


def _adapter_versions(results: Dict[str, Any]) -> List[Tuple[str, str]]:
    return [(platform, results[platform]["adapter_version"]) for platform, _ in PLATFORM_HEADINGS
            if isinstance(results.get(platform), dict) and isinstance(results[platform].get("adapter_version"), str)]


def collect_results(files: Sequence[Path]) -> Tuple[Dict[str, Any], List[Tuple[str, str]], List[str]]:
    """The given results files: data by kind, the Evidence rows (name, sha256) and the problems.

    ``fill_doc`` and ``check-doc`` both read the files through this, so both render the same block.
    A file that cannot be read or is no results file is a problem and is left out.
    """
    problems: List[str] = []
    results: Dict[str, Any] = {}
    listed: List[Tuple[str, str, str]] = []
    for path in files:
        path = Path(path)
        try:
            data = load_json(path)
            digest = sha256_file(path)
        except (OSError, ValueError, RecursionError) as exc:
            problems.append("results %s cannot be read (%s)" % (path.name, type(exc).__name__))
            continue
        if isinstance(data, dict) and data.get("schema") == RESULTS_SCHEMA and not isinstance(data.get("platform"), str):
            problems.append("results %s: platform is %s, not text" % (path.name, _json_type(data.get("platform"))))
            continue
        kind = classify_results(data)
        if kind is None:
            problems.append("results %s is neither a live results file nor page diagnostics" % path.name)
            continue
        problems += results_field_problems(path.name, data, kind)
        if kind in results:
            problems.append("two results files for %s" % kind)
        if path.name in [name for _, name, _ in listed]:
            problems.append("two results files are named %s" % path.name)
        results.setdefault(kind, data)
        listed.append((kind, path.name, digest))
    versions = _unique([version for _, version in _adapter_versions(results)])
    if len(versions) > 1:
        problems.append("the results files disagree on adapter_version: %s"
                        % ", ".join("%s %s" % (platform, redact_text(version, 40))
                                    for platform, version in _adapter_versions(results)))
    ranked = sorted(enumerate(listed), key=lambda pair: (KIND_ORDER.index(pair[1][0]), pair[0]))
    return results, [(name, digest) for _, (_, name, digest) in ranked], problems


def render_block(results: Dict[str, Any], listed: Sequence[Tuple[str, str]]) -> List[str]:
    """The lines between the two markers, from the results files only (the renderer of fill-doc and check-doc).

    ``results`` maps android, ios and chatgpt to their file's data; ``listed`` is the Evidence
    rows (name, sha256), the results files in the order android, ios, diagnostics.
    """
    lines: List[str] = [""]

    def section(title: str) -> None:
        lines.extend([title, ""])

    def table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join("---" for _ in header) + "|")
        lines.extend("| " + " | ".join(row) + " |" for row in rows)
        lines.append("")

    versions = _adapter_versions(results)
    distinct = _unique([version for _, version in versions])
    if not distinct:
        version = "not recorded"
    elif len(distinct) == 1:
        version = distinct[0]
    else:
        version = "the files disagree: " + ", ".join("%s %s" % pair for pair in versions)
    environment = {"Adapter version": version}
    for platform, heading in PLATFORM_HEADINGS:
        data = results.get(platform)
        if not isinstance(data, dict):
            environment["%s target" % heading] = "no results file"
        else:
            target = data.get("target_id")
            environment["%s target" % heading] = target if isinstance(target, str) else "not recorded"
    section("## Environment")
    table(("Item", "Value"), [[item, _cell(environment[item])] for item in RESULT_ENVIRONMENT_ROWS])

    not_exercised: List[Tuple[str, Any]] = []
    section("## Device runs")
    for platform, heading in PLATFORM_HEADINGS:
        data = results.get(platform)
        recorded = _checks_by_id(data)
        rows = []
        for check_id in CHECK_IDS:
            item = recorded.get(check_id)
            if data is None:
                status, detail = "not_exercised", "no results file for %s" % heading
            elif item is None:
                status, detail = "not_exercised", "not in the %s results file" % heading
            else:
                status, detail = item.get("status"), item.get("detail") or "-"
            rows.append([check_id, _cell(status), _cell(detail)])
            if status == "not_exercised":
                not_exercised.append(("%s %s" % (platform, check_id), detail))
        section("### %s" % heading)
        table(STATUS_HEADER, rows)

    section("## Measurements")
    table(("Profile", "Platform") + MEASURE_COLUMNS,
          [[profile, platform] + [fmt_number((_measurement_source(results, platform, profile) or {}).get(field))
                                  for field in MEASURE_KEYS]
           for profile, platform in MEASURE_ROWS])

    diagnostics = results.get("chatgpt")
    page = _checks_by_id(diagnostics)
    rows = []
    for check_id in PAGE_CHECK_IDS:
        if diagnostics is None:
            status, detail = "not_exercised", "no diagnostics file"
        elif check_id in page:
            status, detail = page[check_id].get("status"), page[check_id].get("detail") or "-"
        else:
            status, detail = "not_exercised", "not in the diagnostics file"
        rows.append([check_id, _cell(status), _cell(detail)])
        if status == "not_exercised":
            not_exercised.append(("chatgpt %s" % check_id, detail))
    section("## ChatGPT run")
    table(STATUS_HEADER, rows)

    section("## Not exercised and why")
    table(("Item", "Reason"), [[_cell(item), _cell(reason)] for item, reason in not_exercised] or [["none", "none"]])
    section("## Evidence")
    table(("File", "sha256"), [[_cell(name), _cell(digest)] for name, digest in listed] or [["none", "none"]])
    return lines


def _block_text(lines: Sequence[str]) -> str:
    return "".join(line + "\n" for line in lines)


def _final_newline(text: str) -> str:
    return text[:-1] if text.endswith("\n") else text


def find_block(text: str) -> Tuple[Optional[Tuple[int, int]], List[str], List[str]]:
    """The marker lines: ``((begin index, end index) or None, the document's lines, marker problems)``.

    Each marker stands alone on its own line, exactly once, begin before end; any other line that
    holds a marker's text is a copy and refused.
    """
    lines = text.split("\n")
    problems: List[str] = []
    begins = [index for index, line in enumerate(lines) if line == BLOCK_BEGIN]
    ends = [index for index, line in enumerate(lines) if line == BLOCK_END]
    for name, marker, found in (("begin", BLOCK_BEGIN, begins), ("end", BLOCK_END, ends)):
        if not found:
            problems.append("the %s marker %s is missing (it stands alone on its own line)" % (name, marker))
        elif len(found) > 1:
            problems.append("the %s marker appears %d times (lines %s); it appears once"
                            % (name, len(found), ", ".join(str(index + 1) for index in found)))
    span: Optional[Tuple[int, int]] = None
    if len(begins) == 1 and len(ends) == 1:
        if ends[0] < begins[0]:
            problems.append("the end marker (line %d) comes before the begin marker (line %d)"
                            % (ends[0] + 1, begins[0] + 1))
        else:
            span = (begins[0], ends[0])
    for index, line in enumerate(lines):
        if line not in (BLOCK_BEGIN, BLOCK_END) and MARKER_RE.search(line):
            problems.append("line %d holds a copy of a results marker; only the two marker lines may" % (index + 1))
    return span, lines, problems


def block_difference(lines: Sequence[str], span: Tuple[int, int], expected: Sequence[str]) -> Optional[str]:
    """The first line where the block differs from the rendering, or None when the bytes are equal."""
    begin, end = span
    actual = list(lines[begin + 1:end])
    if _final_newline(_block_text(actual)) == _final_newline(_block_text(expected)):
        return None
    index = 0
    while index < len(actual) and index < len(expected) and actual[index] == expected[index]:
        index += 1
    want = repr(redact_text(expected[index])) if index < len(expected) else "the end of the block"
    got = repr(redact_text(actual[index])) if index < len(actual) else "the end of the block"
    return ("the results block differs from the results files at line %d: expected %s, found %s"
            % (begin + 2 + index, want, got))


def doc_problems(text: str, files: Sequence[Path]) -> List[str]:
    """``check-doc``: the problems of the document against the block rendered again from ``files``.

    The bytes between the markers must equal ``render_block`` of the given files (only the final
    newline is normalized), so a claim no given file backs, a file left out, a changed cell, row,
    column, table or heading inside the block all show as the first differing line. Outside the
    block: no line with ``|``, no marker copy, no ``pending``. Anywhere: no bearer or ``token=``
    value. Malformed results fields are named as problems.
    """
    results, listed, problems = collect_results(files)
    span, lines, marker_problems = find_block(text)
    problems += marker_problems
    problems += ["the document holds %s" % hit for hit in secret_hits([text], None, [])]
    if span is not None:
        begin, end = span
        for index, line in enumerate(lines):
            if begin <= index <= end:
                continue
            if "|" in line:
                problems.append("line %d outside the results block contains '|' (tables belong in the block)"
                                % (index + 1))
            if PENDING_RE.search(line):
                problems.append("line %d outside the results block is still pending" % (index + 1))
        difference = block_difference(lines, span, render_block(results, listed))
        if difference:
            problems.append(difference)
    return _unique(problems)


def fill_doc(text: str, files: Sequence[Path]) -> str:
    """The document with the block between the markers written from ``files`` (what check-doc renders again).

    Raises ``DocRefused`` when the markers are not exactly one begin and one end line, in order,
    or when the filled document would hold a bearer or ``token=`` value. Text outside the block
    is kept as it is.
    """
    results, listed, _ = collect_results(files)
    span, lines, problems = find_block(text)
    if problems or span is None:
        raise DocRefused(problems or ["the results block cannot be found"])
    begin, end = span
    filled = "\n".join(list(lines[:begin + 1]) + render_block(results, listed) + list(lines[end:]))
    hits = secret_hits([filled], None, [])
    if hits:
        raise DocRefused(["the filled document would hold %s" % hit for hit in hits])
    return filled


# ====================================================================== runtime

def live_env(state: Path) -> Dict[str, str]:
    """The server's environment for a live run: this checkout's CLI, a temporary state base."""
    env = conf.clean_env()
    env.update({"AUTONOM_HOME": str(state), "AUTONOM_CLI": str(REPO_CLI), "AUTONOM_MCP_CLI_PYTHON": sys.executable,
                "AUTONOM_MCP_PYTHON": sys.executable, "PYTHONDONTWRITEBYTECODE": "1"})
    return env


class Runtime:
    """One device run's temporary root, environment and CLI (live, or the fake world in the self-test)."""

    def __init__(self, platform: str, target: str, world: Optional[conf.FakeWorld] = None) -> None:
        self.platform = platform
        self.target = target
        self.world = world
        if world is None:
            self.root = Path(tempfile.mkdtemp(prefix="mcp-live-")).resolve()
            os.chmod(str(self.root), 0o700)
            self.state = self.root / "state"
            self.state.mkdir(mode=0o700)
            self.env = live_env(self.state)
            self.cli_argv = [sys.executable, str(REPO_CLI)]
        else:
            self.root = world.tmp
            self.state = world.state
            self.env = world.env()
            self.cli_argv = [sys.executable, str(conf.FAKE_CLI)]
        self.flows = self.root / "flows"
        self.flows.mkdir(exist_ok=True)

    @property
    def roots(self) -> List[str]:
        return sorted({str(self.root), os.path.realpath(str(self.root))})

    def prepare(self) -> None:
        (self.flows / SHORT_FLOW).write_text(flow_text(self.platform, 2), encoding="utf-8")
        (self.flows / LONG_FLOW).write_text(flow_text(self.platform, LONG_FLOW_STEPS), encoding="utf-8")
        mcp_dir = self.state / "chatgpt-mcp"
        mcp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        config = {"flow_roots": [str(self.flows)], "allowed_targets": ["%s:%s" % (self.platform, self.target)],
                  "control_idle_s": 30}
        (mcp_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    def cli(self, *argv: str, timeout: float = 60.0) -> Tuple[int, Dict[str, Any]]:
        proc = subprocess.run(self.cli_argv + list(argv), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout, env=self.env, cwd=str(self.root), check=False)
        for stream in (proc.stdout, proc.stderr):
            try:
                payload = json.loads(stream)
            except ValueError:
                payload = None
                for line in reversed(stream.splitlines()):
                    try:
                        payload = json.loads(line)
                        break
                    except ValueError:
                        continue
            if isinstance(payload, dict):
                return proc.returncode, payload
        return proc.returncode, {"ok": False, "error_code": "cli_output_invalid"}

    def start_server(self) -> conf.McpClient:
        return conf.McpClient.from_plugin(PLUGIN, self.env)

    def cleanup(self) -> None:
        if self.world is None:
            shutil.rmtree(self.root, ignore_errors=True)


class CanvasPeek:
    """Read-only look at the server's workspace Canvas (its discovery document; tokens stay in memory).

    Every token a discovery document shows is remembered in ``seen`` (a Canvas that restarts gets
    a new token), so each one can be scanned for and removed before results are saved.
    """

    def __init__(self, state: Path, workspace: str = WORKSPACE) -> None:
        self.state = state
        self.workspace = workspace
        self.seen: set = set()

    def document(self) -> Optional[Dict[str, Any]]:
        for path in sorted((self.state / "canvas").glob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict) and doc.get("mode") == "workspace" and doc.get("workspace") == self.workspace \
                    and conf.pid_alive(doc.get("pid")):
                if isinstance(doc.get("token"), str) and doc["token"]:
                    self.seen.add(doc["token"])
                return doc
        return None

    def token(self) -> Optional[str]:
        doc = self.document()
        return doc.get("token") if doc and isinstance(doc.get("token"), str) else None

    def get(self, path: str) -> Tuple[int, Dict[str, Any]]:
        doc = self.document()
        if not doc:
            return 0, {}
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (int(doc["port"]), path), headers={
            "Authorization": "Bearer " + str(doc.get("token")), "Accept": "application/json",
            "X-Autonom-Origin": "agent"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=10) as answer:
                return answer.status, json.loads(answer.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, {}
        except (OSError, ValueError):
            return 0, {}

    def device_of(self, session_id: str) -> Optional[str]:
        status, payload = self.get("/api/devices")
        for device in payload.get("devices") or [] if status == 200 else []:
            if isinstance(device, dict) and (device.get("session") or {}).get("id") == session_id:
                return device.get("id")
        return None

    def lease_generation(self, device_id: str) -> Optional[int]:
        status, payload = self.get("/d/%s/status" % urllib.parse.quote(device_id, safe="~"))
        generation = (payload.get("lease") or {}).get("generation") if status == 200 else None
        return generation if isinstance(generation, int) and not isinstance(generation, bool) else None


class Hooks:
    """Live hooks do nothing; the self-test's fake device uses them to act like a real device."""

    def after_tap(self, completed: bool) -> None:
        pass

    def after_pause(self) -> None:
        pass

    def after_resume(self) -> None:
        pass


# ====================================================================== the steps

class DeviceRun:
    def __init__(self, rt: Runtime, measure_s: float, thumb_s: float = 10.0, cancel_after_s: float = 3.0,
                 hooks: Optional[Hooks] = None, boot: bool = False) -> None:
        self.rt = rt
        self.platform = rt.platform
        self.target = rt.target
        self.measure_s = measure_s
        self.thumb_s = thumb_s
        self.cancel_after_s = cancel_after_s
        self.hooks = hooks or Hooks()
        self.boot = boot
        self.report = conf.Report()
        self.state: Dict[str, Any] = {}
        self.measurements: Dict[str, Any] = {"focus": None, "thumb": None}
        self.client: Optional[conf.McpClient] = None
        self.tools: Dict[str, Dict[str, Any]] = {}
        self.canvas = CanvasPeek(rt.state)
        self.booted_here = False

    # -- calls

    def call(self, name: str, arguments: Dict[str, Any], timeout: float = 120.0) -> Dict[str, Any]:
        assert self.client is not None
        result = self.client.call(name, arguments, timeout=timeout)
        problems = conf.envelope_problems(result, self.tools.get(name) or {})
        expect(not problems, "%s result: %s" % (name, "; ".join(problems[:3])))
        self.remember_token()
        return result

    def sc(self, name: str, arguments: Dict[str, Any], timeout: float = 120.0) -> Dict[str, Any]:
        return self.call(name, arguments, timeout)["structuredContent"]

    def ok(self, name: str, arguments: Dict[str, Any], timeout: float = 120.0) -> Dict[str, Any]:
        structured = self.sc(name, arguments, timeout)
        expect(structured.get("ok") is True and structured.get("outcome") == "completed",
               "%s answered %s %s: %s" % (name, structured.get("outcome"), structured.get("error_code"),
                                          short(structured.get("error") or "", 160)))
        return structured

    def remember_token(self) -> None:
        """Read the discovery document again: a restarted Canvas adds its new token to the set."""
        self.canvas.token()

    @property
    def tokens(self) -> List[str]:
        """Every Canvas token seen during the run."""
        return sorted(self.canvas.seen)

    def ref(self) -> Dict[str, str]:
        need = [key for key in ("host_id", "session_id") if not self.state.get(key)]
        if need:
            raise CheckFailed("skipped: needs %s from an earlier step" % ", ".join(need))
        return {"host_id": self.state["host_id"], "session_id": self.state["session_id"]}

    # -- the run

    def start(self) -> None:
        self.rt.prepare()
        self.client = self.rt.start_server()
        init = self.client.initialize(name="autonom-mcp-live")
        expect("result" in init, "initialize failed: %s" % short(init.get("error")))
        self.tools = {tool["name"]: tool for tool in self.client.tools()}
        expect(sorted(self.tools) == sorted(conf.TOOL_ORDER), "the server does not list the 28 tools")

    def run(self) -> Dict[str, Any]:
        started = time.time()
        try:
            try:
                self.start()
            except Exception as exc:  # noqa: BLE001 - a server that does not start fails every check
                reason = "the adapter server did not start: %s: %s" % (type(exc).__name__, short(str(exc), 200))
                for check_id in CHECK_IDS:
                    self.report.add(check_id, "fail", reason)
            else:
                self.report.run("hosts", self.step_hosts)
                self.report.run("devices", self.step_devices)
                if self.state.get("device_missing"):
                    reason = "the %s target %s is not running (pass --boot)" % (self.platform, self.target)
                    self.report.skip(CHECK_IDS[2:], reason)
                else:
                    for check_id in CHECK_IDS[2:]:
                        self.report.run(check_id, getattr(self, "step_" + check_id))
        finally:
            if self.client is not None and self.client.proc.poll() is None:
                self.remember_token()
                self.close_server()
            if self.booted_here:
                self.rt.cli("devices", "shutdown", "--target", self.target, timeout=180)
        self.final_secret_scan()
        payload = {"schema": RESULTS_SCHEMA, "platform": self.platform, "target_id": self.target,
                   "adapter_version": conf.ADAPTER_VERSION, "host_id": self.state.get("host_id"),
                   "started_at": iso(started), "ended_at": iso(time.time()), "measure_seconds": self.measure_s,
                   "checks": self.ordered_checks(), "measurements": self.measurements}
        # no_secrets has already named what it found; the payload itself never carries the values.
        payload = redact(payload, self.tokens, self.rt.roots)
        payload["ok"] = not results_problems(payload)
        return payload

    def ordered_checks(self) -> List[Dict[str, str]]:
        by_id = {item["id"]: item for item in self.report.checks}
        return [by_id.get(check_id, {"id": check_id, "status": "fail", "detail": "the step never ran"})
                for check_id in CHECK_IDS]

    def close_server(self) -> Optional[int]:
        assert self.client is not None
        code = self.client.close(timeout=40)
        self.state["server_exit"] = code
        self.state["server_lines"] = [line.decode("utf-8", "replace") for line in self.client.raw_lines]
        self.state["server_stderr"] = self.client.stderr_text()
        self.client.kill()
        return code

    def final_secret_scan(self) -> None:
        texts = list(self.state.get("server_lines") or []) + [self.state.get("server_stderr") or ""]
        texts.append(json.dumps({"checks": self.report.checks, "measurements": self.measurements}))
        hits = secret_hits(texts, self.tokens, self.rt.roots)
        if hits:
            for item in self.report.checks:
                if item["id"] == "no_secrets":
                    item.update(status="fail", detail="found after the run: %s" % ", ".join(hits))

    # -- 1. hosts and devices

    def step_hosts(self) -> str:
        structured = self.ok("list_hosts", {})
        hosts = structured.get("hosts") or []
        expect(len(hosts) == 1, "%d hosts" % len(hosts))
        host = hosts[0]
        expect(host.get("kind") == "local" and host.get("state") == "online", "host %s" % short(host))
        expect(isinstance(host.get("host_id"), str) and HOST_ID_RE.match(host["host_id"]), "invalid host id")
        self.state["host_id"] = host["host_id"]
        return "one online local host, version %s" % host.get("version")

    def _find_device(self) -> Optional[Dict[str, Any]]:
        devices = self.ok("list_devices", {"host_id": self.state.get("host_id") or self.ref()["host_id"]})
        for device in devices.get("devices") or []:
            if device.get("platform") == self.platform and device.get("target_id") == self.target:
                return device
        return None

    def _cli_lists_running(self) -> bool:
        code, payload = self.rt.cli("devices", "list", "--platform", self.platform, timeout=90)
        for raw in payload.get("devices") or [] if code == 0 else []:
            target = raw.get("target_id") or raw.get("serial") or raw.get("udid") if isinstance(raw, dict) else None
            if target == self.target and str(raw.get("state") or "").lower() in ("device", "booted"):
                return True
        return False

    def step_devices(self) -> str:
        device = self._find_device()
        online = device is not None and str(device.get("state") or "").lower() in ("device", "booted")
        if not online and self.boot:
            if self.platform == "android":
                argv = ["devices", "boot", "--avd", ANDROID_AVD[0], "--port", str(ANDROID_AVD[1]), "--timeout", "300"]
            else:
                argv = ["devices", "boot", "--target", self.target, "--timeout", "300"]
            code, payload = self.rt.cli(*argv, timeout=420)
            expect(code == 0, "devices boot failed: %s" % short(payload.get("error_code")))
            self.booted_here = True
            device = conf.wait_until(lambda: (lambda d: d if d and str(d.get("state") or "").lower()
                                              in ("device", "booted") else None)(self._find_device()), 120, step=2)
            online = device is not None
        if not online:
            if self._cli_lists_running():
                raise CheckFailed("the CLI lists the target as running but list_devices does not: %s" % short(device))
            self.state["device_missing"] = True
            raise NotExercised("the %s target %s is not running (pass --boot)" % (self.platform, self.target))
        return "listed, state %s%s" % (device.get("state"), ", booted here" if self.booted_here else "")

    # -- 2. start, foreign host

    def step_start(self) -> str:
        host = self.state.get("host_id")
        expect(host, "skipped: needs the host id from hosts")
        args = {"host_id": host, "platform": self.platform, "target_id": self.target, "label": "mcp-live"}
        first = self.ok("start_session", args, timeout=240)
        expect(first.get("created") is True, "the first start did not create a session: %s" % short(first))
        session_id = (first.get("session") or {}).get("session_id")
        expect(isinstance(session_id, str), "no session id")
        self.state["session_id"] = session_id
        second = self.sc("start_session", args, timeout=240)
        expect((second.get("outcome"), second.get("error_code")) == ("rejected", "session_exists"),
               "a second start answered %s %s" % (second.get("outcome"), second.get("error_code")))
        attached = self.ok("start_session", dict(args, attach_existing=True), timeout=240)
        expect(attached.get("created") is False and (attached.get("session") or {}).get("session_id") == session_id,
               "attach_existing answered %s" % short(attached))
        return "created, then session_exists, then attached to the same session"

    def _images(self) -> int:
        listed = self.ok("list_artifacts", dict(self.ref(), kind="image", limit=200))
        return len(listed.get("artifacts") or [])

    def step_foreign_host(self) -> str:
        ref = self.ref()
        before = self._images()
        other = ref["host_id"]
        while other == ref["host_id"]:
            other = "h_" + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz234567") for _ in range(26))
        structured = self.sc("get_screen", {"host_id": other, "session_id": ref["session_id"]})
        expect((structured.get("outcome"), structured.get("error_code")) == ("rejected", "host_not_found"),
               "a foreign host id answered %s %s" % (structured.get("outcome"), structured.get("error_code")))
        after = self._images()
        expect(after == before, "the refused call left a screenshot (%d -> %d)" % (before, after))
        return "host_not_found, no new screenshot"

    # -- 3. screen and tree

    def step_screen(self) -> str:
        screen = self.ok("get_screen", self.ref(), timeout=90).get("screen") or {}
        artifact_id = screen.get("artifact_id")
        expect(isinstance(artifact_id, str), "no artifact id")
        result = self.call("get_artifact", dict(self.ref(), artifact_id=artifact_id))
        expect(result["structuredContent"].get("ok") is True, "get_artifact failed")
        data = base64.b64decode(((result.get("_meta") or {}).get("autonom/artifact") or {}).get("data_b64") or "")
        expect(data[:8] == PNG_MAGIC, "the artifact is not a PNG")
        self.state["screen_artifact"] = artifact_id
        return "PNG %sx%s, %s bytes" % (screen.get("width"), screen.get("height"), screen.get("bytes"))

    def tree(self) -> Dict[str, Any]:
        return self.ok("get_ui_tree", dict(self.ref(), max_nodes=200), timeout=90)

    def step_tree(self) -> str:
        structured = self.tree()
        expect((structured.get("node_count") or 0) > 0, "no nodes")
        return "%d nodes" % structured["node_count"]

    # -- 4. flow

    def _flow_id(self, name: str) -> str:
        flows = self.ok("list_flows", {"host_id": self.ref()["host_id"]}).get("flows") or []
        for flow in flows:
            if flow.get("name") == name:
                return flow["flow_id"]
        raise CheckFailed("list_flows does not list %s" % name)

    def _wait_run(self, run_id: str, timeout: float, until: Sequence[str]) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            run = self.ok("get_run", {"host_id": self.ref()["host_id"], "run_id": run_id}).get("run") or {}
            if run.get("status") in until or time.monotonic() >= deadline:
                return run
            time.sleep(0.5)

    def step_flow_passed(self) -> str:
        flow_id = self._flow_id(SHORT_FLOW)
        run = self.ok("start_flow_run", dict(self.ref(), flow_id=flow_id)).get("run") or {}
        run = self._wait_run(run["run_id"], 300, ("passed", "failed", "interrupted", "cancelled", "outcome_unknown"))
        expect(run.get("status") == "passed", "the flow ended %s (%s)" % (run.get("status"), run.get("error_code")))
        reports = run.get("report_artifacts") or []
        expect(reports, "the run names no report artifact")
        listed = {item.get("artifact_id") for item in
                  self.ok("list_artifacts", dict(self.ref(), limit=200)).get("artifacts") or []}
        missing = [item for item in reports if item not in listed]
        expect(not missing, "%d report artifacts are not listed" % len(missing))
        return "passed, %d steps, %d report artifacts listed" % ((run.get("steps") or {}).get("done") or 0, len(reports))

    # -- 5. tap and reconcile

    def step_tap_changes_screen(self) -> str:
        before = self.tree()
        choice = pick_tap_node(before.get("nodes"))
        expect(choice is not None, "no clickable node with a unique text on screen")
        key, value = choice
        self.state["tap_selector"] = {key: value}
        structured = self.sc("tap_element", dict(self.ref(), selector={key: value}, mode="exact"))
        self.hooks.after_tap(structured.get("ok") is True)
        expect(structured.get("ok") is True and structured.get("outcome") == "completed",
               "tap_element answered %s %s" % (structured.get("outcome"), structured.get("error_code")))
        self.state["tap_operation"] = (structured.get("action") or {}).get("operation_id") or structured["operation_id"]
        changed = conf.wait_until(lambda: tree_hash(self.tree().get("nodes")) != tree_hash(before.get("nodes")),
                                  8, step=1.0)
        expect(changed, "the UI tree did not change after tapping %s %r" % (key, value))
        return "tapped %s %r; the tree changed" % (key, value)

    def step_op_reconciled(self) -> str:
        operation_id = self.state.get("tap_operation")
        expect(operation_id, "skipped: needs the tap's operation id")
        structured = self.ok("get_operation", dict(self.ref(), operation_id=operation_id))
        expect(structured.get("found") is True and structured.get("entries"), "get_operation did not find the tap")
        return "found %d journal entries" % len(structured["entries"])

    # -- 6. pause and resume

    def _journal_taps(self) -> List[Dict[str, Any]]:
        code, payload = self.rt.cli("journal", "--session-id", self.ref()["session_id"], "--grep", "ui tap",
                                    "--max", "500")
        expect(code == 0, "the journal could not be read: %s" % payload.get("error_code"))
        return [entry for entry in payload.get("entries") or [] if isinstance(entry, dict)]

    def step_pause_refuses_agent(self) -> str:
        ref = self.ref()
        stream = self.ok("open_stream", dict(ref, profile="focus"), timeout=90).get("stream") or {}
        self.state["presence_stream"] = stream.get("stream_id")
        device_id = conf.wait_until(lambda: self.canvas.device_of(ref["session_id"]), 10)
        before = self.canvas.lease_generation(device_id) if device_id else None
        seq_before = max([int(entry.get("seq") or 0) for entry in self._journal_taps()] or [0])
        control = self.ok("pause_session", dict(ref)).get("control") or {}
        self.hooks.after_pause()
        holder = control.get("holder") or {}
        generation = control.get("generation")
        expect(holder.get("kind") == "human" and control.get("mine") is True,
               "the pause lease is not the server's Canvas holder: %s" % short(control))
        expect(isinstance(generation, int) and (generation > before if isinstance(before, int) else generation >= 1),
               "the generation did not go up (%s -> %s)" % (before, generation))
        self.state["pause_generation"] = generation
        selector = self.state.get("tap_selector") or {"text": "Settings"}
        refused = self.sc("tap_element", dict(ref, selector=selector, mode="exact"))
        expect(refused.get("outcome") == "rejected" and refused.get("error_code") in CONTROL_CODES,
               "a tap during the pause answered %s %s" % (refused.get("outcome"), refused.get("error_code")))
        late = [entry for entry in self._journal_taps()
                if int(entry.get("seq") or 0) > seq_before and entry.get("ok") is True]
        expect(not late, "the journal shows %d successful taps after the pause" % len(late))
        return "paused at generation %s (was %s); the tap was refused %s" % (generation, before, refused["error_code"])

    def step_resume_reenables(self) -> str:
        ref = self.ref()
        expect(self.state.get("pause_generation") is not None, "skipped: needs the pause")
        self.ok("resume_session", dict(ref))
        self.hooks.after_resume()
        self.ok("press_key", dict(ref, key=RESUME_KEY[self.platform]))
        control = self.ok("get_session", dict(ref)).get("control") or {}
        generation = control.get("generation")
        expect(isinstance(generation, int) and generation > self.state["pause_generation"],
               "the agent lease generation is %s after resume (pause %s)" % (generation, self.state["pause_generation"]))
        stream_id = self.state.pop("presence_stream", None)
        if stream_id:
            self.sc("close_stream", {"stream_id": stream_id})
        return "press_key completed at generation %s" % generation

    # -- 7. cancel

    def step_cancel_run(self) -> str:
        flow_id = self._flow_id(LONG_FLOW)
        run = self.ok("start_flow_run", dict(self.ref(), flow_id=flow_id)).get("run") or {}
        run_id, operation_id = run["run_id"], run.get("operation_id")
        time.sleep(self.cancel_after_s)
        started = time.monotonic()
        cancelled = self.sc("cancel_run", {"host_id": self.ref()["host_id"], "run_id": run_id}, timeout=60)
        expect(cancelled.get("ok") is True, "cancel_run answered %s" % cancelled.get("error_code"))
        final = self._wait_run(run_id, 15, ("cancelled", "passed", "failed", "interrupted", "outcome_unknown"))
        elapsed = time.monotonic() - started
        expect(final.get("status") == "cancelled" and elapsed <= 15.5,
               "the run is %s after %.1f s" % (final.get("status"), elapsed))
        expect(operation_id, "the run has no operation id")
        gone = conf.wait_until(lambda: not processes_with(operation_id), 10, step=0.5)
        expect(gone, "a flow process is still running")
        return "cancelled in %.1f s%s, no flow process left" % (elapsed, " (forced)" if final.get("forced") else "")

    # -- 8. media and input

    def _pull(self, profile: str, seconds: float, wait_ms: int) -> Meter:
        stream = self.ok("open_stream", dict(self.ref(), profile=profile), timeout=90).get("stream") or {}
        stream_id = stream.get("stream_id")
        expect(stream_id, "no stream id")
        meter = Meter(profile)
        after, epoch = 0, None
        meter.started = time.monotonic()
        deadline = meter.started + seconds
        try:
            while time.monotonic() < deadline:
                args: Dict[str, Any] = {"stream_id": stream_id, "after_seq": after, "wait_ms": wait_ms}
                if epoch is not None:
                    args["epoch"] = epoch
                sent = time.monotonic()
                result = self.call("get_frames", args, timeout=30)
                now = time.monotonic()
                structured = result["structuredContent"]
                frames = (((result.get("_meta") or {}).get("autonom/frames") or {}).get("frames")) or []
                meter.record(structured, frames, (now - sent) * 1000.0, after, now)
                if structured.get("ok") is not True:
                    if structured.get("error_code") in FATAL_STREAM_CODES:
                        break
                    continue
                if isinstance(structured.get("last_seq"), int) and (frames or structured.get("resync")):
                    after = structured["last_seq"]
                view = structured.get("stream") or {}
                if isinstance(view.get("epoch"), int):
                    epoch = view["epoch"]
        finally:
            meter.ended = time.monotonic()
            self.sc("close_stream", {"stream_id": stream_id})
        summary = meter.summary()
        summary["codec"] = stream.get("codec")
        summary["width"], summary["height"] = stream.get("width"), stream.get("height")
        self.measurements[profile] = summary
        return meter

    def _mover(self, stop: threading.Event, counter: List[int]) -> None:
        """Scroll the screen up and down while a stream is measured: the
        streams send frames only when the screen changes, so a still screen
        would measure nothing but key frames. Direct device input (adb, idb),
        outside the adapter; Settings is scrolled, nothing is tapped."""
        if self.platform == "android":
            adb = shutil.which("adb") or "adb"
            moves = [[adb, "-s", self.target, "shell", "input", "swipe", "540", "1700", "540", "1000", "400"],
                     [adb, "-s", self.target, "shell", "input", "swipe", "540", "1000", "540", "1700", "400"]]
        else:
            idb = shutil.which("idb") or "idb"
            moves = [[idb, "ui", "swipe", "--udid", self.target, "--duration", "0.4", "200", "650", "200", "350"],
                     [idb, "ui", "swipe", "--udid", self.target, "--duration", "0.4", "200", "350", "200", "650"]]
        index = 0
        while not stop.is_set():
            try:
                subprocess.run(moves[index % 2], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=15, check=False)
                counter[0] += 1
            except (OSError, subprocess.SubprocessError):
                pass
            index += 1
            stop.wait(0.2)

    def step_media_focus(self) -> str:
        stop, counter = threading.Event(), [0]
        # only a real device is scrolled: a self-test world must not reach the
        # host's adb or idb (no device commands in --self-test or --dry-run)
        mover = (threading.Thread(target=self._mover, args=(stop, counter), daemon=True)
                 if getattr(self.rt, "world", None) is None else None)
        if mover is not None:
            mover.start()
        try:
            meter = self._pull("focus", self.measure_s, 250)
        finally:
            stop.set()
            if mover is not None:
                mover.join(20)
        ok, detail = focus_verdict(meter)
        detail = "%s; %d scrolls during the measurement" % (detail, counter[0])
        expect(ok, detail)
        return detail

    def step_media_thumb(self) -> str:
        meter = self._pull("thumb", self.thumb_s, 1000)
        ok, detail = thumb_verdict(meter, self.thumb_s)
        expect(ok, detail)
        return detail

    def step_input_tap(self) -> str:
        ref = self.ref()
        released = self.sc("release_control", dict(ref))
        expect(released.get("outcome") == "completed" or released.get("error_code") == "control_not_held",
               "release_control answered %s %s" % (released.get("outcome"), released.get("error_code")))
        structured = self.ok("send_input", dict(ref, event={"op": "tap", "x": 0.5, "y": 0.5}))
        expect((structured.get("input") or {}).get("op") == "tap", "send_input answered %s" % short(structured))
        return "released (%s), centre tap completed" % (released.get("error_code") or "completed")

    # -- 9. artifacts and secrets

    def step_artifacts(self) -> str:
        listed = self.ok("list_artifacts", dict(self.ref(), limit=200)).get("artifacts") or []
        expect(listed, "no artifacts")
        images = [item for item in listed if item.get("kind") == "image"][:3]
        expect(images, "no image artifacts")
        for item in images:
            result = self.call("get_artifact", dict(self.ref(), artifact_id=item["artifact_id"]))
            expect(result["structuredContent"].get("ok") is True, "get_artifact failed for an image")
            data = base64.b64decode(((result.get("_meta") or {}).get("autonom/artifact") or {}).get("data_b64") or "")
            expect(data[:8] == PNG_MAGIC or item.get("mime") != "image/png", "an image artifact is not a PNG")
        kinds = sorted({str(item.get("kind")) for item in listed})
        return "%d artifacts (%s); %d images fetched" % (len(listed), ", ".join(kinds), len(images))

    def step_no_secrets(self) -> str:
        assert self.client is not None
        expect(self.tokens, "the Canvas token was never seen, so it cannot be checked")
        texts = [line.decode("utf-8", "replace") for line in self.client.raw_lines]
        texts.append(self.client.stderr_text())
        texts.append(json.dumps({"checks": self.report.checks, "measurements": self.measurements}))
        hits = secret_hits(texts, self.tokens, self.rt.roots)
        expect(not hits, "found %s" % ", ".join(hits))
        return "%d server lines and the log checked" % len(self.client.raw_lines)

    # -- 10. stop

    def step_stop(self) -> str:
        ref = self.ref()
        structured = self.ok("stop_session", dict(ref), timeout=180)
        expect(structured.get("stopped") is True, "stop_session answered %s" % short(structured))
        failed_hooks = [hook.get("name") for hook in structured.get("hooks") or [] if hook.get("ok") is False]
        after = self.ok("get_session", dict(ref))
        expect(not after.get("streams"), "streams are still open: %s" % short(after.get("streams")))
        expect((after.get("entry") or {}).get("live") is False, "the session is still live")
        expect(self.canvas.device_of(ref["session_id"]) is None, "the Canvas still holds the device")
        code = self.close_server()
        expect(code == 0, "the server exited %r" % code)
        stopped = conf.wait_until(lambda: self.canvas.document() is None, 15, step=0.25)
        expect(stopped, "the workspace Canvas is still running after the server exit")
        return "stopped; streams closed; device detached; Canvas stopped%s" % (
            "; hooks failed: %s" % failed_hooks if failed_hooks else "")


def processes_with(needle: str) -> List[int]:
    out = subprocess.run(["ps", "-A", "-o", "pid=,command="], stdin=subprocess.DEVNULL, capture_output=True,
                         text=True, check=False).stdout
    pids = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and needle in parts[1] and parts[0].isdigit() and int(parts[0]) != os.getpid():
            if "ps -A" not in parts[1]:
                pids.append(int(parts[0]))
    return pids


def iso(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


# ====================================================================== commands

class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError("invalid_arguments", message, "See --help.")


def run_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="chatgpt_mcp_live.py", description=__doc__.split("\n\n")[0],
                     epilog="Allowed devices: --android %s, --ios %s. emulator-5554 is always refused."
                     % (ANDROID_SERIAL, IOS_UDID))
    parser.add_argument("--android", metavar="SERIAL", help="the Android emulator (only %s)" % ANDROID_SERIAL)
    parser.add_argument("--ios", metavar="UDID", help="the iOS Simulator (only %s)" % IOS_UDID)
    parser.add_argument("--evidence-dir", metavar="DIR", help="where mcp-live-<platform>.json is written")
    parser.add_argument("--measure-seconds", type=int, default=20, metavar="N", help="focus measurement (20)")
    parser.add_argument("--boot", action="store_true", help="boot a target that is not running, shut it down after")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; touch no device")
    parser.add_argument("--self-test", action="store_true", help="helpers, refusals and a fake run; no device")
    return parser


def sub_parser(command: str) -> argparse.ArgumentParser:
    parser = _Parser(prog="chatgpt_mcp_live.py %s" % command)
    # --results FILE... and --results FILE --results FILE both collect every file.
    if command == "check":
        parser.add_argument("--results", nargs="+", action="extend", required=True, metavar="FILE")
    elif command == "check-chatgpt":
        parser.add_argument("--results", action="append", required=True, metavar="FILE")
        parser.add_argument("--screenshots", required=True, metavar="DIR")
    else:  # check-doc and fill-doc
        parser.add_argument("--doc", required=True, metavar="FILE")
        parser.add_argument("--results", nargs="+", action="extend", default=[], metavar="FILE")
    return parser


def devices_of(args: argparse.Namespace) -> List[Tuple[str, str]]:
    devices = []
    if args.android is not None:
        refuse_device("android", args.android)
        devices.append(("android", args.android))
    if args.ios is not None:
        refuse_device("ios", args.ios)
        devices.append(("ios", args.ios))
    return devices


def plan_of(args: argparse.Namespace, devices: List[Tuple[str, str]]) -> Dict[str, Any]:
    return {"ok": True, "dry_run": True, "cli": str(REPO_CLI.relative_to(ROOT)), "plugin": str(PLUGIN.relative_to(ROOT)),
            "devices": [{"platform": platform, "target_id": target, "boot_when_missing": bool(args.boot)}
                        for platform, target in devices],
            "evidence_dir": args.evidence_dir, "measure_seconds": args.measure_seconds,
            "flows": [SHORT_FLOW, LONG_FLOW], "config": {"allowed_targets": ["%s:%s" % item for item in devices],
                                                         "control_idle_s": 30},
            "steps": list(CHECK_IDS), "files": ["mcp-live-%s.json" % platform for platform, _ in devices]}


def run_command(argv: List[str]) -> int:
    args = run_parser().parse_args(argv)
    if args.self_test:
        if any(getattr(args, name) for name in ("android", "ios", "evidence_dir", "boot", "dry_run")):
            raise UsageError("invalid_arguments", "--self-test takes no other option", "Run --self-test alone.")
        return self_test()
    devices = devices_of(args)
    if not devices:
        raise UsageError("invalid_arguments", "No device named", "Pass --android %s and/or --ios %s."
                         % (ANDROID_SERIAL, IOS_UDID))
    if not 1 <= args.measure_seconds <= 600:
        raise UsageError("invalid_arguments", "--measure-seconds must be 1 to 600", "Use the default 20.")
    if args.dry_run:
        print(json.dumps(plan_of(args, devices), indent=2))
        return 0
    if not args.evidence_dir:
        raise UsageError("invalid_arguments", "--evidence-dir is required for a live run", "Name a folder.")
    evidence = Path(args.evidence_dir).expanduser().resolve()
    if not evidence.parent.is_dir():
        raise UsageError("invalid_arguments", "The evidence folder's parent does not exist", "Create it first.")
    evidence.mkdir(mode=0o700, exist_ok=True)
    check_private_dir(evidence)  # before any device step: an unsafe existing folder is refused
    if sys.version_info < (3, 11):
        raise UsageError("python_too_old", "The Autonom CLI needs Python 3.11 or newer", "Use Python 3.11+.")
    all_ok = True
    for platform, target in devices:
        rt = Runtime(platform, target)
        device_run = DeviceRun(rt, float(args.measure_seconds), boot=args.boot)
        try:
            payload = device_run.run()
        finally:
            rt.cleanup()
        path = evidence / ("mcp-live-%s.json" % platform)
        payload = save_results(path, payload, device_run.tokens, rt.roots)
        all_ok = all_ok and payload["ok"]
        print(json.dumps({"platform": platform, "ok": payload["ok"], "file": path.name,
                          "failed": [item["id"] for item in payload["checks"] if item["status"] == "fail"]}))
    return 0 if all_ok else 1


def check_command(command: str, argv: List[str]) -> int:
    args = sub_parser(command).parse_args(argv)
    if command == "check":
        files = []
        for raw in args.results:
            path = Path(raw)
            try:
                problems = results_problems(load_json(path))
            except (OSError, ValueError, RecursionError) as exc:
                problems = ["cannot be read (%s)" % type(exc).__name__]
            files.append({"file": path.name, "problems": problems})
        ok = all(not item["problems"] for item in files)
        print(json.dumps({"ok": ok, "files": files}, indent=2))
        return 0 if ok else 1
    if command == "check-chatgpt":
        if len(args.results) != 1:
            raise UsageError("invalid_arguments", "check-chatgpt takes one --results file",
                             "Pass the one diagnostics file.")
        try:
            data = load_json(Path(args.results[0]))
        except (OSError, ValueError, RecursionError) as exc:
            data = None
            problems = ["the diagnostics file cannot be read (%s)" % type(exc).__name__]
        else:
            problems = []
        screenshots = Path(args.screenshots)
        if not screenshots.is_dir():
            problems.append("the screenshots folder does not exist")
        problems += chatgpt_problems(data if data is not None else {}, screenshots if screenshots.is_dir() else None) \
            if data is not None else []
        print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
        return 0 if not problems else 1
    doc = Path(args.doc)
    files = [Path(item) for item in args.results]
    try:
        with open(str(doc), encoding="utf-8", newline="") as handle:  # the exact bytes: no newline translation
            text = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        print(json.dumps({"problems": ["the document cannot be read (%s)" % type(exc).__name__]}, indent=2))
        return 1
    if command == "fill-doc":
        try:
            filled = fill_doc(text, files)
        except DocRefused as exc:
            print(json.dumps({"written": False, "problems": exc.problems}, indent=2))
            return 1
        try:
            mode = stat.S_IMODE(os.stat(str(doc)).st_mode)
            write_atomic(doc, filled, mode)
        except OSError as exc:
            print(json.dumps({"written": False, "problems": ["the document cannot be written (%s)"
                                                              % type(exc).__name__]}, indent=2))
            return 1
        problems = doc_problems(filled, files)
        print(json.dumps({"written": True, "problems": problems}, indent=2))
        return 1 if problems else 0
    problems = doc_problems(text, files)
    print(json.dumps({"problems": problems}, indent=2))
    return 1 if problems else 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv[:1] and argv[0] in ("check", "check-chatgpt", "check-doc", "fill-doc"):
            return check_command(argv[0], argv[1:])
        return run_command(argv)
    except UsageError as exc:
        sys.stderr.write(json.dumps(exc.payload()) + "\n")
        return 2


# ====================================================================== self-test

class FakeDevice(Hooks):
    """Makes the fake CLI act like one shared device: a tap changes the tree; a pause moves the lease."""

    def __init__(self, world: conf.FakeWorld, platform: str, target: str) -> None:
        self.world = world
        self.platform = platform
        self.target = target
        self.tree_path = world.tmp / "tree.json"
        self.version = 0
        self.write_tree()
        world.extra_env["FAKE_MCP_TREE"] = str(self.tree_path)

    def write_tree(self) -> None:
        nodes = [dict(node) for node in self.world.fake.DEFAULT_NODES]
        nodes.append({"ref": "n9", "text": "Screen %d" % self.version, "desc": "", "resource_id": "",
                      "class_name": "android.widget.TextView", "role": "text", "bounds": [40, 600, 1040, 700],
                      "clickable": False, "enabled": True})
        self.tree_path.write_text(json.dumps(nodes), encoding="utf-8")

    def after_tap(self, completed: bool) -> None:
        if completed:
            self.version += 1
            self.write_tree()

    def after_pause(self) -> None:
        lease = self.world.fake.get_lease(self.world.fake_state, self.platform, self.target) or {}
        self.world.fake.set_lease(self.world.fake_state, self.platform, self.target,
                                  holder={"kind": "human", "id": "canvas:1:1", "label": "Canvas 1"},
                                  generation=int(lease.get("generation") or 0) + 1, ttl_s=60)

    def after_resume(self) -> None:
        lease = self.world.fake.get_lease(self.world.fake_state, self.platform, self.target) or {}
        self.world.fake.set_lease(self.world.fake_state, self.platform, self.target, holder=None,
                                  generation=int(lease.get("generation") or 0))


def _synthetic_live(platform: str) -> Dict[str, Any]:
    target = ALLOWED[platform][0]
    measurements = {profile: {"frames_per_s": 29.5 if profile == "focus" else 0.8, "bytes_per_s": 412345.6,
                              "call_ms_median": 12.3, "call_ms_p95": 40.0, "largest_packet": 65000,
                              "key_frame_bytes": 64000, "resyncs": 0} for profile in ("focus", "thumb")}
    return {"schema": RESULTS_SCHEMA, "platform": platform, "target_id": target, "ok": True,
            "adapter_version": conf.ADAPTER_VERSION,
            "checks": [{"id": check_id, "status": "pass", "detail": "synthetic"} for check_id in CHECK_IDS],
            "measurements": measurements}


def coordinator_filled(text: str) -> str:
    """The template's coordinator lists with a value in place of each placeholder (for tests)."""
    return text.replace(": pending\n", ": recorded\n").replace("\n- pending\n", "\n- none\n")


def self_test() -> int:
    count = [0]

    def check(condition: Any, message: str) -> None:
        count[0] += 1
        if not condition:
            raise AssertionError(message)

    script = str(Path(__file__).resolve())
    scratch = Path(tempfile.mkdtemp(prefix="mcp-live-self-test-")).resolve()
    try:
        stubs, stub_log = scratch / "stubs", scratch / "stubs.log"
        conf.make_device_stubs(stubs, stub_log)
        env = conf.clean_env()
        env.update({"PATH": str(stubs) + os.pathsep + os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
                    "HOME": str(scratch / "home"), "AUTONOM_HOME": str(scratch / "state"),
                    "XDG_STATE_HOME": str(scratch / "xdg")})

        def run(*argv: str) -> subprocess.CompletedProcess:
            return subprocess.run([sys.executable, script, *argv], stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=120, env=env, cwd=str(scratch), check=False)

        # -- pure helpers
        check(flow_text("android", 2).startswith("schema: autonom.dev/flow/v1\nappId: com.android.settings\n"),
              "flow header")
        check(flow_text("ios", LONG_FLOW_STEPS).count("\n- scroll") == LONG_FLOW_STEPS, "the long flow scrolls 60 times")
        nodes = [{"text": "Airplane mode", "clickable": True}, {"text": "Battery", "clickable": True},
                 {"text": "Battery", "clickable": True, "desc": ""}, {"desc": "General", "clickable": True},
                 {"text": "Wi-Fi", "clickable": True}, {"text": "Hidden", "clickable": True, "visible": False}]
        check(pick_tap_node(nodes) == ("desc", "General"), "a unique safe label is picked")
        check(pick_tap_node([{"text": "Erase all", "clickable": True}]) is None, "risky labels are never tapped")
        check(tree_hash([{"text": "a"}]) != tree_hash([{"text": "b"}]), "tree hash")
        check(percentile([1, 2, 3, 4], 50) == 2.5 and percentile([], 50) is None, "percentile")
        meter = Meter("focus")
        meter.started = 0.0
        frame = lambda seq, key=False: {"seq": seq, "key": key, "data_b64": base64.b64encode(b"x" * seq).decode()}
        meter.record({"ok": True, "resync": False}, [frame(1, True), frame(2)], 10.0, 0, 0.5)
        meter.record({"ok": True, "resync": False}, [frame(4)], 20.0, 2, 1.0)
        meter.record({"ok": True, "resync": True, "dropped": 3}, [frame(9, True), frame(10)], 30.0, 4, 1.5)
        meter.ended = 2.0
        summary = meter.summary()
        check((meter.gaps, meter.resyncs, meter.dropped, meter.frames) == (1, 1, 3, 5), "gaps and resyncs")
        check(summary["frames_per_s"] == 2.5 and summary["largest_packet"] == 10, "meter summary")
        check(not focus_verdict(meter)[0], "a gap fails the focus check")
        check(secret_hits(["x Bearer abc"], None, []) == ["a bearer value"], "bearer")
        check(secret_hits(["/tmp/mcp-live-x/state"], "tok", ["/tmp/mcp-live-x"]) == ["the temporary root path"], "root")
        check(secret_hits(["tok-123456"], "tok-123456", []) == ["the Canvas token"], "token")
        env_live = live_env(scratch / "state")
        check(env_live["AUTONOM_CLI"] == str(REPO_CLI) and env_live["AUTONOM_HOME"] == str(scratch / "state"),
              "the live run uses this checkout's CLI and a temporary state base")
        # -- refusals and the dry run, with failing device tools first on PATH
        for argv in (["--dry-run", "--android", "emulator-5554"], ["--dry-run", "--android", "emulator-5586"],
                     ["--dry-run", "--ios", "00000000-0000-0000-0000-000000000000"],
                     ["--dry-run", "--android", IOS_UDID], ["--evidence-dir", "/nonexistent/x", "--android", "emulator-5554"],
                     ["--android", "emulator-5584", "--ios", "emulator-5586"]):
            proc = run(*argv)
            check(proc.returncode == 2 and proc.stdout == "", "%s exits 2 quietly" % argv)
            check(json.loads(proc.stderr)["error_code"] == "device_not_allowed", "%s is refused" % argv)
        check(not Path("/nonexistent/x").exists(), "a refused run creates nothing")
        for argv in ([], ["--android", "emulator-5584"], ["--self-test", "--dry-run"], ["--dry-run", "--android",
                     "emulator-5584", "--measure-seconds", "0"], ["check-doc"], ["bogus"]):
            proc = run(*argv)
            check(proc.returncode == 2 and json.loads(proc.stderr)["error_code"] == "invalid_arguments",
                  "%s is invalid" % argv)
        proc = run("--dry-run", "--android", ANDROID_SERIAL, "--ios", IOS_UDID, "--boot")
        check(proc.returncode == 0, "the dry run: %s" % proc.stderr)
        plan = json.loads(proc.stdout)
        check([item["target_id"] for item in plan["devices"]] == [ANDROID_SERIAL, IOS_UDID], "the planned devices")
        check(plan["cli"] == "scripts/autonom.py" and plan["steps"] == list(CHECK_IDS), "the plan")
        check(plan["files"] == ["mcp-live-android.json", "mcp-live-ios.json"], "the planned files")
        check(not stub_log.exists(), "no device tool was called by the refusals or the dry run")
        # -- check, check-chatgpt, check-doc on synthetic files
        android, ios = scratch / "mcp-live-android.json", scratch / "mcp-live-ios.json"
        android.write_text(json.dumps(_synthetic_live("android")), encoding="utf-8")
        ios.write_text(json.dumps(_synthetic_live("ios")), encoding="utf-8")
        proc = run("check", "--results", str(android), str(ios))
        check(proc.returncode == 0 and json.loads(proc.stdout)["ok"] is True, "check passes: %s" % proc.stdout)
        bad = _synthetic_live("android")
        bad["checks"][3] = {"id": "foreign_host", "status": "fail", "detail": "x"}
        bad["checks"][4] = {"id": "screen", "status": "not_exercised", "detail": ""}
        check(len(results_problems(bad)) == 2, "a failed and an unexplained check are problems")
        bad = _synthetic_live("android")
        bad["target_id"] = "emulator-5554"
        check(results_problems(bad), "a results file for another device is refused")
        diag = {"schema_version": 1, "run_id": "diag-20261021-120000-abcd", "host_id": "h_" + "a" * 26,
                "checks": [{"id": item, "status": "pass", "detail": ""} for item in PAGE_CHECK_IDS],
                "measurements": {"focus": {key: 1 for key in MEASURE_KEYS}, "thumb": None}}
        diag_path = scratch / "diag.json"
        diag_path.write_text(json.dumps(diag), encoding="utf-8")
        shots = scratch / "shots"
        shots.mkdir()
        for name in SCREENSHOTS[:2]:
            (shots / name).write_bytes(PNG_MAGIC + b"\0" * 8)
        proc = run("check-chatgpt", "--results", str(diag_path), "--screenshots", str(shots))
        check(proc.returncode == 1 and "chatgpt-thread.png" in proc.stdout, "a missing screenshot is refused")
        (shots / SCREENSHOTS[2]).write_bytes(PNG_MAGIC + b"\0" * 8)
        proc = run("check-chatgpt", "--results", str(diag_path), "--screenshots", str(shots))
        check(proc.returncode == 0, "check-chatgpt passes: %s" % proc.stdout)
        template = (ROOT / "docs" / "plans" / "CHATGPT_P3_RESULTS.md").read_text(encoding="utf-8")
        files = [android, ios, diag_path]
        problems = doc_problems(template, files)
        check(any(item.startswith("the results block differs from the results files at line") for item in problems)
              and any(item.endswith("outside the results block is still pending") for item in problems),
              "the template fails while it is pending: %s" % problems)
        filled = coordinator_filled(fill_doc(template, files))
        check(doc_problems(filled, files) == [], "the filled document passes: %s" % doc_problems(filled, files))
        check(doc_problems(filled.replace("Status: template.", "Status: filled.", 1), files) == []
              and "Status: template." in template, "text outside the block is free")
        begin = filled.split("\n").index(BLOCK_BEGIN)
        at = filled.index("| media_focus | pass |")
        line = filled[:at].count("\n") + 1
        tampered = filled.replace("| media_focus | pass |", "| media_focus | fail |", 1)
        check(doc_problems(tampered, files) == [
            "the results block differs from the results files at line %d: expected '| media_focus | pass | synthetic |',"
            " found '| media_focus | fail | synthetic |'" % line] and line > begin, "a changed byte in the block fails")
        problems = doc_problems(filled, [android, diag_path])
        check(len(problems) == 1 and "expected '| iOS target | no results file |'" in problems[0],
              "a results file left out fails: %s" % problems)
        check(any("outside the results block contains '|'" in item for item in
                  doc_problems(filled + "\nCheck | Status\n--- | ---\nhosts | pass\n", files)),
              "a table without outer pipes outside the block fails")
        check(any("the begin marker appears 2 times" in item for item in
                  doc_problems(filled + BLOCK_BEGIN + "\n", files)), "a second marker fails")
        malformed = _synthetic_live("android")
        malformed.update(checks=5, measurements=["x"])
        bad_dir = scratch / "bad"
        bad_dir.mkdir(mode=0o700)
        bad_path = bad_dir / "mcp-live-android.json"
        bad_path.write_text(json.dumps(malformed), encoding="utf-8")
        problems = doc_problems(filled, [bad_path, ios, diag_path])
        check("results mcp-live-android.json: checks is a number, not a list" in problems
              and "results mcp-live-android.json: measurements is a list, not an object" in problems,
              "malformed results fields are problems: %s" % problems)
        leaky_detail = _synthetic_live("ios")
        leaky_detail["checks"][2]["detail"] = "sent Bearer abc"
        leaky_path = bad_dir / "mcp-live-ios.json"
        leaky_path.write_text(json.dumps(leaky_detail), encoding="utf-8")
        try:
            fill_doc(template, [android, leaky_path])
            refused = False
        except DocRefused as exc:
            refused = exc.problems == ["the filled document would hold a bearer value"]
        check(refused, "fill_doc refuses a bearer value in a detail")
        # -- the results file on disk carries no Canvas token seen and no temporary root
        canvas_state = scratch / "canvas-state"
        (canvas_state / "canvas").mkdir(parents=True)
        peek = CanvasPeek(canvas_state)
        tokens = []
        for _ in range(2):  # the Canvas restarts with a new token
            tokens.append("canvas-token-" + secrets.token_hex(8))
            (canvas_state / "canvas" / "w.json").write_text(json.dumps(
                {"mode": "workspace", "workspace": WORKSPACE, "pid": os.getpid(), "port": 1, "token": tokens[-1]}),
                encoding="utf-8")
            peek.token()
        check(peek.seen == set(tokens), "every Canvas token seen is remembered")
        temp_root = str(scratch / "run-root")
        leaky = _synthetic_live("android")
        leaky["checks"][0]["detail"] = "Bearer %s at %s/state" % (tokens[0], temp_root)
        leaky["checks"][1]["detail"] = "then %s" % tokens[1]
        leaky["checks"][16] = {"id": "no_secrets", "status": "fail", "detail": "found after the run: the Canvas token"}
        leaky["measurements"]["focus"][temp_root] = tokens[1]
        saved = bad_dir / "saved.json"
        save_results(saved, leaky, peek.seen, [temp_root])
        on_disk = saved.read_text(encoding="utf-8")
        check(not any(token in on_disk for token in tokens) and temp_root not in on_disk and REDACTED_TOKEN in on_disk
              and REDACTED_ROOT in on_disk, "every token and the root are removed before the file is written")
        check(json.loads(on_disk)["checks"][16]["detail"] == "found after the run: the Canvas token"
              and saved.stat().st_mode & 0o777 == 0o600, "no_secrets still names what it found; the file is 0600")
        victim = scratch / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        link = bad_dir / "linked.json"
        link.symlink_to(victim)
        save_results(link, _synthetic_live("ios"), None, [])
        check(victim.read_text(encoding="utf-8") == "keep" and not link.is_symlink(), "a link at the path is not followed")
        open_dir = scratch / "open"
        open_dir.mkdir(mode=0o700)
        os.chmod(str(open_dir), 0o770)
        try:
            save_results(open_dir / "x.json", {}, None, [])
            unsafe = False
        except UsageError as exc:
            unsafe = exc.code == "evidence_dir_unsafe"
        check(unsafe and not (open_dir / "x.json").exists(), "a group-writable evidence folder is refused")
        proc = run("--android", ANDROID_SERIAL, "--evidence-dir", str(open_dir))
        check(proc.returncode == 2 and json.loads(proc.stderr)["error_code"] == "evidence_dir_unsafe",
              "a live run into an unsafe folder is refused before it starts: %s" % proc.stderr)
        doc_path = scratch / "doc.md"
        doc_path.write_text(template, encoding="utf-8")
        proc = run("check-doc", "--doc", str(doc_path), "--results", str(android))
        check(proc.returncode == 1 and json.loads(proc.stdout)["problems"], "check-doc refuses the template")
        proc = run("fill-doc", "--doc", str(doc_path), "--results", str(android), str(ios), "--results", str(diag_path))
        check(proc.returncode == 1 and json.loads(proc.stdout)["written"] is True
              and all("still pending" in item for item in json.loads(proc.stdout)["problems"]),
              "fill-doc writes the block; the coordinator's items are still to do: %s" % proc.stdout)
        doc_path.write_text(coordinator_filled(doc_path.read_text(encoding="utf-8")), encoding="utf-8")
        proc = run("check-doc", "--doc", str(doc_path), "--results", str(android), "--results", str(ios),
                   "--results", str(diag_path))
        check(proc.returncode == 0 and json.loads(proc.stdout) == {"problems": []},
              "check-doc counts every repeated --results file: %s" % proc.stdout)
        proc = run("check", "--results", str(android), "--results", str(ios))
        check(proc.returncode == 0 and len(json.loads(proc.stdout)["files"]) == 2, "check counts repeated --results")
        proc = run("check-chatgpt", "--results", str(diag_path), "--results", str(diag_path), "--screenshots", str(shots))
        check(proc.returncode == 2 and json.loads(proc.stderr)["error_code"] == "invalid_arguments",
              "check-chatgpt takes one --results file")
        # -- every step against the fake CLI and the fake Canvas
        if shutil.which("node") is None:
            print("self-test: node is missing; the fake device run was skipped")
        else:
            world = conf.FakeWorld(prefix="mcp-live-fake-")
            try:
                world.write_canvas({"video": {"packets": None, "packet_bytes": 1500, "key_every": 30,
                                              "interval_ms": 33}})
                world.extra_env["FAKE_MCP_STEP_MS"] = "150"
                device = FakeDevice(world, "android", ANDROID_SERIAL)
                rt = Runtime("android", ANDROID_SERIAL, world=world)
                started = time.monotonic()
                payload = DeviceRun(rt, measure_s=2.0, thumb_s=4.0, cancel_after_s=1.0, hooks=device).run()
                failed = [item for item in payload["checks"] if item["status"] != "pass"]
                check(not failed, "fake device run: %s" % json.dumps(failed, indent=1))
                check(payload["ok"] and results_problems(payload) == [], "the fake results pass check")
                check(payload["measurements"]["focus"]["frames_per_s"] >= 5, "focus measurements")
                check(not world.stub_calls(), "the fake run called a device tool: %s" % world.stub_calls())
                check(not [doc for doc in world.discovery() if conf.pid_alive(doc.get("pid"))],
                      "the fake Canvas outlived the run")
                print("self-test: fake device run passed in %.1f s" % (time.monotonic() - started))
            finally:
                world.cleanup()
            # A target that is not running: every step after devices is not exercised, and check passes.
            world = conf.FakeWorld(prefix="mcp-live-fake-")
            try:
                world.fake.set_devices(world.fake_state, [])
                rt = Runtime("ios", IOS_UDID, world=world)
                payload = DeviceRun(rt, measure_s=1.0).run()
                statuses = {item["id"]: item["status"] for item in payload["checks"]}
                check(statuses.pop("hosts") == "pass" and set(statuses.values()) == {"not_exercised"},
                      "a missing target is not exercised: %s" % payload["checks"])
                check(payload["ok"] and results_problems(payload) == [], "a run without its device passes check")
                check(not world.stub_calls(), "the missing-device run called a device tool")
            finally:
                world.cleanup()
        check(not stub_log.exists() or not stub_log.read_text(encoding="utf-8").strip(),
              "a device tool was called during the self-test")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print("self-test ok (%d assertions)" % count[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
