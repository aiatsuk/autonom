#!/usr/bin/env python3
"""Stand-in for ``scripts/autonom.py`` used by the Autonom MCP adapter tests.

The adapter runs it through ``AUTONOM_CLI`` (with ``AUTONOM_MCP_CLI_PYTHON`` set
to the test interpreter). It answers every allowlist key of contract 2.6 with
the merged CLI's output shapes and keeps its state in ``$FAKE_MCP_STATE`` (a
temp dir):

``host.json``      the host id (minted on the first ``host info``)
``devices.json``   the device inventory (a default Android and iOS pair)
``sessions.json``  sessions; each has an artifacts dir under ``sessions/<id>/``
``leases.json``    P2-style leases per ``<platform>:<target_id>``
``journal/<id>.ndjson``  one entry per journaled verb, with ``operation_id``
                   when the verb ran under a claim or a lease code refused it

``$FAKE_MCP_CLI_LOG`` gets one NDJSON line per invocation at its start
(``key``, ``argv``, claim env, ``pid``, ``cwd``, ``start``; the text of
``ui type --sensitive`` is logged as ``"<redacted>"``); its end time and exit
code go to ``$FAKE_MCP_CLI_LOG.ends`` (a process killed on timeout has none).

``$FAKE_MCP_CLI_MODE`` is a comma list of faults: ``timeout:<key>`` (leaves a
grandchild in its process group and sleeps), ``malformed:<key>``,
``crash:<key>``, ``fail:<key>:<code>``, ``slow:<key>:<ms>``. A ``fail`` answer
says ``fake failure <code>`` with hint ``fake hint`` unless
``$FAKE_MCP_FAIL_ERROR`` / ``$FAKE_MCP_FAIL_HINT`` give the text (for example a
message naming a local file, as the real CLI does).

Flows: ``flow run --events`` prints NDJSON events, one step per ``- `` line of
the flow file (or ``$FAKE_MCP_FLOW_STEPS``), ``$FAKE_MCP_STEP_MS`` per step
(default 50), final status ``$FAKE_MCP_FLOW_STATUS`` (default passed) unless a
step is refused by the lease (then ``interrupted``); SIGINT ends it with exit
130 unless ``$FAKE_MCP_FLOW_IGNORE_SIGINT=1``.

Profiles: ``session start --profile agent`` (or ``add_session(profile="agent")``)
records an agent-profile session, as the merged CLI does since DEC-028: there
``ui tree`` without ``--format`` prints ``outline`` without ``nodes``, and
``ui tap/type/key`` observe (adding ``screen``, ``changes`` and ``foreground``
after ``$FAKE_MCP_OBSERVE_MS``, default 0) unless ``--no-observe`` is given.
``--format json|outline`` and ``--observe/--no-observe`` win over the profile
(the last of the two observe flags wins).

Canvas verbs run ``$FAKE_MCP_CANVAS_CMD`` (split like a shell line, with the
canvas argv appended; ``serve`` replaces this process) and otherwise refuse
``canvas_not_found``.

The helper functions at the bottom (``add_session``, ``set_lease``, ...) let
tests prepare state without running the fake.
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import re
import secrets
import shlex
import signal
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

CLAIM_FLAGS = {"--control-holder": "holder", "--control-generation": "generation",
               "--operation-id": "operation_id"}
PROFILES = ("default", "agent")
CONTROL_CODES = ("control_held", "control_generation_stale", "control_lease_expired", "control_not_held")
DEFAULT_DEVICES = [
    {"platform": "android", "target_id": "emulator-5584", "state": "device",
     "name": "sdk_gphone64_arm64", "avd": "Autonom_Split_API36", "serial": "emulator-5584"},
    {"platform": "ios", "target_id": "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", "state": "booted",
     "name": "Autonom-Split-Test", "avd": None, "runtime": "iOS 26.0"},
]
DEFAULT_NODES = [
    {"ref": "n0", "text": "", "desc": "", "resource_id": "", "class_name": "android.widget.FrameLayout",
     "role": "group", "bounds": [0, 0, 1080, 2400], "clickable": False, "enabled": True},
    {"ref": "n1", "text": "Settings", "desc": "", "resource_id": "android:id/title",
     "class_name": "android.widget.TextView", "role": "text", "bounds": [40, 200, 1040, 320],
     "clickable": True, "enabled": True},
    {"ref": "n2", "text": "Network", "desc": "Network settings", "resource_id": "",
     "class_name": "android.widget.Button", "role": "button", "bounds": [40, 360, 1040, 480],
     "clickable": True, "enabled": True},
]


class Failure(Exception):
    def __init__(self, code: str, message: str, hint: str = "", **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.extra = extra


# ---------------------------------------------------------------- state

def state_dir() -> Path:
    raw = os.environ.get("FAKE_MCP_STATE")
    if not raw:
        raise Failure("usage_error", "FAKE_MCP_STATE is not set", "Set FAKE_MCP_STATE to a temp dir.")
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextlib.contextmanager
def locked(root: Path) -> Iterator[None]:
    with open(root / ".lock", "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read(root: Path, name: str, default: Any) -> Any:
    try:
        return json.loads((root / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(root: Path, name: str, value: Any) -> None:
    path = root / name
    temporary = path.with_name(".%s.%d.tmp" % (name, os.getpid()))
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: int) -> str:
    seconds, millis = divmod(int(ms), 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + ".%03dZ" % millis


def host_id(root: Path) -> str:
    with locked(root):
        data = _read(root, "host.json", None)
        if isinstance(data, dict) and isinstance(data.get("host_id"), str):
            return data["host_id"]
        encoded = base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=").lower()
        value = "h_" + encoded
        _write(root, "host.json", {"schema": 1, "host_id": value, "created_at": _iso(_now_ms())})
        return value


def devices(root: Path) -> List[Dict[str, Any]]:
    data = _read(root, "devices.json", None)
    return data if isinstance(data, list) else [dict(item) for item in DEFAULT_DEVICES]


def sessions(root: Path) -> Dict[str, Dict[str, Any]]:
    data = _read(root, "sessions.json", {})
    return data if isinstance(data, dict) else {}


def leases(root: Path) -> Dict[str, Dict[str, Any]]:
    data = _read(root, "leases.json", {})
    return data if isinstance(data, dict) else {}


def lease_state(lease: Optional[Dict[str, Any]], now: int) -> str:
    if not lease:
        return "none"
    if lease.get("holder") is None:
        return "none" if not lease.get("generation") else "free"
    return "held" if now < int(lease.get("expires_at_ms") or 0) else "expired"


def journal(root: Path, session_id: str, verb: str, ok: bool, error_code: Optional[str],
            operation_id: Optional[str]) -> None:
    directory = root / "journal"
    directory.mkdir(exist_ok=True)
    path = directory / ("%s.ndjson" % session_id)
    with locked(root):
        try:
            seq = sum(1 for _ in path.open(encoding="utf-8")) + 1
        except OSError:
            seq = 1
        entry: Dict[str, Any] = {"seq": seq, "ts": _iso(_now_ms()), "kind": "action", "verb": verb,
                                 "ok": ok, "error_code": error_code}
        if operation_id:
            entry["operation_id"] = operation_id
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------- argv

def split_claim(argv: List[str]) -> Tuple[Dict[str, Optional[str]], List[str]]:
    values: Dict[str, Optional[str]] = {dest: None for dest in CLAIM_FLAGS.values()}
    rest: List[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            rest.extend(argv[index:])
            break
        name, equals, inline = token.partition("=")
        if name in CLAIM_FLAGS and (equals or token == name):
            if equals:
                values[CLAIM_FLAGS[name]] = inline
                index += 1
            else:
                values[CLAIM_FLAGS[name]] = argv[index + 1] if index + 1 < len(argv) else None
                index += 2
            continue
        rest.append(token)
        index += 1
    return values, rest


KEYS = {
    ("host", "info"): "host_info", ("devices", "list"): "devices", ("session", "list"): "sessions",
    ("session", "show"): "session_show", ("session", "start"): "session_start",
    ("session", "stop"): "session_stop", ("ui", "tree"): "ui_tree", ("ui", "tap"): "ui_tap",
    ("ui", "type"): "ui_type", ("ui", "key"): "ui_key", ("control", "status"): "control_status",
    ("control", "acquire"): "control_acquire", ("control", "release"): "control_release",
    ("control", "renew"): "control_renew", ("flow", "run"): "flow_run",
    ("canvas", "serve"): "canvas_serve", ("canvas", "attach"): "canvas_attach",
    ("canvas", "detach"): "canvas_detach", ("canvas", "stop"): "canvas_stop",
}


def key_of(rest: List[str]) -> Tuple[Optional[str], List[str]]:
    if rest[:1] == ["screenshot"]:
        return "screenshot", rest[1:]
    if rest[:1] == ["journal"]:
        return "journal_find", rest[1:]
    key = KEYS.get(tuple(rest[:2]))
    return key, rest[2:]


def parse_flags(args: List[str], flags: Dict[str, bool], positionals: int = 0) -> Dict[str, Any]:
    """``flags`` maps an option to True when it takes a value. ``--`` ends options."""
    out: Dict[str, Any] = {"_pos": [], "_rest": None, "_multi": {}}
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            out["_rest"] = args[index + 1:]
            break
        name, equals, inline = token.partition("=")
        if name in flags:
            if flags[name]:
                if equals:
                    value = inline
                    index += 1
                elif index + 1 < len(args):
                    value = args[index + 1]
                    index += 2
                else:
                    raise Failure("usage_error", "argument %s: expected one argument" % name)
                out[name] = value
                out["_multi"].setdefault(name, []).append(value)
            else:
                out[name] = True
                index += 1
            continue
        if token.startswith("-") and not re.match(r"^-\d", token):
            raise Failure("usage_error", "unrecognized arguments: %s" % token)
        out["_pos"].append(token)
        index += 1
    if len(out["_pos"]) != positionals:
        raise Failure("usage_error", "expected %d positional arguments" % positionals)
    return out


# ---------------------------------------------------------------- verbs

def _session(root: Path, session_id: Optional[str], *, live: bool = False) -> Dict[str, Any]:
    if not session_id:
        raise Failure("no_active_session", "no active session", "Start one with 'autonom session start'.")
    record = sessions(root).get(session_id)
    if record is None:
        raise Failure("session_not_found", "no session %r" % session_id, "Sessions live under the store.")
    if live and record.get("stopped_at"):
        raise Failure("session_stopped", "session %s is stopped" % session_id,
                      "Start a new session; a stopped one only serves read-only verbs.")
    return record


def _identity(record: Dict[str, Any]) -> Dict[str, Any]:
    out = {"platform": record["platform"], "target_id": record["target_id"]}
    if record["platform"] == "android":
        out["serial"] = record["target_id"]
    return out


def _list_entry(record: Dict[str, Any]) -> Dict[str, Any]:
    return {"session_id": record["session_id"], "platform": record["platform"],
            "target_id": record["target_id"], "label": record.get("label"),
            "started_at": record.get("started_at"), "stopped_at": record.get("stopped_at"),
            "started_by": record.get("started_by"), "current": False,
            "live": not record.get("stopped_at")}


def _lease_key(record: Dict[str, Any]) -> str:
    return "%s:%s" % (record["platform"], record["target_id"])


def _status_entry(lease: Dict[str, Any], now: int) -> Dict[str, Any]:
    state = lease_state(lease, now)
    previous = lease.get("previous_holder") if state == "held" else None
    return {"platform": lease.get("platform"), "target_id": lease.get("target_id"), "state": state,
            "generation": lease.get("generation"), "holder": lease.get("holder"),
            "expires_at": lease.get("expires_at") if lease.get("holder") else None,
            "remaining_ms": max(0, int(lease.get("expires_at_ms") or 0) - now) if state == "held" else
            (0 if state == "expired" else None),
            "session_id": lease.get("session_id"), "updated_at": lease.get("updated_at"),
            "took_over_from": dict(previous) if isinstance(previous, dict) else None}


def _refusal(code: str, lease: Optional[Dict[str, Any]], record: Dict[str, Any],
             claim: Dict[str, Optional[str]]) -> Failure:
    holder = (lease or {}).get("holder")
    message = {"control_held": "the target is controlled by another holder",
               "control_generation_stale": "the control claim is stale",
               "control_lease_expired": "the control lease expired",
               "control_not_held": "the claim does not hold the control lease"}[code]
    return Failure(code, message, "Acquire the lease again before driving the device.",
                   platform=record["platform"], target_id=record["target_id"],
                   capability="control.lease", generation=(lease or {}).get("generation"),
                   holder=holder, expires_at=(lease or {}).get("expires_at") if holder else None,
                   claimed_holder=claim.get("holder"), claimed_generation=claim.get("generation"))


def admit(root: Path, record: Dict[str, Any], claim: Dict[str, Optional[str]]) -> Optional[Dict[str, Any]]:
    """None when admitted without a lease, the control object when admitted under one."""
    lease = leases(root).get(_lease_key(record))
    state = lease_state(lease, _now_ms())
    holder = (lease or {}).get("holder") or {}
    if claim.get("holder"):
        if state == "held" and holder.get("id") == claim["holder"]:
            if str(lease.get("generation")) != str(claim.get("generation")):
                raise _refusal("control_generation_stale", lease, record, claim)
            return {"holder": claim["holder"], "generation": lease["generation"],
                    "operation_id": claim.get("operation_id")}
        if state == "held":
            raise _refusal("control_held", lease, record, claim)
        if state == "expired" and holder.get("id") == claim["holder"]:
            raise _refusal("control_lease_expired", lease, record, claim)
        raise _refusal("control_not_held", lease, record, claim)
    if state == "held":
        raise _refusal("control_held", lease, record, claim)
    return None


def png_bytes(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\x00" + b"\x80" * (width * 3) for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def run_verb(key: str, args: List[str], claim: Dict[str, Optional[str]]) -> Tuple[int, Any]:
    root = state_dir()
    if key == "host_info":
        parse_flags(args, {})
        return 0, {"ok": True, "host_id": host_id(root), "name": "fake-host", "autonom_version": "0.36.2",
                   "platform": sys.platform,
                   "capabilities": ["sessions", "control.lease", "canvas", "flows", "artifacts"]}
    if key == "devices":
        parse_flags(args, {})
        return 0, {"ok": True, "devices": devices(root), "warnings": []}
    if key == "sessions":
        flags = parse_flags(args, {"--all": False})
        entries = [_list_entry(record) for record in sessions(root).values()
                   if flags.get("--all") or not record.get("stopped_at")]
        entries.sort(key=lambda item: (str(item["started_at"] or ""), item["session_id"]))
        return 0, {"ok": True, "sessions": entries, "count": len(entries)}
    if key == "session_show":
        flags = parse_flags(args, {"--session-id": True})
        return 0, {"ok": True, "session": _session(root, flags.get("--session-id"))}
    if key == "session_start":
        flags = parse_flags(args, {"--platform": True, "--target": True, "--alongside": False,
                                   "--label": True, "--profile": True})
        return 0, start_session(root, flags)
    if key == "session_stop":
        flags = parse_flags(args, {"--session-id": True})
        with locked(root):
            table = sessions(root)
            record = _session(root, flags.get("--session-id"), live=True)
            record["stopped_at"] = _iso(_now_ms())
            table[record["session_id"]] = record
            _write(root, "sessions.json", table)
        return 0, {"ok": True, "stopped": True, "session_id": record["session_id"]}
    if key == "screenshot":
        # `--no-agent` as the real CLI takes it: today's full-size PNG whatever the profile
        flags = parse_flags(args, {"--session-id": True, "--label": True, "--no-agent": False})
        record = _session(root, flags.get("--session-id"), live=True)
        width, height = (int(item) for item in os.environ.get("FAKE_MCP_SHOT_SIZE", "4x8").split("x"))
        directory = Path(record["artifacts_dir"]) / "screenshots"
        directory.mkdir(parents=True, exist_ok=True)
        name = "shot-%d-%s.png" % (len(list(directory.iterdir())) + 1, flags.get("--label") or "screen")
        path = directory / re.sub(r"[^A-Za-z0-9._-]", "_", name)
        path.write_bytes(png_bytes(width, height))
        journal(root, record["session_id"], "screenshot", True, None, None)
        return 0, {"ok": True, "path": str(path), "copies": [], "width": width, "height": height,
                   **_identity(record)}
    if key == "ui_tree":
        flags = parse_flags(args, {"--session-id": True, "--max-nodes": True, "--format": True,
                                   "--no-accessibility-recovery": False, "--interactable": False})
        chosen = flags.get("--format")
        if chosen is not None and chosen not in ("json", "outline"):
            raise Failure("usage_error", "argument --format: invalid choice: %r" % chosen)
        record = _session(root, flags.get("--session-id"))
        nodes = DEFAULT_NODES
        if os.environ.get("FAKE_MCP_TREE"):
            nodes = json.loads(Path(os.environ["FAKE_MCP_TREE"]).read_text(encoding="utf-8"))
        if flags.get("--interactable"):
            nodes = [node for node in nodes if node.get("clickable")]
        limit = int(flags.get("--max-nodes") or 200)
        payload: Dict[str, Any] = {"ok": True, "source": "uiautomator", "target": record["target_id"],
                                   "count": min(len(nodes), limit), "nodes": nodes[:limit],
                                   **_identity(record), "screen": [1080, 2400]}
        if len(nodes) > limit:
            payload["truncated"] = True
        if (chosen or ("outline" if _agent(record) else "json")) == "outline":
            payload["format"] = "outline"
            payload["outline"] = outline(nodes[:limit])
            payload.pop("nodes")
        return 0, payload
    if key in ("ui_tap", "ui_type", "ui_key"):
        return ui_action(root, key, args, claim)
    if key == "control_status":
        flags = parse_flags(args, {"--session-id": True})
        record = _session(root, flags.get("--session-id"))
        lease = leases(root).get(_lease_key(record))
        entries = [_status_entry(lease, _now_ms())] if lease else []
        return 0, {"ok": True, "leases": entries, "count": len(entries),
                   "lease": entries[0] if entries else None, **_identity(record)}
    if key in ("control_acquire", "control_release", "control_renew"):
        return control_verb(root, key, args, claim)
    if key == "journal_find":
        flags = parse_flags(args, {"--session-id": True, "--grep": True, "--max": True})
        record = _session(root, flags.get("--session-id"))
        path = root / "journal" / ("%s.ndjson" % record["session_id"])
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        pattern = re.compile(flags.get("--grep") or "")
        matched = [json.loads(line) for line in lines if line.strip() and pattern.search(line)]
        limit = int(flags.get("--max") or 50)
        entries = matched[-limit:]
        return 0, {"ok": True, "count": len(entries), "total_matched": len(matched),
                   "truncated": len(matched) > len(entries), "entries": entries, "journal": str(path)}
    if key == "flow_run":
        return flow_run(root, args, claim)
    if key in ("canvas_serve", "canvas_attach", "canvas_detach", "canvas_stop"):
        return canvas_verb(key, args)
    raise Failure("usage_error", "unknown verb")


def start_session(root: Path, flags: Dict[str, Any]) -> Dict[str, Any]:
    platform, target = flags.get("--platform"), flags.get("--target")
    if not platform or not target:
        raise Failure("no_target", "no target named", "Pass --platform and --target.")
    device = next((item for item in devices(root)
                   if item.get("platform") == platform and item.get("target_id") == target), None)
    if device is None or device.get("state") not in ("device", "booted"):
        raise Failure("no_target", "target %s is not connected" % target, "Boot the device first.")
    label = flags.get("--label")
    if label is not None and (not 1 <= len(label) <= 64 or not label.isprintable()):
        raise Failure("invalid_value", "--label must be 1 to 64 printable characters", "Shorten it.")
    profile = flags.get("--profile")
    if profile is not None and profile not in PROFILES:
        raise Failure("usage_error", "argument --profile: invalid choice: %r" % profile)
    with locked(root):
        table = sessions(root)
        for record in table.values():
            if record["platform"] == platform and record["target_id"] == target and not record.get("stopped_at"):
                raise Failure("session_already_active", "a session already owns %s" % target,
                              "Stop it first.", session_id=record["session_id"])
        session_id = "s_" + secrets.token_hex(5)
        directory = root / "sessions" / session_id
        directory.mkdir(parents=True, exist_ok=True)
        record = {"session_id": session_id, "platform": platform, "target_id": target, "label": label,
                  "started_at": _iso(_now_ms()), "stopped_at": None, "started_by": None,
                  "artifacts_dir": str(directory)}
        if profile is not None:
            record["profile"] = profile
        (directory / "session.json").write_text(json.dumps(record), encoding="utf-8")
        table[session_id] = record
        _write(root, "sessions.json", table)
    return {"ok": True, "session": dict(record, primary=False), "installed": None, "launched": None}


def _agent(record: Dict[str, Any]) -> bool:
    return record.get("profile") == "agent"


def outline(nodes: List[Dict[str, Any]]) -> str:
    """One line per node, like the merged CLI's ``ui tree --format outline``."""
    return "\n".join('%s %s "%s"' % (node.get("ref") or "?", node.get("role") or "node",
                                      node.get("text") or node.get("desc") or "") for node in nodes)


