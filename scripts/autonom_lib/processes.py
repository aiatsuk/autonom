"""Machine-level registry of the processes Autonom spawns, and their reaping.

Long-lived children — `mitmdump`, an iOS `log stream`, a screen recorder — used
to be tracked only inside their own session directory, so `doctor` looked for
them under `Path.cwd()/.autonom`. A proxy started in one working directory was
therefore invisible from another, and could hold a port for hours while every
diagnostic reported a clean machine.

Two independent mechanisms fix that, deliberately overlapping:

1. **The registry** (`$AUTONOM_HOME/processes/processes.json`) records every
   child at spawn time, so discovery no longer depends on the caller's cwd.
2. **Signature discovery** finds a `mitmdump` running *our* addon even when the
   registry entry was lost — a machine that lost power mid-run, an artifacts
   directory deleted by hand, a registry file removed. Without this, "clean up
   whatever is left" would be a promise the registry alone cannot keep.

A process is only ever classified, never guessed at: `live` means a proxy is
doing its job for an intact session and must be left alone; `background`
holds the other long-lived children a session or a foreground command still
owns (an iOS log-stream writer, `canvas serve` and its node child, an
`idb_companion` Autonom's own idb calls started); `orphan` means nothing
owns it any more — including a process whose session has stopped. A
`group_remnant` (a recorded group leader that is gone while a group with its
id still has members) is only ever reported: the id may have been reused.

A supervised child (`run_supervised`, used by `canvas serve`) runs in its own
process group, so terminating it takes its whole tree: killing the canvas used
to leave its node child, and node's own children, running.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import errors
from . import session as session_mod

try:  # POSIX (macOS, Linux: every platform Autonom runs on); absent on Windows
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

# Unmistakably ours: mitmproxy invoked with Autonom's own addon file. Matching
# on "mitmdump" alone would sweep up a colleague's unrelated proxy.
ADDON_MARKER = "mitm_addon.py"
PROXY_MARKER = "autonom_dir="
# `owner` of a process the harness itself keeps (an emulator from `devices
# boot`) rather than a session. Owned for as long as it runs.
HARNESS_OWNER = "harness"
# The process name idb's client spawns per simulator when no companion is
# connected; one survives every idb call, and every session, unless stopped.
COMPANION_MARKER = "idb_companion"


def registry_dir() -> Path:
    explicit = os.environ.get("AUTONOM_HOME")
    if explicit:
        root = Path(explicit)
    else:
        state = os.environ.get("XDG_STATE_HOME")
        root = Path(state) / "autonom" if state else Path.home() / ".local/state/autonom"
    path = root / "processes"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def registry_file() -> Path:
    return registry_dir() / "processes.json"


def _registry_file_if_present() -> Path | None:
    """The registry file without creating anything: teardown paths must not
    materialise a machine store that was never used."""
    explicit = os.environ.get("AUTONOM_HOME")
    if explicit:
        root = Path(explicit)
    else:
        state = os.environ.get("XDG_STATE_HOME")
        root = Path(state) / "autonom" if state else Path.home() / ".local/state/autonom"
    path = root / "processes" / "processes.json"
    return path if path.exists() else None


def _read() -> list[dict[str, Any]]:
    path = registry_file()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    entries = payload.get("processes") if isinstance(payload, dict) else payload
    return list(entries or [])


def _write(entries: list[dict[str, Any]]) -> None:
    path = registry_file()
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=".processes-",
        suffix=".tmp", delete=False,
    )
    try:
        json.dump({"processes": entries}, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.chmod(handle.name, 0o600)
    os.replace(handle.name, path)


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """Serialise every read-modify-write of the registry across processes.

    The write itself is atomic, but two writers that both read the old list
    each drop the other's row — and a lost supervisor row turns its live
    child into an "orphan" that the default `cleanup` kills. Companion
    tracking writes during ordinary idb calls, so concurrent writers are
    normal, not rare. `flock` on a sibling lock file (never the registry,
    which `os.replace` swaps out). Not reentrant: callers never nest it.
    """
    if fcntl is None:  # pragma: no cover - non-POSIX: best effort, unlocked
        yield
        return
    descriptor = os.open(str(registry_dir() / "processes.lock"),
                         os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


REDACTED = "<redacted>"
_SECRET_CONTAINS = ("token", "password", "passwd", "secret", "credential", "bearer")
_SECRET_ENDS = ("key", "auth")
_SECRET_WORDS = frozenset({"pass", "pwd", "pw"})
# `scheme://user:pass@host` anywhere in an argument, including inside
# `--mode upstream:http://u:p@h`: the userinfo goes, scheme and host stay.
# The lookbehind lets a match start only where a run of scheme characters
# starts: unanchored, every position of a long letter run was a fresh start
# that scanned to the run's end looking for `://`, and a 400 KB argv took
# minutes (quadratic). Anchored, the scan is linear. The run may open with
# characters a scheme cannot start with (`curl -xhttp://u:p@proxy`); they are
# kept as found, and the scheme starts at the run's first letter, which is
# where the unanchored pattern found it too.
_URL_USERINFO = re.compile(
    r"(?<![A-Za-z0-9+.-])([0-9+.-]*[A-Za-z][A-Za-z0-9+.-]*://)[^/\s]*@")  # to the last `@`
# `ps` lines are unbounded (an argv can be megabytes); discovery redacts only
# a bounded head of each, and keeps a bounded head of the result.
_DISCOVERY_SCAN_CHARS = 8192
_DISCOVERY_KEEP_CHARS = 400


def _secret_name(name: str) -> bool:
    for part in re.split(r"[-_.]+", name.lstrip("-").lower()):
        if (part in _SECRET_WORDS or part.endswith(_SECRET_ENDS)
                or any(word in part for word in _SECRET_CONTAINS)):
            return True
    return False


def redact_command(command: str) -> str:
    """A command line with the values of secret-looking options replaced.

    `--token X`, `--token=X`, `--api-key X`, `--bearer X`, `--password=X`,
    environment-style `PASS=x` / `PASSWD=x` and other `name=value` arguments
    whose name looks secret (`proxyauth=u:p`) keep their name and lose their
    value; URL credentials become `scheme://<redacted>@host`. A secret-named
    flag followed by another option is a boolean (`--no-auth --token S`): it
    consumes nothing, so the option after it is still judged on its own.
    Whitespace between arguments (spaces, tabs, newlines) is kept as found.
    Registry rows are printed whole by `processes` and `doctor`, and
    `canvas serve --token` must never leak its token there (AGENTS rule 7).
    """
    out: list[str] = []
    redact_next = False
    for piece in re.split(r"(\s+)", command):
        if not piece or piece.isspace():
            out.append(piece)
            continue
        if redact_next:
            redact_next = False
            if not piece.startswith("-"):
                out.append(REDACTED)
                continue
        token = (_URL_USERINFO.sub(lambda match: match.group(1) + REDACTED + "@", piece)
                 if "://" in piece else piece)
        name, sep, _value = token.partition("=")
        if sep and _secret_name(name):
            out.append(f"{name}={REDACTED}")
        elif token.startswith("-") and not sep and _secret_name(token):
            out.append(token)
            redact_next = True
        else:
            out.append(token)
    return "".join(out)


def _redacted(entry: dict[str, Any]) -> dict[str, Any]:
    if isinstance(entry.get("command"), str):
        entry = {**entry, "command": redact_command(entry["command"])}
    return entry


def register(kind: str, pid: int, **detail: Any) -> dict[str, Any]:
    """Record a child. Called at spawn time, before anything can go wrong.
    A `command` detail is stored redacted (`redact_command`)."""
    entry = _redacted({
        "kind": kind,
        "pid": int(pid),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **{key: value for key, value in detail.items() if value is not None},
    })
    with _locked():
        entries = [item for item in _read() if item.get("pid") != pid]
        entries.append(entry)
        _write(entries)
    return entry


def update(pid: int, **detail: Any) -> dict[str, Any] | None:
    """Add fields to an existing entry (an emulator learns its serial late)."""
    with _locked():
        entries = _read()
        for entry in entries:
            if entry.get("pid") == pid:
                entry.update(_redacted(detail))
                _write(entries)
                return entry
    return None


def entries() -> list[dict[str, Any]]:
    """The registry rows as recorded. Read-only."""
    return [dict(item) for item in _read()]


def deregister(pid: int) -> None:
    with _locked():
        entries = _read()
        remaining = [item for item in entries if item.get("pid") != pid]
        if len(remaining) != len(entries):
            _write(remaining)


# --- discovery ----------------------------------------------------------------


def utf8_locale() -> str:
    """A UTF-8 locale this host has: `en_US.UTF-8` ships with every macOS,
    `C.UTF-8` with current glibc and musl."""
    return "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"


def _ps_output(argv: Sequence[str], *, timeout: float) -> str | None:
    """`ps`'s stdout as text, or None when it could not run.

    `ps` renders a command line through the caller's locale: under
    `LC_ALL=C` macOS prints every non-ASCII byte as a meta escape (`café`
    reads `cafM-CM-)`), so a writer whose stream file sits under a
    non-ASCII `AUTONOM_HOME` no longer carried its own path and was taken
    for a stranger. `ps` therefore always runs under a UTF-8 locale, and
    its bytes are decoded as UTF-8 with `surrogateescape` — the way Python
    decodes the file names it compares them with."""
    env = dict(os.environ)
    env["LC_ALL"] = utf8_locale()
    try:
        completed = subprocess.run(
            list(argv), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False, env=env,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    output = completed.stdout
    if isinstance(output, bytes):
        return output.decode("utf-8", "surrogateescape")
    return output or ""


def _running_processes() -> list[tuple[int, str]]:
    """(pid, command) for every process on the machine, or [] if ps is unusable."""
    output = _ps_output(["ps", "-Ao", "pid=,command="], timeout=15)
    if output is None:
        return []
    found = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        head, _, command = stripped.partition(" ")
        if head.isdigit() and command:
            found.append((int(head), command))
    return found


def discover_proxies() -> list[dict[str, Any]]:
    """Autonom proxies found by command line, registry or no registry."""
    found = []
    for pid, command in _running_processes():
        if ADDON_MARKER not in command:
            continue
        if "mitmdump" not in command and "mitmproxy" not in command:
            continue
        directory = None
        for token in command.split():
            if token.startswith(PROXY_MARKER):
                directory = token[len(PROXY_MARKER):]
        found.append({"kind": "proxy", "pid": pid, "artifacts_dir": directory,
                      "command": _discovered_command(command), "source": "signature"})
    return found


def discover_companions(udid: str | None) -> list[dict[str, Any]]:
    """Running `idb_companion` processes serving simulator `udid`."""
    if not udid:
        return []
    found = []
    for pid, command in _running_processes():
        if COMPANION_MARKER in command and udid in command:
            found.append({"kind": "idb_companion", "pid": pid, "udid": udid,
                          "command": _discovered_command(command)})
    return found


def _discovered_command(command: str) -> str:
    """A discovered process's command line as shown: redacted, bounded."""
    return redact_command(command[:_DISCOVERY_SCAN_CHARS])[:_DISCOVERY_KEEP_CHARS]


