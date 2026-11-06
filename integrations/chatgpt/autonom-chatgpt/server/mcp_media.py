"""Bridge-only media (contract section 5): tools 23-28.

The server owns one workspace Canvas (``canvas_workspace``, ``chatgpt`` by
default) and carries device video to the page only through tool calls:

- ``CanvasLink`` finds a live workspace Canvas from its discovery document, or
  starts ``canvas serve --workspace W --ephemeral --bootable-only --port 0
  --control-idle N`` as a long-lived child (stdout and stderr to
  ``/dev/null``: the Canvas prints its URL with the token), attaches session
  devices and checks that the Canvas bound the same session. Every HTTP request names ``X-Autonom-Origin``
  explicitly (``human`` for input and control, ``agent`` otherwise), because
  the Canvas counts a missing or unknown origin as ``agent``. The token lives
  only in this process's memory.
- ``Stream`` reads ``/d/<id>/ws/video`` as an API client (no ``?origin=``) into
  a bounded buffer and keeps one control connection: ``origin=human`` for a
  ``focus`` stream (presence, so the Canvas renews a person's lease while they
  watch) and ``origin=agent`` for a ``thumb`` stream. The only message ever sent
  on it is the key-frame request, at most once per second.
- ``send_input`` sends one discrete event per call to the Canvas input routes as
  human input; ``pause_session``/``resume_session`` are the Canvas Take over and
  Hand back. None of them takes the session guard, and pause/resume drop the
  agent ``LeaseKeeper`` entry of the target.

Decisions recorded here (contract "decide"):

- ``system`` buttons go to ``POST /d/<id>/key`` with the key the Canvas page's
  own buttons send: home ``KEYCODE_HOME``, back ``KEYCODE_BACK``, app-switch
  ``KEYCODE_APP_SWITCH``, power ``KEYCODE_POWER``; the page sends the volume
  buttons only as streamed system ops, so volume-up/-down use the Canvas key
  route's own ``KEYCODE_VOLUME_UP``/``KEYCODE_VOLUME_DOWN``. A ``key`` event
  ``K`` is sent as ``KEYCODE_K`` (unchanged when it already starts with it).
- Points are ``round(v * size)`` (half up, as the page's ``Math.round``) on the
  ``display`` of ``/d/<id>/status``, sent with that size as ``dw``/``dh``; a
  swipe's ``duration_ms`` is the route's ``duration``.
- Sequence numbers grow across the video sessions of one stream (a new session
  clears the buffer and increments ``epoch``), so an ``after_seq`` from before a
  new session always answers a resync, with or without ``epoch``. A ``thumb``
  answer is always one whole key frame; its ``resync`` is true only for an
  ``epoch`` that differs.
- Every buffered packet is newer than every evicted one: a packet that can never
  fit the buffer is dropped together with everything buffered before it, so a
  resync starts at a key frame after the gap, never before it.
- The Canvas HTTP timeout (10 s) scales with the ``cli_timeout_scale`` test
  seam, as the CLI timeouts do.
- Canvas HTTP goes through ``urllib`` without proxies; a refused connection
  means nothing was sent, every other transport failure after the request
  started counts as possibly delivered.
- ``open_stream`` and ``close_stream`` change only this server's streams and
  the Canvas tiles, never the device: they are not mutating tools (2.5).

Python 3.9 compatible, standard library and sibling modules only.
"""
from __future__ import annotations

import base64
import json
import math
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, NamedTuple, Optional, Tuple

import mcp_ws
from mcp_cli import kill_group
from mcp_common import (CONTROL_VIEW, HOST_ID, OPERATION_ID_RE, SESSION_ID, SESSION_REF, STREAM_ID_RE,
                        CallContext, Reply, Services, Tool, ToolError, bad_argument, integer, new_id, nullable,
                        obj, register_secret, rejected, string)
from mcp_env import state_base

DISCOVERY_SCHEMA = "autonom-canvas/v1"
CANVAS_START_WAIT_S = 30.0
# How long a started CLI may still leave with already_running after another Canvas of the
# workspace appears that is not its own process or its child (see CanvasLink._start).
CANVAS_CLAIM_WAIT_S = 3.0
HTTP_TIMEOUT_S = 10.0
BINDING_WAIT_S = 15.0
OPEN_WAIT_S = 1.0
MAX_PACKETS = 2000
HARD_FRAME_CAP = 4 * 1024 * 1024
MAX_DISPLAY_SIDE = 10000  # the Canvas MAX_SENT_DISPLAY_SIDE for dw/dh
THUMB_INTERVAL_S = 1.0
KEYFRAME_INTERVAL_S = 1.0
POLL_S = 0.05
REAP_EVERY_S = 0.25
DEFAULT_WAIT_MS = 250
KEYFRAME_REQUEST = json.dumps({"t": "system", "op": "keyframe"}, separators=(",", ":"))
LEASE_SESSION_CODES = ("control_held", "control_generation_stale", "control_lease_expired",
                       "control_not_held", "session_stopped", "session_target_mismatch")
SYSTEM_KEYS = {"home": "KEYCODE_HOME", "back": "KEYCODE_BACK", "app-switch": "KEYCODE_APP_SWITCH",
               "power": "KEYCODE_POWER", "volume-up": "KEYCODE_VOLUME_UP",
               "volume-down": "KEYCODE_VOLUME_DOWN"}
INPUT_OPS = ("tap", "swipe", "key", "text", "system")
EVENT_FIELDS = {"tap": ("x", "y"), "swipe": ("x1", "y1", "x2", "y2", "duration_ms"), "key": ("key",),
                "text": ("text",), "system": ("button",)}
EVENT_OPTIONAL = {"text": ("sensitive",)}
KEY_RE = r"^[A-Z][A-Z0-9_]{0,47}$"

HINTS = {
    "canvas_unavailable": ("The workspace Canvas is not reachable or does not show the device; check "
                           "`autonom canvas list` and call again."),
    "session_binding_mismatch": ("The Canvas shows this device for another Autonom session; stop that "
                                 "session or detach the device from the Canvas first."),
    "stream_limit": "Close a stream with close_stream first.",
    "stream_not_found": "The stream is closed or unknown; open a new stream with open_stream.",
    "media_unavailable": "The stream failed; close it and open a new stream with open_stream.",
    "canvas_refused": "The Canvas refused the input (input paused or owned locally); check the Canvas page.",
    "input_failed": "The input may or may not have reached the device; check the screen before sending again.",
    "control_failed": "The Canvas answer was lost; check the control state before pausing or resuming again.",
    "lease_refused": "Wait for the lease to end, or hand the device back, before acting again.",
    "invalid_arguments": "Fix the event and call again.",
}