def observe_wanted(args: List[str], record: Dict[str, Any]) -> bool:
    """``--observe`` / ``--no-observe`` (the last before ``--`` wins), else the profile."""
    chosen = None
    for token in args:
        if token == "--":
            break
        if token == "--observe":
            chosen = True
        elif token == "--no-observe":
            chosen = False
    return _agent(record) if chosen is None else chosen


def ui_action(root: Path, key: str, args: List[str], claim: Dict[str, Optional[str]]) -> Tuple[int, Any]:
    observe_flags = {"--observe": False, "--no-observe": False}
    if key == "ui_tap":
        flags = parse_flags(args, {"--session-id": True, "--text": True, "--desc": True,
                                   "--resource-id": True, "--class-name": True, "--role": True,
                                   "--mode": True, "--index": True, **observe_flags})
    elif key == "ui_type":
        flags = parse_flags(args, {"--session-id": True, "--sensitive": False, **observe_flags})
    else:
        flags = parse_flags(args, {"--session-id": True, **observe_flags}, positionals=1)
    record = _session(root, flags.get("--session-id"), live=True)
    verb = {"ui_tap": "ui tap", "ui_type": "ui type", "ui_key": "ui key"}[key]
    try:
        control = admit(root, record, claim)
    except Failure as exc:
        journal(root, record["session_id"], verb, False, exc.code, claim.get("operation_id"))
        raise
    payload: Dict[str, Any] = {"ok": True, **_identity(record)}
    if key == "ui_tap":
        selector = {name: flags[flag] for flag, name in (("--text", "text"), ("--desc", "desc"),
                                                         ("--resource-id", "resource_id"),
                                                         ("--class-name", "class_name"),
                                                         ("--role", "role")) if flag in flags}
        if not selector:
            raise Failure("selector_required", "a selector is required", "Pass --text or --desc.")
        payload["tapped"] = {"selector": selector, "mode": flags.get("--mode", "contains")}
    elif key == "ui_type":
        payload["typed"] = len(flags.get("_rest") or [""])
        payload["chars"] = len((flags.get("_rest") or [""])[0])
    else:
        payload["key"] = flags["_pos"][0]
    if control is not None:
        payload["control"] = control
    if observe_wanted(args, record):
        time.sleep(int(os.environ.get("FAKE_MCP_OBSERVE_MS", "0")) / 1000.0)
        payload["screen"] = {"outline": outline(DEFAULT_NODES), "count": len(DEFAULT_NODES),
                             "truncated": False}
        payload["changes"] = []
        payload["foreground"] = None
    journal(root, record["session_id"], verb, True, None,
            claim.get("operation_id") if control is not None else None)
    return 0, payload