def command_of(pid: int) -> str | None:
    """The command line of one pid, or None when ps cannot answer. Rendered
    under a UTF-8 locale whatever the caller's (`_ps_output`), so a
    non-ASCII argument reads as itself."""
    try:
        argv = ["ps", "-ww", "-o", "command=", "-p", str(int(pid))]
    except (TypeError, ValueError):
        return None
    text = (_ps_output(argv, timeout=10) or "").strip()
    return text or None


# --- ownership ------------------------------------------------------------------


def harness_owned(entry: dict[str, Any]) -> bool:
    """Kept by the harness itself rather than a session. Only `devices boot`
    registers emulators, so an entry written before `owner` existed is the
    same kind of process."""
    return entry.get("owner") == HARNESS_OWNER or entry.get("kind") == "emulator"


def session_of(entry: dict[str, Any]) -> str | None:
    """The session id that owns an entry, if any (`owner` or `session_id`)."""
    owner = entry.get("owner")
    if owner and owner != HARNESS_OWNER:
        return str(owner)
    session_id = entry.get("session_id")
    return str(session_id) if session_id else None


def owned_by(entry: dict[str, Any], session_id: str | None) -> bool:
    return bool(session_id) and session_of(entry) == session_id


def _session_record_for(entry: dict[str, Any]) -> dict[str, Any] | None:
    """The owning session's record: `session.json` in the entry's
    artifacts dir, or its parent (a proxy registers `<session>/network`)."""
    directory = entry.get("artifacts_dir")
    if not directory:
        return None
    wanted = session_of(entry)
    root = Path(directory)
    for candidate in (root / "session.json", root.parent / "session.json"):
        if not candidate.is_file():
            continue
        try:
            record = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if isinstance(record, dict) and (not wanted or record.get("session_id") in (None, wanted)):
            return record
    return None


