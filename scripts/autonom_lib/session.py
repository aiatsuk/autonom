"""Session records and artifact directories (CAP-PLAT-003).

Schema v2 adds `platform`, `target_id`, and the `tooling` / `network` /
`background` / `consent_log` blocks. Every v1 key is still written, so a 0.4.0
consumer keeps working (INV-01), and a v1 record found on disk is upgraded **in
memory only** — an upgrade must never silently rewrite a file this process did
not create, because a user may be mid-investigation when they update (INV-02).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable

from . import errors

try:  # POSIX (macOS, Linux: every platform Autonom runs on); absent on Windows
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = 2
# The session one CLI invocation is bound to: None follows `current.json`,
# a session id is that session (`--session-id`).
_SELECTED: ContextVar[str | None] = ContextVar("autonom_session", default=None)
_SESSION_ID = re.compile(r"s_[A-Za-z0-9]+")


def select(session_id: str | None):
    """Bind the rest of this invocation to `session_id`; returns the reset token."""
    return _SELECTED.set(session_id)


def load_by_id(session_id: str, cwd: Path | None = None) -> dict[str, Any]:
    """A session record by id, stopped or not. The id is checked before it
    becomes part of a path."""
    if not _SESSION_ID.fullmatch(session_id or ""):
        raise errors.AutonomError(
            errors.SESSION_NOT_FOUND, f"no session {session_id!r}",
            f"Session ids look like s_0123456789; sessions live under {artifacts_root(cwd)}.")
    path = artifacts_root(cwd) / session_id / "session.json"
    if not path.is_file():
        raise errors.AutonomError(
            errors.SESSION_NOT_FOUND, f"no session {session_id!r}",
            f"Sessions live under {artifacts_root(cwd)}.")
    return upgrade(json.loads(path.read_text(encoding="utf-8")))


def _current_id(cwd: Path | None) -> str | None:
    path = artifacts_root(cwd) / "current.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value.get("session_id") if isinstance(value, dict) else None


def sessions_home() -> Path:
    """The machine-global session store: `$AUTONOM_HOME/sessions`, else
    `~/.autonom/sessions`. Sessions live here — not in the project — so a run is
    not tied to the directory it was launched from and the active session is
    found from anywhere, the same way mocks, the process registry, and per-app
    knowledge are already machine-level."""
    home = os.environ.get("AUTONOM_HOME")
    base = Path(home) if home else Path.home() / ".autonom"
    root = base / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def artifacts_root(cwd: Path | None = None) -> Path:
    # Default is the global store. An explicit cwd forces the legacy
    # project-local `.autonom/` layout (used by a few tests and anyone who
    # deliberately wants a run's artifacts to live beside the code).
    if cwd is not None:
        base = cwd / ".autonom"
        base.mkdir(parents=True, exist_ok=True)
        return base
    return sessions_home()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_record(
    *,
    platform: str,
    target_id: str,
    app_id: str | None,
    artifacts_dir: Path,
    session_id: str,
    tooling: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "platform": platform,
        "target_id": target_id,
        "aliases": {"serial": target_id} if platform == "android" else {"udid": target_id},
        "app_id": app_id,
        "install_path": None,
        "started_at": _now(),
        "artifacts_dir": str(artifacts_dir),
        "display": None,
        "tooling": tooling or {},
        "network": {
            "enabled": False,
            "proxy_host": None,
            "proxy_port": None,
            "device_proxy": None,
            "attached": False,
            "previous_http_proxy": None,
        },
        "background": {"log_stream_pid": None, "recorder_pid": None},
        "streams": [],
        "consent_log": [],
    }
    if platform == "android":
        # DEC-004: `serial` and `adb` are permanent for Android callers.
        record["serial"] = target_id
        record["adb"] = (tooling or {}).get("adb")
    return record


def upgrade(record: dict[str, Any]) -> dict[str, Any]:
    """Bring a v1 record up to v2 shape without touching the file it came from."""
    if not record:
        return record
    upgraded = dict(record)
    upgraded.setdefault("schema_version", 1)
    platform = upgraded.get("platform") or "android"
    upgraded["platform"] = platform
    if not upgraded.get("target_id"):
        upgraded["target_id"] = upgraded.get("serial") or upgraded.get("udid") or ""
    if not upgraded.get("aliases"):
        key = "serial" if platform == "android" else "udid"
        upgraded["aliases"] = {key: upgraded["target_id"]} if upgraded["target_id"] else {}
    upgraded.setdefault("install_path", None)
    upgraded.setdefault("display", None)
    upgraded.setdefault("tooling", {"adb": upgraded.get("adb")} if upgraded.get("adb") else {})
    upgraded.setdefault(
        "network",
        {
            "enabled": False,
            "proxy_host": None,
            "proxy_port": None,
            "device_proxy": None,
            "attached": False,
            "previous_http_proxy": None,
        },
    )
    upgraded.setdefault("background", {"log_stream_pid": None, "recorder_pid": None})
    upgraded.setdefault("streams", [])
    upgraded.setdefault("consent_log", [])
    return upgraded


def register_stream(record: dict[str, Any], *, stream_id: str, kind: str,
                    path: str, label: str | None = None,
                    pid: int | None = None) -> dict[str, Any]:
    """Record a followable append-only file so `session outputs` can list it.

    Idempotent per stream_id: a writer restarting (network start twice) updates
    its entry in place. `path` is relative to the artifacts dir. The caller
    saves the record.
    """
    entry: dict[str, Any] = {"id": stream_id, "kind": kind, "path": path}
    if label:
        entry["label"] = label
    if pid:
        entry["pid"] = pid
    streams = record.setdefault("streams", [])
    for index, existing in enumerate(streams):
        if existing.get("id") == stream_id:
            streams[index] = entry
            return record
    streams.append(entry)
    return record


def start_session(
    tool: str,
    *,
    serial: str | None = None,
    app_id: str | None = None,
    cwd: Path | None = None,
    platform: str = "android",
    target_id: str | None = None,
    tooling: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the artifact tree and the session record.

    `tool` and `serial` keep their 0.4.0 positions so existing callers and tests
    (which pass `start_session("adb", serial=...)`) are unaffected.
    """
    resolved_id = target_id or serial
    if not resolved_id:
        raise errors.AutonomError(errors.NO_TARGET, "a target id is required to start a session")
    session_id = f"s_{uuid.uuid4().hex[:10]}"
    root = artifacts_root(cwd) / session_id
    for name in ("shots", "trees", "logs", "network", "recordings", "crashes",
                 "files", "output"):
        (root / name).mkdir(parents=True, exist_ok=True)

    resolved_tooling = dict(tooling or {})
    if platform == "android":
        resolved_tooling.setdefault("adb", tool)
    else:
        resolved_tooling.setdefault("simctl", tool)

    record = new_record(
        platform=platform,
        target_id=resolved_id,
        app_id=app_id,
        artifacts_dir=root,
        session_id=session_id,
        tooling=resolved_tooling,
    )
    save(record, cwd)
    return record