def _media_rejected(code: str, message: str, **extras: Any) -> ToolError:
    return rejected(code, message, HINTS.get(code), **extras)


def _short(value: Any, limit: int = 300) -> str:
    return " ".join(str(value).split())[:limit]


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _parent_pid(pid: Any) -> Optional[int]:
    """The parent pid of ``pid`` from ``ps``, or None when it cannot be told."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=2, check=False, env=env).stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return int(out) if out.isdigit() else None


def _json_object(raw: bytes) -> Dict[str, Any]:
    try:
        value = json.loads((raw or b"{}").decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------- Canvas link

class CanvasTransport(Exception):
    """A Canvas HTTP request got no HTTP answer. ``sent`` is False only when the
    connection was refused, so nothing reached the Canvas."""

    def __init__(self, message: str, sent: bool) -> None:
        super().__init__(message)
        self.sent = sent


class CanvasLink:
    """The server's workspace Canvas: discovery, start, HTTP, attach, stop."""

    def __init__(self, services: Services) -> None:
        self.services = services
        # Lock order: _attach_lock, then _lock, then _devices_lock (held only around the dict).
        self._lock = threading.RLock()
        self._attach_lock = threading.Lock()
        self._devices_lock = threading.Lock()
        self.doc: Optional[Dict[str, Any]] = None
        self.started_here = False
        self.child: Any = None
        self.devices: Dict[str, Dict[str, Any]] = {}
        # Set by stop() (shutdown) before it takes the lock: from then on nothing finds,
        # starts or adopts a Canvas, so a handler still running cannot leave an orphan.
        self._closed = threading.Event()

    # -- settings

    def workspace(self) -> str:
        return str(self.services.config.get("canvas_workspace") or "chatgpt")

    def scale(self) -> float:
        try:
            return float(self.services.runner.scale())
        except Exception:  # noqa: BLE001 - a missing seam means 1.0
            return 1.0

    def http_timeout(self) -> float:
        return max(0.5, HTTP_TIMEOUT_S * self.scale())

    def discovery_dir(self) -> Path:
        return state_base(self.services.env, self.services.home) / "canvas"

    # -- discovery and start

    def _find(self) -> Optional[Dict[str, Any]]:
        """The live workspace Canvas of ``canvas_workspace`` from its discovery file."""
        try:
            files = sorted(self.discovery_dir().glob("*.json"))
        except OSError:
            return None
        workspace = self.workspace()
        for path in files:
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            if not isinstance(document, dict) or document.get("schema") != DISCOVERY_SCHEMA:
                continue
            if document.get("mode") != "workspace" or document.get("workspace") != workspace:
                continue
            port, pid, token = document.get("port"), document.get("pid"), document.get("token")
            if not (isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536):
                continue
            if not _pid_alive(pid) or (token is not None and not isinstance(token, str)):
                continue
            return {"port": port, "pid": pid, "token": token}
        return None

    def _adopt(self, found: Dict[str, Any], started: bool) -> Dict[str, Any]:
        register_secret(found.get("token"))
        holder_ids = getattr(self.services.keeper, "human_holder_ids", None)
        if isinstance(holder_ids, set):
            # The Canvas holder (`canvas:<port>:<pid>`) is this server's own human holder.
            holder_ids.add("canvas:%d:%d" % (found["port"], found["pid"]))
        self.doc = dict(found)
        self.started_here = started
        return dict(self.doc)

    def alive(self) -> bool:
        """Whether the adopted Canvas still runs (a child of ours is asked directly: a zombie
        still answers a signal probe)."""
        doc, child = self.doc, self.child
        if doc is None:
            return False
        if child is not None and child.pid == doc.get("pid"):
            return child.poll() is None
        return _pid_alive(doc.get("pid"))

    def _check_open(self) -> None:
        if self._closed.is_set():
            raise _media_rejected("canvas_unavailable", "The server is shutting down; the Canvas is closed")

    def ensure(self) -> Dict[str, Any]:
        """The Canvas ``{port, pid, token}``: the running one, else one started now."""
        with self._lock:
            self._check_open()
            if self.child is not None and self.child.poll() is not None:
                # The CLI this server started has ended (and is reaped now). It lives as long as
                # the Canvas it started, so no Canvas running now is this server's.
                self.child = None
                self.started_here = False
            if self.doc is not None and self.alive():
                return dict(self.doc)
            if self.doc is not None:
                self.services.log("media: the workspace Canvas is gone; looking for it again")
                self.doc = None
                with self._devices_lock:
                    self.devices.clear()
            found = self._find()
            if found is not None:
                ours = self.child is not None and self.child.poll() is None and self.started_here
                return self._adopt(found, started=ours)
            return self._start()

    @staticmethod
    def _spawned_by(child: Any, found: Dict[str, Any]) -> bool:
        """Whether the Canvas of ``found`` is ``child`` itself or a child of it."""
        pid = found.get("pid")
        return pid == child.pid or _parent_pid(pid) == child.pid

    def _start(self) -> Dict[str, Any]:
        workspace = self.workspace()
        idle = int(self.services.config.get("control_idle_s") or 120)
        try:
            child = self.services.runner.spawn("canvas_serve", {"workspace": workspace, "control_idle": idle})
        except ToolError as exc:
            raise _media_rejected("canvas_unavailable", "The workspace Canvas could not be started (%s)"
                                  % exc.error_code)
        self.child = child
        deadline = time.monotonic() + CANVAS_START_WAIT_S
        claim_deadline: Optional[float] = None
        while True:
            if self._closed.is_set():
                break  # shutdown began while starting: end the child, adopt nothing
            found = self._find()
            if found is not None:
                running = child.poll() is None
                if running and not self._spawned_by(child, found):
                    # Another server's Canvas may have won the race while our CLI is still
                    # starting; our CLI then leaves with already_running. Wait a moment for it
                    # to show which, so another server's Canvas is never taken for ours.
                    now = time.monotonic()
                    if claim_deadline is None:
                        claim_deadline = now + CANVAS_CLAIM_WAIT_S * self.scale()
                    if now < claim_deadline:
                        time.sleep(0.1)
                        continue
                    running = child.poll() is None
                if not running:
                    self.child = None  # another server's Canvas won the race; ours answered and left
                self.services.log("media: workspace Canvas %s on port %d (%s)" % (
                    workspace, found["port"], "started" if running else "already running"))
                return self._adopt(found, started=running)
            if child.poll() is not None:
                self.child = None
                raise _media_rejected("canvas_unavailable", "The workspace Canvas exited while starting (exit %s)"
                                      % child.returncode)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        kill_group(child.pid)
        try:
            child.wait(timeout=5)
        except Exception:  # noqa: BLE001 - reaping is best effort
            pass
        self.child = None
        self._check_open()
        raise _media_rejected("canvas_unavailable", "The workspace Canvas did not start within %.0f s"
                              % CANVAS_START_WAIT_S)

    # -- HTTP

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, *, origin: str,
                timeout: Optional[float] = None) -> Tuple[int, Dict[str, Any]]:
        """One authenticated request; ``(status, JSON object)`` for any HTTP answer."""
        if origin not in ("human", "agent"):
            raise ValueError("the origin must be named explicitly")
        doc = self.ensure()
        headers = {"Accept": "application/json", "X-Autonom-Origin": origin}
        if doc.get("token"):
            headers["Authorization"] = "Bearer " + doc["token"]
        data = None
        if method == "POST":
            data = json.dumps(body or {}, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (doc["port"], path), data=data,
                                         headers=headers, method=method)
        # The Canvas is loopback only; no configured proxy may ever see the Bearer token.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=timeout or self.http_timeout()) as response:  # noqa: S310
                return int(response.status), _json_object(response.read())
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001 - an unreadable body is an empty one
                raw = b""
            finally:
                exc.close()
            return int(exc.code), _json_object(raw)
        except urllib.error.URLError as exc:
            reason = exc.reason
            sent = not isinstance(reason, (ConnectionRefusedError, FileNotFoundError))
            raise CanvasTransport("the Canvas did not answer (%s)" % type(reason).__name__, sent)
        except (socket.timeout, OSError, ValueError) as exc:
            raise CanvasTransport("the Canvas did not answer (%s)" % type(exc).__name__,
                                  not isinstance(exc, ConnectionRefusedError))

    @staticmethod
    def device_path(device_id: str, rest: str) -> str:
        return "/d/%s%s" % (urllib.parse.quote(device_id, safe=""), rest)

    # -- devices

    def _bound_session(self, device_id: str) -> Optional[str]:
        try:
            status, payload = self.request("GET", "/api/devices", origin="agent")
        except CanvasTransport:
            return None
        if status != 200:
            return None
        for item in payload.get("devices") or []:
            if isinstance(item, dict) and item.get("id") == device_id:
                session = item.get("session")
                if isinstance(session, dict) and isinstance(session.get("id"), str):
                    return session["id"]
        return None

    def attach(self, ref: Dict[str, Any]) -> Dict[str, Any]:
        """The Canvas device of the session, attached when needed and bound to ``ref``."""
        session_id = str(ref["session_id"])
        with self._devices_lock:
            cached = self.devices.get(session_id)
        if cached is not None and self.alive():
            return dict(cached)  # fast path: another session's attach never delays this one
        with self._attach_lock:
            self.ensure()
            with self._devices_lock:
                cached = self.devices.get(session_id)
            if cached is not None:
                return dict(cached)
            params = {"workspace": self.workspace(), "platform": ref["platform"], "target_id": ref["target_id"]}
            self._check_open()
            result = self.services.runner.run("canvas_attach", params)
            if not result.ok:
                error = result.to_error()
                extras = {key: value for key, value in error.extras.items()
                          if key in ("cli_error_code", "cli_error", "cli_hint")}
                raise _media_rejected("canvas_unavailable", "The Canvas could not attach %s:%s (%s)" % (
                    ref["platform"], ref["target_id"], result.error_code), **extras)
            data = result.data or {}
            device = data.get("device") if isinstance(data.get("device"), dict) else {}
            device_id = device.get("id") if isinstance(device.get("id"), str) else \
                "%s~%s" % (ref["platform"], ref["target_id"])
            attached_here = data.get("attached") is True
            session = device.get("session")
            bound = session.get("id") if isinstance(session, dict) and isinstance(session.get("id"), str) else None
            deadline = time.monotonic() + BINDING_WAIT_S
            while bound is None and time.monotonic() < deadline:
                # The attach that made the device starts its session; a joining one may see none yet.
                time.sleep(0.2)
                bound = self._bound_session(device_id)
            if bound != session_id:
                if attached_here:
                    self._detach(ref, device_id)
                raise _media_rejected(
                    "session_binding_mismatch",
                    "The Canvas bound %s:%s to %s, not to %s" % (ref["platform"], ref["target_id"],
                                                                 bound or "no session", session_id))
            entry = {"device_id": device_id, "attached_here": attached_here, "platform": ref["platform"],
                     "target_id": ref["target_id"], "session_id": session_id}
            with self._devices_lock:
                self.devices[session_id] = entry
            return dict(entry)

    def forget(self, session_id: str) -> None:
        with self._devices_lock:
            self.devices.pop(session_id, None)

    def _detach(self, ref: Dict[str, Any], device_id: str) -> bool:
        result = self.services.runner.run("canvas_detach", {
            "workspace": self.workspace(), "platform": ref["platform"], "target_id": ref["target_id"]})
        if not result.ok:
            self.services.log("media: detaching %s failed (%s)" % (device_id, result.error_code))
        return result.ok

    def detach_session(self, session_id: str) -> Optional[str]:
        """Detach the session's device when this server attached it (session hook)."""
        with self._devices_lock:
            entry = self.devices.pop(session_id, None)
        if entry is None:
            return None
        if not entry["attached_here"]:
            return "kept %s (not attached by this server)" % entry["device_id"]
        ok = self._detach(entry, entry["device_id"])
        return ("detached %s" if ok else "detaching %s failed") % entry["device_id"]

    # -- shutdown

    def stop(self) -> Optional[str]:
        """Stop the Canvas only when this server started it; afterwards the link stays closed."""
        self._closed.set()
        with self._lock:
            with self._devices_lock:
                self.devices.clear()
            child = self.child
            if child is not None and child.poll() is not None:
                child = self.child = None  # our CLI has ended, and with it the Canvas it started
            # The Canvas is ours only while the CLI that started it still runs: an adopted
            # Canvas whose start our CLI answered with already_running belongs to another server.
            if not (self.started_here and child is not None):
                self.doc = None
                self.started_here = False
                return None
            result = self.services.runner.run("canvas_stop", {"workspace": self.workspace()})
            if child is not None:
                try:
                    child.wait(timeout=5)
                except Exception:  # noqa: BLE001 - a child that does not exit is ended below
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except OSError:
                        pass
                    try:
                        child.wait(timeout=2)
                    except Exception:  # noqa: BLE001
                        kill_group(child.pid)
            self.doc = None
            self.child = None
            self.started_here = False
            return "stopped" if result.ok else "stop answered %s" % result.error_code


