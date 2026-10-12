"""Supervision of the probe media endpoint child (contract 2.6).

The child is ``node <P>/media/probe-media.mjs`` (or the test fake). It gets
a fresh token in its environment only, prints one ``ready`` line, and is
shared between server processes through ``<state>/chatgpt-probe/media.json``
(0600): the only place the token is stored on disk. Nothing the child
prints ever reaches the server's stdout.
"""
from __future__ import annotations

import collections
import contextlib
import http.client
import json
import os
import queue
import secrets
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from probe_env import DEFAULT_MEDIA_READY_TIMEOUT, ProbeError, Resolver, child_env, is_int
from probe_state import MEDIA, StateStore

STOP_GRACE_S = 2.0
TAIL_LINES = 50
REASON_CHARS = 400
TAIL_LINE_CHARS = 2000
MAX_BODY = 8 * 1024 * 1024


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def redact(text: str, secret: Optional[str]) -> str:
    if secret:
        text = text.replace(secret, "[redacted]")
    return text


def http_get(port: int, path: str, token: str, timeout: float) -> Tuple[int, Any]:
    """GET ``path`` from the child on 127.0.0.1 with the bearer token, no Origin."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Authorization": "Bearer " + token,
                                           "Accept": "application/json"})
        response = conn.getresponse()
        body = response.read(MAX_BODY + 1)
    finally:
        conn.close()
    if len(body) > MAX_BODY:
        raise ValueError("response too large")
    return response.status, json.loads(body.decode("utf-8"))


class MediaChild:
    def __init__(self, resolver: Resolver, config: Dict[str, Any], store: StateStore,
                 log: Callable[[str], None], owner_pid: Optional[int] = None) -> None:
        self.resolver = resolver
        self.config = config
        self.store = store
        self.log = log
        self.owner_pid = owner_pid if owner_pid is not None else os.getpid()
        self.proc: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None
        self.port: Optional[int] = None
        self.token: Optional[str] = None
        self.owned = False
        self.shared = False
        self.failure: Optional[ProbeError] = None
        self._tail_lock = threading.Lock()
        self._stdout_tail: Deque[str] = collections.deque(maxlen=TAIL_LINES)
        self._stderr_tail: Deque[str] = collections.deque(maxlen=TAIL_LINES)
        self._threads: List[threading.Thread] = []

    # -- state

    def status(self) -> str:
        if self.proc is not None:
            return "running" if self.proc.poll() is None else "stopped"
        if self.shared and self.pid:
            return "running" if pid_alive(self.pid) else "stopped"
        return "failed" if self.failure is not None else "stopped"

    def effective_port(self) -> int:
        if self.port and self.status() == "running":
            return self.port
        return int(self.config["media_port"])

    def current_token(self) -> Optional[str]:
        return self.token if self.status() == "running" else None

    # -- start / reuse

    def ensure(self) -> None:
        """Make sure a child runs: reuse a live one (own or shared) or start one."""
        if self.status() == "running":
            return
        self._forget()
        self.failure = None
        try:
            with self.store.lock():
                record = self.store.read_media()
                if record is not None and self._adopt(record):
                    return
                if self.store.media_file_exists():
                    self.store.remove(MEDIA)
                self._start()
                try:
                    self.store.write_media(self.pid or 0, self.port or 0, self.token or "",
                                           self.owner_pid)
                except ProbeError:
                    self._terminate()
                    self._clear()
                    raise
        except ProbeError as exc:
            self.failure = exc
            self.log("media child unavailable: %s" % exc.error_code)

    def _adopt(self, record: Dict[str, Any]) -> bool:
        pid, port, token = record["pid"], record["port"], record["token"]
        if not pid_alive(pid):
            return False
        try:
            status, body = http_get(port, "/probe/status", token, 1.0)
        except (OSError, ValueError, http.client.HTTPException):
            return False
        if status != 200 or not isinstance(body, dict) or body.get("ok") is not True:
            return False
        self.pid, self.port, self.token = pid, port, token
        self.shared, self.owned = True, False
        self.log("reusing the media child of another probe server on port %d" % port)
        return True

    def _media_argv(self) -> List[str]:
        node = self.resolver.resolve_node()
        root = self.resolver.resolve_autonom_root()
        argv = [node, self.resolver.media_script(), "--port", str(int(self.config["media_port"])),
                "--autonom-root", root]
        for origin in self.config.get("allow_origins") or []:
            argv += ["--allow-origin", origin]
        for route in self.config.get("remote_routes") or []:
            origin = route["origin"]
            if origin.startswith("http://127.0.0.1:"):
                argv += ["--allow-host", origin[len("http://"):]]
        for spec in self.config.get("allowed_devices") or []:
            argv += ["--allow-device", spec]
        adb = self.resolver.resolve_adb()
        if adb:
            argv += ["--adb", adb]
        return argv

    def _start(self) -> None:
        argv = self._media_argv()
        token = secrets.token_urlsafe(32)
        env = child_env(self.resolver.env, self.resolver.home)
        env["AUTONOM_PROBE_TOKEN"] = token
        with self._tail_lock:
            self._stdout_tail.clear()
            self._stderr_tail.clear()
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, env=env, close_fds=True)
        except OSError as exc:
            raise ProbeError("media_start_failed", "The probe media endpoint could not be started",
                             reason=redact("%s: %s" % (type(exc).__name__, exc), token)[-REASON_CHARS:])
        self.proc, self.pid, self.token = proc, proc.pid, token
        first: "queue.Queue[Optional[str]]" = queue.Queue()
        self._threads = [
            threading.Thread(target=self._drain_stdout, args=(proc, first, token), daemon=True),
            threading.Thread(target=self._drain_stderr, args=(proc, token), daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        ready_timeout = float(self.config.get("media_ready_timeout_s") or DEFAULT_MEDIA_READY_TIMEOUT)
        started = time.monotonic()
        try:
            line = first.get(timeout=ready_timeout)
            timed_out = False
        except queue.Empty:
            line, timed_out = None, True
        waited = time.monotonic() - started
        message: Any = None
        if line is not None:
            try:
                message = json.loads(line)
            except (ValueError, RecursionError):
                message = None
        if (isinstance(message, dict) and message.get("event") == "ready"
                and is_int(message.get("port")) and 0 < message["port"] <= 65535
                and proc.poll() is None):
            self.port = message["port"]
            self.owned, self.shared = True, False
            self.log("media child started on 127.0.0.1:%d (pid %d)" % (self.port, proc.pid))
            return
        # Every failure path kills the child before reporting.
        self._terminate()
        code = proc.returncode
        port_in_use = (isinstance(message, dict) and message.get("event") == "error"
                       and message.get("error_code") == "media_port_in_use") or code == 3
        reason = self._reason(token, timed_out, line, code, ready_timeout, waited)
        self._clear()
        if port_in_use:
            raise ProbeError("media_port_in_use",
                             "The probe media port %d is in use" % int(self.config["media_port"]),
                             port=int(self.config["media_port"]))
        raise ProbeError("media_start_failed", "The probe media endpoint did not become ready",
                         reason=reason)

    def _reason(self, token: str, timed_out: bool, line: Optional[str], code: Optional[int],
                limit: float, waited: float) -> str:
        with self._tail_lock:
            tail = "\n".join(self._stderr_tail)
        if timed_out:
            head = ("no ready line within %g s (waited %.1f s; AUTONOM_PROBE_MEDIA_READY_TIMEOUT "
                    "raises the limit)" % (limit, waited))
        elif line is None:
            head = "exited with code %s before ready" % code
        else:
            # Redact before cutting: a cut through the token would hide it from redact().
            head = "unexpected first line: %s" % redact(line, token)[:120]
        text = head + (": " + tail if tail else "")
        return redact(text, token)[-REASON_CHARS:]

    def _drain_stdout(self, proc: subprocess.Popen, first: "queue.Queue[Optional[str]]",
                      token: str) -> None:
        sent = False
        stream = proc.stdout
        if stream is None:
            first.put(None)
            return
        try:
            for raw in iter(stream.readline, b""):
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not sent:
                    first.put(text)
                    sent = True
                    continue
                with self._tail_lock:
                    self._stdout_tail.append(redact(text, token)[:TAIL_LINE_CHARS])
        except (OSError, ValueError):
            pass
        if not sent:
            first.put(None)

    def _drain_stderr(self, proc: subprocess.Popen, token: str) -> None:
        stream = proc.stderr
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                # Redact the whole line first, then cut it, so no token prefix survives a cut.
                text = redact(raw.decode("utf-8", "replace").rstrip("\r\n"), token)
                with self._tail_lock:
                    self._stderr_tail.append(text[:TAIL_LINE_CHARS])
        except (OSError, ValueError):
            pass

    # -- stop

    def _terminate(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.stdin is not None:
            with contextlib.suppress(OSError, ValueError):
                proc.stdin.close()
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=STOP_GRACE_S)
        for thread in self._threads:
            thread.join(timeout=0.5)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()

    def _clear(self) -> None:
        self.proc = None
        self.pid = None
        self.port = None
        self.token = None
        self.owned = False
        self.shared = False
        self._threads = []

    def _forget_record(self, pid: Optional[int]) -> None:
        if pid is None:
            return
        try:
            with self.store.lock(timeout=0.5):
                self.store.remove_media_if(pid)
        except (TimeoutError, ProbeError):
            with contextlib.suppress(OSError):
                self.store.remove_media_if(pid)

    def _forget(self) -> None:
        """Drop a dead child (own or shared) before starting again."""
        if self.proc is not None:
            pid = self.pid
            self._terminate()
            self._forget_record(pid)
        self._clear()

    def stop(self) -> None:
        """Stop a child this process started and remove its record; a shared one is left alone."""
        if self.proc is not None:
            pid = self.pid
            self._terminate()
            self._forget_record(pid)
            self.log("media child stopped")
        self._clear()

    # -- requests to the child

    def get(self, path: str, timeout: float) -> Tuple[int, Any]:
        if self.status() != "running" or not self.port or not self.token:
            raise ProbeError("media_not_running", "The probe media endpoint is not running",
                             media_state=self.status())
        return http_get(self.port, path, self.token, timeout)