def _write_atomic(path: Path, payload: str) -> None:
    """Replace `path` in one step: a reader (the Canvas bridge, a parallel
    verb) sees the old file or the new one, never a truncated one."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                             dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = 0o644  # what write_text gave a new file under the usual umask
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


@contextlib.contextmanager
def _record_lock(directory: Path):
    """Serialize writes of one session's record across processes (`save`
    against `session stop`): an exclusive flock on the session directory
    itself, so no lock file joins the session's artifacts."""
    if fcntl is None:  # pragma: no cover - Windows
        yield
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)  # releases the lock


def save(record: dict[str, Any], cwd: Path | None = None) -> dict[str, Any]:
    """Persist to both the session directory and the current-session pointer.

    A stopped session is read-only: a record that carries `stopped_at`, or
    whose `session.json` already does (a long-running command holding a copy
    from before the stop), is returned unwritten. The check and both writes
    hold the session's lock, which `stop_session` takes too, so a save racing
    a stop can neither drop `stopped_at` nor bring `current.json` back. Each
    file is replaced atomically."""
    path = Path(record["artifacts_dir"]) / "session.json"
    if record.get("stopped_at"):
        return record
    path.parent.mkdir(parents=True, exist_ok=True)
    with _record_lock(path.parent):
        stored = _read_stored(path)
        if stored.get("stopped_at"):
            return record
        # The stopping mark (`stopping_at`, `stopping_tokens`) belongs to
        # `session stop` alone (mark_stopping and clear_stopping): whatever
        # this copy says, the file's mark is kept, so a command holding a
        # copy from before the mark (or before an aborted stop cleared it)
        # neither drops nor revives it.
        _copy_stopping(stored, record)
        payload = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
        _write_atomic(path, payload)
        # A command bound to another session (--session-id) must not move the
        # machine's current pointer; the current session itself is still mirrored.
        if _SELECTED.get() is None or _current_id(cwd) == record.get("session_id"):
            _write_current(cwd, record)
    return record


