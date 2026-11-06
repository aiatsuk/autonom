"""Session tools (contract section 3): tools 4-16 of the catalog.

- Discovery: ``list_devices``, ``list_sessions``, ``get_session``.
- Lifecycle: ``start_session`` (explicit online target, ``session_exists`` or
  attach) and ``stop_session`` (session hooks first, then ``session stop``).
- Reads: ``get_screen`` (an artifact id, never a path; the PNG inline only up
  to ``inline_image_max_bytes``) and ``get_ui_tree`` (never the accessibility
  recovery).
- Semantic mutations: ``tap_element``, ``type_text``, ``press_key``, each run
  once under the session guard and the agent lease; nothing is ever retried.
- Control: ``acquire_control``, ``release_control``; reconciliation:
  ``get_operation``.

Only the core helpers are used: ``ctx.resolve`` and ``Refs`` for references,
``SessionGuard`` / ``SessionHooks`` / ``SessionViews``, ``LeaseKeeper`` and the
allowlisted ``Runner``. Python 3.9 compatible and standard library only.
"""
from __future__ import annotations

import base64
import copy
import os
import re
import stat
import struct
import unicodedata
from typing import Any, Dict, List, Mapping, Optional, Tuple

from mcp_cli import CONTROL_CODES, CliResult
from mcp_common import (AGENT_HOLDER_LABEL, ARTIFACT_ID_RE, CONTROL_VIEW, HOST_ID, OPERATION_ID_RE, PLATFORM,
                        RUN_ID_RE, SESSION_ENTRY, SESSION_ID, SESSION_REF, STREAM_ID_RE, TARGET_ID,
                        TARGET_ID_RE, CallContext, Reply, Tool, ToolError, bad_argument, integer, nullable,
                        obj, rejected, string)
from mcp_control import refusal_view
from mcp_refs import session_entry

MAX_DEVICES = 64
MAX_SESSIONS = 256
MAX_STREAMS = 16
MAX_HOOKS = 32
MAX_NODES = 500
DEFAULT_NODES = 200
LABEL_MAX = 64  # the CLI's session.LABEL_MAX
SELECTOR_MAX = 200
TEXT_MAX = 1000
STOP_WAIT_S = 15.0
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ONLINE_STATES = ("device", "booted")
SELECTOR_FIELDS = ("text", "desc", "resource_id", "class_name", "role")
KEY_RE = r"^[A-Z][A-Z0-9_]{0,47}$"
VERBS = {"ui_tap": "ui tap", "ui_type": "ui type", "ui_key": "ui key"}
_STREAM = re.compile(STREAM_ID_RE)
_RUN = re.compile(RUN_ID_RE)
_TARGET = re.compile(TARGET_ID_RE)
TEXT_CONTROL_OK = ("\n", "\t")
_CAPABILITY = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")

READ = {"readOnlyHint": True, "openWorldHint": False}
MUTATE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
DESTROY = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}
WIDGET = {"openai/widgetAccessible": True}

UNKNOWN_HINT = ("The action may or may not have reached the device. Call get_operation with this "
                "operation_id and check the screen before acting again.")
START_UNKNOWN_HINT = "Call list_sessions before starting again."
CONTROL_UNKNOWN_HINT = ("The lease may or may not have changed, and get_operation cannot find control verbs. "
                        "Call get_session to read the control lease before acting again.")
STOP_UNKNOWN_HINT = "Call list_sessions to see whether the session stopped before acting again."
EXISTS_HINT = "Pass attach_existing: true to use that session, or stop it first."
NOT_FOUND_HINT = ("The call may not have reached the Autonom CLI, or its process ended before it was "
                  "journaled. Check the screen with get_screen before retrying.")
TYPE_FAILED = "The Autonom CLI could not type the text (%s)"
TYPE_CLI_ERROR = "The Autonom CLI answered %s; its message is withheld because it may contain the typed text."
TYPE_REJECTED_HINT = ("Nothing was typed. Act on the error_code (and control, when present), then call "
                      "type_text again.")
TYPE_LOG_STDERR = " stderr: <withheld: the call carried typed text>"
TYPE_LOG_WITHHELD = "cli ui_type failed; log line withheld: the call carried typed text"
# The fixed head the core runner writes for a failed call, before any " stderr: "
# tail; only a line that starts with it is forwarded, and only this head.
_TYPE_LOG_HEAD = re.compile(r"cli ui_type exit (?:-?[0-9]+|None) status [a-z_]+ [0-9]+\.[0-9]{2}s"
                            r"(?: code [A-Za-z0-9_.-]{1,64})?")
STOP_LEASE_HOOK = "agent_lease"

# ---------------------------------------------------------------- schemas