# ---------------------------------------------------------------- streams

class Packet(NamedTuple):
    seq: int
    key: bool
    pts: int
    data: bytes


class Stream:
    """One device video stream with its bounded packet buffer and control connection."""

    def __init__(self, stream_id: str, ref: Dict[str, Any], profile: str, device_id: str,
                 cap_bytes: int, log: Any) -> None:
        self.stream_id = stream_id
        self.session = {"host_id": ref["host_id"], "session_id": ref["session_id"]}
        self.session_id = str(ref["session_id"])
        self.profile = profile
        self.device_id = device_id
        self.cap_bytes = int(cap_bytes)
        self.log = log
        self.cond = threading.Condition()
        self.state = "open"
        self.error = ""
        self.packets: Deque[Packet] = deque()
        self.bytes = 0
        self.width: Optional[int] = None
        self.height: Optional[int] = None
        self.config: Optional[bytes] = None
        self.codec: Optional[str] = None
        self.epoch = 0
        self.next_seq = 1
        self.evicted_through = 0
        self.session_first_seq = 0  # the first seq of the current video session after the first one
        self.unread_evicted = 0
        self.read_through = 0
        self.unknown_messages = 0
        self.last_pull = time.monotonic()
        self.pulling = 0
        self.last_thumb: Optional[float] = None
        self.last_keyframe_request: Optional[float] = None
        self.keyframe_requests = 0
        self.video: Optional[mcp_ws.WebSocketClient] = None
        self.control: Optional[mcp_ws.WebSocketClient] = None
        self._closing = False

    # -- view

    def view(self) -> Dict[str, Any]:
        with self.cond:
            return {"stream_id": self.stream_id, "session": dict(self.session), "profile": self.profile,
                    "state": self.state, "width": self.width, "height": self.height, "codec": self.codec,
                    "epoch": self.epoch, "buffer_bytes": self.bytes}

    # -- ingest (reader thread)

    def _evict_one(self) -> None:
        old = self.packets.popleft()
        self.bytes -= len(old.data)
        self.evicted_through = max(self.evicted_through, old.seq)
        if old.seq > self.read_through:
            self.unread_evicted += 1

    def on_session(self, width: int, height: int) -> None:
        with self.cond:
            known = self.width is not None
            while self.packets:
                self._evict_one()
            self.evicted_through = max(self.evicted_through, self.next_seq - 1)
            self.width, self.height = int(width), int(height)
            if known:
                self.epoch += 1
                # Even a client that read every packet of the old session must resync.
                self.session_first_seq = self.next_seq
            self.cond.notify_all()

    def on_config(self, data: bytes) -> None:
        with self.cond:
            self.config = bytes(data)
            self.codec = mcp_ws.codec_from_sps(self.config)
            self.cond.notify_all()

    def on_packet(self, key: bool, pts: int, data: bytes) -> None:
        size = len(data)
        with self.cond:
            seq = self.next_seq
            self.next_seq += 1
            if size > min(self.cap_bytes, HARD_FRAME_CAP):
                # It can never fit the buffer: dropped as if evicted at once, and with it every
                # older packet, so no resync can start at a key frame before the gap.
                while self.packets:
                    self._evict_one()
                self.evicted_through = max(self.evicted_through, seq)
                if seq > self.read_through:
                    self.unread_evicted += 1
                return
            while self.packets and (self.bytes + size > self.cap_bytes or len(self.packets) >= MAX_PACKETS):
                self._evict_one()
            self.packets.append(Packet(seq, bool(key), int(pts), bytes(data)))
            self.bytes += size
            self.cond.notify_all()

    def fail(self, reason: str) -> None:
        with self.cond:
            if self.state != "open" or self._closing:
                return
            self.state = "failed"
            self.error = _short(reason)
            self.cond.notify_all()
        self.log("media: stream %s failed: %s" % (self.stream_id, _short(reason)))
        self._drop_connections()

    def close(self) -> None:
        with self.cond:
            self._closing = True
            if self.state == "open":
                self.state = "closed"
            self.cond.notify_all()
        self._drop_connections()

    def _drop_connections(self) -> None:
        for conn in (self.control, self.video):
            if conn is not None:
                try:
                    conn.close(mcp_ws.CLOSE_NORMAL)
                except Exception:  # noqa: BLE001 - closing is best effort
                    pass

    def video_loop(self) -> None:
        conn = self.video
        try:
            while conn is not None:
                try:
                    opcode, data = conn.recv()
                except mcp_ws.WebSocketTimeout:
                    if self._closing:
                        return
                    continue
                message = mcp_ws.parse_video_message(data) if opcode == mcp_ws.OP_BINARY else ("unknown",)
                if message[0] == "session":
                    self.on_session(message[1], message[2])
                elif message[0] == "config":
                    self.on_config(message[1])
                elif message[0] == "packet":
                    self.on_packet(message[1], message[2], message[3])
                else:
                    with self.cond:
                        self.unknown_messages += 1
        except mcp_ws.WebSocketError as exc:
            if not self._closing:
                self.fail("the video connection ended: %s" % exc)
        except Exception as exc:  # noqa: BLE001 - a reader fault fails the stream, never the server
            if not self._closing:
                self.fail("the video reader failed: %s" % type(exc).__name__)

    def control_loop(self) -> None:
        conn = self.control
        try:
            while conn is not None:
                try:
                    conn.recv()  # the Canvas state and lease messages: read and ignored
                except mcp_ws.WebSocketTimeout:
                    if self._closing:
                        return
                    continue
        except mcp_ws.WebSocketError as exc:
            if not self._closing:
                self.fail("the control connection ended: %s" % exc)
        except Exception as exc:  # noqa: BLE001
            if not self._closing:
                self.fail("the control reader failed: %s" % type(exc).__name__)

    # -- pull

    def _newest_key(self) -> Optional[int]:
        for index in range(len(self.packets) - 1, -1, -1):
            if self.packets[index].key:
                return index
        return None

    def _take(self, start: int, max_bytes: int) -> List[Packet]:
        out: List[Packet] = []
        total = 0
        for index in range(start, len(self.packets)):
            packet = self.packets[index]
            if out and total + len(packet.data) > max_bytes:
                break
            out.append(packet)
            total += len(packet.data)
        return out

    def _maybe_request_keyframe(self) -> None:
        """At most one key-frame request per second on this stream's control connection."""
        now = time.monotonic()
        if self.last_keyframe_request is not None and now - self.last_keyframe_request < KEYFRAME_INTERVAL_S:
            return
        conn = self.control
        if conn is None or self.state != "open":
            return
        self.last_keyframe_request = now
        try:
            conn.send_text(KEYFRAME_REQUEST)
            self.keyframe_requests += 1
        except mcp_ws.WebSocketError:
            pass  # the control reader reports the lost connection

    def _focus(self, after_seq: int, epoch: Optional[int], max_bytes: int) -> Tuple[List[Packet], bool]:
        resync = ((epoch is not None and epoch != self.epoch) or after_seq < self.evicted_through
                  or after_seq < self.session_first_seq)
        if resync:
            index = self._newest_key()
            if index is None:
                self._maybe_request_keyframe()  # a resync is pending: ask the encoder now
                return [], True
            return self._take(index, max_bytes), True
        if not self.packets:
            return [], False
        start = max(0, after_seq + 1 - self.packets[0].seq)
        if start >= len(self.packets):
            return [], False
        return self._take(start, max_bytes), False

    def _thumb(self, after_seq: int, epoch: Optional[int]) -> Tuple[List[Packet], bool]:
        resync = epoch is not None and epoch != self.epoch
        now = time.monotonic()
        if self.last_thumb is not None and now - self.last_thumb < THUMB_INTERVAL_S:
            return [], resync
        index = self._newest_key()
        if index is not None and (self.packets[index].seq > after_seq or resync):
            self.last_thumb = now
            return [self.packets[index]], resync
        self._maybe_request_keyframe()  # a thumb is due and no newer key frame is buffered
        return [], resync

    def pull(self, after_seq: int, epoch: Optional[int], max_bytes: int, wait_s: float,
             cancelled: threading.Event) -> Dict[str, Any]:
        started = time.monotonic()
        deadline = started + max(0.0, wait_s)
        with self.cond:
            self.pulling += 1
            self.last_pull = started
            try:
                frames: List[Packet] = []
                resync = False
                while self.state == "open":
                    if self.profile == "focus":
                        frames, resync = self._focus(after_seq, epoch, max_bytes)
                    else:
                        frames, resync = self._thumb(after_seq, epoch)
                    if frames:
                        break
                    now = time.monotonic()
                    if now >= deadline or cancelled.is_set():
                        break
                    self.cond.wait(min(POLL_S, deadline - now))
                if frames:
                    self.read_through = max(self.read_through, frames[-1].seq)
                dropped = self.unread_evicted
                self.unread_evicted = 0
                state, error, config = self.state, self.error, self.config
            finally:
                self.pulling -= 1
                self.last_pull = time.monotonic()
        return {"frames": frames, "resync": resync, "dropped": dropped, "state": state, "error": error,
                "config": config, "server_ms": int((time.monotonic() - started) * 1000)}