def _session_stopped(entry: dict[str, Any]) -> bool:
    if not session_of(entry):
        return False
    record = _session_record_for(entry)
    return bool(record and record.get("stopped_at"))


def _ownership(entry: dict[str, Any],
               registered: dict[int, dict[str, Any]]) -> tuple[bool, str | None]:
    """(owned, reason-when-not). Owned means something can still stop it the
    normal way; otherwise only `cleanup` ever will."""
    if harness_owned(entry):
        return True, None
    parent = entry.get("parent_pid")
    if parent:
        # A supervised child lives exactly as long as its supervisor. A live
        # supervisor whose own row is missing still owns it: a lost row must
        # never make the default `cleanup` kill a running canvas.
        if not session_mod.pid_alive(int(parent)):
            return False, "its supervisor exited and left it running"
        parent_entry = registered.get(int(parent))
        return _ownership(parent_entry, registered) if parent_entry else (True, None)
    if _session_stopped(entry):
        return False, "its session has stopped"
    if entry.get("role") == "supervisor" and not session_of(entry):
        # A foreground command (`canvas serve` outside a session): owned by
        # the terminal running it for as long as it runs.
        return True, None
    return (True, None) if _still_owned(entry) else (
        False, "no session owns this process any more")


def _still_owned(entry: dict[str, Any]) -> bool:
    """Does an intact session still claim this process?

    A proxy whose artifacts directory or `proxy.json` has gone is answerable to
    nobody: `network stop` can no longer reach it, because the file it reads to
    find the pid is exactly what disappeared.

    A harness-owned process (an emulator booted by `devices boot`) has no
    session directory by design; it is owned while it runs, and released by
    `devices shutdown`.
    """
    if harness_owned(entry):
        return True
    directory = entry.get("artifacts_dir")
    if not directory:
        return False
    root = Path(directory)
    if not root.is_dir():
        return False
    if entry.get("kind") == "proxy":
        return (root / "proxy.json").exists()
    return True