def _read_stored(path: Path) -> dict[str, Any]:
    """The record file as it is on disk, or {} when missing or unreadable."""
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def _stopped_on_disk(path: Path) -> bool:
    return bool(_read_stored(path).get("stopped_at"))


def on_disk(record: dict[str, Any]) -> dict[str, Any]:
    """The latest `session.json` of `record`'s session, read from its own
    artifacts directory: what a long-running command checks to learn that
    the session began stopping (`stopping_at`) or stopped (`stopped_at`)
    since it loaded its copy. Falls back to `record` when the file is
    missing or unreadable."""
    directory = record.get("artifacts_dir")
    if not directory:
        return record
    return _read_stored(Path(directory) / "session.json") or record


_STOPPING_FIELDS = ("stopping_at", "stopping_tokens")
# The one stopper a record from before owner tokens stands for: it carries a
# bare `stopping_at`, which no token of this version can clear.
ANONYMOUS_STOPPER = "anonymous"


def _copy_stopping(source: dict[str, Any], target: dict[str, Any]) -> None:
    """Make `target`'s stopping mark exactly `source`'s."""
    for field in _STOPPING_FIELDS:
        if field in source:
            value = source[field]
            target[field] = list(value) if isinstance(value, list) else value
        else:
            target.pop(field, None)


def active_stoppers(record: dict[str, Any]) -> list[str]:
    """The owner tokens of the `session stop` commands still tearing this
    session down. A bare `stopping_at` (a record from the version before
    owner tokens, or one whose token list is unreadable) counts as one
    anonymous stopper."""
    tokens = record.get("stopping_tokens")
    if isinstance(tokens, list):
        tokens = [token for token in tokens if isinstance(token, str) and token]
        if tokens:
            return tokens
    return [ANONYMOUS_STOPPER] if record.get("stopping_at") else []


def is_stopping(record: dict[str, Any]) -> bool:
    """True while any `session stop` holds the stopping mark."""
    return bool(active_stoppers(record))


def _set_stopping(record: dict[str, Any], *, add: str | None = None,
                  remove: str | None = None, cwd: Path | None = None) -> None:
    path = Path(record["artifacts_dir"]) / "session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _record_lock(path.parent):
        stored = _read_stored(path) or dict(record)
        if stored.get("stopped_at"):
            return  # a stopped session is read-only
        before = {field: stored.get(field) for field in _STOPPING_FIELDS}
        tokens = active_stoppers(stored)
        if add is not None:
            tokens.append(add)
            stored["stopping_at"] = stored.get("stopping_at") or _now()
            stored["stopping_tokens"] = tokens
        elif remove in tokens:
            tokens.remove(remove)
            if tokens:
                stored["stopping_tokens"] = tokens
            else:
                for field in _STOPPING_FIELDS:
                    stored.pop(field, None)
        if {field: stored.get(field) for field in _STOPPING_FIELDS} != before:
            payload = json.dumps(stored, indent=2, ensure_ascii=False) + "\n"
            _write_atomic(path, payload)
            if _current_id(cwd) == stored.get("session_id"):
                _write_current(cwd, stored)
        _copy_stopping(stored, record)