# ---------------------------------------------------------------- media state

class Media:
    """Streams, the Canvas link and the idle reaper of one server."""

    def __init__(self, services: Services) -> None:
        self.services = services
        self.link = CanvasLink(services)
        self._lock = threading.Lock()
        self.streams: Dict[str, Stream] = {}
        self._reserved = 0  # slots held by open_stream calls that have not added their stream yet
        self._reaper: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    def idle_s(self) -> float:
        return float(self.services.config.get("stream_idle_s") or 30)

    def reserve(self, limit: int) -> None:
        """Hold one of ``limit`` stream slots for an ``open_stream`` call, so calls running at
        the same time can never open more than ``max_streams`` streams together."""
        with self._lock:
            if self._stopping.is_set():
                raise _media_rejected("canvas_unavailable", "The server is shutting down")
            if len(self.streams) + self._reserved >= limit:
                raise _media_rejected("stream_limit", "This server already has %d open streams (max_streams)"
                                      % limit)
            self._reserved += 1

    def release(self) -> None:
        """Give back a slot from ``reserve`` that no stream used."""
        with self._lock:
            self._reserved = max(0, self._reserved - 1)

    def add(self, stream: Stream) -> bool:
        """Turn a reserved slot into the open ``stream``; False (stream not kept) once stopping."""
        with self._lock:
            self._reserved = max(0, self._reserved - 1)
            if self._stopping.is_set():
                return False
            self.streams[stream.stream_id] = stream
            if self._reaper is None:
                self._reaper = threading.Thread(target=self._reap, name="mcp-media-reaper", daemon=True)
                self._reaper.start()
        return True

    def get(self, stream_id: str) -> Stream:
        with self._lock:
            stream = self.streams.get(stream_id)
        if stream is None:
            raise _media_rejected("stream_not_found", "Stream %s is not open" % stream_id)
        return stream

    def remove(self, stream_id: str) -> Optional[Stream]:
        with self._lock:
            stream = self.streams.pop(stream_id, None)
        if stream is not None:
            stream.close()
        return stream

    def of_session(self, session_id: str) -> List[Stream]:
        with self._lock:
            return [stream for stream in self.streams.values() if stream.session_id == session_id]

    def _reap(self) -> None:
        while not self._stopping.wait(REAP_EVERY_S):
            now = time.monotonic()
            idle = self.idle_s()
            with self._lock:
                stale = [stream for stream in self.streams.values()
                         if not stream.pulling and now - stream.last_pull > idle]
            for stream in stale:
                self.services.log("media: stream %s closed after %.0f s without a pull" % (stream.stream_id, idle))
                self.remove(stream.stream_id)

    def close_all(self) -> List[str]:
        with self._lock:
            ids = list(self.streams)
        for stream_id in ids:
            self.remove(stream_id)
        return ids

    def session_stopping(self, session_id: str) -> str:
        """Session hook: close the session's streams, detach a device this server attached."""
        closed = [stream.stream_id for stream in self.of_session(session_id)]
        for stream_id in closed:
            self.remove(stream_id)
        detail = self.link.detach_session(session_id)
        return "closed %d stream(s)%s" % (len(closed), "; " + detail if detail else "")

    def shutdown_streams(self) -> None:
        with self._lock:
            self._stopping.set()  # under the lock: no add() can slip in after close_all()
        self.close_all()

    def shutdown_canvas(self) -> None:
        self.link.stop()


