"""Host identity, session references, the session guard and shared hooks (contract 2.8).

- ``Refs.local_host()`` reads ``autonom host info`` once and caches the first
  success for the server's life; ``Refs.resolve(host_id, session_id)`` turns an
  explicit reference into the session entry or a ``rejected`` refusal.
- ``SessionGuard`` serialises the agent's own mutations per session.
- ``SessionHooks`` runs the ``stopping`` hooks other modules register.
- ``SessionViews`` lets one tool module read another's per-session lists.
- ``artifact_key`` / ``artifact_id`` / ``Refs.session_artifacts_dir`` give
  ``mcp_sessions`` and ``mcp_runs`` the same artifact identity.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple, Union

from mcp_common import (HOST_ID_RE, SESSION_ID_RE, TARGET_ID_RE, ToolError, rejected)
from mcp_env import Config, ensure_private_dir, sessions_home

SESSION_CACHE_S = 1.0
ARTIFACT_KEY_NAME = "artifact-key"
ARTIFACT_KEY_BYTES = 32
_HOST = re.compile(HOST_ID_RE)
_SESSION = re.compile(SESSION_ID_RE)
_TARGET = re.compile(TARGET_ID_RE)


def _short(value: Any, limit: int = 64) -> Optional[str]:
    return value[:limit] if isinstance(value, str) else None


def started_by_view(value: Any) -> Optional[Dict[str, Any]]:
    """The CLI's ``started_by`` (``{kind, port, pid}`` or null) in the adapter shape."""
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        return None

    def number(item: Any) -> Optional[int]:
        return item if isinstance(item, int) and not isinstance(item, bool) and item >= 0 else None

    return {"kind": value["kind"][:40], "port": number(value.get("port")), "pid": number(value.get("pid"))}


def session_entry(host_id: str, raw: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """One ``session list`` entry as the adapter returns it, or None when unusable."""
    session_id = raw.get("session_id")
    platform = raw.get("platform") or "android"
    target_id = raw.get("target_id") or raw.get("serial")
    if not (isinstance(session_id, str) and _SESSION.fullmatch(session_id)):
        return None
    if platform not in ("android", "ios") or not (isinstance(target_id, str) and _TARGET.fullmatch(target_id)):
        return None
    label = raw.get("label")
    return {
        "host_id": host_id,
        "session_id": session_id,
        "platform": platform,
        "target_id": target_id,
        "label": label if isinstance(label, str) and len(label) <= 64 else None,
        "live": raw.get("live") is True,
        "started_at": _short(raw.get("started_at")),
        "stopped_at": _short(raw.get("stopped_at")),
        "started_by": started_by_view(raw.get("started_by")),
    }


# ---------------------------------------------------------------- artifacts

def artifact_key(state_dir: Path) -> bytes:
    """32 random bytes in ``<MCP state dir>/artifact-key`` (0600).

    The key is written and synced in a private temporary file first and then
    published with ``os.link``, which fails when the name exists. So the key
    file never exists half-written, even when the server is killed mid-way, and
    a creator that loses the race reads the winner's complete key.
    """
    ensure_private_dir(Path(state_dir))
    path = Path(state_dir) / ARTIFACT_KEY_NAME
    if not path.exists():
        key = os.urandom(ARTIFACT_KEY_BYTES)
        fd, temporary = tempfile.mkstemp(prefix=".%s." % ARTIFACT_KEY_NAME, suffix=".tmp", dir=str(state_dir))
        try:
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, key)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.link(temporary, str(path))
            return key
        except FileExistsError:
            pass  # another creator published first; read its key below
        except OSError as exc:
            raise ToolError("artifact_key_invalid", "The artifact key could not be created: %s"
                            % type(exc).__name__)
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    for _attempt in range(20):
        try:
            data = path.read_bytes()
        except OSError:
            data = b""
        if len(data) >= ARTIFACT_KEY_BYTES:
            return data[:ARTIFACT_KEY_BYTES]
        time.sleep(0.05)
    raise ToolError("artifact_key_invalid", "The artifact key file is empty or unreadable",
                    "Move <state>/chatgpt-mcp/artifact-key aside; artifact ids then change.")


def artifact_id(key: bytes, host_id: str, session_id: str, relpath: str) -> str:
    """``art_`` + the first 24 hex of HMAC-SHA256(key, host \\0 session \\0 relpath)."""
    rel = str(relpath).replace(os.sep, "/").lstrip("/")
    message = ("%s\0%s\0%s" % (host_id, session_id, rel)).encode("utf-8", "surrogateescape")
    return "art_" + hmac.new(key, message, hashlib.sha256).hexdigest()[:24]


# ---------------------------------------------------------------- refs