def scan() -> dict[str, Any]:
    """Classify every Autonom process on the machine. Read-only."""
    entries = {int(item["pid"]): dict(item) for item in _read() if item.get("pid")}
    registered = dict(entries)
    for candidate in discover_proxies():
        pid = candidate["pid"]
        if pid in entries:
            entries[pid].setdefault("artifacts_dir", candidate.get("artifacts_dir"))
            entries[pid]["source"] = "registry+signature"
        else:
            # Running with our signature but unknown to the registry: the entry
            # was lost, so nothing but this scan can ever find it again.
            entries[pid] = candidate

    live, orphans, stale, harness, background, remnants = [], [], [], [], [], []
    for pid, entry in sorted(entries.items()):
        if not session_mod.pid_alive(pid):
            if _group_remnant(entry):
                # Reported, never an orphan: an orphan is what `cleanup` kills,
                # and a group without its leader cannot be shown to be ours.
                remnants.append(remnant_report(entry))
            else:
                stale.append(entry)
            continue
        if harness_owned(entry):
            # Neither a session's live process nor an orphan: `live` is read
            # as "serving some session" (doctor names foreign proxies from
            # it), and an orphan is what `cleanup` kills.
            harness.append(entry)
            continue
        owned, reason = _ownership(entry, registered)
        if not owned:
            entry.setdefault("reason", reason)
            orphans.append(entry)
        elif entry.get("kind", "proxy") == "proxy":
            live.append(entry)
        else:
            # Owned, but not a proxy: kept apart from `live`, which doctor
            # reads as "a proxy serving some session".
            background.append(entry)
    return {"live": live, "orphans": orphans, "stale_entries": stale, "harness": harness,
            "background": background, "group_remnants": remnants}


# --- reaping ------------------------------------------------------------------


