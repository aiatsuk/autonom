"""Host client of the on-device Android helper, protocol 1 (AH-02..AH-07, AU-05).

The helper (``native/android/helper``, built on first use by
``android_helper_build``) runs on the device as the shell user and answers
JSON lines on the abstract socket ``autonom-helper``. This module starts,
reaches, checks and stops it; it never decides which backend serves a verb
(the Android backend does, with `preference` and `HelperUnavailable`).

Device record
    ``<state>/android-helper/devices/<serial>.json``: ``serial``, ``port``
    (the host side of ``adb forward``), ``pid`` (the device process),
    ``build``, ``started_at``, ``last_request_at`` (epoch seconds of the last
    hello this host sent), and, for a device where the helper cannot run, an
    ``unavailable`` entry (below). Every start and stop of one serial runs
    under an exclusive ``flock`` on ``devices/<serial>.lock``. ``<state>`` is
    the Autonom state base (``android_helper_build.state_base``).

Use lock (`use`)
    A process talks to the helper of a device only while it holds the
    exclusive ``flock`` on ``devices/<serial>.use.lock``: from the connect
    through the hello, the request and its reply, and around a stop or a
    status hello. Inside one process a lock per serial guards the threads
    too (re-entrant for the thread that holds it). The record lock above is
    taken only inside the use lock, always in that order, so the two never
    deadlock. The holder writes its pid to ``devices/<serial>.use.owner``;
    the operating system drops a ``flock`` when its process dies, so a
    crashed caller never leaves the lock held. A caller waits for the lock
    up to `USE_WAIT_S` (the longest request, a start and a margin); one that
    cannot get it in time raises `HelperBusy` (``helper_busy``, the holder's
    pid when known): nothing is sent, nothing is stopped, and no
    uiautomator dump may run beside that call.

    So while the lock is held nobody else of this state directory is
    talking to the helper: a helper that does not answer hello within its
    timeout is stuck, not busy. It is stopped (a quit needs an answered
    hello, so the kill of its verified pid, then this record's forward) and
    started once (AH-05).

    The lock lives in the state directory, while the helper's socket is the
    whole device's: another Autonom state directory (another
    ``AUTONOM_HOME``) talks to the same helper under its own lock, through
    its own ``adb forward``. So a silent hello counts as stuck only while
    ``adb forward --list`` shows no other host port forwarded to this
    device's ``localabstract:autonom-helper`` (`_after_silence`). With one,
    the silence may be that directory's call: hello is sent again and
    waited for up to `USE_WAIT_S`, as for a held lock; still silent, the
    call raises `HelperBusy` and nothing is sent or stopped (`stop` ends it
    then, as a stop must). The check is one ``forward --list``, made only
    after a hello went unanswered.

Order of checks (`ensure`)
    1. the device record (a file read): a ``uiautomation_busy`` remembered
       less than 60 s ago ends the call;
    2. the host toolchain and the cached build (`android_helper_build.ensure`):
       a missing tool or a failed build ends the call;
    3. the use lock (a host file, above);
    4. only then device calls: a per-boot ``unavailable`` entry is compared
       with the device's ``/proc/sys/kernel/random/boot_id``; a recorded
       helper is reached on its port and must answer ``hello`` with protocol 1
       and the expected build. A helper that answers with another build or
       protocol is quit (and its verified pid killed) before anything else is
       sent; one that is dead (refused, end of stream) or stuck (no answer in
       time) is stopped and restarted once.

    So a host without the tools makes no adb call for the helper, and a
    device where the helper cannot run costs one attempt per boot (60 s for
    ``uiautomation_busy``), not one per call. ``force=True`` (``--ui-backend
    helper``) and `status` ignore the remembered entry and try again.

Start
    ``adb push <dex> /data/local/tmp/autonom-helper-<build>.dex``; every other
    ``autonom-helper-*.dex`` there is removed by its exact name; the helper is
    started detached under ``app_process`` with ``--nice-name=autonom-helper``
    and ``--idle-ms 120000``; the recorded port is reused when ``adb forward
    --list`` still shows it for this serial, else ``adb forward tcp:0
    localabstract:autonom-helper`` gives a fresh one; hello is polled for up
    to 5 s.

Forward ports
    hello names no device, so a recorded port is used (for a request, a
    status hello or a stop's quit) only while ``adb forward --list`` shows
    ``<serial> tcp:<port> localabstract:autonom-helper`` for this target's
    serial: after an adb server restart a reused host port could reach
    another device's helper. A port that is not listed so is never sent to
    and never removed (it is not this record's now); a fresh forward for the
    serial is made instead. The list is read once per process and device
    (`_forward_confirmed`; a start reads it again, `ui_android.reset` empties
    what was read at each CLI invocation, and a long-lived process such as
    the Mobile Canvas bridge reads it again once `FORWARD_CONFIRM_TTL_S`
    passed).

Requests (`request`, `Helper.call`)
    Every request carries a unique 32-hex id and has a host timeout: hello,
    quit 2 s; read, display, screenshot 8 s; tap, long press, swipe, key 8 s
    (plus the gesture's own duration); type and set_text 12 s. An input that
    was written and not answered (timeout, end of stream, reset) is
    ``ui_action_uncertain`` naming the request id and the op: it is never
    sent again, by a restart or by any other route. A read that fails after
    it was written is sent once more to a restarted helper; a second failure
    is `HelperUnavailable`. That restart kills only the helper that failed
    the read, and only while the record still names its pid: a helper that
    another caller started meanwhile is never ended by it. A pid adb cannot
    check (its `cat /proc/<pid>/cmdline` times out, or adb itself fails, as
    for a device that went offline) is never taken for a helper that is
    gone: the restart, the end of a stuck helper, the replacement of a dead
    helper or of one of another build, and a stop kill nothing, the record
    keeps the pid, and the call fails (`HelperUnavailable` for a request)
    instead of starting a second helper. `request` holds the use lock from
    before its hello until its last reply, the retry included.

    A start that fails after its launch, in any way (no hello within 5 s,
    a refused hello, an adb failure or timeout, no forward port, a record
    that cannot be written), ends what it launched and removes this record's
    forward, so no helper is left holding UiAutomation that no record names;
    a launched pid adb cannot check is not killed, and the record keeps it
    (with its forward), so the next call checks it again before it starts
    anything.
    What it launched is the verified ``autonom-helper`` pids that `pidof`
    did not show before the launch: a helper that was already running
    (another Autonom state directory's, or one an earlier record lost) is
    never killed by that cleanup. When such a helper holds the socket (the
    new one exits with ``socket_in_use``), what its hello does decides:
    with the expected build it is used; with another build or protocol,
    answered at once, it is idle and is replaced as AH-03 asks (quit, its
    verified pid killed only if it stays, the expected build launched
    again; once per start); a hello that stays queued behind another call
    until the start budget ends means it is busy, so it is left running and
    the start raises `HelperBusy` (``helper_foreign_pids``), remembering
    nothing.

Stop (`stop`; `session stop`, `ui helper stop`, before a uiautomator dump)
    Under the use lock: ``quit`` on the recorded port, then a ``kill`` of the
    recorded pid only when ``/proc/<pid>/cmdline`` starts with
    ``autonom-helper`` (the pid is gone when ``cat`` says "No such file or
    directory" or "No such process", in the words of the device's toybox,
    busybox or toolbox), then ``adb forward --remove tcp:<recorded port>``
    only when ``forward --list`` shows that port for this serial. Never
    ``--remove-all``, never another pid. A stop that cannot get the use lock
    in time raises `HelperBusy` and ends nothing.

Only light standard-library modules are imported at module level: the module
sits on the path of quick verbs.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shlex
import socket
import threading
import time
from typing import Any, Iterator, Mapping

from . import adb as adb_mod
from . import android_helper_build as build_mod
from . import errors

PROTOCOL = build_mod.PROTOCOL
SOCKET = "autonom-helper"
PROCESS = "autonom-helper"
IDLE_MS = 120000
MAIN_CLASS = "dev.autonom.helper.Main"
DEVICE_DIR = "/data/local/tmp"
DEVICE_LOG = f"{DEVICE_DIR}/autonom-helper.log"
DEX_PREFIX = "autonom-helper-"
MIN_SDK = build_mod.MIN_API

# Backend preference (AU-05): `--ui-backend`, then this variable, then the
# session's stored choice, then `auto`.
BACKEND_ENV = "AUTONOM_ANDROID_UI_BACKEND"
BACKENDS = ("auto", "helper", "uiautomator")

CONNECT_TIMEOUT_S = 2.0
START_BUDGET_S = 5.0
START_POLL_S = 0.1
# How long a caller waits for the use lock another Autonom process holds: the
# longest request (12 s), a helper start (5 s) and a margin. A longer holder
# (a `ui wait --settled` of more than that) makes the waiter `helper_busy`.
USE_WAIT_S = 20.0
# `status` only reports: it waits for a short while, then says who holds it.
STATUS_USE_WAIT_S = 2.0
USE_POLL_S = 0.05
BUSY_TTL_S = 60.0
# A per-boot entry whose boot id could not be read lasts this long instead.
NO_BOOT_ID_TTL_S = 600.0
QUIT_EXIT_WAIT_S = 1.0
ADB_TIMEOUT_S = 30
PUSH_TIMEOUT_S = 120
TIMEOUTS_S = {
    "hello": 2.0, "quit": 2.0,
    "read": 8.0, "display": 8.0, "screenshot": 8.0,
    "tap": 8.0, "long_press": 8.0, "swipe": 8.0, "key": 8.0,
    "type": 12.0, "set_text": 12.0,
}
DEFAULT_TIMEOUT_S = 8.0
INPUT_OPS = frozenset({"tap", "long_press", "swipe", "key", "type", "set_text"})
READ_OPS = frozenset({"read", "display", "screenshot"})

ROOT_DEVICES = "devices"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_DEX_NAME = re.compile(r"^autonom-helper-[A-Za-z0-9._-]+\.dex$")
_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"

HINT_BUSY = ("Another UiAutomation user (uiautomator2, Appium, an instrumentation test or "
             "another Autonom state directory) holds the device; stop it, or wait. Autonom "
             "tries the helper again after 60 s; until then Android calls use uiautomator "
             "and adb input.")
HINT_DEVICE = ("The helper cannot run on this device; Android calls use uiautomator and adb "
               "input until it reboots. Pass --ui-backend helper (or set "
               "AUTONOM_ANDROID_UI_BACKEND=helper) to try again now.")
HINT_TRANSIENT = ("Check the device with 'autonom devices' and retry; 'autonom ui helper "
                  "status' shows the helper. Until then Android calls use uiautomator and adb "
                  "input.")
HINT_HELPER_BUSY = ("{who} is using this device's helper, which serves one call at a time. "
                    "Nothing was sent, and the helper was neither stopped nor bypassed with a "
                    "uiautomator dump, as either would cut that call. Retry when it ends; "
                    "'autonom ui helper status' shows the helper.")
HINT_HELPER_SHARED = (" When no other Autonom state directory is using this device, the "
                      "helper has stopped answering: 'autonom ui helper stop' ends it.")
HINT_HELPER_FOREIGN = ("A helper this state directory did not start (another Autonom state "
                       "directory's, or one an earlier record lost) holds this device's helper "
                       "socket and is in a call: its hello stayed queued. Nothing was sent, "
                       "and it was not stopped, as that would cut that call. Retry when the "
                       "call ends; it stops by itself 2 minutes after its last call, and "
                       "'autonom ui helper status' lists it under other_pids.")
HINT_PREFERENCE = ("Use auto (the helper when it can serve the call), helper, or uiautomator "
                   "(today's uiautomator dump and adb input).")


class HelperUnavailable(errors.AutonomError):
    """The helper cannot serve this call; nothing was sent to it.

    `reason` is the short text a fallback reports as `fallback_reason`; the
    error itself is `android_helper_unavailable` (``--ui-backend helper``)."""

    def __init__(self, reason: str, hint: str | None = None, **extra: Any) -> None:
        super().__init__(errors.ANDROID_HELPER_UNAVAILABLE,
                         f"the Android helper cannot serve this call: {reason}",
                         hint or HINT_TRANSIENT, reason=reason, **extra)
        self.reason = reason


class HelperBusy(HelperUnavailable):
    """Another live Autonom process (or thread) holds the device's use lock
    past the wait: it is talking to the helper now. Nothing was sent and
    nothing stopped; a caller must not stop the helper or dump with
    uiautomator for it (that would cut the other call), so auto mode does
    not fall back on this. ``helper_busy`` and ``helper_owner_pid`` (when
    the owner file names it) are in the payload.

    With `shared_forward` (``tcp:<port>``) the lock was this process's, but
    the helper stayed silent while another Autonom state directory forwards
    that host port to it (`_after_silence`): that directory may be in a
    call. The payload adds ``helper_shared_forward``.

    With `foreign_pids` a start of this state directory found a helper that
    was running before its launch holding the device's socket and busy: the
    new one exited with ``socket_in_use`` and the start's hellos stayed
    queued behind that helper's call for the whole start budget. Stopping it
    would cut that call, so it was left running. (One that answers hello at
    once with another build is idle and is replaced instead.) The payload
    adds ``helper_foreign_pids``."""

    def __init__(self, waited_s: float, owner_pid: int | None, *,
                 shared_forward: str | None = None,
                 foreign_pids: list[int] | None = None) -> None:
        named = f"Autonom process {owner_pid}" if owner_pid else None
        extra: dict[str, Any] = {"backend": "helper", "helper_busy": True}
        if foreign_pids is not None:
            pids = ", ".join(str(pid) for pid in foreign_pids) or "unknown"
            reason = (f"helper_busy: a helper this state directory did not start (pid {pids}) "
                      f"holds this device's helper socket and its hello stayed queued behind "
                      f"another call; it was left running (waited {waited_s:g} s for the "
                      f"start)")
            hint = HINT_HELPER_FOREIGN
            extra["helper_foreign_pids"] = list(foreign_pids)
        elif shared_forward:
            reason = (f"helper_busy: the helper on this device did not answer while another "
                      f"Autonom state directory forwards {shared_forward} to it (waited "
                      f"{waited_s:g} s for it)")
            hint = (HINT_HELPER_BUSY.format(who="Another Autonom state directory")
                    + HINT_HELPER_SHARED)
            extra["helper_shared_forward"] = shared_forward
        else:
            reason = (f"helper_busy: {named or 'another Autonom process'} is using the helper "
                      f"on this device (waited {waited_s:g} s for it)")
            hint = HINT_HELPER_BUSY.format(who=named or "Another Autonom process")
        if owner_pid:
            extra["helper_owner_pid"] = owner_pid
        super().__init__(reason, hint, **extra)
        self.owner_pid = owner_pid
        self.waited_s = waited_s
        self.shared_forward = shared_forward
        self.foreign_pids = list(foreign_pids) if foreign_pids is not None else None


class _Unanswered(Exception):
    """A request that was written (or whose write was attempted) and got no
    reply: end of stream, reset, timeout, or a reply that is not its own."""

    def __init__(self, op: str, request_id: str, why: str, *, timed_out: bool = False) -> None:
        super().__init__(why)
        self.op = op
        self.request_id = request_id
        self.why = why
        self.timed_out = timed_out


# --- Preference ------------------------------------------------------------------

def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def preference(flag: str | None = None, *, env: Mapping[str, str] | None = None,
               session_value: str | None = None) -> str:
    """The Android UI backend asked for: `flag` (``--ui-backend``), else
    ``AUTONOM_ANDROID_UI_BACKEND``, else a session's stored Android choice,
    else ``auto``. A flag or variable outside auto|helper|uiautomator is
    ``invalid_value``; a stored value that is not an Android one (an iOS
    session's ``idb``) is ignored."""
    for value, source in ((flag, "--ui-backend"), (_env(env).get(BACKEND_ENV), BACKEND_ENV)):
        if value is None or not str(value).strip():
            continue
        word = str(value).strip().lower()
        if word not in BACKENDS:
            raise errors.AutonomError(
                errors.INVALID_VALUE,
                f"{source} {value!r} is not an Android UI backend (auto, helper, uiautomator)",
                HINT_PREFERENCE, **({"flag": source} if source.startswith("--") else
                                    {"variable": source}))
        return word
    stored = str(session_value or "").strip().lower()
    return stored if stored in BACKENDS else "auto"


# --- Device records ------------------------------------------------------------------

def _safe(serial: str) -> str:
    raw = str(serial)
    safe = _UNSAFE.sub("_", raw)[:128]
    if safe == raw:
        return safe
    import hashlib

    return f"{safe[:115]}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


def devices_dir(env: Mapping[str, str] | None = None) -> Path:
    return build_mod.helper_root(env) / ROOT_DEVICES


def record_path(serial: str, env: Mapping[str, str] | None = None) -> Path:
    return devices_dir(env) / f"{_safe(serial)}.json"


def load_record(serial: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The device record of `serial`, or {} (none, or one that does not parse)."""
    try:
        value = json.loads(record_path(serial, env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or value.get("serial") != str(serial):
        return {}
    return value


def has_record(serial: str, env: Mapping[str, str] | None = None) -> bool:
    """True when a helper was started (or found unusable) on `serial`: a file
    check, no device call."""
    return record_path(serial, env).is_file()


def _save_record(record: dict[str, Any], env: Mapping[str, str] | None) -> None:
    path = record_path(record["serial"], env)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _live_cleared(record: dict[str, Any], unchecked: int | None = None) -> dict[str, Any]:
    """The record without a running helper: only a remembered `unavailable`,
    and `unchecked`, a helper pid that `_cleanup_after_failure` could not
    check (adb could not tell whether it still runs), with this record's
    port: it stays named (INV-3), and the next call checks it again."""
    kept = {"serial": record["serial"]}
    if record.get("unavailable"):
        kept["unavailable"] = record["unavailable"]
    if isinstance(unchecked, int):
        kept["pid"] = unchecked
        if isinstance(record.get("port"), int):
            kept["port"] = record["port"]
    return kept


def _store(record: dict[str, Any], env: Mapping[str, str] | None) -> None:
    """Save `record`, or delete the file when it holds nothing but the serial."""
    if set(record) <= {"serial"}:
        try:
            record_path(record["serial"], env).unlink()
        except FileNotFoundError:
            pass
        return
    _save_record(record, env)


def forget(serial: str, env: Mapping[str, str] | None = None) -> bool:
    """Drop the record of `serial` without a device call (its device is gone,
    so the helper and its forward went with it). True when one existed."""
    path = record_path(serial, env)
    existed = path.is_file()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return existed


@contextmanager
def _device_lock(serial: str, env: Mapping[str, str] | None) -> Iterator[None]:
    import fcntl  # POSIX only, like the build lock

    directory = devices_dir(env)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / f"{_safe(serial)}.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# --- Use lock --------------------------------------------------------------------------

def use_lock_path(serial: str, env: Mapping[str, str] | None = None) -> Path:
    return devices_dir(env) / f"{_safe(serial)}.use.lock"


def use_owner_path(serial: str, env: Mapping[str, str] | None = None) -> Path:
    return devices_dir(env) / f"{_safe(serial)}.use.owner"


def use_owner(serial: str, env: Mapping[str, str] | None = None) -> int | None:
    """The pid the use lock's owner file names, or None (none, unreadable)."""
    try:
        value = json.loads(use_owner_path(serial, env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = value.get("pid") if isinstance(value, dict) else None
    return pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None


class _UseHold:
    """This process's hold of one device's use lock: a re-entrant thread
    lock, how deep the holding thread is in it, and the locked file."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.depth = 0
        self.handle: Any = None


_USE_GUARD = threading.Lock()
_USE_HOLDS: dict[str, _UseHold] = {}


def _unwritable(exc: OSError, env: Mapping[str, str] | None) -> HelperUnavailable:
    return HelperUnavailable(
        f"the helper's device record cannot be written: {exc}",
        f"Check that {devices_dir(env)} can be created and written (the Autonom state "
        f"directory); until then Android calls use uiautomator and adb input.")


@contextmanager
def use(serial: str, *, wait: float | None = None,
        env: Mapping[str, str] | None = None) -> Iterator[None]:
    """Hold the use lock of `serial` (see the module docstring) for the
    block: wait up to `wait` seconds (`USE_WAIT_S` by default) for another
    process or thread to let it go, else `HelperBusy`. Re-entrant for the
    thread that holds it. A lock file that cannot be made is
    `HelperUnavailable` (nothing was sent)."""
    budget = USE_WAIT_S if wait is None else max(0.0, float(wait))
    deadline = time.monotonic() + budget
    path = use_lock_path(serial, env)
    with _USE_GUARD:
        hold = _USE_HOLDS.setdefault(str(path), _UseHold())
    if not hold.lock.acquire(timeout=budget):
        raise HelperBusy(budget, use_owner(serial, env))  # another thread of this process
    try:
        if hold.depth == 0:
            hold.handle = _lock_use_file(serial, path, deadline, budget, env)
        hold.depth += 1
        try:
            yield
        finally:
            hold.depth -= 1
            if hold.depth == 0:
                handle, hold.handle = hold.handle, None
                _unlock_use_file(serial, handle, env)
    finally:
        hold.lock.release()


def _lock_use_file(serial: str, path: Path, deadline: float, budget: float,
                   env: Mapping[str, str] | None) -> Any:
    import fcntl  # POSIX only, like the other locks

    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = path.open("a")
    except OSError as exc:
        raise _unwritable(exc, env) from None
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise HelperBusy(budget, use_owner(serial, env)) from None
                time.sleep(min(USE_POLL_S, left))
    except BaseException as exc:
        handle.close()
        if isinstance(exc, OSError) and not isinstance(exc, BlockingIOError):
            raise _unwritable(exc, env) from None
        raise
    owner = use_owner_path(serial, env)
    temporary = owner.with_name(f".{owner.name}.{os.getpid()}.tmp")
    try:  # a courtesy for a waiter's hint: never fails the call
        temporary.write_text(json.dumps({"pid": os.getpid(), "since": _now_iso()}) + "\n",
                             encoding="utf-8")
        temporary.replace(owner)
    except OSError:
        pass
    return handle


def _unlock_use_file(serial: str, handle: Any, env: Mapping[str, str] | None) -> None:
    import fcntl

    if handle is None:
        return
    try:
        if use_owner(serial, env) == os.getpid():
            use_owner_path(serial, env).unlink()
    except OSError:
        pass
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --- adb ---------------------------------------------------------------------------

def _adb(target: Any, args: list[str], *, timeout: float = ADB_TIMEOUT_S,
         check: bool = False) -> tuple[int, str]:
    """One adb call on `target`'s serial: (exit code, output). A call that
    does not finish in `timeout` is `backend_failed`, like a failing one."""
    import subprocess

    try:
        completed = adb_mod.run_adb(target.tool, args, serial=target.target_id,
                                    timeout=timeout, check=check)
    except subprocess.TimeoutExpired:
        raise errors.AutonomError(
            errors.BACKEND_FAILED, f"adb {' '.join(args[:2])} did not finish within {timeout:g} s",
            "The device stopped responding; check 'autonom devices'.") from None
    output = completed.stdout if isinstance(completed.stdout, str) else ""
    return completed.returncode, output or ""


def _shell(target: Any, *words: str, timeout: float = ADB_TIMEOUT_S) -> tuple[int, str]:
    return _adb(target, ["shell", *words], timeout=timeout)


def _cmdline_is_helper(target: Any, pid: int) -> bool | None:
    """True when `/proc/<pid>/cmdline` starts with `autonom-helper`; False
    when the device answered that the pid runs something else, or nothing
    (`cat` exits 0 with another command line, or fails saying "No such file
    or directory" or "No such process" in whatever words its shell or cat
    uses: toybox, busybox, toolbox, mksh); None when adb could not tell:
    the call timed out, the adb process could not run, or adb itself failed
    (a line of its output starting with ``error:`` or ``adb:``, such as
    "error: device offline"), or the failure says anything else. Unknown is
    never taken for "not running" (INV-3: the record keeps naming a helper
    this state directory started)."""
    path = f"/proc/{int(pid)}/cmdline"
    try:
        code, output = _shell(target, "cat", path)
    except (errors.AutonomError, OSError):
        return None
    if code == 0:
        return output.split("\0", 1)[0].strip() == PROCESS
    if any(line.strip().startswith(("error:", "adb:")) for line in output.splitlines()):
        return None
    if "No such file or directory" in output or "No such process" in output:
        return False
    return None


def _kill_verified(target: Any, pid: Any) -> dict[str, Any]:
    """`kill <pid>` only when that pid is still the helper (INV-2).
    ``verification`` says why nothing was killed: ``not_running`` (the pid is
    not the helper) or ``unreadable`` (adb could not tell, `_unverified`)."""
    if not isinstance(pid, int) or pid <= 0:
        return {"pid": None, "killed": False}
    verdict = _cmdline_is_helper(target, pid)
    if verdict is not True:
        return {"pid": pid, "killed": False,
                "verification": "not_running" if verdict is False else "unreadable"}
    _shell(target, "kill", str(pid))
    return {"pid": pid, "killed": True}


def _unverified(pid: int, what: str, *, kept: bool = True) -> errors.AutonomError:
    """``backend_failed`` for a helper pid adb could not check (a failed or
    timed-out `cat /proc/<pid>/cmdline`): nothing was killed and, for a
    recorded pid (``kept``), the device record still names it (INV-3), so a
    later call checks it again. ``what`` is the step that needed the
    check."""
    return errors.AutonomError(
        errors.BACKEND_FAILED,
        f"adb could not tell whether pid {pid} is still the Android helper, so {what} "
        "killed nothing" + ("; the device record keeps the pid" if kept else ""),
        "Check the device with 'autonom devices'; once adb answers, 'autonom ui helper "
        "stop' ends the helper.")


def _wait_gone(target: Any, pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if _cmdline_is_helper(target, pid) is False:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def _helper_pids(target: Any) -> list[int]:
    try:
        code, output = _shell(target, "pidof", PROCESS)
    except errors.AutonomError:
        return []
    return [int(word) for word in output.split() if word.isdigit()] if code == 0 else []


def _forward_list(target: Any) -> list[tuple[str, str, str]] | None:
    """The rows of `adb forward --list` (every serial's), or None when adb
    failed."""
    try:
        code, output = _adb(target, ["forward", "--list"])
    except errors.AutonomError:
        return None
    if code != 0:
        return None
    rows = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 3:
            rows.append((parts[0], parts[1], parts[2]))
    return rows


def _forward_rows(target: Any) -> list[tuple[str, str, str]]:
    return _forward_list(target) or []


# Host ports this process has seen forwarded to a device's helper socket
# (`<serial> tcp:<port> localabstract:autonom-helper` in `adb forward
# --list`), per state directory and serial. A recorded port is used only when
# it is in there (`_forward_confirmed`): hello names no device, and after an
# adb server restart a reused host port could reach another device's helper,
# whose taps this device's calls would then send. The list is read once per
# process and device (and again on each start, `_own_forward_listed`); a
# forward this process makes counts at once, one it removes no longer does.
# `ui_android.reset` empties it at the start of each CLI invocation, and a
# reading older than `FORWARD_CONFIRM_TTL_S` is read again, so a long-lived
# process (the Mobile Canvas bridge, a long Flow run) does not trust a port
# for longer than that after an adb server restart.
FORWARD_CONFIRM_TTL_S = 30.0
_CONFIRMED: dict[tuple[str, str], tuple[set[int], float]] = {}
_CONFIRMED_GUARD = threading.Lock()


def reset_forward_cache() -> None:
    """Forget every forward this process saw (a new CLI invocation)."""
    with _CONFIRMED_GUARD:
        _CONFIRMED.clear()


def _confirmed_key(target: Any, env: Mapping[str, str] | None) -> tuple[str, str]:
    return str(devices_dir(env)), str(target.target_id)


def _helper_ports(target: Any, rows: list[tuple[str, str, str]]) -> set[int]:
    """The host ports `rows` forward to this target's helper socket."""
    ports = set()
    for serial, local, remote in rows:
        if (serial == target.target_id and remote == f"localabstract:{SOCKET}"
                and local.startswith("tcp:") and local[4:].isdigit()):
            ports.add(int(local[4:]))
    return ports


def _note_forwards(target: Any, rows: list[tuple[str, str, str]],
                   env: Mapping[str, str] | None) -> None:
    with _CONFIRMED_GUARD:
        _CONFIRMED[_confirmed_key(target, env)] = (_helper_ports(target, rows),
                                                   time.monotonic())


def _note_forward(target: Any, port: int, env: Mapping[str, str] | None, *,
                  present: bool) -> None:
    """Record that this process made (``present``) or removed the forward of
    host ``port`` to this target's helper socket."""
    with _CONFIRMED_GUARD:
        held = _CONFIRMED.get(_confirmed_key(target, env))
        if held is not None:
            (held[0].add if present else held[0].discard)(int(port))
        elif present:
            _CONFIRMED[_confirmed_key(target, env)] = ({int(port)}, time.monotonic())


def _forward_confirmed(target: Any, port: int, env: Mapping[str, str] | None) -> bool:
    """Is host ``port`` this target's serial's forward to the helper socket?
    One `forward --list` per process and device (again once the reading is
    older than `FORWARD_CONFIRM_TTL_S`); a failed read confirms nothing (and
    is not kept)."""
    key = _confirmed_key(target, env)
    with _CONFIRMED_GUARD:
        held = _CONFIRMED.get(key)
        if held is not None and time.monotonic() - held[1] < FORWARD_CONFIRM_TTL_S:
            return int(port) in held[0]
    rows = _forward_list(target)
    if rows is None:
        return False
    _note_forwards(target, rows, env)
    return int(port) in _helper_ports(target, rows)


def _own_forward_listed(target: Any, port: int, env: Mapping[str, str] | None = None) -> bool:
    """`forward --list` shows `<serial> tcp:<port> localabstract:autonom-helper`
    now (a fresh read, which also refreshes what `_forward_confirmed` knows)."""
    rows = _forward_list(target)
    if rows is None:
        return False
    _note_forwards(target, rows, env)
    return int(port) in _helper_ports(target, rows)


def _shared_forward(target: Any, port: Any) -> str | None:
    """The first other host port (``tcp:<port>``) that `forward --list`
    shows forwarded to this device's helper socket, besides this record's
    `port`; None when there is none. That is how another Autonom state
    directory, which this one's use lock does not keep out, talks to the
    same helper."""
    own = f"tcp:{int(port)}" if isinstance(port, int) else None
    for serial, local, remote in _forward_rows(target):
        if serial == target.target_id and remote == f"localabstract:{SOCKET}" and local != own:
            return local
    return None


def _new_forward(target: Any, env: Mapping[str, str] | None = None) -> int:
    """A fresh `adb forward tcp:0 localabstract:autonom-helper` for this
    target's serial; its host port."""
    code, output = _adb(target, ["forward", "tcp:0", f"localabstract:{SOCKET}"])
    word = output.strip().splitlines()[-1].strip() if output.strip() else ""
    if code != 0 or not word.isdigit() or int(word) <= 0:
        raise HelperUnavailable(f"adb forward did not give a port ({(output.strip() or code)!s:.120})")
    _note_forward(target, int(word), env, present=True)
    return int(word)


def _remove_own_forward(target: Any, port: Any, env: Mapping[str, str] | None) -> bool | None:
    """`adb forward --remove tcp:<port>` only while `forward --list` shows
    that port for this serial's helper socket (never another forward, never
    `--remove-all`): True when removed, False when the removal failed, None
    when it was not listed."""
    if not isinstance(port, int) or not _own_forward_listed(target, port, env):
        return None
    code, _ = _adb(target, ["forward", "--remove", f"tcp:{port}"])
    _note_forward(target, port, env, present=False)
    return code == 0


def _boot_id(target: Any) -> str | None:
    try:
        code, output = _shell(target, "cat", _BOOT_ID_PATH, timeout=10)
    except errors.AutonomError:
        return None
    value = output.strip().lower()
    return value if code == 0 and re.fullmatch(r"[0-9a-f-]{8,64}", value) else None


def _sdk_int(target: Any) -> int | None:
    try:
        code, output = _shell(target, "getprop", "ro.build.version.sdk", timeout=10)
    except errors.AutonomError:
        return None
    word = output.strip()
    return int(word) if code == 0 and word.isdigit() else None


# --- Connection ------------------------------------------------------------------------

# The op this thread last wrote to a helper, and when (``time.monotonic()``,
# just before the write): a caller that times what follows an input (the
# observe budget) counts from the request itself, not from the helper start
# or the hello before it.
_LAST_WRITE = threading.local()


def _request_id() -> str:
    return os.urandom(16).hex()


def last_write() -> tuple[str, float] | None:
    """``(op, monotonic time)`` of this thread's latest request written to a
    helper, or None."""
    return getattr(_LAST_WRITE, "value", None)


def timeout_for(op: str, args: Mapping[str, Any] | None = None) -> float:
    """The host timeout of `op`: the design's table, plus the gesture's own
    duration for a long press or a swipe."""
    base = TIMEOUTS_S.get(op, DEFAULT_TIMEOUT_S)
    if op in ("long_press", "swipe") and args:
        duration = args.get("duration_ms")
        if isinstance(duration, (int, float)) and duration > 0:
            base += float(duration) / 1000.0
    return base


class Connection:
    """One TCP connection to the helper through the adb forward."""

    def __init__(self, port: int, *, timeout: float = CONNECT_TIMEOUT_S) -> None:
        self.port = int(port)
        self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        self._buffer = b""
        self.sent: list[dict[str, str]] = []

    def call(self, op: str, *, timeout: float | None = None, request_id: str | None = None,
             **args: Any) -> dict[str, Any]:
        """Send one request and read its reply. Raises `_Unanswered` once the
        write was attempted and no reply of this id came back."""
        rid = request_id or _request_id()
        wait = timeout if timeout is not None else timeout_for(op, args)
        line = json.dumps({"id": rid, "op": op, **args}, ensure_ascii=False,
                          separators=(",", ":")) + "\n"
        self.sent.append({"id": rid, "op": op})
        deadline = time.monotonic() + wait
        _LAST_WRITE.value = (op, time.monotonic())
        try:
            self.sock.settimeout(max(0.05, wait))
            self.sock.sendall(line.encode("utf-8"))
        except OSError as exc:
            raise _Unanswered(op, rid, f"the request could not be written: {exc}") from exc
        raw = self._readline(op, rid, deadline, wait)
        try:
            reply = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise _Unanswered(op, rid, "the reply is not JSON") from exc
        if not isinstance(reply, dict) or reply.get("id") != rid:
            raise _Unanswered(op, rid, "the reply is not the one of this request")
        return reply

    def _readline(self, op: str, rid: str, deadline: float, wait: float) -> bytes:
        while b"\n" not in self._buffer:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _Unanswered(op, rid, f"no reply within {wait:g} s", timed_out=True)
            try:
                self.sock.settimeout(left)
                chunk = self.sock.recv(65536)
            except socket.timeout as exc:
                raise _Unanswered(op, rid, f"no reply within {wait:g} s",
                                  timed_out=True) from exc
            except OSError as exc:
                raise _Unanswered(op, rid, f"the connection failed: {exc}") from exc
            if not chunk:
                raise _Unanswered(op, rid, "the helper closed the connection")
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Helper:
    """A connection whose hello answered with protocol 1 and the expected
    build. `started` says this call started the helper; `replaced` that a
    stale or other-build helper was stopped first."""

    def __init__(self, target: Any, connection: Connection, hello: dict[str, Any], *,
                 started: bool, replaced: bool, env: Mapping[str, str] | None) -> None:
        self.target = target
        self.connection = connection
        self.hello = hello
        self.build = hello.get("build")
        self.pid = hello.get("pid")
        self.sdk_int = hello.get("sdk_int")
        self.port = connection.port
        self.started = started
        self.replaced = replaced
        self.env = env

    def call(self, op: str, *, timeout: float | None = None, **args: Any) -> dict[str, Any]:
        """One request on this connection. An input (any op that is not a
        read) left unanswered closes the connection and raises
        ``ui_action_uncertain``; a read left unanswered raises `_Unanswered`
        and leaves the connection to the caller, who must not send on it
        again and closes it (`request` holds it until that helper is
        restarted, so the helper serves no other caller meanwhile)."""
        try:
            return self.connection.call(op, timeout=timeout, **args)
        except _Unanswered as exc:
            if op not in READ_OPS:  # an input, or any op not known to be a read
                self.close()
                raise action_uncertain(exc) from None
            raise

    def provenance(self) -> dict[str, Any]:
        return {"build": self.build, "pid": self.pid, "port": self.port,
                "started": self.started, "replaced": self.replaced}

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Helper":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def action_uncertain(exc: _Unanswered) -> errors.AutonomError:
    """An input that reached the helper's socket and was not acknowledged
    (AH-06): it may have happened, and it is never sent again."""
    return errors.AutonomError(
        errors.UI_ACTION_UNCERTAIN,
        f"the {exc.op} was written to the Android helper and not acknowledged "
        f"({exc.why}); it may or may not have happened",
        "Read the screen with 'autonom ui tree' before doing anything else; Autonom never "
        "sends this input again.",
        request_id=exc.request_id, op=exc.op, backend="helper")


# --- hello outcomes ------------------------------------------------------------------

class _Hello:
    """What one hello attempt on a port found."""

    def __init__(self, state: str, reply: dict[str, Any] | None = None,
                 connection: Connection | None = None, why: str = "") -> None:
        # ready | other_build | busy | failed | dead | hung
        self.state = state
        self.reply = reply or {}
        self.connection = connection
        self.why = why


def _hello(port: int, expected: str | None, *, timeout: float | None = None) -> _Hello:
    """One hello on `port`, answered within `timeout` (the hello timeout by
    default). ``hung``: no answer in time (under the use lock that means
    stuck, unless `_after_silence` finds another state directory's
    forward); ``dead``: refused, or the end of the stream."""
    try:
        connection = Connection(port)
    except OSError as exc:
        return _Hello("dead", why=f"connection refused ({exc})")
    try:
        reply = connection.call("hello",
                                timeout=TIMEOUTS_S["hello"] if timeout is None else timeout)
    except _Unanswered as exc:
        connection.close()
        return _Hello("hung" if exc.timed_out else "dead", why=exc.why)
    if reply.get("ok") is not True:
        connection.close()
        code = str((reply.get("error") or {}).get("code") or "")
        message = str((reply.get("error") or {}).get("message") or "")
        state = {"uiautomation_busy": "busy", "uiautomation_failed": "failed"}.get(code, "dead")
        return _Hello(state, reply, why=f"{code}: {message}".strip(": "))
    if reply.get("protocol") != PROTOCOL or (expected is not None and reply.get("build") != expected):
        return _Hello("other_build", reply, connection,
                      why=f"protocol {reply.get('protocol')}, build {reply.get('build')}")
    return _Hello("ready", reply, connection)


def _after_silence(target: Any, port: int, expected: str | None, first: _Hello, *,
                   refuse: bool = True) -> _Hello:
    """What to make of `first`, a hello sent on `port` while this process
    holds the use lock that got no answer (``hung``).

    With no other host port forwarded to this device's helper socket
    (`_shared_forward`), nobody else can be talking to the helper: it is
    stuck, and `first` comes back as it is (the caller stops it). With one,
    the silence may be a call of another Autonom state directory, which this
    directory's lock does not keep out: hello is sent once more and waited
    for up to `USE_WAIT_S`, as a held lock is waited for, and what it finds
    comes back. Still no answer: `HelperBusy` (nothing sent, nothing
    stopped), or, with `refuse` false (a stop, which must end the helper
    whatever it does), the hung outcome."""
    shared = _shared_forward(target, port)
    if shared is None:
        return first
    outcome = _hello(port, expected, timeout=USE_WAIT_S)
    if outcome.state == "hung" and refuse:
        raise HelperBusy(TIMEOUTS_S["hello"] + USE_WAIT_S, None, shared_forward=shared)
    return outcome


# --- Negative cache -----------------------------------------------------------------

def _remember(record: dict[str, Any], target: Any, code: str, reason: str, hint: str, *,
              per_boot: bool) -> dict[str, Any]:
    entry: dict[str, Any] = {"code": code, "reason": reason, "hint": hint, "at": _now_iso()}
    if per_boot:
        boot = _boot_id(target)
        entry["boot_id"] = boot
        if boot is None:
            entry["until"] = time.time() + NO_BOOT_ID_TTL_S
    else:
        entry["until"] = time.time() + BUSY_TTL_S
    record["unavailable"] = entry
    return entry


def _remembered_without_device(record: dict[str, Any]) -> dict[str, Any] | None:
    """A remembered entry that holds by time alone (busy, or no boot id)."""
    entry = record.get("unavailable")
    if not isinstance(entry, dict):
        return None
    until = entry.get("until")
    if isinstance(until, (int, float)):
        return entry if until > time.time() else None
    return None


def _remembered_for_boot(record: dict[str, Any], target: Any) -> dict[str, Any] | None:
    entry = record.get("unavailable")
    if not isinstance(entry, dict) or not entry.get("boot_id"):
        return None
    return entry if _boot_id(target) == entry["boot_id"] else None


def _refuse_remembered(entry: dict[str, Any]) -> HelperUnavailable:
    return HelperUnavailable(f"remembered: {entry.get('reason')}", entry.get("hint"),
                             remembered=True)


# --- ensure ----------------------------------------------------------------------------

def ensure(target: Any, *, force: bool = False, env: Mapping[str, str] | None = None,
           use_wait: float | None = None) -> Helper:
    """A `Helper` on `target`, started when none answers. Raises
    `HelperUnavailable` (nothing sent) when it cannot be had, `HelperBusy`
    when another process holds the use lock past `use_wait`; see the module
    docstring for the order of checks. `force` ignores a remembered entry.

    The hello runs under the use lock; a caller that keeps talking to the
    returned helper holds `use` around it (as `request` does), or the lock
    is let go when this returns."""
    built = _prepare(target, force=force, env=env)
    with use(target.target_id, wait=use_wait, env=env):
        return _reach(target, built, force=force, env=env)


def _prepare(target: Any, *, force: bool, env: Mapping[str, str] | None) -> Any:
    """Checks 1 and 2 (the record, the toolchain and the build): no device
    call, no use lock, so a first build never holds up another caller."""
    record = load_record(target.target_id, env)
    if not force:
        entry = _remembered_without_device(record)
        if entry is not None:
            raise _refuse_remembered(entry)
    built = build_mod.ensure(adb_path=target.tool, env=env)
    if not built.ok:
        raise HelperUnavailable(str(built.reason), built.hint)
    return built


def _reach(target: Any, built: Any, *, force: bool, env: Mapping[str, str] | None) -> Helper:
    """Check 4, under the use lock: the device calls of `ensure`."""
    try:
        return _ensure_on_device(target, built, force=force, env=env)
    except HelperUnavailable:
        raise
    except errors.AutonomError as exc:
        # An adb failure while reaching or starting the helper: no request
        # was written, so the call may still be served another way.
        raise HelperUnavailable(f"adb failed: {exc.message[:200]}") from None
    except OSError as exc:  # the device record or its lock cannot be written
        raise _unwritable(exc, env) from None


def _ensure_on_device(target: Any, built: Any, *, force: bool,
                      env: Mapping[str, str] | None) -> Helper:
    serial = target.target_id
    with _device_lock(serial, env):
        record = load_record(serial, env) or {"serial": serial}
        record["serial"] = serial
        if record.get("unavailable"):
            entry = (_remembered_without_device(record)
                     or _remembered_for_boot(record, target))
            if entry is not None and not force:
                raise _refuse_remembered(entry)
            record.pop("unavailable", None)
            _store(record, env)
        replaced = False
        port = record.get("port")
        if isinstance(port, int) and not _forward_confirmed(target, port, env):
            # The recorded host port is not this serial's forward to the
            # helper (an adb server restart dropped it; the port may now
            # forward to another device's helper, and hello names no device):
            # it is never used, nor removed (it is not ours now). A fresh
            # forward for this serial reaches this device's helper, if one
            # still runs; else the start below makes it.
            port = record["port"] = _new_forward(target, env)
            _save_record(record, env)  # the new forward is this record's
        if isinstance(port, int):
            outcome = _hello(port, built.build_id)
            if outcome.state == "hung":
                # waited for (or HelperBusy) only while another state
                # directory forwards to this device's helper
                outcome = _after_silence(target, port, built.build_id, outcome)
            if outcome.state == "ready":
                return _ready(target, record, outcome, started=False, replaced=False, env=env)
            if outcome.state in ("busy", "failed"):
                _give_up(target, record, outcome, env)
            replaced = True
            if outcome.state == "hung":
                # The use lock is held and no other state directory forwards
                # to the helper, so nobody else is talking to it: a helper
                # that does not answer hello in time is stuck. It is
                # stopped, then started once (AH-05).
                _end_stuck(target, record, env)
            else:
                _retire(target, record, outcome)
        elif isinstance(record.get("pid"), int):
            # A pid kept without a port: a failed start could not check it
            # (`_cleanup_after_failure`). It is checked again (and ended
            # when it is still the helper) before anything is started, so
            # no second helper starts beside it and the record keeps naming
            # it while adb cannot tell (INV-3).
            _end_unchecked(target, record, env)
        return _start(target, record, built, replaced=replaced, env=env)


def _ready(target: Any, record: dict[str, Any], outcome: _Hello, *, started: bool,
           replaced: bool, env: Mapping[str, str] | None) -> Helper:
    assert outcome.connection is not None
    pid = outcome.reply.get("pid")
    record["pid"] = pid if isinstance(pid, int) else record.get("pid")
    record["build"] = outcome.reply.get("build")
    record["port"] = outcome.connection.port
    record["last_request_at"] = time.time()
    record.pop("unavailable", None)
    try:
        _save_record(record, env)
    except BaseException:
        outcome.connection.close()
        raise
    return Helper(target, outcome.connection, outcome.reply, started=started,
                  replaced=replaced, env=env)


def _give_up(target: Any, record: dict[str, Any], outcome: _Hello,
             env: Mapping[str, str] | None, *, before: frozenset[int] | None = None) -> None:
    """A hello refused with uiautomation_busy or uiautomation_failed: the
    helper exits after that reply. Remember it and stop. ``before``: during
    a start, the helper pids that ran before its launch (never ended here);
    outside a start the helper is the one its hello named, else the
    recorded one."""
    busy = outcome.state == "busy"
    hint = HINT_BUSY if busy else HINT_DEVICE
    reason = outcome.why or ("uiautomation_busy" if busy else "uiautomation_failed")
    pid = outcome.reply.get("pid")
    if before is None and not isinstance(pid, int):
        pid = record.get("pid")
    _survivors, unchecked = _cleanup_after_failure(target, record, pid, before, env)
    _remember(record, target, "uiautomation_busy" if busy else "uiautomation_failed",
              reason, hint, per_boot=not busy)
    _store(_live_cleared(record, unchecked), env)
    raise _cleaned_up(HelperUnavailable(reason, hint))


def _retire(target: Any, record: dict[str, Any], outcome: _Hello) -> None:
    """Stop a helper that must not serve: another build or protocol (quit on
    its own connection, then its verified pid), or a dead one (its recorded
    pid, if that process is still the helper).

    When adb cannot tell whether that pid is still the helper, nothing is
    killed and `_unverified` is raised before anything is started: the
    record is left as it is (a recorded pid stays named, INV-3), and no
    second helper is started beside one that may still hold UiAutomation.
    During a start that helper ran before the launch (it is not this
    record's), so the start then ends as any failed start does."""
    pid = outcome.reply.get("pid")
    if not isinstance(pid, int):
        pid = record.get("pid")
    other_build = outcome.state == "other_build"
    if other_build and outcome.connection is not None:
        try:
            outcome.connection.call("quit")
        except _Unanswered:
            pass
        finally:
            outcome.connection.close()
        if isinstance(pid, int) and _wait_gone(target, pid, QUIT_EXIT_WAIT_S):
            return
    killed = _kill_verified(target, pid)
    if killed.get("verification") == "unreadable":
        raise _unverified(killed["pid"], "replacing the helper of another build" if other_build
                          else "ending the helper that stopped answering",
                          kept=record.get("pid") == killed["pid"])


def _end_unchecked(target: Any, record: dict[str, Any], env: Mapping[str, str] | None) -> None:
    """The recorded pid that a failed start could not check, with no port
    left to reach it: killed when it is still the helper (INV-2), then
    dropped from the record. When adb still cannot tell, nothing is killed,
    the record keeps the pid (INV-3) and `_unverified` is raised, so nothing
    is started beside it."""
    killed = _kill_verified(target, record.get("pid"))
    if killed.get("verification") == "unreadable":
        raise _unverified(killed["pid"], "starting the helper")
    record.pop("pid", None)
    _save_record(record, env)


def _end_stuck(target: Any, record: dict[str, Any], env: Mapping[str, str] | None) -> None:
    """Stop a recorded helper that did not answer hello while the use lock
    was held (stuck): a quit needs an answered hello, so its recorded pid is
    killed when it is still the helper (INV-2), and this record's forward is
    removed when listed. The record keeps neither.

    When adb cannot tell whether the recorded pid is still the helper,
    nothing is killed or removed, the record is left as it is (it still
    names the pid, INV-3) and `_unverified` is raised: no second helper is
    started beside one that may still hold UiAutomation, and the call fails
    or falls back as for a helper that cannot be reached."""
    outcome = _kill_verified(target, record.get("pid"))
    if outcome.get("verification") == "unreadable":
        raise _unverified(outcome["pid"], "ending the helper that did not answer")
    _remove_own_forward(target, record.get("port"), env)
    for key in ("pid", "port"):
        record.pop(key, None)
    _save_record(record, env)


def _cleanup_after_failure(target: Any, record: dict[str, Any], pid: Any,
                           before: frozenset[int] | None = None,
                           env: Mapping[str, str] | None = None
                           ) -> tuple[list[int], int | None]:
    """After a start (or a recorded helper's hello) that did not end ready:
    the helper of the attempt is killed while it is still `autonom-helper`
    (INV-2) and this record's forward removed, so nothing of the attempt
    holds UiAutomation or a host port.

    That helper is ``pid`` when its hello named it; else, during a start,
    every pid `pidof` shows now that it did not show before the launch
    (``before``). A helper that was running before the launch is never
    killed here, as another Autonom state directory may have started it and
    be in a call with it. Returns those that still run (found by that
    `pidof`; empty when ``pid`` named the helper), for the caller to
    report, and the first helper pid adb could not check (None when every
    check answered): it is not killed, this record's forward is not
    removed, and the caller keeps both in the record (`_live_cleared`), so
    the record still names it (INV-3) and the next call checks it again."""
    survivors: list[int] = []
    if isinstance(pid, int):
        candidates = [] if before is not None and pid in before else [pid]
    elif before is not None:
        now = _helper_pids(target)
        survivors = [candidate for candidate in now if candidate in before]
        candidates = [candidate for candidate in now if candidate not in before]
    else:
        candidates = []
    unchecked: int | None = None
    for candidate in candidates:
        killed = _kill_verified(target, candidate)
        if killed.get("verification") == "unreadable" and unchecked is None:
            unchecked = candidate
    if unchecked is None:
        _remove_own_forward(target, record.get("port"), env)
    return survivors, unchecked


def _cleaned_up(error: BaseException) -> BaseException:
    """Mark `error` as raised after `_cleanup_after_failure` already ran, so
    `_start` does not end the attempt a second time."""
    error._helper_cleaned_up = True  # type: ignore[attr-defined]
    return error


def _abandon_start(target: Any, record: dict[str, Any], env: Mapping[str, str] | None,
                   before: frozenset[int]) -> None:
    """A start that failed after its launch for any other reason (an adb
    failure or timeout, no forward port, a device record that cannot be
    written): what it may have started is ended as after a start timeout
    (the verified helper pids that were not running before the launch,
    ``before``; this record's forward), and the record keeps no pid or port
    of it, except a helper pid adb could not check (with this record's
    port), which it keeps naming (INV-3). Nothing is remembered (the cause
    may pass). A failure here is not reported: the first one is."""
    unchecked = None
    try:
        _survivors, unchecked = _cleanup_after_failure(target, record, None, before, env)
    except Exception:  # noqa: BLE001 - never hide the error that ended the start
        pass
    try:
        _store(_live_cleared(record, unchecked), env)
    except OSError:
        pass


def _start(target: Any, record: dict[str, Any], built: Any, *, replaced: bool,
           env: Mapping[str, str] | None) -> Helper:
    build = str(built.build_id)
    sdk = _sdk_int(target)
    if sdk is not None and sdk < MIN_SDK:
        reason = f"device API {sdk} is below {MIN_SDK}"
        _remember(record, target, "api_too_low", reason, HINT_DEVICE, per_boot=True)
        _store(_live_cleared(record), env)
        raise HelperUnavailable(reason, HINT_DEVICE)
    dex_name = f"{DEX_PREFIX}{build}.dex"
    dex_device = f"{DEVICE_DIR}/{dex_name}"
    try:
        _adb(target, ["push", str(built.dex), dex_device], timeout=PUSH_TIMEOUT_S, check=True)
    except errors.AutonomError as exc:
        raise HelperUnavailable(f"adb push of the helper failed: {exc.message[:200]}") from None
    _remove_other_builds(target, dex_name)
    # The helpers already running belong to someone else (another Autonom
    # state directory, or one an earlier record lost): a start that fails
    # never ends them, only what its own launch added.
    before = frozenset(_helper_pids(target))
    # From the launch on, a helper may run that holds UiAutomation: every way
    # out that does not end ready ends it again (INV-3), so no helper is left
    # that no record names and `stop` could not reach.
    try:
        return _launch_and_greet(target, record, build, dex_device, before,
                                 replaced=replaced, env=env)
    except BaseException as exc:
        if not getattr(exc, "_helper_cleaned_up", False):
            _abandon_start(target, record, env, before)
        raise


def _launch_and_greet(target: Any, record: dict[str, Any], build: str, dex_device: str,
                      before: frozenset[int], *, replaced: bool,
                      env: Mapping[str, str] | None) -> Helper:
    """Launch the helper and poll hello for up to `START_BUDGET_S`.
    ``before``: the helper pids `pidof` showed before the launch.

    A helper that was running before the launch and holds the socket (the
    new one then exits with ``socket_in_use``) is used when it answers with
    the expected build. One that answers hello at once with another build
    or protocol is idle (a helper in a call leaves hello queued), so it is
    replaced as AH-03 asks, whoever started it: quit on its own connection,
    its verified pid killed only when it is still there after the quit, and
    the expected build launched again with a fresh start budget. That
    happens once per start (AH-05): a second helper of another build on the
    socket ends the start as unavailable. A hello that stays queued behind
    another call until the start budget ends means that helper is busy: it
    is not stopped, what the launch added is ended and `HelperBusy`
    (``helper_foreign_pids``) is raised, nothing remembered."""
    serial = target.target_id
    _launch(target, dex_device, build)
    port = record.get("port")
    if not (isinstance(port, int) and _own_forward_listed(target, port, env)):
        port = _new_forward(target, env)
    record.update({"serial": serial, "port": port, "pid": None, "build": build,
                   "started_at": _now_iso()})
    _save_record(record, env)
    began = time.monotonic()
    deadline = began + START_BUDGET_S
    swapped = False  # a helper of another build was quit and ours launched again
    while True:
        last = _hello(port, build)
        if last.state == "ready":
            return _ready(target, record, last, started=True, replaced=replaced or swapped,
                          env=env)
        if last.state in ("busy", "failed"):
            _give_up(target, record, last, env, before=before)
        if last.state == "other_build" and not swapped:
            # It answered at once, so it is in no call: quit it (then its
            # verified pid, only if it stays) and start the expected build.
            _retire(target, record, last)
            swapped = True
            _launch(target, dex_device, build)
            deadline = time.monotonic() + START_BUDGET_S
            continue
        if last.state == "other_build":
            # Another helper of another build holds the socket after the
            # first was replaced: no second replacement (AH-05). What our
            # launches added is ended as after a start timeout (that helper
            # too, when one of them is it); one that ran before is left.
            if last.connection is not None:
                last.connection.close()
            _survivors, unchecked = _cleanup_after_failure(target, record, None, before, env)
            _store(_live_cleared(record, unchecked), env)
            raise _cleaned_up(HelperUnavailable(
                f"a helper of another build ({last.why}) took the helper socket again after "
                f"one was replaced; it is not replaced twice in one start"))
        if time.monotonic() >= deadline:
            break
        time.sleep(START_POLL_S)
    survivors, unchecked = _cleanup_after_failure(target, record, None, before, env)
    if survivors:
        # A helper that ran before the launch is still there: it holds the
        # socket (the new one exited, or the hello queued behind its call).
        _store(_live_cleared(record, unchecked), env)
        raise _cleaned_up(HelperBusy(START_BUDGET_S, None, foreign_pids=survivors))
    reason = f"the helper did not answer hello within {START_BUDGET_S:g} s of its start ({last.why})"
    _remember(record, target, "start_timeout", reason, HINT_DEVICE, per_boot=True)
    _store(_live_cleared(record, unchecked), env)
    raise _cleaned_up(HelperUnavailable(reason, HINT_DEVICE))


def _launch(target: Any, dex_device: str, build: str) -> None:
    command = (f"CLASSPATH={shlex.quote(dex_device)} nohup app_process /system/bin "
               f"--nice-name={PROCESS} {MAIN_CLASS} --build {shlex.quote(build)} "
               f"--idle-ms {IDLE_MS} --socket {SOCKET} >{DEVICE_LOG} 2>&1 &")
    code, output = _shell(target, command)
    if code != 0:
        raise HelperUnavailable(f"the helper did not start: {output.strip()[:200]}")


def _remove_other_builds(target: Any, keep: str) -> list[str]:
    """Every other `autonom-helper-*.dex` in /data/local/tmp, by exact name."""
    try:
        code, output = _shell(target, "ls", DEVICE_DIR)
    except errors.AutonomError:
        return []
    removed = []
    for name in output.split() if code == 0 else []:
        if name != keep and _DEX_NAME.match(name):
            _shell(target, "rm", "-f", f"{DEVICE_DIR}/{name}")
            removed.append(name)
    return removed


# --- request ---------------------------------------------------------------------------

def request(target: Any, op: str, *, force: bool = False, timeout: float | None = None,
            env: Mapping[str, str] | None = None, use_wait: float | None = None,
            **args: Any) -> dict[str, Any]:
    """`op` on the helper of `target`, its reply (``ok`` true or false) plus
    ``helper`` (build, pid, port, started, replaced, retried).

    The use lock is held from before the hello until the last reply (the
    retry included); `HelperBusy` (waited `use_wait`, `USE_WAIT_S` by
    default) means another process holds it and nothing was sent or
    stopped. Before anything is written the helper may be started or
    replaced once (`ensure`); `HelperUnavailable` means nothing was sent. An
    input written and not answered raises ``ui_action_uncertain`` and is
    never sent again. A read written and not answered is sent once more to a
    restarted helper; unanswered again, it is `HelperUnavailable`. The
    restart ends only the helper that left the read unanswered (`restart`):
    when another caller replaced it meanwhile, the read goes to that
    caller's helper and nothing is killed."""
    if op in ("hello", "quit"):
        raise ValueError(f"{op} is sent by the client itself")
    built = _prepare(target, force=force, env=env)
    with use(target.target_id, wait=use_wait, env=env):
        helper = _reach(target, built, force=force, env=env)
        try:
            reply = helper.call(op, timeout=timeout, **args)
            return {**reply, "helper": {**helper.provenance(), "retried": False}}
        except _Unanswered as exc:  # a read: inputs raised ui_action_uncertain
            first = exc
            # Still holding the failed connection, so that helper serves no
            # other caller before it is ended.
            try:
                restart(target, helper.pid, env=env)
            except errors.AutonomError as failure:
                raise HelperUnavailable(f"{op} was not answered ({first.why}) and the helper "
                                        f"could not be restarted: {failure.message[:200]}"
                                        ) from None
            except OSError as failure:  # the device record or its lock
                raise HelperUnavailable(f"{op} was not answered ({first.why}) and the helper's "
                                        f"device record cannot be written: {failure}") from None
        finally:
            helper.close()
        helper = ensure(target, force=force, env=env)
        try:
            reply = helper.call(op, timeout=timeout, **args)
            return {**reply, "helper": {**helper.provenance(), "retried": True}}
        except _Unanswered as again:
            raise HelperUnavailable(
                f"{op} was not answered twice ({first.why}; then {again.why})") from None
        finally:
            helper.close()


def restart(target: Any, pid: Any, *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """End the helper that did not answer, without asking it. `pid` is that
    helper's own (from its hello). It is killed only while the device record
    still names it (and `/proc/<pid>/cmdline` is still the helper, INV-2):
    a record that names another pid, or none, means another caller stopped
    or replaced that helper meanwhile, and the helper the record names now
    may be serving that caller, so nothing is killed (``skipped``). The
    forward stays for the next start to reuse; the next `ensure` starts a
    helper only when none answers. Runs under the use lock (`request`
    already holds it). When adb cannot tell whether `pid` is still the
    helper, nothing is killed, the record keeps the pid (INV-3) and
    `_unverified` is raised, so `request` gives up on the helper
    (`HelperUnavailable`: auto falls back) instead of starting another."""
    serial = target.target_id
    if not isinstance(pid, int) or pid <= 0:
        return {"pid": None, "killed": False, "skipped": "pid_unknown"}
    with use(serial, env=env), _device_lock(serial, env):
        record = load_record(serial, env)
        if not record:
            return {"pid": pid, "killed": False, "skipped": "no_record"}
        if record.get("pid") != pid:
            return {"pid": pid, "killed": False, "skipped": "record_changed",
                    "record_pid": record.get("pid")}
        outcome = _kill_verified(target, pid)
        if outcome.get("verification") == "unreadable":
            raise _unverified(pid, "the restart")
        record.pop("pid", None)
        _save_record(record, env)
        return outcome


# --- stop and status ------------------------------------------------------------------

def stop(target: Any, *, env: Mapping[str, str] | None = None,
         use_wait: float | None = None) -> dict[str, Any]:
    """Stop the recorded helper of `target` (AH-07): quit, then a kill of the
    recorded pid only while it is still `autonom-helper`, then the recorded
    forward only (`forward --remove tcp:<port>`). No record: no device call.

    It runs under the use lock, so it never cuts another process's request:
    it waits for that call to end (up to `use_wait`, `USE_WAIT_S` by
    default), and raises `HelperBusy` (nothing stopped) when it does not end
    in time. Holding the lock, a helper that does not answer hello is stuck
    and is killed (its verified pid) without a quit; while another state
    directory forwards to the helper, it is first given up to `USE_WAIT_S`
    to answer (`_after_silence`), so a call of that directory that ends in
    that time is not cut.

    When adb cannot tell whether that pid is still the helper (after a quit
    that it did not see end, or for a helper that did not answer), nothing
    is killed and no forward removed, the record keeps the pid (INV-3) and
    `_unverified` (``backend_failed``) is raised: `ui helper stop` and
    `session stop` report the failure, and a uiautomator read goes ahead as
    after any failed stop."""
    serial = target.target_id
    empty = {"stopped": False, "was_running": False, "pid": None, "port": None, "build": None}
    if not has_record(serial, env):
        return empty
    with use(serial, wait=use_wait, env=env), _device_lock(serial, env):
        record = load_record(serial, env)
        port, pid = record.get("port"), record.get("pid")
        if not isinstance(port, int) and not isinstance(pid, int):
            if record:
                _store(_live_cleared(record), env)
            return empty
        result: dict[str, Any] = {"stopped": False, "was_running": False, "pid": pid,
                                  "port": port, "build": record.get("build"), "quit": False,
                                  "killed": False, "forward_removed": False}
        # A recorded port that `forward --list` does not show for this serial
        # may reach another device's helper: no hello (nor quit) on it; the
        # recorded pid is still ended below, on this serial.
        if isinstance(port, int) and _forward_confirmed(target, port, env):
            outcome = _hello(port, None)
            if outcome.state == "hung":
                outcome = _after_silence(target, port, None, outcome, refuse=False)
            if outcome.state in ("ready", "other_build") and outcome.connection is not None:
                result["was_running"] = True
                if isinstance(outcome.reply.get("pid"), int):
                    pid = result["pid"] = outcome.reply["pid"]
                try:
                    result["quit"] = outcome.connection.call("quit").get("ok") is True
                except _Unanswered:
                    pass
                finally:
                    outcome.connection.close()
            elif outcome.state in ("busy", "failed"):
                result["was_running"] = True  # it exits right after that reply
            elif outcome.state == "hung":
                # no answer under the lock (and, with another state
                # directory's forward, none after its wait either): stuck
                result["stuck"] = True
        if isinstance(pid, int):
            if result["quit"] and _wait_gone(target, pid, QUIT_EXIT_WAIT_S):
                pass
            else:
                killed = _kill_verified(target, pid)
                if killed.get("verification") == "unreadable":
                    if record.get("pid") != pid:  # the helper's own, from its hello
                        record["pid"] = pid
                        _save_record(record, env)
                    raise _unverified(pid, "the stop")
                result["killed"] = killed["killed"]
                result["was_running"] = result["was_running"] or killed["killed"]
                if killed.get("verification"):
                    result["kill_verification"] = killed["verification"]
        removed = _remove_own_forward(target, port, env)
        if removed is not None:
            result["forward_removed"] = removed
        result["stopped"] = result["was_running"]
        _store(_live_cleared(record), env)
        return result


def status(target: Any, *, env: Mapping[str, str] | None = None,
           use_wait: float | None = None) -> dict[str, Any]:
    """Whether a helper runs on `target` (AH-07): `running`, `build`, `pid`,
    `port`, `idle_ms` (since this host's last hello before this one) and
    `idle_limit_ms`; `other_pids` lists `autonom-helper` processes this
    state directory did not start, `unavailable` a remembered failure. It
    never starts a helper; its own hello restarts the helper's idle clock.

    The hello runs under the use lock, waited for `use_wait`
    (`STATUS_USE_WAIT_S` by default): while another Autonom process holds
    it, nothing is sent to the helper, `in_use` names that process
    (``pid``, null when unknown) and the rest is what the record says. A
    lock file that cannot be made sends nothing either (``error``)."""
    serial = target.target_id
    record = load_record(serial, env)
    result: dict[str, Any] = {"running": False, "build": None, "pid": None, "port": None,
                              "idle_ms": None, "idle_limit_ms": IDLE_MS}
    port = record.get("port")
    if isinstance(port, int):
        result["port"] = port
        try:
            with use(serial, wait=STATUS_USE_WAIT_S if use_wait is None else use_wait,
                     env=env):
                _status_hello(target, record, port, result, env)
        except HelperBusy as busy:
            result.update(running=isinstance(record.get("pid"), int), build=record.get("build"),
                          pid=record.get("pid"), started_at=record.get("started_at"),
                          in_use={"pid": busy.owner_pid})
        except HelperUnavailable as failure:  # the lock file cannot be made: no hello
            result.update(build=record.get("build"), pid=record.get("pid"), answering=None,
                          error=failure.reason)
    others = [pid for pid in _helper_pids(target) if pid != result.get("pid")]
    if others:
        result["other_pids"] = others
    entry = record.get("unavailable")
    if isinstance(entry, dict):
        result["unavailable"] = {key: entry.get(key) for key in ("code", "reason", "at")}
    return result


def _status_hello(target: Any, record: dict[str, Any], port: int, result: dict[str, Any],
                  env: Mapping[str, str] | None) -> None:
    serial = target.target_id
    if not _forward_confirmed(target, port, env):
        # not this serial's forward: a hello there could reach another device
        result.update(build=record.get("build"), pid=record.get("pid"), answering=False,
                      error=f"tcp:{port} is not forwarded to this device's helper")
        return
    outcome = _hello(port, None)
    if outcome.state in ("ready", "other_build") and outcome.connection is not None:
        outcome.connection.close()
        reply = outcome.reply
        result.update(running=True, build=reply.get("build"), pid=reply.get("pid"),
                      protocol=reply.get("protocol"), sdk_int=reply.get("sdk_int"),
                      started_at=record.get("started_at"))
        last = record.get("last_request_at")
        if isinstance(last, (int, float)):
            result["idle_ms"] = max(0, int((time.time() - last) * 1000))
        with _device_lock(serial, env):
            fresh = load_record(serial, env)
            if fresh.get("port") == port:
                fresh["last_request_at"] = time.time()
                _save_record(fresh, env)
    else:
        result.update(build=record.get("build"), pid=record.get("pid"),
                      answering=False, error=outcome.why or outcome.state)


# --- doctor ----------------------------------------------------------------------------

DOCTOR_NEEDS = ("adb + a JDK (javac, JAVA_HOME or PATH) + Android SDK build-tools (d8) and a "
                "platform (android.jar)")


def doctor_capability(adb_path: str | None, *, env: Mapping[str, str] | None = None
                      ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The `android_helper` capability of `doctor`: the tools found and whether
    the build for them is cached, never a build or a device call. With
    ``AUTONOM_ANDROID_UI_BACKEND=uiautomator`` no tool is asked either."""
    warnings: list[dict[str, Any]] = []
    entry: dict[str, Any] = {"ready": False, "needs": DOCTOR_NEEDS}
    try:
        chosen = preference(env=env)
    except errors.AutonomError as exc:
        chosen = "auto"
        warnings.append({"code": "invalid_android_ui_backend", "variable": BACKEND_ENV,
                         "error": exc.message, "hint": exc.hint})
    entry["backend"] = chosen
    if chosen == "uiautomator":
        entry["reason"] = f"switched off by {BACKEND_ENV}=uiautomator"
        return entry, warnings
    try:
        found = build_mod.probe(adb_path=adb_path, env=env)
    except Exception as exc:  # noqa: BLE001 - a probe must never take doctor down
        entry.update(tools=False, reason=f"helper probe failed: {exc}")
        return entry, warnings
    entry["tools"] = bool(found.get("tools"))
    for key in ("javac", "d8", "android_jar", "build", "cached", "failed", "hint"):
        if key in found:
            entry[key] = found[key]
    if not found.get("tools"):
        entry["reason"] = found.get("reason")
    elif found.get("failed"):
        entry["reason"] = "the helper build for these tools failed earlier"
    elif not adb_path:
        entry["reason"] = "adb not found"
    entry["ready"] = bool(adb_path and found.get("tools") and not found.get("failed"))
    return entry, warnings