REF_INPUT = {"host_id": HOST_ID, "session_id": SESSION_ID}
REF_REQUIRED = ("host_id", "session_id")
DEVICE = obj({
    "host_id": HOST_ID,
    "platform": PLATFORM,
    "target_id": TARGET_ID,
    "name": string(max_length=200),
    "state": string(max_length=40),
    "avd": nullable(string(max_length=200)),
    "session_id": nullable(SESSION_ID),
}, ("host_id", "platform", "target_id", "name", "state", "avd", "session_id"))
STREAM = obj({"stream_id": string(STREAM_ID_RE), "profile": string(max_length=40)}, ("stream_id", "profile"))
HOOK_RESULT = obj({"name": string(max_length=80), "ok": {"type": "boolean"},
                   "detail": nullable(string(max_length=500))}, ("name", "ok", "detail"))
SCREEN = obj({
    "artifact_id": string(ARTIFACT_ID_RE),
    "width": nullable(integer(minimum=0)),
    "height": nullable(integer(minimum=0)),
    "bytes": integer(minimum=0),
    "mime": {"const": "image/png"},
    "inline": {"type": "boolean"},
}, ("artifact_id", "width", "height", "bytes", "mime", "inline"))
SIZE = obj({"width": integer(minimum=0), "height": integer(minimum=0)}, ("width", "height"))
ACTION = obj({"verb": string(enum=tuple(VERBS.values())), "operation_id": string(OPERATION_ID_RE)},
             ("verb", "operation_id"))
JOURNAL_ENTRY = obj({
    "verb": string(max_length=200),
    "ok": nullable({"type": "boolean"}),
    "at": nullable(string(max_length=64)),
    "error_code": nullable(string(max_length=80)),
}, ("verb", "ok", "at", "error_code"))
LABEL = string(min_length=1, max_length=LABEL_MAX, description="1-64 printable characters.")
SELECTOR = obj({name: string(min_length=1, max_length=SELECTOR_MAX) for name in SELECTOR_FIELDS},
               description="At least one field; matched with mode (contains by default).")
CONTROL_OUT = nullable(CONTROL_VIEW)


# ---------------------------------------------------------------- helpers

def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _local_host_id(ctx: CallContext, host_filter: Optional[str]) -> Tuple[str, bool]:
    """The local host id (recorded for the envelope) and whether the filter keeps it."""
    host_id = ctx.services.refs.local_host()["host_id"]
    ctx.host_id = host_id
    return host_id, host_filter is None or host_filter == host_id


def _check_argv_text(field: str, value: Optional[str]) -> None:
    """Refuse a string argument that cannot reach a CLI argv as itself.

    The one check every free-text argument of these tools passes (selector
    values, the typed text, the session label) before the guard, the agent
    lease or any CLI call: NUL ends an argv string, and a lone surrogate
    (U+D800-U+DFFF) has no UTF-8 form, so it either fails to encode or reaches
    the argv as a raw byte (\\udc9b is 0x9b, an 8-bit CSI). Ids, keys and enums
    match ASCII-only schema patterns, so they cannot carry either.
    """
    if value is None:
        return
    if "\x00" in value:
        raise bad_argument(field, "must not contain NUL")
    if any("\ud800" <= char <= "\udfff" for char in value):
        raise bad_argument(field, "must not contain lone surrogates")


def _check_printable(field: str, value: Optional[str]) -> None:
    _check_argv_text(field, value)
    if value is not None and not value.isprintable():
        raise bad_argument(field, "must be printable characters only")


def _online(device: Mapping[str, Any]) -> bool:
    """Running as the CLI reports it: ``running`` when present, else the state."""
    running = device.get("running")
    if isinstance(running, bool):
        return running
    state = device.get("state")
    return isinstance(state, str) and state.lower() in ONLINE_STATES


def _target_of(raw: Mapping[str, Any]) -> Any:
    return raw.get("target_id") or raw.get("serial") or raw.get("udid")