def _reap_child(pid: int) -> None:
    """Collect `pid` if it is our own exited child, so a zombie does not
    look alive to `kill(pid, 0)` for the whole termination timeout."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def terminate_group(pgid: int, *, timeout: float = 5.0) -> bool:
    """SIGTERM a whole process group, SIGKILL whatever is left after
    `timeout`. Returns True when the group existed. Never our own group."""
    if not pgid or pgid <= 1 or pgid == os.getpgrp():
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        _reap_child(pgid)
        if not _group_alive(pgid):
            return True
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    for _ in range(40):  # SIGKILL is not instant under load: see it land
        _reap_child(pgid)
        if not _group_alive(pgid):
            break
        time.sleep(0.05)
    return True


def _group_remnant(entry: dict[str, Any]) -> bool:
    """A group-leading row whose leader is gone while a group with that id
    still has members.

    That group is NOT necessarily the one the row recorded. A group id is
    only held while the group has members; once it empties — after a reboot,
    or when the recorded tree ended without a cleanup — the id is free, and
    a double-forking daemon (the adb fork-server, ssh/gpg agents: setsid,
    fork, parent exits) leaves exactly this shape, members in a group whose
    leader is dead. So a remnant is reported (`remnant_report`) and never
    signalled: not by default `cleanup`, not by `cleanup --all`, not by
    `session stop`."""
    group = entry.get("process_group")
    try:
        pid = int(entry.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    return bool(group) and int(group) == pid and pid > 1 and _group_alive(pid)


def group_members(pgid: int) -> list[int]:
    """The live pids in process group `pgid`, from ps; [] when ps cannot say."""
    try:
        completed = subprocess.run(
            ["ps", "-Ao", "pid=,pgid="],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    members = []
    for line in (completed.stdout or "").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1] == str(pgid):
            members.append(int(fields[0]))
    return members


def remnant_report(entry: dict[str, Any]) -> dict[str, Any]:
    """What `processes`, `cleanup` and `session stop` say about a remnant
    group instead of killing it."""
    pgid = int(entry["pid"])
    report = {key: entry[key] for key in ("kind", "pid", "process_group", "owner",
                                          "session_id", "started_at") if key in entry}
    report.update({
        "members": group_members(pgid),
        "reason": "the recorded leader is gone but process group "
                  f"{pgid} still has members; a group id is reused once its group "
                  "has ended, so they may not be Autonom's — left running",
        "hint": f"Inspect them with 'ps -Ao pid,pgid,command' (pgid {pgid}); only "
                f"if they are yours, stop them with 'kill -- -{pgid}'.",
    })
    return report


def _leads_group(pid: int, pgid: int) -> bool:
    try:
        return os.getpgid(pid) == pgid
    except (ProcessLookupError, PermissionError, OSError):
        return False


def terminate_entry(entry: dict[str, Any]) -> str:
    """Stop one registered process; its whole group when it leads one.

    Returns ``terminated``, ``termination_failed``, ``already_exited``,
    ``pid_reused`` (the pid now runs something without the entry's recorded
    `signature`), ``unverified_skipped`` (a signature was recorded but `ps`
    could not be asked, or the recorded signature is empty — an empty mark
    proves nothing) or ``group_remnant`` (the leader is gone and a group
    with its id lives on, see `_group_remnant`). The last three never signal
    anything: a process that cannot be shown to be ours is left alone.

    Only a row with no `signature` key at all is signalled unchecked: a
    proxy found by its command line in this very scan (`discover_proxies`),
    and rows written before signatures existed. Harness rows (emulators)
    carry none either, but `cleanup` and `reap_session` never target them.
    A signature is checked with `entry_matches`, which holds a log-stream
    row with a plain-string signature to the bounded writer's own shape.
    """
    pid = int(entry["pid"])
    group = entry.get("process_group")
    leads = bool(group) and int(group) == pid
    if not session_mod.pid_alive(pid):
        return "group_remnant" if leads and _group_remnant(entry) else "already_exited"
    if "signature" in entry:
        signature = entry.get("signature")
        if not usable_signature(signature):
            return "unverified_skipped"
        command = command_of(pid)
        if command is None:
            # ps could not answer: gone in the meantime, or unverifiable.
            return "already_exited" if not session_mod.pid_alive(pid) else "unverified_skipped"
        if not entry_matches(entry, command):
            return "pid_reused"
    if leads and _leads_group(pid, pid):
        return "terminated" if terminate_group(pid) else "termination_failed"
    return "terminated" if session_mod.terminate_pid(pid) else "termination_failed"


def signature_matches(signature: Any, command: str) -> bool:
    """Does `command` still carry a row's recorded `signature`?

    A string must appear in the command line; a list is several marks that
    must all appear — an `idb_companion` row records the binary *and* the
    simulator it serves, so another simulator's companion (or any process
    that merely names the UDID) is never taken for it."""
    if not usable_signature(signature):
        return False
    marks = signature if isinstance(signature, (list, tuple)) else [signature]
    return all(str(mark) in command for mark in marks)


def entry_matches(entry: dict[str, Any], command: str) -> bool:
    """Does `command` still carry registry row `entry`'s signature?

    `signature_matches`, plus one stricter case: a log-stream row whose
    signature is a plain string. Rows written before the writer's strict
    signature (`logs.writer_signature`) recorded just the stream file's
    path, which `tail -f <stream>` or a writer of `<stream>.bak` carries
    too; such a row matches only the bounded writer of exactly that file
    (`logs.is_writer_command`: the writer's script line, and the path as a
    whole argument followed by its cap)."""
    signature = entry.get("signature")
    if not signature_matches(signature, command):
        return False
    if entry.get("kind") == "log_stream" and isinstance(signature, str):
        from . import logs  # late: logs imports this module lazily too

        return logs.is_writer_command(command, signature)
    return True


def usable_signature(signature: Any) -> bool:
    """A signature that can prove something: a non-empty string, or a
    non-empty list of non-empty strings. `""`, `[]`, `[""]` and `None` match
    every command line, so they verify nothing."""
    marks = signature if isinstance(signature, (list, tuple)) else [signature]
    return bool(marks) and all(isinstance(mark, str) and mark for mark in marks)


# Outcomes that leave the registry row in place for a later attempt.
_KEEP_ROW = ("termination_failed", "unverified_skipped", "group_remnant")


def reap_stale_entries() -> int:
    """Drop registry rows whose process is gone. Touches no process. A row
    whose leader is gone while a group with its id lives on is kept, so the
    remnant keeps being reported until that group ends."""
    with _locked():
        entries = _read()
        remaining = [item for item in entries if item.get("pid") and (
            session_mod.pid_alive(int(item["pid"])) or _group_remnant(item))]
        if len(remaining) != len(entries):
            _write(remaining)
    return len(entries) - len(remaining)


def _supervisors_first(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # A supervisor takes its child's group down itself; its child's entry is
    # then already gone, rather than killed out from under a live supervisor.
    return sorted(entries, key=lambda item: 0 if item.get("role") == "supervisor" else 1)


def cleanup(*, dry_run: bool = False, include_live: bool = False) -> dict[str, Any]:
    """Terminate orphans (and, on request, healthy processes too).

    `include_live` exists for "stop everything Autonom started" — the honest
    escape hatch when a run is being abandoned. It is never the default: a live
    proxy may be serving a session in another terminal. It also takes the
    session-owned background processes (log streams, canvas, companions
    Autonom started). Harness-owned processes (a booted emulator) are never
    terminated here; `devices shutdown` is the verb that stops an emulator
    cleanly. Remnant groups (`_group_remnant`) are only reported, under
    `group_remnants`, with or without `include_live`.
    """
    state = scan()
    targets = list(state["orphans"])
    if include_live:
        targets += state["live"] + state["background"]

    actions = []
    for entry in _supervisors_first(targets):
        pid = int(entry["pid"])
        action = {"kind": entry.get("kind", "process"), "pid": pid,
                  "artifacts_dir": entry.get("artifacts_dir"),
                  "reason": entry.get("reason", "requested")}
        if dry_run:
            action["result"] = "would_terminate"
        else:
            action["result"] = terminate_entry(entry)
            if action["result"] not in _KEEP_ROW:
                deregister(pid)
        actions.append(action)

    reaped = 0 if dry_run else reap_stale_entries()
    return {
        "dry_run": dry_run,
        "actions": actions,
        "terminated": sum(1 for item in actions if item["result"] == "terminated"),
        "failed": sum(1 for item in actions if item["result"] == "termination_failed"),
        "skipped_unverified": sum(1 for item in actions
                                  if item["result"] == "unverified_skipped"),
        "group_remnants": state["group_remnants"],
        "skipped_group_remnants": len(state["group_remnants"]) + sum(
            1 for item in actions if item["result"] == "group_remnant"),
        "stale_entries_reaped": reaped,
        "still_live": 0 if include_live else len(state["live"]) + len(state["background"]),
        "harness_owned": len(state["harness"]),
        "registry": str(registry_file()),
    }


# --- session-owned processes ------------------------------------------------------


def reap_session(record: dict[str, Any]) -> dict[str, Any]:
    """Terminate every registered process `record`'s session owns.

    `session stop` calls this last (after the proxy and the recorder). On an
    iOS session it also reports each `idb_companion` still serving the
    simulator that this session's idb calls did not start: Autonom never
    kills a companion it cannot prove it started, it says so instead
    (`companion_left_running`, with the pid).
    """
    result: dict[str, Any] = {"terminated": [], "companion_left_running": []}
    session_id = record.get("session_id")
    if not session_id:
        return result
    handled: set[int] = set()
    if _registry_file_if_present() is not None:
        owned = [entry for entry in _read() if owned_by(entry, session_id)]
        for entry in _supervisors_first(owned):
            pid = int(entry["pid"])
            outcome = terminate_entry(entry)
            if outcome == "group_remnant":
                result.setdefault("group_remnants", []).append(remnant_report(entry))
            elif outcome != "already_exited":
                result["terminated"].append({"kind": entry.get("kind"), "pid": pid,
                                             "result": outcome})
            if outcome not in _KEEP_ROW:
                deregister(pid)
            handled.add(pid)
    if (record.get("platform") or "android") == "ios":
        udid = record.get("target_id")
        for companion in discover_companions(udid):
            if companion["pid"] in handled:
                continue
            result["companion_left_running"].append({
                "pid": companion["pid"], "udid": udid,
                "reason": "not started by this session's idb calls; left running",
                "hint": f"Stop it yourself with 'kill {companion['pid']}' if nothing "
                        "else uses the simulator.",
            })
    return result


@contextlib.contextmanager
def track_idb_companions(udid: str | None, *,
                         record: dict[str, Any] | None = None) -> Iterator[None]:
    """Attribute an `idb_companion` that appears for `udid` during an idb call.

    Wrap every idb invocation that may spawn a local companion. When no
    companion served the simulator before the call and one does after it,
    Autonom's call started it: it is registered under the session that owns
    the simulator, so `session stop` can stop it. A companion that was
    already running is never claimed. Without a current session for `udid`
    nothing is tracked. Never raises on its own account.
    """
    session = None
    before: set[int] | None = None
    try:
        session = record if record is not None else session_mod.load_current()
        if session and udid and session.get("target_id") == udid:
            before = {item["pid"] for item in discover_companions(udid)}
    except Exception:  # noqa: BLE001 - tracking must never fail an idb call
        before = None
    try:
        yield
    finally:
        if before is not None and not before and session:
            try:
                for companion in discover_companions(udid):
                    session_id = session.get("session_id")
                    # both marks: the binary alone matches any simulator's
                    # companion (`signature_matches`)
                    register("idb_companion", companion["pid"], owner=session_id,
                             session_id=session_id,
                             artifacts_dir=session.get("artifacts_dir"),
                             udid=udid, signature=[COMPANION_MARKER, udid],
                             spawned_by="idb")
            except Exception:  # noqa: BLE001
                pass


def _signature(argv: Sequence[str]) -> str:
    """The token of `argv` that identifies the process in `ps` output: the
    script an interpreter runs (`node bridge.mjs`), else the program name.
    An interpreter's own path is a poor witness — macOS reports a framework
    Python under a different path than the one that was executed."""
    if len(argv) > 1 and not str(argv[1]).startswith("-"):
        return Path(str(argv[1])).name
    return Path(str(argv[0])).name


class _Stopped(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def run_supervised(command: Sequence[str], *, kind: str, owner: str | None = None,
                   artifacts_dir: str | None = None, **detail: Any) -> int:
    """Run a long-lived foreground child in its own process group, registered
    (supervisor and child) for its whole life, and take the group down with it.

    `canvas serve` used to `subprocess.run` its node bridge: the pair was
    invisible to `processes` / `cleanup --all`, and terminating the CLI left
    node — and node's own children — running. Now SIGTERM/SIGHUP to the
    supervisor, Ctrl-C, or the child exiting all end with the group gone and
    both registry rows removed; a supervisor killed outright leaves a child
    `processes` reports as an orphan and `cleanup` reaps by group.
    Returns the child's exit code (128+N for a signal that stopped us).
    """
    argv = [str(part) for part in command]
    me = os.getpid()
    common = {key: value for key, value in
              {"owner": owner, "session_id": owner, "artifacts_dir": artifacts_dir,
               **detail}.items() if value is not None}

    def _on_signal(signum: int, _frame: Any) -> None:
        raise _Stopped(signum)

    # Installed before anything is spawned or registered: a SIGTERM that
    # arrived between registration and installation would kill the
    # supervisor outright and leave the group running.
    previous: dict[int, Any] = {}
    for signum in (signal.SIGTERM, signal.SIGHUP):
        try:
            previous[signum] = signal.signal(signum, _on_signal)
        except (ValueError, OSError):  # not the main thread
            pass
    child: subprocess.Popen | None = None
    registered: list[int] = []
    code: int | None = None
    try:
        try:
            child = subprocess.Popen(argv, start_new_session=True)  # noqa: S603
        except OSError as exc:
            raise errors.AutonomError(errors.BACKEND_FAILED,
                                      f"could not start {argv[0]}: {exc}") from exc
        try:
            register(kind, me, role="supervisor", child_pid=child.pid,
                     process_group=child.pid,
                     signature=Path(sys.argv[0]).name if sys.argv and sys.argv[0] else None,
                     **common)
            registered.append(me)
            # No command line: `canvas serve --token X` would print its token
            # through `processes` and `doctor`. The signature (a file name)
            # is all a later kill needs to verify the pid.
            register(f"{kind}_child", child.pid, parent_pid=me, process_group=child.pid,
                     signature=_signature(argv), **common)
            registered.append(child.pid)
        except OSError:
            pass  # the registry is a safety net; the child still runs supervised
        # Waited for, not reaped: see `_stop_supervised_group`.
        _child_exited(child, None)
    except _Stopped as stopped:
        code = 128 + stopped.signum
    except KeyboardInterrupt:
        code = 130
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        if child is not None:
            _stop_supervised_group(child)
        for pid in registered:
            try:
                deregister(pid)
            except OSError:
                pass
    if code is None:
        code = child.returncode if child is not None and child.returncode is not None else 0
    return code


def _child_exited(child: subprocess.Popen, timeout: float | None) -> bool:
    """Has our child exited? Waits up to `timeout` seconds (None: until it
    has), and never reaps it — a reaped leader frees its pid, and with it its
    process-group id, which a stranger's new group may then take.

    `os.waitid(WNOWAIT)` where Python has it; macOS builds before 3.13 do
    not, and a kqueue exit notification is the same promise there. Without
    either the child is reaped (`wait`), the behaviour before this existed.
    """
    if child.returncode is not None:
        return True
    pid = child.pid
    if hasattr(os, "waitid"):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            flags = os.WEXITED | os.WNOWAIT | (0 if deadline is None else os.WNOHANG)
            try:
                if os.waitid(os.P_PID, pid, flags) is not None:
                    return True
            except ChildProcessError:
                return True  # reaped elsewhere: gone either way
            if deadline is None:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
    try:
        import select

        queue = select.kqueue()
    except (ImportError, AttributeError, OSError):
        if timeout is None:
            child.wait()
            return True
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return True
    try:
        event = select.kevent(pid, filter=select.KQ_FILTER_PROC,
                              flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                              fflags=select.KQ_NOTE_EXIT)
        # an already-exited (zombie) child answers at once, with ESRCH
        return bool(queue.control([event], 1, timeout))
    finally:
        queue.close()


def _live_group_members(pgid: int, *, besides: int) -> list[int] | None:
    """Live (non-zombie) pids of group `pgid` other than `besides`, from
    ps; None when ps cannot say."""
    try:
        completed = subprocess.run(
            ["ps", "-Ao", "pid=,pgid=,stat="],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    members = []
    for line in (completed.stdout or "").splitlines():
        fields = line.split()
        if (len(fields) >= 3 and fields[0].isdigit() and fields[1] == str(pgid)
                and not fields[2].startswith("Z") and int(fields[0]) != besides):
            members.append(int(fields[0]))
    return members


def _stop_supervised_group(child: subprocess.Popen, *, timeout: float = 5.0) -> None:
    """Take the child's whole group down, then reap the child.

    The child leads its group (`start_new_session`), so its pid is the
    group id — and until the child is reaped that id cannot be reused, even
    once every member has exited. So: wait without reaping, signal the
    group while the (possibly zombie) leader pins the id, reap last.
    Reaping first (`poll()`, then `killpg`) left a window in which the id
    could name a stranger's group by the time the signal was sent.
    """
    pgid = child.pid
    if child.returncode is not None:
        # already reaped (no non-reaping wait on this platform): the id is
        # no longer pinned; the unpinned best effort is all that is left
        if _group_alive(pgid):
            terminate_group(pgid)
        return

    def remaining() -> bool:
        if not _child_exited(child, 0):
            return True
        others = _live_group_members(pgid, besides=pgid)
        return others is None or bool(others)

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + timeout
    while remaining() and time.monotonic() < deadline:
        time.sleep(0.05)
    if remaining():
        try:
            os.killpg(pgid, signal.SIGKILL)  # still pinned: cannot hit a stranger
        except OSError:
            pass
        _child_exited(child, 2.0)
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