def mark_stopping(record: dict[str, Any], cwd: Path | None = None) -> str:
    """Enter this `session stop` as an active stopper before it tears
    anything down, atomically and under the record lock, so a command racing
    the stop (an XCUITest runner request) refuses with `session_stopped`
    instead of starting what the teardown can no longer reach. `stopping_at`
    keeps the time the first active stopper began; `stopping_tokens` lists
    one owner token per active stopper. Returns this stopper's token, which
    `clear_stopping` takes. Records written by older versions simply lack
    the mark."""
    token = uuid.uuid4().hex
    _set_stopping(record, add=token, cwd=cwd)
    return token


def clear_stopping(record: dict[str, Any], token: str,
                   cwd: Path | None = None) -> None:
    """Withdraw the stopper `token` names when its `session stop` aborts
    before the session is stopped (an accessibility restore that failed
    keeps the pointer). The mark stays while another stopper is still
    active; once none is, it is dropped and the session is usable again."""
    _set_stopping(record, remove=token, cwd=cwd)


def stop_session(cwd: Path | None = None, *, reap: bool = True) -> dict[str, Any] | None:
    """Mark the current session stopped and clear the pointer.

    With `reap` (the default), every process the machine registry records as
    this session's — the iOS log-stream writer, a `canvas serve` pair, an
    `idb_companion` its own idb calls started — is terminated first, and the
    outcome is kept on the record as `process_teardown` (with any companion
    it could not attribute under `companion_left_running`). This is the
    safety net under the CLI's own teardown list: a process the list forgot
    must not outlive the session it served.
    """
    current = artifacts_root(cwd) / "current.json"
    record = load_current(cwd)
    if not record:
        return None
    record["stopped_at"] = _now()
    if reap:
        teardown = reap_owned_processes(record)
        if (teardown.get("terminated") or teardown.get("companion_left_running")
                or teardown.get("group_remnants")):
            record["process_teardown"] = teardown
    session_path = Path(record["artifacts_dir"]) / "session.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    with _record_lock(session_path.parent):
        _write_atomic(session_path,
                      json.dumps(record, indent=2, ensure_ascii=False) + "\n")
        if _current_id(cwd) == record.get("session_id"):
            current.unlink(missing_ok=True)
    return record


def load_current(cwd: Path | None = None) -> dict[str, Any] | None:
    selected = _SELECTED.get()
    if selected:
        return load_by_id(selected, cwd)
    return _load_pointer(cwd)


def _load_pointer(cwd: Path | None = None) -> dict[str, Any] | None:
    """The machine's current session (`current.json`), whatever is selected."""
    path = artifacts_root(cwd) / "current.json"
    if not path.exists():
        return None
    return upgrade(json.loads(path.read_text(encoding="utf-8")))


def journal_session(cwd: Path | None = None) -> dict[str, Any] | None:
    """The session this invocation's journal entry belongs to.

    A session that has stopped is read-only: `--session-id` may name one for
    reading, but nothing is ever appended to its timeline after the stop. The
    entry then goes where it always went, the machine's current session (or
    nowhere when there is none)."""
    record = load_current(cwd)
    if record and record.get("stopped_at"):
        return _load_pointer(cwd)
    return record


def require_current(cwd: Path | None = None) -> dict[str, Any]:
    record = load_current(cwd)
    if not record:
        raise errors.AutonomError(
            errors.NO_ACTIVE_SESSION,
            "no active session",
            "Start one with 'autonom session start'.",
        )
    return record


def _write_current(cwd: Path | None, record: dict[str, Any]) -> None:
    path = artifacts_root(cwd) / "current.json"
    _write_atomic(path, json.dumps(record, indent=2, ensure_ascii=False) + "\n")


def artifact_path(record: dict[str, Any], *parts: str) -> Path:
    path = Path(record["artifacts_dir"]).joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# --- teardown ----------------------------------------------------------------