def control_verb(root: Path, key: str, args: List[str], claim: Dict[str, Optional[str]]) -> Tuple[int, Any]:
    spec = {"--session-id": True, "--holder": True, "--generation": True, "--ttl": True,
            "--kind": True, "--label": True, "--takeover": False}
    flags = parse_flags(args, spec)
    record = _session(root, flags.get("--session-id"))
    holder_id = flags.get("--holder") or claim.get("holder")
    if not holder_id:
        raise Failure("usage_error", "--holder is required")
    now = _now_ms()
    lease_key = _lease_key(record)
    with locked(root):
        table = leases(root)
        lease = table.get(lease_key)
        state = lease_state(lease, now)
        current = (lease or {}).get("holder") or {}
        ask = {"holder": holder_id, "generation": flags.get("--generation")}
        if key == "control_acquire":
            ttl = int(flags.get("--ttl") or 120)
            if state == "held" and current.get("id") == holder_id:
                lease = dict(lease, expires_at_ms=now + ttl * 1000, expires_at=_iso(now + ttl * 1000),
                             updated_at=_iso(now), reason="renewed")
                renewed = True
            elif state == "held":
                raise _refusal("control_held", lease, record, ask)
            else:
                generation = int((lease or {}).get("generation") or 0) + 1
                lease = {"schema": 1, "platform": record["platform"], "target_id": record["target_id"],
                         "generation": generation,
                         "holder": {"kind": flags.get("--kind") or "agent", "id": holder_id,
                                    "label": flags.get("--label") or holder_id},
                         "session_id": record["session_id"], "acquired_at": _iso(now),
                         "expires_at_ms": now + ttl * 1000, "expires_at": _iso(now + ttl * 1000),
                         "previous_holder": None, "updated_at": _iso(now), "reason": "acquired"}
                renewed = False
            table[lease_key] = lease
            _write(root, "leases.json", table)
            return 0, {"ok": True, "acquired": True, "renewed": renewed, "taken_over": False, "lease": lease,
                       "generation": lease["generation"], "state": "held", **_identity(record),
                       "env": {"AUTONOM_CONTROL_HOLDER": holder_id,
                               "AUTONOM_CONTROL_GENERATION": str(lease["generation"])}}
        generation = flags.get("--generation")
        if state in ("none",) or not current or current.get("id") != holder_id or \
                (key == "control_renew" and state != "held"):
            if key == "control_renew" and state == "expired" and current.get("id") == holder_id:
                raise _refusal("control_lease_expired", lease, record, ask)
            if state == "held" and current.get("id") != holder_id:
                raise _refusal("control_held", lease, record, ask)
            raise _refusal("control_not_held", lease, record, ask)
        if generation is not None and str(generation) != str(lease.get("generation")):
            raise _refusal("control_generation_stale", lease, record, ask)
        if key == "control_release":
            lease = dict(lease, holder=None, previous_holder=current, expires_at=None, expires_at_ms=None,
                         updated_at=_iso(now), reason="released")
            table[lease_key] = lease
            _write(root, "leases.json", table)
            return 0, {"ok": True, "released": True, "was_expired": state == "expired", "lease": lease,
                       **_identity(record)}
        ttl = int(flags.get("--ttl") or 120)
        lease = dict(lease, expires_at_ms=now + ttl * 1000, expires_at=_iso(now + ttl * 1000),
                     updated_at=_iso(now), reason="renewed")
        table[lease_key] = lease
        _write(root, "leases.json", table)
        return 0, {"ok": True, "acquired": True, "renewed": True, "taken_over": False, "lease": lease,
                   "generation": lease["generation"], "state": "held", **_identity(record),
                   "env": {"AUTONOM_CONTROL_HOLDER": holder_id,
                           "AUTONOM_CONTROL_GENERATION": str(lease["generation"])}}


