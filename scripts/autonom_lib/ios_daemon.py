"""Client of the iOS device daemon (speed plan 2b, ID-06..ID-08).

One Node daemon per simulator (`ios-device-daemon.mjs`, next to the Canvas
scripts) keeps the simulator's one `idb_companion` connection open and
serves tree reads, quiet waits and inputs over a private Unix socket. This
module is the CLI's side of it, and the only Python code that knows the
daemon's files and protocol:

- find a live daemon from its registry `<state base>/iosd/<UDID>.json`
  (schema `autonom-iosd/v1`; live means its pid exists and its socket
  answers). The state base is `$AUTONOM_HOME`, else `~/.autonom`, the one
  the daemon itself uses;
- otherwise start one, under the clients' start lock `<UDID>.lock` (an
  O_EXCL file holding our pid; a lock whose pid is gone is stale), through
  `processes.run_supervised(kind="iosd")` in a detached supervisor, so the
  pair is registered like every other long-lived child. The daemon prints
  one status line when it serves, refuses or fails; that line decides;
- send one request line and read one reply line within a deadline;
- say why it was not used: `daemon_disabled` (AUTONOM_IOS_DAEMON=off),
  `node_missing`, `daemon_start_failed`, `companion_too_old` or
  `daemon_unreachable`. A reason is remembered for the rest of this
  process, so a daemon that cannot start is not started again per call.

Failures carry a stage. `before_send`: the request line was never written
(nobody answered at the socket, or the daemon refused it before acting),
so the caller may serve the call another way. `after_send`: the line was
written and no usable reply came back (the connection closed, the deadline
passed, or the daemon said the device may have seen it); an input is then
never sent again (INV-1).

The daemon never boots, reboots, erases or shuts down a simulator, and
nothing here does either. Standard library only.
"""
from __future__ import annotations

import itertools
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .paths import ROOT

ENV = "AUTONOM_IOS_DAEMON"
NODE_ENV = "AUTONOM_NODE"
OFF_VALUES = frozenset({"off", "0", "false", "no"})
SCHEMA = "autonom-iosd/v1"
DAEMON_SCRIPT = ROOT / "plugins/autonom/skills/android-emulator-browser/scripts/ios-device-daemon.mjs"

# Why a call was not served by the daemon (`transport_reason`).
DAEMON_DISABLED = "daemon_disabled"
NODE_MISSING = "node_missing"
DAEMON_START_FAILED = "daemon_start_failed"
COMPANION_TOO_OLD = "companion_too_old"
DAEMON_UNREACHABLE = "daemon_unreachable"
REASONS = (DAEMON_DISABLED, NODE_MISSING, DAEMON_START_FAILED, COMPANION_TOO_OLD,
           DAEMON_UNREACHABLE)

BEFORE_SEND = "before_send"
AFTER_SEND = "after_send"

# The daemon's own start waits up to 40 s (idb connect is bounded by 30 s).
START_TIMEOUT_S = 40.0
# How long a client waits for another client's start lock.
LOCK_TIMEOUT_S = 20.0
CONNECT_TIMEOUT_S = 2.0
# One reply line; a 3000-node tree is about 1.5 MB.
MAX_LINE_BYTES = 4 * 1024 * 1024
_LOCK_GRACE_S = 2.0

# Supervisor: run_supervised in a detached interpreter. The daemon (its child)
# inherits stdout, so its one status line reaches the client that started it.
_SUPERVISOR = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from autonom_lib import ios_daemon\n"
    "sys.exit(ios_daemon.supervise(sys.argv[2:]))\n"
)

_REQUEST_IDS = itertools.count(1)
# (state base, udid) -> reason the daemon is not used in this process.
_UNAVAILABLE: dict[tuple[str, str], str] = {}