def run_teardown(actions: list[tuple[str, Callable[[], Any]]]) -> list[dict[str, Any]]:
    """Best-effort teardown (INV-10).

    Each action is isolated: `session stop` must never fail because a proxy was
    already dead or a companion refused to disconnect. Failures are reported per
    action so a partial teardown is visible rather than silent.
    """
    results: list[dict[str, Any]] = []
    for name, action in actions:
        try:
            detail = action()
            results.append({"action": name, "ok": True, "detail": detail})
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            results.append({"action": name, "ok": False, "error": str(exc)})
    return results


def reap_owned_processes(record: dict[str, Any]) -> dict[str, Any]:
    """`processes.reap_session`, never raising: teardown must not fail."""
    try:
        from . import processes

        return processes.reap_session(record)
    except Exception as exc:  # noqa: BLE001 - teardown must not raise (INV-10)
        return {"terminated": [], "companion_left_running": [], "error": str(exc)}


def _collect_if_child(pid: int) -> None:
    """Reap `pid` when it is this process's own exited child: a zombie
    answers `kill(pid, 0)`, which kept every self-started process "alive"
    for the whole termination timeout."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _is_zombie(pid: int) -> bool:
    """True when `pid` has exited but its parent has not reaped it yet.

    A zombie still answers `kill(pid, 0)`, so an exited process whose parent
    is slow to reap it (or is a long-lived process that never does) looked
    alive until the termination timeout ran out. Never raises; unknown is
    "not a zombie"."""
    try:
        completed = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                   capture_output=True, text=True, timeout=5,
                                   check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.stdout.strip().startswith("Z")


def _exited(pid: int, *, ask_ps: bool = True) -> bool:
    _collect_if_child(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return ask_ps and _is_zombie(pid)


def terminate_pid(pid: int | None, *, timeout: float = 5.0) -> bool:
    """Stop a session-owned background process; returns True when it was alive."""
    if not pid:
        return False
    try:
        os.kill(pid, 15)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.time() + timeout
    polls = 0
    while time.time() < deadline:
        # `ps` (the zombie check) every fifth poll: a live process shutting
        # down is polled every 50 ms, and kill(pid, 0) alone is free.
        if _exited(pid, ask_ps=polls % 5 == 0):
            return True
        polls += 1
        time.sleep(0.05)
    try:
        os.kill(pid, 9)
    except OSError:
        pass
    for polls in range(40):  # SIGKILL is not instant under load: see it land
        if _exited(pid, ask_ps=polls % 5 == 0):
            break
        time.sleep(0.05)
    return True


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return not _is_zombie(pid)


# --- Android app control (unchanged behavior) --------------------------------


def install_app(adb: str, serial: str, path: Path) -> None:
    from . import adb as adb_mod

    adb_mod.run_adb(adb, ["install", "-r", str(path)], serial=serial, timeout=180, check=True)


def _component(app_id: str, activity: str) -> str:
    return activity if "/" in activity else f"{app_id}/{activity}"


def _raise_if_am_failed(output: str, component: str) -> None:
    """`am start` reports most failures on stdout and still exits 0.

    Measured: `am start -n pkg/.Bad` prints `Error type 3` and `Error:
    Activity class {pkg/.Bad} does not exist.` with exit 0, so the launch used
    to report ok while nothing started. A `Warning:` (the task was only
    brought to the front) is not a failure.
    """
    for line in (output or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("Error"):
            continue
        if "does not exist" in stripped:
            raise errors.AutonomError(
                errors.INVALID_VALUE,
                f"cannot start {component}: {stripped[:200]}",
                "Check the package is installed ('adb shell pm list packages') and the "
                "activity name; without --activity the launcher activity is used.",
            )
        if stripped.startswith("Error:"):
            raise errors.AutonomError(
                errors.BACKEND_FAILED, f"am start {component} failed: {stripped[:200]}",
                "Run 'adb shell cmd package resolve-activity --brief <package>' to see "
                "what the package can start.",
            )


def _am_start(adb: str, serial: str, argv: list[str], component: str,
              *, timeout: float) -> str:
    from . import adb as adb_mod

    completed = adb_mod.run_adb(
        adb, ["shell", "am", "start", *argv], serial=serial, timeout=timeout, check=True,
    )
    output = completed.stdout if isinstance(completed.stdout, str) else ""
    _raise_if_am_failed(output, component)
    return output


_WAIT_KEYS = {"Status": "status", "LaunchState": "launch_state", "Activity": "activity",
              "TotalTime": "total_time_ms", "WaitTime": "wait_time_ms"}


def parse_am_wait(output: str) -> dict[str, Any] | None:
    """The launch report `am start -W` prints, or None when there is none.

    ``{"launch_state", "activity", "total_time_ms", "wait_time_ms", "status",
    "brought_to_front"}``: `launch_state` is lower-cased (``cold``, ``warm``,
    ``hot``, ``unknown`` — the last when an existing task was only brought to
    the front, which is what a resume normally is).
    """
    fields: dict[str, Any] = {}
    brought = False
    for line in (output or "").splitlines():
        stripped = line.strip()
        if "brought to the front" in stripped:
            brought = True
        key, sep, value = stripped.partition(":")
        if not sep or key not in _WAIT_KEYS:
            continue
        value = value.strip()
        name = _WAIT_KEYS[key]
        if name.endswith("_ms"):
            fields[name] = int(value) if value.isdigit() else None
        elif name == "launch_state":
            fields[name] = value.split()[0].lower() if value else None
        else:
            fields[name] = value or None
    if not fields:
        return None
    report = {"launch_state": None, "activity": None, "total_time_ms": None,
              "wait_time_ms": None, "status": None}
    report.update(fields)
    report["brought_to_front"] = brought
    return report


def _monkey_launch(adb: str, serial: str, app_id: str) -> None:
    """The last-resort launch, for a package with no resolvable launcher
    activity. `monkey` freezes rotation to 0 and thaws it again when it ends,
    which silently undoes a pinned orientation — never the first choice."""
    from . import adb as adb_mod

    # `--pct-syskeys 0`: monkey refuses to run at all on an AVD with
    # hw.keyboard=no ("SYS_KEYS has no physical keys but with factor 2.0%",
    # exit 251) because its default event mix includes system keys. One launch
    # event needs none of them.
    completed = adb_mod.run_adb(
        adb,
        ["shell", "monkey", "-p", app_id, "-c", "android.intent.category.LAUNCHER",
         "--pct-syskeys", "0", "1"],
        serial=serial,
        timeout=30,
        check=False,
    )
    output = completed.stdout if isinstance(completed.stdout, str) else ""
    if "No activities found to run" in output:
        raise errors.AutonomError(
            errors.APP_NOT_INSTALLED,
            f"{app_id} has no launcher activity on {serial} (is it installed?)",
            "Check the package id with 'adb shell pm list packages', or launch a "
            "specific activity with --activity.",
        )
    if completed.returncode != 0:
        raise adb_mod.AdbError(output.strip() or f"monkey -p {app_id} failed ({completed.returncode})")


MAIN_ACTION = "android.intent.action.MAIN"
LAUNCHER_CATEGORY = "android.intent.category.LAUNCHER"
# FLAG_ACTIVITY_NEW_TASK | FLAG_ACTIVITY_RESET_TASK_IF_NEEDED: exactly what the
# home screen (and monkey) send, so an existing task is brought to the front
# as it stands — no CLEAR_TASK, nothing the user was doing is thrown away.
RESUME_TASK_FLAGS = "0x10200000"


def launch_app(adb: str, serial: str, app_id: str,
               activity: str | None = None) -> dict[str, Any]:
    """Start (or bring to the front) the app the way its launcher icon does.

    `am start -W` on the resolved launcher activity, never `monkey` first:
    monkey freezes the rotation to 0 and thaws it when it finishes, so every
    resume reset the orientation a test had pinned. monkey remains the
    fallback for a package whose launcher activity cannot be resolved.

    Returns ``{"mode": "resume", "component", "launch"}`` where `launch` is
    the `-W` report (`parse_am_wait`), or None when monkey had to be used.
    """
    if activity:
        component = _component(app_id, activity)
        output = _am_start(adb, serial, ["-W", "-n", component], component, timeout=60)
        return {"mode": "resume", "component": component, "launch": parse_am_wait(output)}
    component = resolve_launcher_activity(adb, serial, app_id)
    if component is None:
        _monkey_launch(adb, serial, app_id)
        return {"mode": "resume", "component": None, "launch": None,
                "note": "no launcher activity resolved; launched via monkey"}
    output = _am_start(
        adb, serial,
        ["-W", "-n", component, "-a", MAIN_ACTION, "-c", LAUNCHER_CATEGORY,
         "-f", RESUME_TASK_FLAGS],
        component, timeout=60,
    )
    return {"mode": "resume", "component": component, "launch": parse_am_wait(output)}


FRESH_TASK_FLAGS = "0x10008000"  # FLAG_ACTIVITY_NEW_TASK | FLAG_ACTIVITY_CLEAR_TASK


def resolve_launcher_activity(adb: str, serial: str, app_id: str) -> str | None:
    """The component the launcher would start, or None when the package has
    no launcher activity (a service-only app, a wrong id)."""
    from . import adb as adb_mod

    completed = adb_mod.run_adb(
        adb,
        ["shell", "cmd", "package", "resolve-activity", "--brief",
         "-a", MAIN_ACTION, "-c", LAUNCHER_CATEGORY, app_id],
        serial=serial, timeout=30, check=False,
    )
    text = completed.stdout if isinstance(completed.stdout, str) else ""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if "/" in line and line.startswith(app_id):
            return line
    return None


def launch_app_fresh(adb: str, serial: str, app_id: str,
                     activity: str | None = None) -> dict:
    """Start the launcher activity (or `activity`) on a cleared task.

    A resume (`launch_app`) brings back whatever the app's task holds, which
    on a real device meant a flow's first selector met a subscreen — or,
    with Android Settings, a search activity of *another* package that
    `force-stop` never touches. Clearing the task starts the app where a
    user launching it from the home screen would land, without wiping data.

    With an explicit `activity` the same cleared-task start targets that
    component, so `--fresh --activity` no longer falls back to a resume.
    The `-W` launch report is returned under `launch` when there is one.
    """
    if activity:
        component = _component(app_id, activity)
        output = _am_start(adb, serial, ["-W", "-n", component, "-f", FRESH_TASK_FLAGS],
                           component, timeout=60)
        return _with_launch({"mode": "fresh", "component": component}, output)
    component = resolve_launcher_activity(adb, serial, app_id)
    if component is None:
        _monkey_launch(adb, serial, app_id)
        return {"mode": "resume", "component": None,
                "note": "no launcher activity resolved; resumed via monkey"}
    # FLAG_ACTIVITY_NEW_TASK | FLAG_ACTIVITY_CLEAR_TASK as a raw flag value:
    # `am start` has no `--activity-new-task` option, and measured on an
    # API-37 emulator `--activity-clear-task` alone left Settings on its
    # SubSettings screen — CLEAR_TASK only clears when paired with NEW_TASK.
    output = _am_start(
        adb, serial,
        ["-W", "-n", component,
         "-a", MAIN_ACTION, "-c", LAUNCHER_CATEGORY,
         "-f", FRESH_TASK_FLAGS],
        component, timeout=60,
    )
    return _with_launch({"mode": "fresh", "component": component}, output)


def _with_launch(detail: dict[str, Any], output: str) -> dict[str, Any]:
    report = parse_am_wait(output)
    if report is not None:
        detail["launch"] = report
    return detail


def force_stop(adb: str, serial: str, app_id: str) -> None:
    from . import adb as adb_mod

    adb_mod.run_adb(adb, ["shell", "am", "force-stop", app_id], serial=serial, timeout=20, check=True)


def clear_data(adb: str, serial: str, app_id: str) -> None:
    from . import adb as adb_mod

    adb_mod.run_adb(adb, ["shell", "pm", "clear", app_id], serial=serial, timeout=30, check=True)