def flow_run(root: Path, args: List[str], claim: Dict[str, Optional[str]]) -> Tuple[int, Any]:
    flags = parse_flags(args, {"--session-id": True, "--events": False, "--env": True}, positionals=1)
    record = _session(root, flags.get("--session-id"), live=True)
    path = Path(flags["_pos"][0])
    if not path.is_file():
        raise Failure("flow_file_not_found", "flow file not found", "Check the path.")
    steps = os.environ.get("FAKE_MCP_FLOW_STEPS")
    count = int(steps) if steps else max(1, sum(1 for line in path.read_text(encoding="utf-8").splitlines()
                                                 if line.lstrip().startswith("- ")))
    delay = int(os.environ.get("FAKE_MCP_STEP_MS", "50")) / 1000.0
    final = os.environ.get("FAKE_MCP_FLOW_STATUS", "passed")
    run_id = "flow_" + secrets.token_hex(6)
    run_dir = Path(record["artifacts_dir"]) / "flows" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    events_path = run_dir / "events.ndjson"
    sequence = [0]

    if os.environ.get("FAKE_MCP_FLOW_IGNORE_SIGINT") == "1":
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    else:
        def interrupted(_signum: int, _frame: Any) -> None:
            sys.stderr.write(json.dumps({"ok": False, "error": "interrupted"}) + "\n")
            sys.stderr.flush()
            os._exit(130)
        signal.signal(signal.SIGINT, interrupted)

    def emit(kind: str, payload: Dict[str, Any]) -> None:
        sequence[0] += 1
        event = {"schema": "autonom.event/v1", "schema_version": 1,
                 "event_id": "evt_" + secrets.token_hex(6), "run_id": run_id, "sequence": sequence[0],
                 "wall_time": _iso(_now_ms()), "session_id": record["session_id"], "flow_id": None,
                 "kind": kind, "platform": record["platform"], "target_id": record["target_id"],
                 "sensitive": False, "payload": payload}
        line = json.dumps(event)
        with events_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        print(line, flush=True)

    emit("flow.run.started", {"flow": str(path), "name": path.stem, "steps": count, "dry_run": False})
    status = final
    failure = None
    for index in range(count):
        emit("flow.step.started", {"step_index": index, "command": "tapOn", "step_id": "step_%d" % index})
        time.sleep(delay)
        try:
            admit(root, record, claim)
        except Failure as exc:
            emit("flow.step.finished", {"step_index": index, "command": "tapOn", "status": "failed",
                                        "error_code": exc.code, "duration_ms": int(delay * 1000)})
            status, failure = "interrupted", {"error_code": exc.code, "step_index": index}
            break
        step_status = "failed" if final == "failed" and index == count - 1 else "passed"
        emit("flow.step.finished", {"step_index": index, "command": "tapOn", "status": step_status,
                                    "duration_ms": int(delay * 1000)})
        if step_status == "failed":
            failure = {"error_code": "flow_assertion_timeout", "step_index": index}
    report = run_dir / "report.html"
    report.write_text("<!doctype html><title>fake report</title>\n", encoding="utf-8")
    emit("flow.run.finished", {"status": status, "steps": count, "failure": failure, "replay_target": None,
                               "report": str(report)})
    journal(root, record["session_id"], "flow run", status == "passed", None, claim.get("operation_id"))
    return (0 if status == "passed" else 1), None