class Unavailable(Exception):
    """The daemon did not serve a request: ``reason`` (a REASONS entry),
    ``stage`` (BEFORE_SEND or AFTER_SEND) and, when the daemon answered,
    its ``code`` and ``hint``."""

    def __init__(self, reason: str, message: str, *, stage: str = BEFORE_SEND,
                 code: str | None = None, hint: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.stage = stage
        self.code = code
        self.hint = hint


def reset() -> None:
    """Forget the reasons remembered in this process (tests)."""
    _UNAVAILABLE.clear()


def disabled() -> bool:
    return (os.environ.get(ENV) or "").strip().lower() in OFF_VALUES


def state_base() -> Path:
    """`$AUTONOM_HOME`, else `~/.autonom`: the daemon's `defaultStateBase`."""
    explicit = os.environ.get("AUTONOM_HOME")
    # absolute, symlinks kept: node's path.resolve() spells it the same way
    return Path(os.path.abspath(explicit)) if explicit else Path.home() / ".autonom"


def daemon_paths(base: Path, udid: str) -> dict[str, Path]:
    directory = base / "iosd"
    return {"dir": directory, "registry": directory / f"{udid}.json",
            "start_lock": directory / f"{udid}.lock", "log": directory / f"{udid}.log"}


def _pid_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def socket_connectable(path: Any, timeout: float = 0.5) -> bool:
    if not isinstance(path, str) or not path:
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect(path)
    except OSError:
        return False
    finally:
        probe.close()
    return True


def read_registry(base: Path, udid: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(daemon_paths(base, udid)["registry"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def live_registry(base: Path, udid: str) -> dict[str, Any] | None:
    """The registry of a daemon of ``udid`` that runs and answers, or None."""
    registry = read_registry(base, udid)
    if (not registry or registry.get("schema") != SCHEMA or registry.get("udid") != udid
            or not _pid_alive(registry.get("pid"))):
        return None
    return registry if socket_connectable(registry.get("socket")) else None


def live_companion_pids(udid: str | None) -> set[int]:
    """Companion pids a live daemon of ``udid`` owns (``owned: true``): it
    stops them by its own idle rule, so session stop leaves them alone
    (ID-16). A companion the daemon only adopted (``owned: false``) is not
    listed: the daemon never stops it, so whoever started it still does.
    Never raises."""
    if not udid:
        return set()
    try:
        registry = live_registry(state_base(), udid)
    except Exception:  # noqa: BLE001 - a teardown helper must not fail
        return set()
    companion = (registry or {}).get("companion")
    if not isinstance(companion, dict) or companion.get("owned") is not True:
        return set()
    pid = companion.get("pid")
    return {pid} if _pid_alive(pid) else set()


# --- start --------------------------------------------------------------------


def _try_lock(path: Path) -> bool:
    """Take the O_EXCL start lock (our pid inside); a lock whose holder is
    gone is removed and taken."""
    for _ in range(3):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                text = path.read_text(encoding="utf-8").strip()
                age = time.time() - path.stat().st_mtime
            except OSError:
                continue  # released while we looked
            holder = int(text) if text.isdigit() else None
            if holder is None and age < _LOCK_GRACE_S:
                return False  # just created; the pid is on its way
            if holder is not None and _pid_alive(holder):
                return False
            try:
                path.unlink()
            except OSError:
                pass
            continue
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        return True
    return False


def _release_lock(path: Path) -> None:
    try:
        if path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass


def find_node() -> str | None:
    explicit = os.environ.get(NODE_ENV)
    if explicit:
        return explicit if Path(explicit).is_file() else None
    return shutil.which("node")


def supervise(argv: list[str]) -> int:
    """The detached supervisor's body: run the daemon under
    ``processes.run_supervised(kind="iosd")`` until it exits."""
    from . import processes

    udid = argv[argv.index("--udid") + 1] if "--udid" in argv else None
    return processes.run_supervised(argv, kind="iosd", udid=udid)


def _read_status_line(stream: Any, deadline: float) -> str | None:
    """The first line ``stream`` carries before ``deadline``, or None."""
    fd = stream.fileno()
    data = b""
    while b"\n" not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            return None
        chunk = os.read(fd, 65536)
        if not chunk:
            return None  # the daemon (and its supervisor) exited without a line
        data += chunk
        if len(data) > MAX_LINE_BYTES:
            return None
    return data.split(b"\n", 1)[0].decode("utf-8", "replace")


def _from_status(status: dict[str, Any], base: Path, udid: str) -> dict[str, Any]:
    state = status.get("status")
    if state == "already_running":
        live = live_registry(base, udid)
        if live is None:
            raise Unavailable(DAEMON_START_FAILED,
                              "another iOS device daemon owns the simulator but does not answer")
        status = {**live, "status": live.get("state")}
        state = status["status"]
    if state == "unsupported":
        raise Unavailable(COMPANION_TOO_OLD,
                          f"idb_companion {status.get('companion_version') or '(unknown)'} is "
                          "older than 1.6.4", code=COMPANION_TOO_OLD)
    if state == "failed":
        error = status.get("error") if isinstance(status.get("error"), dict) else {}
        if error.get("code") == COMPANION_TOO_OLD:
            raise Unavailable(COMPANION_TOO_OLD, str(error.get("message") or "companion too old"),
                              code=COMPANION_TOO_OLD)
        reason = error.get("reason")
        raise Unavailable(DAEMON_START_FAILED,
                          "the iOS device daemon failed to start"
                          + (f" ({reason})" if reason else ""))
    if state != "serving" or not isinstance(status.get("socket"), str):
        raise Unavailable(DAEMON_START_FAILED, f"the iOS device daemon reported {state!r}")
    return status


def _start(base: Path, udid: str, deadline: float) -> dict[str, Any]:
    node = find_node()
    if not node:
        raise Unavailable(NODE_MISSING, "node is not on PATH; the iOS device daemon needs Node 22+")
    if not DAEMON_SCRIPT.is_file():
        raise Unavailable(DAEMON_START_FAILED, f"the daemon script is missing: {DAEMON_SCRIPT}")
    paths = daemon_paths(base, udid)
    try:
        paths["dir"].mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise Unavailable(DAEMON_START_FAILED, f"cannot create {paths['dir']}: {exc}") from exc
    lock_deadline = min(deadline, time.monotonic() + LOCK_TIMEOUT_S)
    while not _try_lock(paths["start_lock"]):
        live = live_registry(base, udid)
        if live is not None:
            return {**live, "status": live.get("state")}
        if time.monotonic() > lock_deadline:
            raise Unavailable(DAEMON_START_FAILED,
                              f"the start lock {paths['start_lock']} stayed taken")
        time.sleep(0.05)
    try:
        live = live_registry(base, udid)
        if live is not None:
            return {**live, "status": live.get("state")}
        # -I: no cwd, PYTHONPATH or user site on sys.path, so a file named
        # like a stdlib module where the agent stands never runs inside the
        # long-lived supervisor; cwd=ROOT keeps the pair off that directory.
        command = [sys.executable, "-I", "-c", _SUPERVISOR, str(ROOT / "scripts"),
                   node, str(DAEMON_SCRIPT), "--udid", udid, "--state-base", str(base)]
        try:
            supervisor = subprocess.Popen(  # noqa: S603
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
                cwd=str(ROOT))
        except OSError as exc:
            raise Unavailable(DAEMON_START_FAILED, f"could not start the daemon: {exc}") from exc
        try:
            line = _read_status_line(supervisor.stdout, deadline)
        finally:
            supervisor.stdout.close()
        if line is None:
            raise Unavailable(DAEMON_START_FAILED,
                              "the iOS device daemon did not report that it serves")
        try:
            status = json.loads(line)
        except ValueError:
            raise Unavailable(DAEMON_START_FAILED, "the iOS device daemon printed no status line")
        if not isinstance(status, dict):
            raise Unavailable(DAEMON_START_FAILED, "the iOS device daemon printed no status line")
        return _from_status(status, base, udid)
    finally:
        _release_lock(paths["start_lock"])


def ensure(udid: str, *, timeout_s: float = START_TIMEOUT_S) -> dict[str, Any]:
    """The serving daemon of ``udid`` (its registry fields): a live one,
    else one started now. Raises ``Unavailable`` with the reason it cannot
    be used; that reason is then remembered for this process."""
    base = state_base()
    key = (str(base), udid)
    if disabled():
        raise Unavailable(DAEMON_DISABLED, f"{ENV}=off")
    remembered = _UNAVAILABLE.get(key)
    if remembered:
        raise Unavailable(remembered, f"the iOS device daemon was not usable earlier ({remembered})")
    try:
        live = live_registry(base, udid)
        if live is not None:
            if live.get("state") == "unsupported":
                raise Unavailable(COMPANION_TOO_OLD,
                                  f"idb_companion {live.get('companion_version') or '(unknown)'} "
                                  "is older than 1.6.4", code=COMPANION_TOO_OLD)
            return live
        return _start(base, udid, time.monotonic() + timeout_s)
    except Unavailable as exc:
        remember(udid, exc.reason)
        raise


def remember(udid: str, reason: str) -> None:
    """Use no daemon for ``udid`` for the rest of this process."""
    _UNAVAILABLE[(str(state_base()), udid)] = reason


# --- one request ----------------------------------------------------------------


def request(socket_path: str, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
    """Send ``payload`` (an ``id`` is added) and return the daemon's reply.

    Raises ``Unavailable`` (reason ``daemon_unreachable``): stage
    BEFORE_SEND when no connection was made, AFTER_SEND when the line may
    have been written and no reply came back within ``timeout_s``."""
    message = {**payload, "id": f"py{os.getpid()}-{next(_REQUEST_IDS)}"}
    line = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
    deadline = time.monotonic() + max(timeout_s, 0.1)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(min(CONNECT_TIMEOUT_S, max(timeout_s, 0.1)))
        try:
            client.connect(socket_path)
        except OSError as exc:
            raise Unavailable(DAEMON_UNREACHABLE,
                              f"no iOS device daemon answered at {socket_path}: {exc}") from exc
        try:
            client.settimeout(max(deadline - time.monotonic(), 0.1))
            client.sendall(line)
        except OSError as exc:
            # part of the line may have gone out: the daemon may act on it
            raise Unavailable(DAEMON_UNREACHABLE,
                              f"the connection to the iOS device daemon broke while sending: {exc}",
                              stage=AFTER_SEND) from exc
        data = b""
        while b"\n" not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Unavailable(DAEMON_UNREACHABLE,
                                  f"the iOS device daemon did not answer {payload.get('op')} "
                                  f"within {timeout_s:g} s", stage=AFTER_SEND)
            client.settimeout(remaining)
            try:
                chunk = client.recv(1 << 20)
            except socket.timeout:
                continue
            except OSError as exc:
                raise Unavailable(DAEMON_UNREACHABLE,
                                  f"the connection to the iOS device daemon broke: {exc}",
                                  stage=AFTER_SEND) from exc
            if not chunk:
                raise Unavailable(DAEMON_UNREACHABLE,
                                  f"the iOS device daemon closed the connection before it "
                                  f"answered {payload.get('op')}", stage=AFTER_SEND)
            data += chunk
            if len(data) > MAX_LINE_BYTES:
                raise Unavailable(DAEMON_UNREACHABLE, "the daemon's reply is too long",
                                  stage=AFTER_SEND)
    finally:
        client.close()
    try:
        reply = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
    except ValueError as exc:
        raise Unavailable(DAEMON_UNREACHABLE, "the iOS device daemon's reply is not JSON",
                          stage=AFTER_SEND) from exc
    if not isinstance(reply, dict) or reply.get("id") != message["id"]:
        raise Unavailable(DAEMON_UNREACHABLE, "the iOS device daemon answered another request",
                          stage=AFTER_SEND)
    return reply