def media_of(services: Services) -> Media:
    media = services.extensions.get("mcp_media")
    if media is None:
        media = Media(services)
        services.extensions["mcp_media"] = media
    return media


# ---------------------------------------------------------------- answers

def _canvas_message(payload: Dict[str, Any], status: int) -> str:
    message = payload.get("error") or payload.get("message")
    return _short(message if isinstance(message, str) and message else "the Canvas answered HTTP %d" % status)


def _control_view(ctx: CallContext, media: Media, device_id: str) -> Optional[Dict[str, Any]]:
    """The lease view of the device from the Canvas status (after a lease refusal)."""
    try:
        status, payload = media.link.request("GET", CanvasLink.device_path(device_id, "/status"), origin="agent")
    except CanvasTransport:
        return None
    lease = payload.get("lease") if status == 200 else None
    return ctx.services.keeper.view(lease) if isinstance(lease, dict) else None


def _refusal(ctx: CallContext, media: Media, ref: Dict[str, Any], device_id: str, status: int,
             payload: Dict[str, Any], what: str) -> ToolError:
    """The ``rejected`` answer of a 4xx Canvas refusal (contract 5.4)."""
    message = _canvas_message(payload, status)
    code = payload.get("error_code") if isinstance(payload.get("error_code"), str) else None
    if status == 400:
        return rejected("invalid_arguments", "The Canvas refused the %s: %s" % (what, message),
                        HINTS["invalid_arguments"], field="event", problem=message[:200])
    if status == 409 and code:
        hint = HINTS["lease_refused"] if code in LEASE_SESSION_CODES else HINTS["canvas_refused"]
        return rejected(code[:80], "The Canvas refused the %s: %s" % (what, message), hint,
                        control=_control_view(ctx, media, device_id))
    if status in (401, 403, 404):
        if status == 404:
            media.link.forget(str(ref["session_id"]))
        return _media_rejected("canvas_unavailable", "The Canvas answered HTTP %d to the %s: %s"
                               % (status, what, message))
    return _media_rejected("canvas_refused", "The Canvas refused the %s: %s" % (what, message))


