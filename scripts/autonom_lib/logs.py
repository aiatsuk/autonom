"""Device logs: bounded tails of logcat / the unified log, narrowed to one app.

Narrowing is the part that has to be honest. `--package X` used to filter by
X's *current pid* and silently return every device line when X was not
running; an iOS Flutter app (whose executable is `Runner`, not anything in its
bundle id) returned nothing at all. Every tail now says how it was narrowed:

- Android: ``filter`` is ``uid`` (logcat ``--uid``, API 31+: every process the
  app ever ran, including before a crash), ``pid`` (the running process only)
  or ``none`` (no process filter was possible; only lines naming the package
  are kept). Anything short of ``uid`` carries a ``log_filter_degraded``
  warning, and an unknown package is ``app_not_installed``.
- iOS: `ios_simctl` owns the match — the `log` predicate and its
  client-side twin are built from the installed bundle path (or, failing
  that, the executable name), so a second Flutter `Runner` on the same
  simulator never leaks into this app's lines.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from . import adb as adb_mod
from . import errors, ios_simctl
from .platform import ANDROID, Target


def compile_grep(grep: str | None) -> re.Pattern[str] | None:
    """The `--grep` filter, or a typed refusal — never a raw `re.error`
    traceback for a pattern like `(`. IGNORECASE matches `journal --grep`
    and `logs follow --grep`."""
    if not grep:
        return None
    try:
        return re.compile(grep, re.IGNORECASE)
    except re.error as exc:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"invalid --grep regex {grep!r}: {exc}",
            "The filter is a Python regular expression; escape literal "
            "characters such as ( [ * with a backslash.",
        ) from exc


# --- Android: how to narrow logcat to one app ---------------------------------

# `logcat --uid` arrived in Android 12 (API 31).
UID_FILTER_MIN_API = 31
# system_server narrates an app's life under its own uid ("Start proc
# 4242:pkg/u0a123", "Process pkg (pid 4242) has died"), so a uid filter alone
# drops exactly the lines that explain a cold start or a crash. These tags are
# kept whenever the line names the package.
LIFECYCLE_TAGS = ("ActivityManager", "AndroidRuntime")
LIFECYCLE_SPECS = tuple(f"{tag}:V" for tag in LIFECYCLE_TAGS)

# `threadtime`: "MM-DD HH:MM:SS.mmm  PID  TID P TAG: message"
_THREADTIME = re.compile(r"^(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+(\d+)\s+\d+\s+"
                         r"[VDIWEFAS]\s+(.*?)\s*:\s")
# `time`: "MM-DD HH:MM:SS.mmm P/TAG( PID): message"
_TIME_FORMAT = re.compile(r"^(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+[VDIWEFAS]/"
                          r"(.*?)\(\s*(\d+)\):")


def parse_logcat_line(line: str) -> tuple[str | None, str | None, str | None]:
    """(timestamp, pid, tag) of a `threadtime` or `time` line; Nones otherwise."""
    match = _THREADTIME.match(line)
    if match:
        return match.group(1), match.group(2), match.group(3).strip()
    match = _TIME_FORMAT.match(line)
    if match:
        return match.group(1), match.group(3), match.group(2).strip()
    return None, None, None


def names_package(line: str, package: str) -> bool:
    """The line mentions the package as a whole token, not as a prefix of a
    longer id (`com.example` must not match `com.example.app`)."""
    start = line.find(package)
    while start >= 0:
        end = start + len(package)
        before = line[start - 1] if start > 0 else " "
        after = line[end] if end < len(line) else " "
        if not (before.isalnum() or before in "._") and not (after.isalnum() or after in "._"):
            return True
        start = line.find(package, start + 1)
    return False


def is_lifecycle_line(line: str, package: str) -> bool:
    _stamp, _pid, tag = parse_logcat_line(line)
    return tag in LIFECYCLE_TAGS and names_package(line, package)


def api_level(adb: str, serial: str) -> int | None:
    completed = adb_mod.run_adb(adb, ["shell", "getprop", "ro.build.version.sdk"],
                                serial=serial, timeout=10, check=False)
    text = (completed.stdout if isinstance(completed.stdout, str) else "").strip()
    return int(text) if text.isdigit() else None


def package_uid(adb: str, serial: str, package: str) -> tuple[bool | None, int | None]:
    """(installed, uid) from `cmd package list packages -U <package>`.

    `installed` is None when the listing itself could not be read — a failed
    command must never be reported as "not installed". The listing's filter
    is a substring match, so only an exact `package:<name>` row counts.
    """
    completed = adb_mod.run_adb(
        adb, ["shell", "cmd", "package", "list", "packages", "-U", package],
        serial=serial, timeout=15, check=False,
    )
    text = completed.stdout if isinstance(completed.stdout, str) else ""
    listed = False
    for raw in text.splitlines():
        fields = raw.strip().split()
        if not fields or not fields[0].startswith("package:"):
            continue
        listed = True
        if fields[0][len("package:"):] != package:
            continue
        for field in fields[1:]:
            if field.startswith("uid:"):
                value = field[len("uid:"):].split(",")[0]
                if value.isdigit():
                    return True, int(value)
        return True, None
    returncode = getattr(completed, "returncode", 0) or 0
    if listed or (returncode == 0 and not text.strip()):
        return False, None
    return None, None


def split_process_name(package: str) -> tuple[str, str | None]:
    """``com.x:remote`` -> (``com.x``, ``remote``): an Android process name is
    the package plus an optional ``:suffix`` for a secondary process."""
    base, sep, suffix = package.partition(":")
    return base, (suffix or None) if sep else None


def logcat_package_filter(adb: str, serial: str, package: str) -> dict[str, Any]:
    """Decide how logcat is narrowed to `package` (uid, pid or none).

    `package` may be a process name (``com.x:remote``): the uid — and so the
    installed check — belongs to the base package, while the pid is that
    process's own. Every process of an app shares its uid, so a uid filter
    cannot single out ``:remote``; that is said in a
    ``process_name_not_filtered`` warning rather than guessed at.

    Raises `app_not_installed` for a package the device does not have: the
    old behaviour — every device line, silently — answered a typo with a
    screenful of someone else's logs.
    """
    base, suffix = split_process_name(package)
    installed, uid = package_uid(adb, serial, base)
    if installed is False:
        raise errors.AutonomError(
            errors.APP_NOT_INSTALLED,
            f"{base} is not installed on {serial}",
            "Check the package id with 'adb shell pm list packages'.",
        )
    api = api_level(adb, serial)
    pid = pid_for_package(adb, serial, package)
    plan: dict[str, Any] = {"filter": "none", "package": base, "process": package,
                            "uid": uid, "pid": pid, "api_level": api, "warnings": []}
    if uid is not None and api is not None and api >= UID_FILTER_MIN_API:
        plan["filter"] = "uid"
    else:
        if api is not None and api < UID_FILTER_MIN_API:
            reason = f"API level {api} predates 'logcat --uid' (API {UID_FILTER_MIN_API})"
        elif api is None:
            reason = "the device's API level could not be read"
        else:
            reason = f"the uid of {base} could not be read"
        if pid:
            plan["filter"] = "pid"
            error = (f"filtered by the running process (pid {pid}), not by uid: {reason}; "
                     "lines from earlier runs of the app are not included")
        else:
            error = (f"{package} is not running and {reason}, so no process filter "
                     "could be applied; only lines that name the package are shown")
        plan["warnings"].append({
            "code": "log_filter_degraded",
            "filter": plan["filter"],
            "error": error,
            "hint": "Launch the app first ('autonom session launch <package>') so its "
                    "pid can be used, or use a device on API 31+ for a uid filter.",
        })
    if suffix and plan["filter"] != "pid":
        plan["warnings"].append({
            "code": "process_name_not_filtered",
            "filter": plan["filter"],
            "error": f"'{package}' names one process of {base}, but every process of "
                     f"the app shares its uid: lines from all of {base}'s processes "
                     "are shown, not only :" + suffix,
            "hint": f"Filter further with --grep, or follow while only {package} "
                    "runs; on an older device the running process's pid narrows it.",
        })
    return plan


def _merge_lines(primary: list[str], extra: list[str]) -> list[str]:
    """Interleave two logcat dumps by timestamp, dropping lines present in
    both (the app's own AndroidRuntime lines are in its uid stream too)."""
    seen = set(primary)
    extra = [line for line in extra if line not in seen]
    merged: list[str] = []
    i = j = 0
    last_a = last_b = ""
    while i < len(primary) or j < len(extra):
        if i < len(primary):
            last_a = parse_logcat_line(primary[i])[0] or last_a
        if j < len(extra):
            last_b = parse_logcat_line(extra[j])[0] or last_b
        if j >= len(extra) or (i < len(primary) and last_a <= last_b):
            merged.append(primary[i])
            i += 1
        else:
            merged.append(extra[j])
            j += 1
    return merged


def _dump(adb: str, serial: str, args: list[str]) -> list[str]:
    completed = adb_mod.run_adb(adb, args, serial=serial, timeout=20, check=True)
    text = completed.stdout if isinstance(completed.stdout, str) else ""
    return text.splitlines()


def tail_logcat_detailed(
    adb: str,
    serial: str,
    *,
    package: str | None = None,
    since_seconds: float | None = 30,
    max_lines: int = 200,
    grep: str | None = None,
) -> dict[str, Any]:
    """`tail_logcat` plus how the lines were narrowed (`filter`) and why a
    narrowing fell short (`warnings`)."""
    pattern = compile_grep(grep)  # refuse a bad pattern before touching the device
    plan = logcat_package_filter(adb, serial, package) if package else None
    window: list[str] = []
    cutoff = None
    if since_seconds is not None and since_seconds > 0:
        cutoff = _device_cutoff(adb, serial, since_seconds)
        if cutoff:
            # Let logcat do the windowing, in its own clock. Dumping the whole
            # buffer and filtering here was both slow (80k+ lines on a busy
            # emulator) and wrong whenever the two clocks had drifted.
            window = ["-t", cutoff]
    base = ["logcat", "-d", "-v", "threadtime", *window]

    def windowed(lines: list[str]) -> list[str]:
        if since_seconds is not None and since_seconds > 0 and not cutoff:
            # Degraded: the device clock was unreadable, so fall back to
            # comparing against the host's. Accurate only while they agree.
            return _filter_recent(lines, since_seconds)
        return lines

    if plan is None:
        lines = windowed(_dump(adb, serial, base))
    elif plan["filter"] == "uid":
        own = windowed(_dump(adb, serial, [*base, f"--uid={plan['uid']}"]))
        lifecycle = windowed(_dump(adb, serial, [*base, "-s", *LIFECYCLE_SPECS]))
        lines = _merge_lines(own, [line for line in lifecycle
                                   if names_package(line, plan["package"])])
    elif plan["filter"] == "pid":
        pid = str(plan["pid"])
        lines = [line for line in windowed(_dump(adb, serial, base))
                 if parse_logcat_line(line)[1] == pid
                 or is_lifecycle_line(line, plan["package"])]
    else:
        lines = [line for line in windowed(_dump(adb, serial, base))
                 if names_package(line, plan["package"])]

    if pattern:
        lines = [line for line in lines if pattern.search(line)]
    if max_lines > 0:
        lines = lines[-max_lines:]
    return {
        "entries": [{"line": line} for line in lines],
        "warnings": list(plan["warnings"]) if plan else [],
        "filter": plan["filter"] if plan else None,
        "uid": plan["uid"] if plan else None,
        "pid": plan["pid"] if plan else None,
    }


def tail_logcat(
    adb: str,
    serial: str,
    *,
    package: str | None = None,
    since_seconds: float | None = 30,
    max_lines: int = 200,
    grep: str | None = None,
) -> list[dict[str, Any]]:
    return tail_logcat_detailed(adb, serial, package=package, since_seconds=since_seconds,
                                max_lines=max_lines, grep=grep)["entries"]


def pid_for_package(adb: str, serial: str, package: str) -> str | None:
    """The single owner of "what is the app's pid" on Android (pidof -s)."""
    completed = adb_mod.run_adb(
        adb,
        ["shell", "pidof", "-s", package],
        serial=serial,
        timeout=10,
        check=False,
    )
    assert isinstance(completed.stdout, str)
    pid = completed.stdout.strip().split()
    return pid[0] if pid else None


def logcat_start_args(adb: str, serial: str) -> list[str]:
    """`-T <device epoch>`: a follow starts at the device's *now*, not the
    whole buffer; `-T 1` when the device clock cannot be read."""
    completed = adb_mod.run_adb(adb, ["shell", "date", "+%s"], serial=serial,
                                timeout=10, check=False)
    epoch = (completed.stdout if isinstance(completed.stdout, str) else "").strip()
    return ["-T", f"{epoch}.000" if epoch.isdigit() else "1"]


UID_STREAM_ENDED = {
    "code": "log_filter_degraded",
    "filter": "uid",
    "reason": "uid_stream_ended",
    "error": "the app's uid logcat ended early; from here on only the lifecycle "
             "lines naming the package are followed",
    "hint": "Re-run 'autonom logs follow'; if it ends again, check 'adb logcat "
            "--uid=<uid>' on the device by hand.",
}


def android_follow_plan(adb: str, serial: str, package: str | None = None, *,
                        from_start: bool = False) -> dict[str, Any]:
    """The logcat process(es) `logs follow --source device` should run.

    Returns ``streams`` — ``(argv, line_filter[, ended_warning])`` entries for
    `follow.follow_processes` — plus the same ``filter`` / ``warnings`` a tail
    reports. A uid follow runs two logcats (the app's uid, and the lifecycle
    tags naming the package) because one logcat cannot OR the two filters;
    if the uid one ends early, the follow says so (`UID_STREAM_ENDED`)
    instead of quietly carrying on with lifecycle lines only.
    """
    start = [] if from_start else logcat_start_args(adb, serial)
    base = [adb, "-s", serial, "logcat", "-v", "time", *start]
    if not package:
        return {"streams": [(base, None)], "filter": None, "warnings": [],
                "uid": None, "pid": None}
    plan = logcat_package_filter(adb, serial, package)
    name = plan["package"]

    def lifecycle(line: str) -> bool:
        return names_package(line, name)

    streams: list[tuple[Any, ...]]
    if plan["filter"] == "uid":
        streams = [
            ([*base, f"--uid={plan['uid']}"], None, UID_STREAM_ENDED),
            ([*base, "-s", *LIFECYCLE_SPECS], lifecycle),
        ]
    elif plan["filter"] == "pid":
        pid = str(plan["pid"])

        def own_or_lifecycle(line: str) -> bool:
            return parse_logcat_line(line)[1] == pid or is_lifecycle_line(line, name)

        streams = [(base, own_or_lifecycle)]
    else:
        streams = [(base, lifecycle)]
    return {"streams": streams, "filter": plan["filter"], "warnings": plan["warnings"],
            "uid": plan["uid"], "pid": plan["pid"]}


_TS = re.compile(r"^(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})")

# No space, so `adb shell date <fmt>` cannot be split into two arguments by the
# device shell — which silently truncated the answer to "08-06".
_DEVICE_CLOCK_FORMAT = "+%m-%d_%H:%M:%S"


def _device_cutoff(adb: str, serial: str, since_seconds: float) -> str | None:
    """A `logcat -t` cutoff expressed in the **device's** wall clock.

    logcat stamps every line with the device clock, so a window computed from
    the host clock is wrong by however far the two have drifted. An emulator
    that outlived a host sleep is routinely tens of seconds behind — enough to
    empty a `--since 30` window entirely while the log is busy, which is
    exactly what happened: `logs tail` reported one irrelevant line while 320
    matching ones sat in the buffer.

    Returns None when the device clock cannot be read; the caller then falls
    back to host-clock filtering and says so.
    """
    completed = adb_mod.run_adb(
        adb, ["shell", "date", _DEVICE_CLOCK_FORMAT],
        serial=serial, timeout=10, check=False,
    )
    text = (completed.stdout or "").strip().replace("_", " ")
    try:
        stamp = time.strptime(f"{time.localtime().tm_year} {text}", "%Y %m-%d %H:%M:%S")
    except ValueError:
        return None
    # Parsing and formatting both go through the host timezone, so it cancels
    # out and what comes back is the device's own wall clock.
    return time.strftime(
        "%m-%d %H:%M:%S.000", time.localtime(time.mktime(stamp) - since_seconds)
    )


def _filter_recent(lines: list[str], since_seconds: float) -> list[str]:
    """Host-clock fallback. Returns exactly what matched — including nothing.

    The previous version substituted `lines[-200:]` when the window came up
    empty, so a filter that had failed returned a plausible-looking tail from
    an unrelated moment and no way to tell. An empty window is an answer.
    """
    cutoff = time.time() - since_seconds
    kept: list[str] = []
    year = time.localtime().tm_year
    for line in lines:
        match = _TS.match(line)
        if not match:
            continue
        try:
            ts = time.strptime(f"{year} {match.group(1)}", "%Y %m-%d %H:%M:%S.%f")
        except ValueError:
            kept.append(line)
            continue
        if time.mktime(ts) >= cutoff:
            kept.append(line)
    return kept


# --- iOS ---------------------------------------------------------------------
#
# Which lines are the app's is decided by `ios_simctl` alone — its predicate
# (`log_predicate` / `app_log_predicate`), its client-side twin
# (`log_line_matches` / `app_log_filter`) and its noise rule
# (`is_log_noise`). With the installed bundle known the match is that bundle
# path, so a second Flutter app (also `Runner`) on the same simulator never
# leaks in; this module only decides *when* to ask.


def app_image(target: Target, bundle_id: str | None, *,
              app_path: str | Path | None = None) -> tuple[str | None, str | None]:
    """``(executable, installed bundle path)`` via `ios_simctl.app_image`
    (cached, never raises); ``(None, None)`` on Android or without a bundle.
    `app_path` (a session's `install_path`) only helps find the executable."""
    if not bundle_id or target.platform == ANDROID:
        return None, None
    return ios_simctl.app_image(target.tool, target.target_id, bundle_id, app_path=app_path)


def app_executable(target: Target, bundle_id: str | None, *,
                   app_path: str | Path | None = None) -> str | None:
    """The app's executable name (``Runner`` for a Flutter app), or None."""
    return app_image(target, bundle_id, app_path=app_path)[0]


def start_log_stream(target: Target, destination: Path, *, bundle_id: str | None = None,
                     executable: str | None = None,
                     app_path: str | Path | None = None,
                     record: dict[str, Any] | None = None) -> int | None:
    """Start a session-long `log stream` writing ndjson to the artifacts dir.

    Returns the pid so `session stop` can reap it (INV-10). The stream runs
    under `ios_simctl.start_log_stream`: filtered by `ios_simctl.log_predicate`
    — to the installed bundle when it resolves, so only this app's lines are
    kept — and capped on disk (AUTONOM_IOS_LOG_MAX_MB, one rotation), so a
    long session cannot fill the disk. The pid is the bounded writer's;
    stopping it stops the stream. With `record`, the writer is also entered in
    the machine process registry under that session, so `processes`,
    `cleanup --all` and `session stop` can all find it. Failure is not
    fatal: logs are supplementary evidence and must never block the UI loop.
    """
    bundle_path = None
    if bundle_id:
        found_executable, bundle_path = app_image(target, bundle_id, app_path=app_path)
        executable = executable or found_executable
    pid = ios_simctl.start_log_stream(target.tool, target.target_id, destination,
                                      bundle_id=bundle_id, executable=executable,
                                      app_path=app_path, bundle_path=bundle_path)
    if pid and record is not None:
        from . import processes

        session_id = record.get("session_id")
        try:
            processes.register(
                "log_stream", pid, owner=session_id, session_id=session_id,
                artifacts_dir=record.get("artifacts_dir"), target_id=target.target_id,
                bundle_id=bundle_id, executable=executable,
                signature=str(destination),
            )
        except OSError:
            pass  # the registry is a safety net; the stream itself is running
    return pid


def start_session_log_stream(target: Target, record: dict[str, Any], *,
                             executable: str | None = None) -> int | None:
    """Start the session's `--log-stream` and record it everywhere it must
    be findable: `background.log_stream_pid`, the `streams[]` catalog and the
    process registry. The session's `install_path` helps resolve the
    executable. The caller saves the record."""
    from . import session as session_mod

    bundle_id = record.get("app_id")
    app_path = record.get("install_path")
    if bundle_id and not executable:
        executable = app_executable(target, bundle_id, app_path=app_path)
    destination = session_mod.artifact_path(record, "logs", "stream.ndjson")
    pid = start_log_stream(target, destination, bundle_id=bundle_id,
                           executable=executable, app_path=app_path, record=record)
    background = record.setdefault("background", {})
    background["log_stream_pid"] = pid
    if executable:
        background["log_stream_executable"] = executable
    if pid:
        session_mod.register_stream(
            record, stream_id="log_stream", kind="device_log",
            path="logs/stream.ndjson", label="ios log stream", pid=pid)
    return pid


def ensure_log_stream(target: Target, record: dict[str, Any]) -> dict[str, Any]:
    """Restart the session's log stream when its writer has died.

    A simulator reboot (`simulator keyboard pin` with a reboot) ends the
    `log stream` child, and with it the bounded writer, so the session kept
    reporting a stream that recorded nothing. A reinstall moves the app to a
    new container, which a running stream's bundle-path predicate cannot
    follow; stop the old writer first and this starts one on the new path.
    Only a session that had a stream gets one back. The caller saves the
    record.
    """
    from . import session as session_mod

    background = record.get("background") or {}
    previous = background.get("log_stream_pid")
    had_stream = bool(previous) or any(
        stream.get("id") == "log_stream" for stream in record.get("streams") or [])
    if target.platform == ANDROID or not had_stream:
        return {"restarted": False, "reason": "no_log_stream"}
    if session_mod.pid_alive(previous):
        return {"restarted": False, "pid": previous, "reason": "alive"}
    pid = start_session_log_stream(target, record,
                                   executable=background.get("log_stream_executable"))
    return {"restarted": bool(pid), "pid": pid, "previous_pid": previous}


def _ios_predicate(bundle_id: str) -> str:
    """Kept for callers of the old name that have no target to resolve the
    app with (the CLI's live follow until it passes one): the subsystem /
    leaf form. `ios_predicate` is the resolved one."""
    return ios_simctl.log_predicate(bundle_id)


def ios_predicate(target: Target, bundle_id: str, *,
                  app_path: str | Path | None = None) -> str:
    """The `log` predicate for the installed app (`ios_simctl.app_log_predicate`)."""
    return ios_simctl.app_log_predicate(target.tool, target.target_id, bundle_id,
                                        app_path=app_path)


def ios_follow_argv(target: Target, package: str | None = None, *,
                    app_path: str | Path | None = None) -> list[str]:
    """The live `log stream` argv for `logs follow --source device` on iOS.

    `log_stream_argv` does not resolve the app itself, so the executable and
    installed bundle path are resolved here (the same `app_image` the
    predicate helpers use) and passed in."""
    executable, bundle_path = app_image(target, package, app_path=app_path)
    return ios_simctl.log_stream_argv(target.tool, target.target_id, bundle_id=package,
                                      executable=executable, bundle_path=bundle_path)


def ios_line_filter(target: Target, package: str | None = None, *,
                    app_path: str | Path | None = None) -> Callable[[str], bool]:
    """The line filter for following an iOS stream: never the `log` tool's
    own chatter (`ios_simctl.is_log_noise`), and — with a package — only the
    app's lines (`ios_simctl.app_log_filter`)."""
    if package:
        return ios_simctl.app_log_filter(target.tool, target.target_id, package,
                                         app_path=app_path)
    return lambda line: not ios_simctl.is_log_noise(line)


def _read_tail(path: Path, max_lines: int) -> list[str]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        # Debug sessions produce modest files; a bounded deque keeps memory flat.
        from collections import deque

        return list(deque(handle, maxlen=max(1, max_lines)))


def tail_ios_detailed(
    target: Target,
    *,
    stream_path: Path | None = None,
    package: str | None = None,
    since_seconds: float | None = 30,
    max_lines: int = 200,
    grep: str | None = None,
    app_path: str | Path | None = None,
) -> dict[str, Any]:
    pattern = compile_grep(grep)
    warnings: list[dict[str, Any]] = []
    lines: list[str] = []
    executable, bundle_path = app_image(target, package, app_path=app_path)

    if stream_path and stream_path.exists():
        lines = [line.rstrip("\n") for line in _read_tail(stream_path, max_lines * 4)]
    else:
        args = ["spawn", target.target_id, "log", "show",
                "--style", "ndjson", "--last", f"{int(since_seconds or 30)}s"]
        if package:
            args += ["--predicate", ios_predicate(target, package, app_path=app_path)]
        completed = ios_simctl.run_simctl(target.tool, args, timeout=30, check=False)
        if completed.returncode != 0:
            warnings.append({
                "code": "log_backend_unavailable",
                "error": (completed.stderr or "").strip()[:400],
                "hint": "Start the session with --log-stream for a continuous buffer.",
            })
        else:
            lines = (completed.stdout or "").splitlines()

    lines = [line for line in lines if line.strip() and not ios_simctl.is_log_noise(line)]
    if package and stream_path:
        lines = [line for line in lines
                 if ios_simctl.log_line_matches(line, package, executable=executable,
                                                bundle_path=bundle_path)]
    if pattern:
        lines = [line for line in lines if pattern.search(line)]
    if max_lines > 0:
        lines = lines[-max_lines:]
    return {
        "entries": [{"line": _compact_ndjson(line)} for line in lines],
        "warnings": warnings,
        "filter": None,
        "executable": executable,
    }


def tail_ios(
    target: Target,
    *,
    stream_path: Path | None = None,
    package: str | None = None,
    since_seconds: float | None = 30,
    max_lines: int = 200,
    grep: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    detail = tail_ios_detailed(target, stream_path=stream_path, package=package,
                               since_seconds=since_seconds, max_lines=max_lines, grep=grep)
    return detail["entries"], detail["warnings"]


def _compact_ndjson(line: str) -> str:
    """Render one ndjson log record as a compact human line; pass through on failure."""
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return line
    if not isinstance(record, dict):
        return line
    parts = [
        record.get("timestamp", ""),
        record.get("processImagePath", "").rsplit("/", 1)[-1],
        record.get("messageType", ""),
        record.get("subsystem", ""),
        record.get("eventMessage", ""),
    ]
    return " ".join(str(part) for part in parts if part).strip() or line


def tail_detailed(
    target: Target,
    *,
    stream_path: Path | None = None,
    package: str | None = None,
    since_seconds: float | None = 30,
    max_lines: int = 200,
    grep: str | None = None,
    app_path: str | Path | None = None,
) -> dict[str, Any]:
    """`tail` with the narrowing reported: ``entries``, ``warnings``,
    ``filter`` (Android: ``uid`` | ``pid`` | ``none``, None without a
    package) and, on iOS, the ``executable`` the predicate used."""
    if target.platform == ANDROID:
        return tail_logcat_detailed(
            target.tool, target.target_id, package=package,
            since_seconds=since_seconds, max_lines=max_lines, grep=grep,
        )
    return tail_ios_detailed(
        target, stream_path=stream_path, package=package,
        since_seconds=since_seconds, max_lines=max_lines, grep=grep, app_path=app_path,
    )


def tail(
    target: Target,
    *,
    stream_path: Path | None = None,
    package: str | None = None,
    since_seconds: float | None = 30,
    max_lines: int = 200,
    grep: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    detail = tail_detailed(target, stream_path=stream_path, package=package,
                           since_seconds=since_seconds, max_lines=max_lines, grep=grep)
    return detail["entries"], detail["warnings"]