def canvas_verb(key: str, args: List[str]) -> Tuple[int, Any]:
    command = os.environ.get("FAKE_MCP_CANVAS_CMD")
    verb = key.split("_", 1)[1]
    if not command:
        raise Failure("canvas_not_found", "no Canvas is running", "Start one with 'autonom canvas serve'.")
    argv = shlex.split(command) + ["canvas", verb] + args
    if key == "canvas_serve":
        os.execvp(argv[0], argv)
    proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, check=False)
    sys.stdout.write(proc.stdout.decode("utf-8", "replace"))
    sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
    return proc.returncode, None


# ---------------------------------------------------------------- faults and main

def faults() -> List[List[str]]:
    return [item.split(":") for item in os.environ.get("FAKE_MCP_CLI_MODE", "").split(",") if item.strip()]


def log_start(key: Optional[str], argv: List[str], claim: Dict[str, Optional[str]],
              extra: Optional[Dict[str, Any]] = None) -> None:
    path = os.environ.get("FAKE_MCP_CLI_LOG")
    if not path:
        return
    shown = list(argv)
    if key == "ui_type" and "--sensitive" in shown and "--" in shown:
        cut = shown.index("--")
        shown = shown[:cut + 1] + ["<redacted>" for _ in shown[cut + 1:]]
    line = {"key": key, "argv": shown, "pid": os.getpid(), "cwd": os.getcwd(), "start": time.time(),
            "claim": {"AUTONOM_CONTROL_HOLDER": os.environ.get("AUTONOM_CONTROL_HOLDER"),
                      "AUTONOM_CONTROL_GENERATION": os.environ.get("AUTONOM_CONTROL_GENERATION")},
            "operation_id": claim.get("operation_id")}
    line.update(extra or {})
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def log_end(code: int) -> None:
    path = os.environ.get("FAKE_MCP_CLI_LOG")
    if path:
        with open(path + ".ends", "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "end": time.time(), "exit": code}) + "\n")


def fail_payload(code: str, message: str, hint: str, extra: Dict[str, Any]) -> str:
    payload = {"ok": False, "error_code": code, "error": message}
    if hint:
        payload["hint"] = hint
    payload.update(extra)
    return json.dumps(payload)


def main(argv: List[str]) -> int:
    values, rest = split_claim(argv)
    claim = {"holder": values["holder"] or os.environ.get("AUTONOM_CONTROL_HOLDER"),
             "generation": values["generation"] or os.environ.get("AUTONOM_CONTROL_GENERATION"),
             "operation_id": values["operation_id"]}
    key, args = key_of(rest)
    extra: Dict[str, Any] = {}
    modes = [item for item in faults() if len(item) >= 2 and item[1] == key]
    timeout_mode = any(item[0] == "timeout" for item in modes)
    if timeout_mode:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        extra["child_pid"] = child.pid
    log_start(key, argv, claim, extra)
    for mode in modes:
        if mode[0] == "slow" and len(mode) >= 3:
            time.sleep(int(mode[2]) / 1000.0)
    if timeout_mode:
        time.sleep(600)
        return 0
    if key is None:
        sys.stderr.write(fail_payload("usage_error", "invalid choice", "usage: autonom ...", {}) + "\n")
        log_end(2)
        return 2
    for mode in modes:
        if mode[0] == "malformed":
            sys.stdout.write("this is not JSON {\n")
            log_end(0)
            return 0
        if mode[0] == "crash":
            sys.stderr.write("Traceback (most recent call last):\n  File \"autonom.py\"\nRuntimeError: boom\n")
            log_end(1)
            return 1
        if mode[0] == "fail" and len(mode) >= 3:
            code = mode[2]
            extra_fields: Dict[str, Any] = {}
            if code in CONTROL_CODES:
                extra_fields = {"capability": "control.lease", "generation": 7,
                                "holder": {"kind": "human", "id": "canvas:1:2", "label": "Canvas 1"},
                                "expires_at": _iso(_now_ms() + 30000)}
            message = os.environ.get("FAKE_MCP_FAIL_ERROR") or "fake failure %s" % code
            hint = os.environ.get("FAKE_MCP_FAIL_HINT") or "fake hint"
            sys.stderr.write(fail_payload(code, message, hint, extra_fields) + "\n")
            log_end(2)
            return 2
    try:
        code, payload = run_verb(key, args, claim)
    except Failure as exc:
        sys.stderr.write(fail_payload(exc.code, exc.message, exc.hint, exc.extra) + "\n")
        log_end(2)
        return 2
    if payload is not None:
        print(json.dumps(payload, indent=2))
    log_end(code)
    return code


# ---------------------------------------------------------------- test helpers

def add_session(root: Path, platform: str = "android", target_id: str = "emulator-5584", *,
                live: bool = True, label: Optional[str] = None, session_id: Optional[str] = None,
                profile: Optional[str] = None) -> str:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    session_id = session_id or "s_" + secrets.token_hex(5)
    directory = root / "sessions" / session_id
    directory.mkdir(parents=True, exist_ok=True)
    now = _iso(_now_ms())
    record = {"session_id": session_id, "platform": platform, "target_id": target_id, "label": label,
              "started_at": now, "stopped_at": None if live else now, "started_by": None,
              "artifacts_dir": str(directory)}
    if profile is not None:
        record["profile"] = profile
    (directory / "session.json").write_text(json.dumps(record), encoding="utf-8")
    with locked(root):
        table = sessions(root)
        table[session_id] = record
        _write(root, "sessions.json", table)
    return session_id


def set_devices(root: Path, items: List[Dict[str, Any]]) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    _write(root, "devices.json", items)


def set_lease(root: Path, platform: str, target_id: str, *, holder: Optional[Dict[str, Any]],
              generation: int, ttl_s: int = 60) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    now = _now_ms()
    with locked(root):
        table = leases(root)
        table["%s:%s" % (platform, target_id)] = {
            "schema": 1, "platform": platform, "target_id": target_id, "generation": generation,
            "holder": holder, "session_id": None, "acquired_at": _iso(now),
            "expires_at_ms": now + ttl_s * 1000 if holder else None,
            "expires_at": _iso(now + ttl_s * 1000) if holder else None, "previous_holder": None,
            "updated_at": _iso(now), "reason": "acquired"}
        _write(root, "leases.json", table)


def get_lease(root: Path, platform: str, target_id: str) -> Optional[Dict[str, Any]]:
    return leases(Path(root)).get("%s:%s" % (platform, target_id))


def read_journal(root: Path, session_id: str) -> List[Dict[str, Any]]:
    path = Path(root) / "journal" / ("%s.ndjson" % session_id)
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []


def read_log(path: Path) -> List[Dict[str, Any]]:
    """Log lines joined with their end records (``end``/``exit`` None when never ended)."""
    path = Path(path)
    try:
        starts = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []
    ends: Dict[int, Dict[str, Any]] = {}
    try:
        for line in Path(str(path) + ".ends").read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                ends[item["pid"]] = item
    except OSError:
        pass
    for item in starts:
        end = ends.get(item["pid"]) or {}
        item["end"] = end.get("end")
        item["exit"] = end.get("exit")
    return starts


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