class Refs:
    """Local host identity and ``(host_id, session_id)`` resolution."""

    def __init__(self, runner: Any, config: Config, env: Mapping[str, str], home: Path,
                 state_dir: Path, log: Callable[[str], None]) -> None:
        self.runner = runner
        self.config = config
        self.env = dict(env)
        self.home = Path(home)
        self.state_dir = Path(state_dir)
        self.log = log
        self._lock = threading.Lock()
        self._host: Optional[Dict[str, Any]] = None
        self._sessions: Optional[Tuple[float, List[Dict[str, Any]]]] = None
        self._key: Optional[bytes] = None

    # -- host

    def local_host(self) -> Dict[str, Any]:
        """``host info`` of this machine; ``host_unavailable`` (rejected) when unreadable."""
        with self._lock:
            if self._host is not None:
                return dict(self._host)
        result = self.runner.run("host_info")
        data = result.data if result.ok else None
        host_id = data.get("host_id") if isinstance(data, dict) else None
        if not (isinstance(host_id, str) and _HOST.fullmatch(host_id)):
            reason = ("the CLI answered %s" % result.error_code) if not result.ok else \
                "the CLI answer has no valid host_id"
            error = result.to_error() if not result.ok else None
            extras: Dict[str, Any] = {}
            if error is not None:
                extras = {key: value for key, value in error.extras.items()
                          if key in ("cli_error_code", "cli_error", "cli_hint")}
            raise rejected("host_unavailable", "The local host identity is unavailable (%s)" % reason,
                           **extras)
        capabilities = data.get("capabilities")
        info = {
            "host_id": host_id,
            "name": (_short(data.get("name"), 80) or "localhost"),
            "autonom_version": _short(data.get("autonom_version"), 40),
            "platform": _short(data.get("platform"), 40),
            "capabilities": [item[:40] for item in capabilities if isinstance(item, str)][:20]
            if isinstance(capabilities, list) else [],
        }
        with self._lock:
            if self._host is None:
                self._host = info
            return dict(self._host)

    def cached_host_id(self) -> Optional[str]:
        with self._lock:
            return self._host["host_id"] if self._host else None

    def check_host(self, host_id: Optional[str]) -> Dict[str, Any]:
        """The local host; ``host_not_found`` (rejected) for any other id."""
        local = self.local_host()
        if host_id is not None and host_id != local["host_id"]:
            raise rejected("host_not_found", "Host %s is not this host" % host_id[:40],
                           "Remote hosts are not supported yet; call list_hosts for the local host_id.")
        return local

    # -- sessions

    def invalidate(self) -> None:
        with self._lock:
            self._sessions = None

    def sessions(self, *, max_age: float = SESSION_CACHE_S) -> List[Dict[str, Any]]:
        """Every session of the store (``session list --all``) as adapter entries.

        A CLI failure raises its ``ToolError`` (``rejected`` when the code says
        nothing ran). Results are cached for ``max_age`` seconds.
        """
        now = time.monotonic()
        with self._lock:
            if self._sessions is not None and now - self._sessions[0] <= max_age:
                return [dict(item) for item in self._sessions[1]]
        host_id = self.local_host()["host_id"]
        result = self.runner.run("sessions", {"all": True})
        if not result.ok:
            raise result.to_error()
        raw = result.data.get("sessions") if isinstance(result.data, dict) else None
        if not isinstance(raw, list):
            raise ToolError("cli_output_invalid", "The session list has no sessions array")
        entries = [entry for entry in (session_entry(host_id, item) for item in raw
                                       if isinstance(item, dict)) if entry is not None]
        with self._lock:
            self._sessions = (time.monotonic(), entries)
        return [dict(item) for item in entries]

    def target_allowed(self, platform: str, target_id: str) -> bool:
        allowed = self.config.get("allowed_targets") or []
        return not allowed or ("%s:%s" % (platform, target_id)) in allowed

    def resolve(self, host_id: str, session_id: str, *, live: bool = True) -> Dict[str, Any]:
        """The session entry for an explicit reference, else a ``rejected`` refusal."""
        self.check_host(host_id)
        try:
            entries = self.sessions()
        except ToolError as exc:
            # Resolution happens before any effect, so this refusal is safe to retry.
            exc.rejected = True
            raise
        entry = next((item for item in entries if item["session_id"] == session_id), None)
        if entry is None:
            raise rejected("session_not_found", "Session %s was not found on this host" % session_id)
        if live and not entry["live"]:
            raise rejected("session_stopped", "Session %s is stopped" % session_id)
        if not self.target_allowed(entry["platform"], entry["target_id"]):
            raise rejected("target_not_allowed", "Target %s:%s is not in allowed_targets"
                           % (entry["platform"], entry["target_id"]))
        return entry

    # -- artifacts

    def artifact_key(self) -> bytes:
        with self._lock:
            if self._key is not None:
                return self._key
        key = artifact_key(self.state_dir)
        with self._lock:
            self._key = key
            return key

    def artifact_id(self, host_id: str, session_id: str, relpath: str) -> str:
        return artifact_id(self.artifact_key(), host_id, session_id, relpath)

    def session_artifacts_dir(self, ref: Mapping[str, Any]) -> Path:
        """The session's artifact directory: ``artifacts_dir`` of ``session show``,
        else ``<sessions home>/<session_id>/`` when its ``session.json`` names the
        same session. ``artifacts_unavailable`` (failed) otherwise."""
        session_id = str(ref["session_id"])
        result = self.runner.run("session_show", {"session_id": session_id})
        if result.ok and isinstance(result.data, dict):
            record = result.data.get("session")
            directory = record.get("artifacts_dir") if isinstance(record, dict) else None
            if isinstance(record, dict) and record.get("session_id") == session_id and \
                    isinstance(directory, str) and os.path.isabs(directory) and os.path.isdir(directory):
                return Path(directory)
        candidate = sessions_home(self.env, self.home) / session_id
        try:
            stored = json.loads((candidate / "session.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            stored = None
        if isinstance(stored, dict) and stored.get("session_id") == session_id:
            return candidate
        raise ToolError("artifacts_unavailable", "The artifact directory of session %s is unavailable"
                        % session_id, "Check the session with get_session.")


# ---------------------------------------------------------------- guard

What = Union[str, Mapping[str, Any]]


def _busy_with(what: What) -> Dict[str, str]:
    if isinstance(what, str):
        return {"tool": what[:80]}
    if "run_id" in what:
        return {"run_id": str(what["run_id"])}
    return {"tool": str(what.get("tool", "unknown"))[:80]}


class SessionGuard:
    """One agent mutation (or flow job) per session at a time; never waits by default."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._busy: Dict[str, Dict[str, str]] = {}

    def try_hold(self, session_id: str, what: What, wait_s: float = 0.0) -> bool:
        deadline = time.monotonic() + max(0.0, wait_s)
        with self._cond:
            while session_id in self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            self._busy[session_id] = _busy_with(what)
            return True

    def hold(self, session_id: str, what: What, wait_s: float = 0.0) -> None:
        """Take the session or raise ``session_busy`` (rejected) with ``busy_with``."""
        if not self.try_hold(session_id, what, wait_s):
            busy = self.busy(session_id) or {"tool": "unknown"}
            raise rejected("session_busy", "Session %s is busy" % session_id, busy_with=busy)

    def release(self, session_id: str) -> None:
        with self._cond:
            self._busy.pop(session_id, None)
            self._cond.notify_all()

    @contextlib.contextmanager
    def mutation(self, session_id: str, what: What, wait_s: float = 0.0) -> Iterator[None]:
        self.hold(session_id, what, wait_s)
        try:
            yield
        finally:
            self.release(session_id)

    def busy(self, session_id: str) -> Optional[Dict[str, str]]:
        with self._cond:
            value = self._busy.get(session_id)
            return dict(value) if value else None


# ---------------------------------------------------------------- hooks and views

class SessionHooks:
    """Named ``stopping(session_id)`` hooks, run in registration order."""

    def __init__(self, log: Optional[Callable[[str], None]] = None) -> None:
        self._lock = threading.Lock()
        self._hooks: List[Tuple[str, Callable[[str], Any]]] = []
        self.log = log

    def register(self, name: str, fn: Callable[[str], Any]) -> None:
        with self._lock:
            self._hooks = [item for item in self._hooks if item[0] != name] + [(name, fn)]

    def names(self) -> List[str]:
        with self._lock:
            return [name for name, _fn in self._hooks]

    def stopping(self, session_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            hooks = list(self._hooks)
        results: List[Dict[str, Any]] = []
        for name, fn in hooks:
            try:
                value = fn(session_id)
                ok = True
            except Exception as exc:  # noqa: BLE001 - one hook never hides the others
                value = getattr(exc, "error_code", None) or type(exc).__name__
                ok = False
                if self.log:
                    self.log("session hook %s failed: %s" % (name, type(exc).__name__))
            if value is None or isinstance(value, str):
                detail = value[:500] if isinstance(value, str) else None
            else:
                try:
                    detail = json.dumps(value, sort_keys=True)[:500]
                except (TypeError, ValueError):
                    detail = type(value).__name__
            results.append({"name": name[:80], "ok": ok, "detail": detail})
        return results


class SessionViews:
    """Per-session list providers (``streams`` from ``mcp_media``)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._providers: Dict[str, Callable[[str], List[Any]]] = {}

    def register(self, name: str, fn: Callable[[str], List[Any]]) -> None:
        with self._lock:
            self._providers[name] = fn

    def get(self, name: str, session_id: str) -> List[Any]:
        with self._lock:
            fn = self._providers.get(name)
        if fn is None:
            return []
        try:
            value = fn(session_id)
        except Exception:  # noqa: BLE001 - a view never breaks the reader
            return []
        return list(value) if isinstance(value, (list, tuple)) else []