# ---------------------------------------------------------------- tools

def open_stream(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    ref = ctx.resolve(args["host_id"], args["session_id"])
    media = media_of(ctx.services)
    limit = int(ctx.services.config.get("max_streams") or 4)
    media.reserve(limit)
    try:
        stream = _connect_stream(ctx, media, ref, args["profile"])
    except BaseException:
        media.release()
        raise
    if not media.add(stream):
        stream.close()
        raise _media_rejected("canvas_unavailable", "The server is shutting down")
    deadline = time.monotonic() + OPEN_WAIT_S
    with stream.cond:
        while stream.state == "open" and (stream.width is None or stream.config is None):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            stream.cond.wait(min(POLL_S, remaining))
    view = stream.view()
    if view["state"] == "failed":
        media.remove(stream.stream_id)
        raise ToolError("media_unavailable", stream.error or "The stream failed", HINTS["media_unavailable"],
                        stream=view)
    return {"stream": view}


def _connect_stream(ctx: CallContext, media: Media, ref: Dict[str, Any], profile: str) -> Stream:
    """Attach the device and open the stream's video and control connections and readers."""
    device = media.link.attach(ref)
    doc = media.link.ensure()
    headers = {"Authorization": "Bearer " + doc["token"]} if doc.get("token") else {}
    stream = Stream(new_id("st_", 8), ref, profile, device["device_id"],
                    int(ctx.services.config.get("stream_buffer_bytes") or 16777216), ctx.services.log)
    base = CanvasLink.device_path(device["device_id"], "")
    origin = "human" if profile == "focus" else "agent"
    timeout = media.link.http_timeout()
    try:
        # No ?origin= on the video connection (an API client; the Canvas counts it as agent).
        stream.video = mcp_ws.WebSocketClient.connect("127.0.0.1", doc["port"], base + "/ws/video", headers,
                                                      timeout=timeout)
        stream.control = mcp_ws.WebSocketClient.connect("127.0.0.1", doc["port"],
                                                        base + "/ws/control?origin=" + origin, headers,
                                                        timeout=timeout)
    except mcp_ws.WebSocketError as exc:
        stream.close()
        if "HTTP 404" in str(exc):
            media.link.forget(stream.session_id)
        raise ToolError("media_unavailable", "The Canvas stream could not be opened: %s" % exc,
                        HINTS["media_unavailable"])
    for target, name in ((stream.video_loop, "video"), (stream.control_loop, "control")):
        threading.Thread(target=target, name="mcp-media-%s-%s" % (name, stream.stream_id), daemon=True).start()
    return stream


def get_frames(ctx: CallContext, args: Dict[str, Any]) -> Reply:
    media = media_of(ctx.services)
    stream = media.get(args["stream_id"])
    ctx.host_id = stream.session["host_id"]
    budget = int(ctx.services.config.get("frame_budget_bytes") or 524288)
    max_bytes = args.get("max_bytes", budget)
    if max_bytes > budget:
        raise bad_argument("max_bytes", "must be <= %d (frame_budget_bytes)" % budget)
    wait_ms = args.get("wait_ms", DEFAULT_WAIT_MS)
    answer = stream.pull(int(args["after_seq"]), args.get("epoch"), int(max_bytes), wait_ms / 1000.0,
                         ctx.cancelled)
    view = stream.view()
    if answer["state"] == "failed":
        raise ToolError("media_unavailable", answer["error"] or "The stream failed", HINTS["media_unavailable"],
                        stream=view)
    if answer["state"] == "closed":
        raise _media_rejected("stream_not_found", "Stream %s was closed" % stream.stream_id)
    frames: List[Packet] = answer["frames"]
    config = answer["config"]
    meta = {"config_b64": base64.b64encode(config).decode("ascii") if config else None,
            "frames": [{"seq": item.seq, "key": item.key, "pts": item.pts,
                        "data_b64": base64.b64encode(item.data).decode("ascii")} for item in frames]}
    fields = {
        "stream": view,
        "frames": len(frames),
        "bytes": sum(len(item.data) for item in frames),
        "first_seq": frames[0].seq if frames else None,
        "last_seq": frames[-1].seq if frames else None,
        "resync": bool(answer["resync"]),
        "dropped": int(answer["dropped"]),
        "server_ms": int(answer["server_ms"]),
    }
    return Reply(fields, meta={"autonom/frames": meta})


def close_stream(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    media = media_of(ctx.services)
    stream = media.get(args["stream_id"])
    ctx.host_id = stream.session["host_id"]
    media.remove(stream.stream_id)
    return {"closed": True}


def _point(value: float, size: int) -> int:
    return max(0, min(int(size), int(math.floor(value * size + 0.5))))


def _display(media: Media, ref: Dict[str, Any], device_id: str) -> Tuple[int, int]:
    """The device display size the Canvas status reports (nothing is sent to the device)."""
    try:
        status, payload = media.link.request("GET", CanvasLink.device_path(device_id, "/status"), origin="agent")
    except CanvasTransport as exc:
        raise _media_rejected("canvas_unavailable", "The Canvas status did not answer: %s" % exc)
    if status != 200:
        if status == 404:
            media.link.forget(str(ref["session_id"]))
        raise _media_rejected("canvas_unavailable", "The Canvas status answered HTTP %d" % status)
    display = payload.get("display")
    width = display.get("width") if isinstance(display, dict) else None
    height = display.get("height") if isinstance(display, dict) else None
    if not all(isinstance(item, int) and not isinstance(item, bool) and 0 < item <= MAX_DISPLAY_SIDE
               for item in (width, height)):
        raise _media_rejected("canvas_unavailable", "The Canvas does not know the display size yet")
    return int(width), int(height)


def check_event(event: Dict[str, Any]) -> None:
    """Per-op required and allowed fields (the schema checks types and ranges)."""
    op = event["op"]
    allowed = EVENT_FIELDS[op] + EVENT_OPTIONAL.get(op, ())
    for name in EVENT_FIELDS[op]:
        if name not in event:
            raise bad_argument("event.%s" % name, "required for op %s" % op)
    for name in event:
        if name != "op" and name not in allowed:
            raise bad_argument("event.%s" % name, "not used by op %s" % op)
    if op == "text" and any(ord(char) < 32 and char not in "\n\t" for char in event["text"]):
        raise bad_argument("event.text", "must not hold control characters other than newline and tab")


def input_request(media: Media, ref: Dict[str, Any], device_id: str,
                  event: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """``(route, body)`` of one checked event, exactly as the Canvas page sends it."""
    op = event["op"]
    if op == "tap":
        width, height = _display(media, ref, device_id)
        return "/tap", {"x": _point(event["x"], width), "y": _point(event["y"], height), "dw": width, "dh": height}
    if op == "swipe":
        width, height = _display(media, ref, device_id)
        return "/swipe", {"x1": _point(event["x1"], width), "y1": _point(event["y1"], height),
                          "x2": _point(event["x2"], width), "y2": _point(event["y2"], height),
                          "duration": int(event["duration_ms"]), "dw": width, "dh": height}
    if op == "key":
        key = event["key"]
        return "/key", {"key": key if key.startswith("KEYCODE_") else "KEYCODE_" + key}
    if op == "text":
        return "/text", {"text": event["text"], "sensitive": bool(event.get("sensitive", False))}
    return "/key", {"key": SYSTEM_KEYS[event["button"]]}


def send_input(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    ref = ctx.resolve(args["host_id"], args["session_id"])
    event = args["event"]
    check_event(event)
    media = media_of(ctx.services)
    device = media.link.attach(ref)
    route, body = input_request(media, ref, device["device_id"], event)
    what = "%s input" % event["op"]
    try:
        status, payload = media.link.request("POST", CanvasLink.device_path(device["device_id"], route), body,
                                             origin="human")
    except CanvasTransport as exc:
        if not exc.sent:
            raise _media_rejected("canvas_unavailable", "The Canvas refused the connection; nothing was sent")
        raise ToolError("input_failed", "The %s was sent but its answer was lost (%s)" % (what, exc),
                        HINTS["input_failed"])
    if status == 200:
        return {"input": {"op": event["op"], "operation_id": ctx.operation_id}}
    if status >= 500 or status < 400:
        raise ToolError("input_failed", "The Canvas answered HTTP %d to the %s: %s"
                        % (status, what, _canvas_message(payload, status)), HINTS["input_failed"])
    raise _refusal(ctx, media, ref, device["device_id"], status, payload, what)


def _control(ctx: CallContext, args: Dict[str, Any], mode: str) -> Dict[str, Any]:
    # Never the session guard (contract 2.8): a pause must work while a flow job holds it.
    ref = ctx.resolve(args["host_id"], args["session_id"])
    media = media_of(ctx.services)
    keeper = ctx.services.keeper
    device = media.link.attach(ref)
    path = CanvasLink.device_path(device["device_id"], "/control")
    what = "take over" if mode == "takeover" else "hand back"
    try:
        status, payload = media.link.request("POST", path, {"mode": mode}, origin="human")
    except CanvasTransport as exc:
        if not exc.sent:
            raise _media_rejected("canvas_unavailable", "The Canvas refused the connection; nothing was sent")
        keeper.drop(ref["platform"], ref["target_id"])
        raise ToolError("control_failed", "The %s was sent but its answer was lost (%s)" % (what, exc),
                        HINTS["control_failed"])
    # Whatever the answer, the agent's cached generation may be stale now: the first agent
    # mutation after a pause or resume acquires again (contract 3.6, 5.5).
    keeper.drop(ref["platform"], ref["target_id"])
    if status == 200:
        lease = payload.get("lease")
        return {"control": keeper.view(lease if isinstance(lease, dict) else None)}
    if status >= 500 or status < 400:
        raise ToolError("control_failed", "The Canvas answered HTTP %d to the %s: %s"
                        % (status, what, _canvas_message(payload, status)), HINTS["control_failed"])
    raise _refusal(ctx, media, ref, device["device_id"], status, payload, what)


def pause_session(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return _control(ctx, args, "takeover")


def resume_session(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return _control(ctx, args, "release")


# ---------------------------------------------------------------- catalog entries

APP_ONLY = {"ui": {"visibility": ["app"]}, "openai/visibility": "private", "openai/widgetAccessible": True}
READ = {"readOnlyHint": True, "openWorldHint": False}
STREAM_STATE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
MUTATION = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
REF_INPUT = {"host_id": HOST_ID, "session_id": SESSION_ID}
STREAM_ID = string(STREAM_ID_RE, description="Stream id from open_stream.")
PROFILE = string(enum=("focus", "thumb"))
NORMALIZED = {"type": "number", "minimum": 0, "maximum": 1}

STREAM_VIEW = obj({
    "stream_id": string(STREAM_ID_RE),
    "session": SESSION_REF,
    "profile": PROFILE,
    "state": string(enum=("open", "failed", "closed")),
    "width": nullable(integer(minimum=0)),
    "height": nullable(integer(minimum=0)),
    "codec": nullable(string(max_length=40)),
    "epoch": integer(minimum=0),
    "buffer_bytes": integer(minimum=0),
}, ("stream_id", "session", "profile", "state", "width", "height", "codec", "epoch", "buffer_bytes"))

EVENT = obj({
    "op": string(enum=INPUT_OPS),
    "x": NORMALIZED, "y": NORMALIZED,
    "x1": NORMALIZED, "y1": NORMALIZED, "x2": NORMALIZED, "y2": NORMALIZED,
    "duration_ms": integer(1, 5000),
    "key": string(KEY_RE),
    "text": string(min_length=1, max_length=500),
    "sensitive": {"type": "boolean"},
    "button": string(enum=tuple(SYSTEM_KEYS)),
}, ("op",), description=("One input event: {op: tap, x, y} (normalized 0..1), {op: swipe, x1, y1, x2, y2, "
                         "duration_ms}, {op: key, key}, {op: text, text, sensitive?} or {op: system, button}."))

TOOLS = (
    Tool(
        name="open_stream",
        title="Open a device stream",
        description=("Open a pull-based H.264 stream of the session's device through the workspace Canvas. "
                     "profile focus delivers every packet and keeps the person's presence for the Canvas "
                     "lease; thumb delivers the newest key frame at most once per second."),
        input_schema=obj(dict(REF_INPUT, profile=PROFILE), ("host_id", "session_id", "profile")),
        output_fields={"stream": STREAM_VIEW},
        annotations=dict(STREAM_STATE),
        meta=dict(APP_ONLY),
        mutating=False,
        handler=open_stream,
    ),
    Tool(
        name="get_frames",
        title="Get stream frames",
        description=("Pull the H.264 packets after after_seq (within max_bytes, waiting up to wait_ms) in "
                     "_meta[\"autonom/frames\"]; resync true restarts at the newest key frame with the "
                     "stream's width, height and config."),
        input_schema=obj({
            "stream_id": STREAM_ID,
            "after_seq": integer(minimum=0),
            "epoch": integer(minimum=0),
            "max_bytes": integer(1, 1048576),
            "wait_ms": integer(0, 1000, default=DEFAULT_WAIT_MS),
        }, ("stream_id", "after_seq")),
        output_fields={
            "stream": STREAM_VIEW,
            "frames": integer(minimum=0),
            "bytes": integer(minimum=0),
            "first_seq": nullable(integer(minimum=1)),
            "last_seq": nullable(integer(minimum=1)),
            "resync": {"type": "boolean"},
            "dropped": integer(minimum=0),
            "server_ms": integer(minimum=0),
        },
        annotations=dict(READ),
        meta=dict(APP_ONLY),
        mutating=False,
        handler=get_frames,
    ),
    Tool(
        name="close_stream",
        title="Close a device stream",
        description="Close a stream opened with open_stream, with its presence connection.",
        input_schema=obj({"stream_id": STREAM_ID}, ("stream_id",)),
        output_fields={"closed": {"type": "boolean"}},
        annotations=dict(STREAM_STATE, idempotentHint=True),
        meta=dict(APP_ONLY),
        mutating=False,
        handler=close_stream,
    ),
    Tool(
        name="send_input",
        title="Send one input event",
        description=("Send one discrete input event to the session's device as the person, through the "
                     "workspace Canvas: tap, swipe, key, text or a system button. Never retried; after "
                     "outcome_unknown check the screen first."),
        input_schema=obj(dict(REF_INPUT, event=EVENT), ("host_id", "session_id", "event")),
        output_fields={"input": obj({"op": string(enum=INPUT_OPS), "operation_id": string(OPERATION_ID_RE)},
                                    ("op", "operation_id"))},
        annotations=dict(MUTATION),
        meta=dict(APP_ONLY),
        mutating=True,
        handler=send_input,
    ),
    Tool(
        name="pause_session",
        title="Pause the agent",
        description=("Take over the session's device as the person (the Canvas Take over): agent "
                     "mutations and flows stop before their next step until resume_session or the "
                     "control idle time."),
        input_schema=obj(dict(REF_INPUT), ("host_id", "session_id")),
        output_fields={"control": nullable(CONTROL_VIEW)},
        annotations=dict(MUTATION),
        meta=dict(APP_ONLY),
        mutating=True,
        handler=pause_session,
    ),
    Tool(
        name="resume_session",
        title="Resume the agent",
        description="Hand the session's device back (the Canvas Hand back), so the agent may act again.",
        input_schema=obj(dict(REF_INPUT), ("host_id", "session_id")),
        output_fields={"control": nullable(CONTROL_VIEW)},
        annotations=dict(MUTATION),
        meta=dict(APP_ONLY),
        mutating=True,
        handler=resume_session,
    ),
)


def setup(services: Services) -> None:
    media = media_of(services)
    services.hooks.register("media", media.session_stopping)
    services.views.register("streams", lambda session_id: [
        {"stream_id": stream.stream_id, "profile": stream.profile} for stream in media.of_session(session_id)])
    services.on_shutdown("streams", media.shutdown_streams, 20)
    services.on_shutdown("canvas", media.shutdown_canvas, 40)