def _device_entry(host_id: str, raw: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """One ``devices list`` entry with only the declared keys, or None when unusable."""
    platform = raw.get("platform")
    target_id = _target_of(raw)
    if platform not in ("android", "ios") or not (isinstance(target_id, str) and _TARGET.fullmatch(target_id)):
        return None
    name, state, avd = raw.get("name"), raw.get("state"), raw.get("avd")
    if not (isinstance(state, str) and state):
        state = "running" if raw.get("running") is True else "unknown"
    return {
        "host_id": host_id,
        "platform": platform,
        "target_id": target_id,
        "name": name[:200] if isinstance(name, str) and name else target_id[:200],
        "state": state[:40],
        "avd": avd[:200] if isinstance(avd, str) and avd else None,
        "session_id": None,
    }


def _devices(ctx: CallContext) -> List[Dict[str, Any]]:
    """The raw ``devices list`` entries; CLI platform warnings become envelope warnings
    (code and platform only: their text can name tool paths)."""
    result = ctx.services.runner.run("devices")
    if not result.ok:
        raise result.to_error()
    data = result.data or {}
    for item in data.get("warnings") or []:
        if isinstance(item, dict):
            platform = item.get("platform") if item.get("platform") in ("android", "ios") else "some"
            code = item.get("error_code") if isinstance(item.get("error_code"), str) else "unknown"
            ctx.warn("devices_partial", "The %s devices could not be listed (%s)" % (platform, code[:80]))
    raw = data.get("devices")
    if not isinstance(raw, list):
        raise ToolError("cli_output_invalid", "The device list has no devices array")
    return [item for item in raw if isinstance(item, dict)]


def _live_by_target(entries: List[Dict[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    return {(item["platform"], item["target_id"]): item for item in entries if item["live"]}


def _ref(entry: Mapping[str, Any]) -> Dict[str, str]:
    return {"host_id": entry["host_id"], "session_id": entry["session_id"]}


def _unknown(error: ToolError, hint: str) -> ToolError:
    """``error`` with ``hint`` when it is not a refusal (its effect is unknown)."""
    if not error.rejected:
        error.hint = hint
    return error


def _type_runner(runner: Any) -> Any:
    """The runner for one ``ui type`` call: the same runner, except that its log
    line keeps no CLI stderr (which may echo the typed text in any encoding, cut
    at any length) and no typed text.

    The result is not value-redacted, so ``error_code``, ``cli_error_code`` and
    the lease holder of a refusal stay exact; ``_type_error`` never copies the
    CLI's free text into a tool result.
    """
    shadow = copy.copy(runner)
    log = runner.log

    def quiet(line: str) -> None:
        # Fails closed: the line has already passed env-value redaction, which can
        # rewrite the " stderr: " marker or the head itself. Only a line whose head
        # is the exact fixed form, followed by nothing or by the marker, keeps that
        # head; any other line is replaced by a fixed text.
        text = str(line)
        match = _TYPE_LOG_HEAD.match(text)
        rest = text[match.end():] if match else None
        if match is None or (rest and not rest.startswith(" stderr: ")):
            log(TYPE_LOG_WITHHELD)
            return
        log(match.group(0) + (TYPE_LOG_STDERR if rest else ""))

    shadow.log = quiet
    return shadow


def _type_error(result: CliResult, control: Optional[Dict[str, Any]]) -> ToolError:
    """A failed ``ui type`` as a tool error built only from codes and fixed text.

    The code, the refusal class and the lease view come from the unredacted
    CLI answer; the CLI's ``error`` and ``hint`` are never copied: they can
    carry the text in the form the device got it (Android ``input text`` sends
    spaces as ``%s`` inside a shell-quoted word), cut at any length, or a text
    too short for value redaction. Redacting values instead would also rewrite
    a code or a lease holder that contains the typed word."""
    code = result.error_code or "cli_failed"
    fields: Dict[str, Any] = {}
    cli_code = result.cli_error_code
    if cli_code:
        fields["cli_error_code"] = cli_code
        fields["cli_error"] = TYPE_CLI_ERROR % cli_code
        capability = result.error.get("capability")
        if isinstance(capability, str) and _CAPABILITY.fullmatch(capability):
            fields["capability"] = capability  # an identifier, never free text
    if control is not None:
        fields["control"] = control
    hint = TYPE_REJECTED_HINT if result.rejected else UNKNOWN_HINT
    return ToolError(code, TYPE_FAILED % code, hint, rejected=result.rejected, **fields)


# ---------------------------------------------------------------- discovery

def list_devices(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    host_id, keep = _local_host_id(ctx, args.get("host_id"))
    if not keep:
        return {"devices": []}
    raw = _devices(ctx)
    live: Dict[Tuple[str, str], Dict[str, Any]] = {}
    try:
        live = _live_by_target(ctx.services.refs.sessions())
    except ToolError as exc:
        ctx.warn("sessions_unavailable", "The session list failed (%s); session_id is null" % exc.error_code)
    devices: List[Dict[str, Any]] = []
    for item in raw:
        entry = _device_entry(host_id, item)
        if entry is None:
            continue
        if len(devices) >= MAX_DEVICES:
            ctx.warn("devices_truncated", "Only the first %d devices are listed" % MAX_DEVICES)
            break
        session = live.get((entry["platform"], entry["target_id"]))
        entry["session_id"] = session["session_id"] if session else None
        devices.append(entry)
    return {"devices": devices}


def list_sessions(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    _host_id, keep = _local_host_id(ctx, args.get("host_id"))
    if not keep:
        return {"sessions": []}
    entries = ctx.services.refs.sessions()
    if not args.get("include_stopped"):
        entries = [entry for entry in entries if entry["live"]]
    if len(entries) > MAX_SESSIONS:
        # The CLI orders sessions by directory name (random ids), not by time:
        # keep every live session, then the most recently started stopped ones.
        ctx.warn("sessions_truncated", "Only %d sessions are listed: the live ones and the most recently "
                 "started stopped ones" % MAX_SESSIONS)
        live = [entry for entry in entries if entry["live"]]
        stopped = sorted((entry for entry in entries if not entry["live"]), key=_started_key, reverse=True)
        entries = sorted((live + stopped)[:MAX_SESSIONS], key=_started_key)
    return {"sessions": entries}


def _started_key(entry: Mapping[str, Any]) -> Tuple[str, str]:
    return entry.get("started_at") or "", entry["session_id"]


def _streams(ctx: CallContext, session_id: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for item in ctx.services.views.get("streams", session_id):
        if not isinstance(item, dict):
            continue
        stream_id, profile = item.get("stream_id"), item.get("profile")
        if isinstance(stream_id, str) and _STREAM.fullmatch(stream_id) and isinstance(profile, str):
            out.append({"stream_id": stream_id, "profile": profile[:40]})
    return out[:MAX_STREAMS]


def get_session(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    ref = ctx.resolve(args["host_id"], args["session_id"], live=False)
    control: Optional[Dict[str, Any]] = None
    try:
        control = ctx.services.keeper.status(ref)
    except ToolError as exc:
        ctx.warn("control_unavailable", "The control status failed (%s)" % exc.error_code)
    busy = ctx.services.guard.busy(ref["session_id"])
    run_id = busy.get("run_id") if busy else None
    return {
        "entry": ref,
        "control": control,
        "streams": _streams(ctx, ref["session_id"]),
        "active_run_id": run_id if isinstance(run_id, str) and _RUN.fullmatch(run_id) else None,
        "busy": busy is not None,
    }


# ---------------------------------------------------------------- lifecycle

def start_session(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    platform, target_id = args["platform"], args["target_id"]
    label = args.get("label")
    _check_printable("label", label)
    services = ctx.services
    host_id = services.refs.check_host(args["host_id"])["host_id"]
    ctx.host_id = host_id
    if not services.refs.target_allowed(platform, target_id):
        raise rejected("target_not_allowed", "Target %s:%s is not in allowed_targets" % (platform, target_id))
    try:
        raw = _devices(ctx)
        existing = _live_by_target(services.refs.sessions(max_age=0)).get((platform, target_id))
    except ToolError as exc:
        exc.rejected = True  # only reads ran: nothing was started
        raise
    device = next((item for item in raw if item.get("platform") == platform and
                   _target_of(item) == target_id), None)
    if device is None or not _online(device):
        raise rejected("target_not_found", "Target %s:%s is not connected and running" % (platform, target_id),
                       "Call list_devices; the target must be connected and running (booted).")
    if existing is not None:
        if args.get("attach_existing"):
            return {"session": _ref(existing), "entry": existing, "created": False}
        raise rejected("session_exists", "Target %s:%s already has the live session %s"
                       % (platform, target_id, existing["session_id"]), EXISTS_HINT,
                       session=_ref(existing), entry=existing)
    params: Dict[str, Any] = {"platform": platform, "target_id": target_id}
    if label is not None:
        params["label"] = label
    result = services.runner.run("session_start", params, operation_id=ctx.operation_id)
    services.refs.invalidate()
    if not result.ok:
        if result.cli_error_code == "session_already_active":
            # Two racing starts: the CLI refused the second before it started anything.
            holder = result.error.get("session_id")
            extras: Dict[str, Any] = {}
            try:
                found = next((item for item in services.refs.sessions(max_age=0)
                              if item["session_id"] == holder), None)
            except ToolError:
                found = None
            if found is not None:
                extras = {"session": _ref(found), "entry": found}
            raise rejected("session_exists", "Target %s:%s already has a live session" % (platform, target_id),
                           EXISTS_HINT, **extras)
        raise _unknown(result.to_error(), START_UNKNOWN_HINT)
    record = (result.data or {}).get("session")
    entry = None
    if isinstance(record, dict):
        raw_entry = dict(record)
        raw_entry.setdefault("live", not record.get("stopped_at"))
        entry = session_entry(host_id, raw_entry)
    if entry is None:
        raise ToolError("cli_output_invalid", "The session start answer has no usable session",
                        START_UNKNOWN_HINT)
    return {"session": _ref(entry), "entry": entry, "created": True}


def _release_agent_lease(ctx: CallContext, ref: Mapping[str, Any]) -> Dict[str, Any]:
    keeper = ctx.services.keeper
    if keeper.cached(ref["platform"], ref["target_id"]) is None:
        return {"name": STOP_LEASE_HOOK, "ok": True, "detail": "not held"}
    try:
        keeper.release(ref)
        return {"name": STOP_LEASE_HOOK, "ok": True, "detail": "released"}
    except ToolError as exc:
        return {"name": STOP_LEASE_HOOK, "ok": False, "detail": exc.error_code}
    except Exception as exc:  # noqa: BLE001 - the stop goes on
        return {"name": STOP_LEASE_HOOK, "ok": False, "detail": type(exc).__name__}


def stop_session(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """Hooks first (a running job is cancelled and lets the guard go), then the
    guard (at most 15 s) and one ``session stop``. Only a semantic tool call in
    flight refuses the stop up front."""
    ref = ctx.resolve(args["host_id"], args["session_id"])
    services = ctx.services
    session_id = ref["session_id"]
    busy = services.guard.busy(session_id)
    if busy is not None and "run_id" not in busy:
        raise rejected("session_busy", "Session %s is busy" % session_id, busy_with=busy)
    hooks = services.hooks.stopping(session_id)
    hooks.append(_release_agent_lease(ctx, ref))
    services.guard.hold(session_id, ctx.tool.name, wait_s=STOP_WAIT_S)
    try:
        # A mutation admitted between the busy check and the hold may have
        # acquired the agent lease again; with the guard held none can now.
        if services.keeper.cached(ref["platform"], ref["target_id"]) is not None:
            hooks[-1] = _release_agent_lease(ctx, ref)
        result = services.runner.run("session_stop", {"session_id": session_id}, operation_id=ctx.operation_id)
    finally:
        services.guard.release(session_id)
        services.refs.invalidate()
        services.keeper.drop(ref["platform"], ref["target_id"])
    if not result.ok:
        raise _unknown(result.to_error(), STOP_UNKNOWN_HINT)
    return {"stopped": True, "hooks": hooks[:MAX_HOOKS]}


# ---------------------------------------------------------------- reads

def _png_size(header: bytes) -> Tuple[Optional[int], Optional[int]]:
    if len(header) >= 24 and header[:8] == PNG_SIGNATURE and header[12:16] == b"IHDR":
        width, height = struct.unpack(">II", header[16:24])
        return width, height
    return None, None


def _shot_relpath(path: str, artifacts_dir: str) -> str:
    """The shot's path relative to the session's artifact directory.

    The directories are resolved (macOS ``/var`` is ``/private/var``), the file
    name is not, so a symlinked shot is opened as itself and refused."""
    parent = os.path.realpath(os.path.dirname(path))
    base = os.path.realpath(artifacts_dir)
    rel = os.path.relpath(os.path.join(parent, os.path.basename(path)), base)
    if rel == "." or rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        raise ToolError("artifact_outside_session", "The screenshot is outside the session's artifacts",
                        "Check the session with get_session.")
    return rel


def _read_shot(artifacts_dir: str, rel: str, cap: int) -> Tuple[int, bytes, Optional[bytes]]:
    """(size, first bytes, whole file when a PNG of at most ``cap`` bytes); never follows a symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(os.path.join(os.path.realpath(artifacts_dir), rel), flags)
    except OSError as exc:
        raise ToolError("screenshot_unreadable", "The screenshot file cannot be read: %s" % type(exc).__name__)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ToolError("screenshot_unreadable", "The screenshot is not a regular file")
        size = info.st_size
        header = os.read(fd, 32)
        data: Optional[bytes] = None
        if header[:8] == PNG_SIGNATURE and size <= cap:
            chunks = [header]
            total = len(header)
            while total < size:
                chunk = os.read(fd, min(65536, size - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            data = b"".join(chunks)
            if len(data) != size:
                data = None
        return size, header, data
    finally:
        os.close(fd)


def get_screen(ctx: CallContext, args: Dict[str, Any]) -> Reply:
    label = args.get("label")
    _check_printable("label", label)
    ref = ctx.resolve(args["host_id"], args["session_id"])
    services = ctx.services
    params: Dict[str, Any] = {"session_id": ref["session_id"]}
    if label is not None:
        params["label"] = label
    result = services.runner.run("screenshot", params)
    if not result.ok:
        raise result.to_error()
    data = result.data or {}
    path = data.get("path")
    if not (isinstance(path, str) and os.path.isabs(path)):
        raise ToolError("cli_output_invalid", "The screenshot answer names no file")
    artifacts_dir = str(services.refs.session_artifacts_dir(ref))
    rel = _shot_relpath(path, artifacts_dir)
    cap = int(services.config.get("inline_image_max_bytes") or 0)
    size, header, whole = _read_shot(artifacts_dir, rel, cap)
    width, height = _int(data.get("width")), _int(data.get("height"))
    if width is None or height is None:
        width, height = _png_size(header)
    screen = {
        "artifact_id": services.refs.artifact_id(ref["host_id"], ref["session_id"], rel),
        "width": width,
        "height": height,
        "bytes": size,
        "mime": "image/png",
        "inline": whole is not None,
    }
    content: List[Dict[str, Any]] = []
    if whole is not None:
        content.append({"type": "image", "data": base64.b64encode(whole).decode("ascii"),
                        "mimeType": "image/png"})
    return Reply({"screen": screen}, content=content)


def _screen_size(value: Any) -> Optional[Dict[str, int]]:
    """The CLI's ``screen`` (``[width, height]``, or an object) as ``{width, height}``."""
    if isinstance(value, dict):
        width, height = _int(value.get("width")), _int(value.get("height"))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        width, height = _int(value[0]), _int(value[1])
    else:
        return None
    if width is None or height is None:
        return None
    return {"width": width, "height": height}


def get_ui_tree(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    ref = ctx.resolve(args["host_id"], args["session_id"])
    max_nodes = args.get("max_nodes", DEFAULT_NODES)
    params: Dict[str, Any] = {"session_id": ref["session_id"], "max_nodes": max_nodes,
                              "interactable": bool(args.get("interactable"))}
    result = ctx.services.runner.run("ui_tree", params)
    if not result.ok:
        raise result.to_error()
    data = result.data or {}
    raw = data.get("nodes")
    if not isinstance(raw, list):
        raise ToolError("cli_output_invalid", "The UI tree answer has no nodes list")
    nodes = [item for item in raw if isinstance(item, dict)]
    truncated = data.get("truncated") is True
    if len(nodes) > max_nodes:
        nodes, truncated = nodes[:max_nodes], True
    for item in data.get("warnings") or []:
        if isinstance(item, dict) and isinstance(item.get("code"), str):
            message = item.get("error") if isinstance(item.get("error"), str) else item["code"]
            ctx.warn(item["code"], message)
    return {"nodes": nodes, "node_count": len(nodes), "truncated": truncated,
            "screen": _screen_size(data.get("screen"))}


# ---------------------------------------------------------------- semantic mutations

def _mutate(ctx: CallContext, args: Dict[str, Any], key: str, params: Dict[str, Any],
            secret: Optional[str] = None) -> Dict[str, Any]:
    """One semantic mutation: guard, agent lease, exactly one CLI call, no retry.

    With ``secret`` (the typed text) the call runs on ``_type_runner`` and a
    failure is built by ``_type_error``: the refusal class and the lease view
    come from the unredacted answer, and no CLI text reaches the result or the
    log. Nothing else of the call carries the text (the lease acquire does not).
    """
    ref = ctx.resolve(args["host_id"], args["session_id"])
    services = ctx.services
    with services.guard.mutation(ref["session_id"], ctx.tool.name):
        claim = services.keeper.ensure(ref)
        params = dict(params, session_id=ref["session_id"])
        runner = _type_runner(services.runner) if secret else services.runner
        result = runner.run(key, params, operation_id=ctx.operation_id, claim=claim)
        control = services.keeper.note(ref, result)
        if not result.ok:
            if secret:
                raise _type_error(result, control)
            error = result.to_error(control=control) if control is not None else result.to_error()
            raise _unknown(error, UNKNOWN_HINT)
    return {"action": {"verb": VERBS[key], "operation_id": ctx.operation_id}}


def tap_element(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    selector = dict(args.get("selector") or {})
    if not selector:
        raise bad_argument("selector", "needs at least one of %s" % ", ".join(SELECTOR_FIELDS))
    for name, value in selector.items():
        _check_argv_text("selector.%s" % name, value)
    mode = args.get("mode", "contains")
    if mode == "regex":
        # The CLI refuses a malformed pattern as invalid_value, which would read
        # as an uncertain outcome here; refuse it first, before any CLI call.
        for name, value in selector.items():
            try:
                re.compile(value)
            except (re.error, RecursionError, OverflowError):
                raise bad_argument("selector.%s" % name, "is not a valid regular expression") from None
    params: Dict[str, Any] = {"selector": selector, "mode": mode}
    if args.get("index") is not None:
        params["index"] = args["index"]
    return _mutate(ctx, args, "ui_tap", params)


def type_text(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    text = args["text"]
    if any(unicodedata.category(char) == "Cc" and char not in TEXT_CONTROL_OK for char in text):
        raise bad_argument("text", "must not contain control characters other than newline and tab")
    _check_argv_text("text", text)
    fields = _mutate(ctx, args, "ui_type", {"text": text, "sensitive": bool(args.get("sensitive"))},
                     secret=text)
    fields["chars"] = len(text)
    return fields


def press_key(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return _mutate(ctx, args, "ui_key", {"key": args["key"]})


# ---------------------------------------------------------------- control

def acquire_control(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """``control acquire`` for the agent holder (renews an own live lease in place); never takes over."""
    ref = ctx.resolve(args["host_id"], args["session_id"])
    services = ctx.services
    keeper = services.keeper
    with services.guard.mutation(ref["session_id"], ctx.tool.name):
        result = services.runner.run("control_acquire", {
            "session_id": ref["session_id"], "holder": keeper.holder_id(), "label": AGENT_HOLDER_LABEL,
            "ttl": args.get("ttl_s") or keeper.ttl()}, operation_id=ctx.operation_id)
        if not result.ok:
            keeper.drop(ref["platform"], ref["target_id"])
            if result.cli_error_code in CONTROL_CODES:
                raise result.to_error(control=refusal_view(result.error, keeper.mine_ids(), keeper.clock()))
            raise _unknown(result.to_error(), CONTROL_UNKNOWN_HINT)
        data = result.data or {}
        # The keeper caches this lease's generation, so later mutations and
        # release_control use it instead of acquiring again.
        keeper._store(ref, data)  # noqa: SLF001 - the keeper's own cache entry for this acquire
        lease = data.get("lease")
    return {"control": keeper.view(lease if isinstance(lease, dict) else None)}


def _own_generation(ctx: CallContext, ref: Mapping[str, Any]) -> int:
    """The generation of the agent lease the CLI records for the target, when this
    agent's holder id holds it (a lease kept over a server restart, or one written
    by an acquire whose answer was lost); else ``control_not_held`` with the view."""
    keeper = ctx.services.keeper
    try:
        view = keeper.status(ref)
    except ToolError as exc:
        exc.rejected = True  # only the status read ran: nothing was released
        raise
    holder = view.get("holder") or {}
    generation = view.get("generation")
    if view.get("state") in ("held", "expired") and holder.get("id") == keeper.holder_id() \
            and isinstance(generation, int):
        return generation
    raise rejected("control_not_held", "This agent holds no control lease on the target",
                   "Call acquire_control to take a free lease.", control=view)


def release_control(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """Release the agent lease: the cached generation, else the one ``control status``
    shows for this agent's holder id (the id is stable across restarts)."""
    ref = ctx.resolve(args["host_id"], args["session_id"], live=False)
    services = ctx.services
    keeper = services.keeper
    with services.guard.mutation(ref["session_id"], ctx.tool.name):
        generation: Optional[int] = None
        if keeper.cached(ref["platform"], ref["target_id"]) is None:
            generation = _own_generation(ctx, ref)
        try:
            view = keeper.release(ref, generation=generation, operation_id=ctx.operation_id)
        except ToolError as exc:
            raise _unknown(exc, CONTROL_UNKNOWN_HINT)
    return {"control": view}


# ---------------------------------------------------------------- reconciliation

def get_operation(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    ref = ctx.resolve(args["host_id"], args["session_id"], live=False)
    operation_id = args["operation_id"]
    result = ctx.services.runner.run("journal_find", {"session_id": ref["session_id"],
                                                      "grep": re.escape(operation_id)})
    if not result.ok:
        raise result.to_error()
    raw = (result.data or {}).get("entries")
    if not isinstance(raw, list):
        raise ToolError("cli_output_invalid", "The journal answer has no entries list")
    entries: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("operation_id") != operation_id:
            continue
        verb, ok, at, code = item.get("verb"), item.get("ok"), item.get("ts"), item.get("error_code")
        entries.append({
            "verb": verb[:200] if isinstance(verb, str) else "unknown",
            "ok": ok if isinstance(ok, bool) else None,
            "at": at[:64] if isinstance(at, str) else None,
            "error_code": code[:80] if isinstance(code, str) else None,
        })
    entries = entries[-5:]
    fields: Dict[str, Any] = {"found": bool(entries), "entries": entries}
    if not entries:
        fields["hint"] = NOT_FOUND_HINT
    return fields


# ---------------------------------------------------------------- catalog entries

TOOLS = (
    Tool(
        name="list_devices",
        title="List devices",
        description=("List the Android emulators and iOS Simulators of a host with their state and the "
                     "live session on each (session_id, or null). Pass host_id to filter."),
        input_schema=obj({"host_id": HOST_ID}),
        output_fields={"devices": {"type": "array", "maxItems": MAX_DEVICES, "items": DEVICE}},
        annotations=dict(READ), meta=dict(WIDGET), mutating=False, handler=list_devices,
    ),
    Tool(
        name="list_sessions",
        title="List sessions",
        description=("List the Autonom sessions of a host (live ones; include_stopped adds stopped "
                     "ones). Each entry carries the host_id and session_id every device tool needs."),
        input_schema=obj({"host_id": HOST_ID, "include_stopped": {"type": "boolean", "default": False}}),
        output_fields={"sessions": {"type": "array", "maxItems": MAX_SESSIONS, "items": SESSION_ENTRY}},
        annotations=dict(READ), meta=dict(WIDGET), mutating=False, handler=list_sessions,
    ),
    Tool(
        name="get_session",
        title="Get session",
        description=("Read one session (stopped ones too): its entry, the control lease of its target, "
                     "the streams this server has open on it and the flow run it is busy with."),
        input_schema=obj(dict(REF_INPUT), REF_REQUIRED),
        output_fields={"entry": SESSION_ENTRY, "control": CONTROL_OUT,
                       "streams": {"type": "array", "maxItems": MAX_STREAMS, "items": STREAM},
                       "active_run_id": nullable(string(RUN_ID_RE)), "busy": {"type": "boolean"}},
        annotations=dict(READ), meta=dict(WIDGET), mutating=False, handler=get_session,
    ),
    Tool(
        name="start_session",
        title="Start session",
        description=("Start an Autonom session on an explicit, running target (platform and target_id "
                     "from list_devices). A target with a live session is refused session_exists with "
                     "that session, unless attach_existing is true, which returns it (created false)."),
        input_schema=obj({"host_id": HOST_ID, "platform": PLATFORM, "target_id": TARGET_ID, "label": LABEL,
                          "attach_existing": {"type": "boolean", "default": False}},
                         ("host_id", "platform", "target_id")),
        output_fields={"session": nullable(SESSION_REF), "entry": SESSION_ENTRY,
                       "created": {"type": "boolean"}},
        annotations=dict(MUTATE), meta=dict(WIDGET), mutating=True, handler=start_session,
    ),
    Tool(
        name="stop_session",
        title="Stop session",
        description=("Stop one session: cancel its flow run, close its streams, detach its Canvas device "
                     "and release this server's agent lease on the target, then stop the session."),
        input_schema=obj(dict(REF_INPUT), REF_REQUIRED),
        output_fields={"stopped": {"type": "boolean"},
                       "hooks": {"type": "array", "maxItems": MAX_HOOKS, "items": HOOK_RESULT}},
        annotations=dict(DESTROY), meta=dict(WIDGET), mutating=True, handler=stop_session,
    ),
    Tool(
        name="get_screen",
        title="Get screen",
        description=("Take a screenshot of the session's device. Returns an artifact id (never a path) "
                     "and the PNG as an image when it is small enough."),
        input_schema=obj(dict(REF_INPUT, label=LABEL), REF_REQUIRED),
        output_fields={"screen": SCREEN},
        annotations=dict(READ), meta=dict(WIDGET), mutating=False, handler=get_screen,
    ),
    Tool(
        name="get_ui_tree",
        title="Get UI tree",
        description=("Read the accessibility tree of the session's screen. nodes is the Autonom CLI's "
                     "compact node list, copied unchanged (each item an object of the CLI compact node "
                     "schema); screen is the coordinate space (it follows the Android rotation)."),
        input_schema=obj(dict(REF_INPUT, max_nodes=integer(1, MAX_NODES, default=DEFAULT_NODES),
                              interactable={"type": "boolean", "default": False}), REF_REQUIRED),
        output_fields={"nodes": {"type": "array", "maxItems": MAX_NODES, "items": {"type": "object"}},
                       "node_count": integer(minimum=0), "truncated": {"type": "boolean"},
                       "screen": nullable(SIZE)},
        annotations=dict(READ), meta=dict(WIDGET), mutating=False, handler=get_ui_tree,
    ),
    Tool(
        name="tap_element",
        title="Tap element",
        description=("Tap the element a selector names (no coordinates), once, under the agent lease. "
                     "Never retry an outcome_unknown answer without get_operation and the screen."),
        input_schema=obj(dict(REF_INPUT, selector=SELECTOR,
                              mode=dict(string(enum=("exact", "contains", "regex")), default="contains"),
                              index=integer(0, 500)), ("host_id", "session_id", "selector")),
        output_fields={"action": ACTION},
        annotations=dict(MUTATE), meta=dict(WIDGET), mutating=True, handler=tap_element,
    ),
    Tool(
        name="type_text",
        title="Type text",
        description=("Type text into the focused field, once, under the agent lease. The text is never "
                     "echoed (the result carries chars only); sensitive keeps it out of Autonom's records."),
        input_schema=obj(dict(REF_INPUT, text=string(min_length=1, max_length=TEXT_MAX),
                              sensitive={"type": "boolean", "default": False}),
                         ("host_id", "session_id", "text")),
        output_fields={"action": ACTION, "chars": integer(minimum=0)},
        annotations=dict(MUTATE), meta=dict(WIDGET), mutating=True, handler=type_text,
    ),
    Tool(
        name="press_key",
        title="Press key",
        description="Press one key (for example BACK, HOME or ENTER), once, under the agent lease.",
        input_schema=obj(dict(REF_INPUT, key=string(KEY_RE)), ("host_id", "session_id", "key")),
        output_fields={"action": ACTION},
        annotations=dict(MUTATE), meta=dict(WIDGET), mutating=True, handler=press_key,
    ),
    Tool(
        name="acquire_control",
        title="Acquire control",
        description=("Acquire (or renew) the agent control lease on the session's target. A lease held "
                     "by a person is refused control_held; this tool never takes over."),
        input_schema=obj(dict(REF_INPUT, ttl_s=integer(5, 3600)), REF_REQUIRED),
        output_fields={"control": CONTROL_OUT},
        annotations=dict(MUTATE), meta=dict(WIDGET), mutating=True, handler=acquire_control,
    ),
    Tool(
        name="release_control",
        title="Release control",
        description="Release the agent control lease this server holds on the session's target.",
        input_schema=obj(dict(REF_INPUT), REF_REQUIRED),
        output_fields={"control": CONTROL_OUT},
        annotations=dict(MUTATE), meta=dict(WIDGET), mutating=True, handler=release_control,
    ),
    Tool(
        name="get_operation",
        title="Get operation",
        description=("Look an operation_id up in the session's journal, to reconcile a mutation whose "
                     "answer was outcome_unknown or lost. found false does not prove nothing happened."),
        input_schema=obj(dict(REF_INPUT, operation_id=string(OPERATION_ID_RE)),
                         ("host_id", "session_id", "operation_id")),
        output_fields={"found": {"type": "boolean"},
                       "entries": {"type": "array", "maxItems": 5, "items": JOURNAL_ENTRY}},
        annotations=dict(READ), meta=dict(WIDGET), mutating=False, handler=get_operation,
    ),
)
