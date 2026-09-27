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
- iOS: `ios_simctl` owns the match — the `log` predicate names the app's
  executable (every Flutter app's is `Runner`), and the client-side filter
  keeps only records whose image UUID is the installed binary's (or one a
  session recorded for it), so a second Flutter `Runner` on the same
  simulator never leaks into this app's lines, and a reinstall — after
  which the log keeps naming the first, deleted container — does not hide
  them.
"""
from __future__ import annotations

import json
import os
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
# (`is_log_noise`). The predicate names the executable, so the `log` tool
# hands over every same-named app's records (and a session's stream file
# holds them raw); the client side keeps the records whose image UUID is
# the app binary's. This module only decides *when* to ask, and remembers
# the UUIDs a session's stream was started for
# (`background.log_stream_image_uuids`), so the file can still be read once
# the app is gone or has been replaced.


def app_image(target: Target, bundle_id: str | None, *,
              app_path: str | Path | None = None) -> tuple[str | None, str | None]:
    """``(executable, installed bundle path)`` via `ios_simctl.app_image`
    (cached, never raises); ``(None, None)`` on Android or without a bundle.
    `app_path` (a session's `install_path`) only helps find the executable."""
    if not bundle_id or target.platform == ANDROID:
        return None, None
    return ios_simctl.app_image(target.tool, target.target_id, bundle_id, app_path=app_path)


def app_identity(target: Target, bundle_id: str | None, *,
                 app_path: str | Path | None = None,
                 ) -> tuple[str | None, str | None, frozenset[str]]:
    """``(executable, installed bundle path, binary UUIDs)`` via
    `ios_simctl.app_identity` (never raises); empty on Android or without a
    bundle."""
    if not bundle_id or target.platform == ANDROID:
        return None, None, frozenset()
    return ios_simctl.app_identity(target.tool, target.target_id, bundle_id,
                                   app_path=app_path)


def recorded_image_uuids(record: dict[str, Any] | None,
                         bundle_id: str | None = None) -> frozenset[str]:
    """The binary UUIDs a session's log stream was started for
    (`background.log_stream_image_uuids`). With `bundle_id`, only when the
    session's app is that bundle: another app's UUIDs must never vouch for
    this one's lines."""
    if not isinstance(record, dict):
        return frozenset()
    if bundle_id is not None and record.get("app_id") != bundle_id:
        return frozenset()
    background = record.get("background")
    if not isinstance(background, dict):
        return frozenset()
    return ios_simctl.normalized_uuids(background.get("log_stream_image_uuids"))


def _stream_record(stream_path: Path | None) -> dict[str, Any] | None:
    """The session record that owns a session's stream file
    (`<artifacts>/logs/stream.ndjson` -> `<artifacts>/session.json`), or
    None. Read-only; never raises."""
    if stream_path is None:
        return None
    candidate = Path(stream_path).parent.parent / "session.json"
    try:
        record = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    try:
        owned = Path(str(record.get("artifacts_dir"))).resolve() == candidate.parent.resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    return record if owned else None


def recorded_line_filter(record: dict[str, Any], package: str | None, *,
                         target: Target | None = None) -> Callable[[str], bool]:
    """The line filter for replaying a session's recorded stream file
    (`logs follow --session-id`). The file holds every same-named app's raw
    records, so the binary UUIDs are what keeps another Flutter `Runner`
    out.

    When `package` is the session's app, the identity the stream recorded
    decides — `log_stream_executable` and `log_stream_image_uuids` — and no
    device is asked: the app may have moved container, been replaced or be
    gone by now. For any other package the recording says nothing about it,
    so its identity is resolved fresh from the installed binary on `target`
    (`ios_line_filter`); without a target only the path rules are left. The
    UUIDs matched on are on the filter as ``image_uuids`` (sorted)."""
    if not package:
        return lambda line: not ios_simctl.is_log_noise(line)
    own = isinstance(record, dict) and record.get("app_id") == package
    if not own and target is not None and target.platform != ANDROID:
        return ios_line_filter(target, package)
    background = record.get("background") if own else None
    executable = (background or {}).get("log_stream_executable") if own else None
    uuids = recorded_image_uuids(record, package)

    def matches(line: str) -> bool:
        return ios_simctl.log_line_matches(line, package, executable=executable, uuids=uuids)

    matches.image_uuids = sorted(uuids)  # type: ignore[attr-defined]
    return matches


def filter_image_uuids(line_filter: Any) -> list[str]:
    """The binary UUIDs a line filter from this module matches on (sorted),
    or [] when it matches on none."""
    values = getattr(line_filter, "image_uuids", None)
    return sorted(ios_simctl.normalized_uuids(values)) if values else []


# --- reading a session's own stream file without --package ---------------------
#
# The session's `--log-stream` writer admits the app's subsystem and every
# process image with the app's executable name, so its file holds every
# same-named app's raw records (every Flutter `Runner` on the simulator).
# They are not dropped when written: the writer cannot know a build that is
# installed while it runs (outside Autonom, or by another tool), and a
# write-time UUID filter would lose that build's records for good. So every
# reader narrows on read — with `--package`, to that app; without it, a read
# of the session's own stream file defaults to the session's app.

SESSION_STREAM_NAMES = ("stream.ndjson", "stream.ndjson.1")
# A package-less read of the current session's file asks the simulator for
# the installed app only when that can add something (the install its stream
# recorded is gone: a reinstall, perhaps another build), and only this long:
# a plain file follow used to need no device at all.
SESSION_PROBE_TIMEOUT = 5.0


def is_session_stream(record: dict[str, Any] | None, path: Path | str | None) -> bool:
    """Is `path` the session's own `--log-stream` file (`logs/stream.ndjson`)
    or its rotation (`.1`)? The same file, not the same spelling: a hard
    link, or `logs/STREAM.ndjson` on a case-insensitive volume, is it too
    (`os.path.samefile`). A file that does not exist yet is compared by its
    resolved path. Never raises."""
    if not isinstance(record, dict) or not record.get("artifacts_dir") or path is None:
        return False
    try:
        logs_dir = Path(str(record["artifacts_dir"])) / "logs"
        candidate = Path(path)
        for name in SESSION_STREAM_NAMES:
            own = logs_dir / name
            if own.is_file() and candidate.is_file() and os.path.samefile(own, candidate):
                return True
        resolved = candidate.resolve()
        return resolved.parent == logs_dir.resolve() and resolved.name in SESSION_STREAM_NAMES
    except (OSError, ValueError, RuntimeError):
        return False


def _session_target(record: dict[str, Any], target: Target | None) -> Target | None:
    """`target` when it is the session's own iOS simulator, else None."""
    if (target is None or target.platform == ANDROID
            or target.target_id != record.get("target_id")):
        return None
    return target


def session_stream_identity(record: dict[str, Any] | None, path: Path | str | None, *,
                            target: Target | None = None,
                            ) -> tuple[str, str | None, str | None, frozenset[str]] | None:
    """``(app_id, executable, bundle path, binary UUIDs)`` a package-less
    read of `path` narrows to, or None to read it raw.

    Only for the session's own stream file (`is_session_stream`) of an iOS
    session with an app. The identity its stream recorded is used at once
    (`log_stream_executable`, `log_stream_image_uuids`); while the install
    it recorded (`log_stream_bundle_path`) is still on disk and its
    Info.plist names the session's app, that binary is read again from disk
    — no device is asked. Only when that install is gone (a reinstall,
    perhaps of another build), belongs to another app or was never
    recorded, and only for the session's own simulator (`target`: the
    current session, never a `--session-id` replay), is the simulator asked
    for the installed app — bounded by `SESSION_PROBE_TIMEOUT`; a probe that
    fails or times out leaves the recorded identity, silently. None as well
    when nothing identifies the app: a filter with nothing to match on would
    drop every line."""
    if not isinstance(record, dict) or (record.get("platform") or ANDROID) == ANDROID:
        return None
    app_id = record.get("app_id")
    if not app_id or not is_session_stream(record, path):
        return None
    app_id = str(app_id)
    background = record.get("background") if isinstance(record.get("background"), dict) else {}
    executable = background.get("log_stream_executable") or None
    uuids = recorded_image_uuids(record, app_id)
    bundle_path = None
    recorded_bundle = background.get("log_stream_bundle_path")
    # the recorded install counts only while its Info.plist names the
    # session's app: a path reused by another app is as good as gone
    if (recorded_bundle and os.path.isdir(str(recorded_bundle))
            and ios_simctl.bundle_identifier(str(recorded_bundle)) == app_id):
        bundle_path = str(recorded_bundle)
        uuids = uuids | ios_simctl.installed_image_uuids(bundle_path, executable, app_id)
    else:
        own_target = _session_target(record, target)
        if own_target is not None:
            found_executable, bundle_path, found = ios_simctl.app_identity(
                own_target.tool, own_target.target_id, app_id,
                app_path=record.get("install_path"), timeout=SESSION_PROBE_TIMEOUT)
            executable = executable or found_executable
            uuids = uuids | found
    if not (executable or bundle_path or uuids):
        return None
    return app_id, executable, bundle_path, uuids


def default_stream_app(record: dict[str, Any] | None, path: Path | str | None, *,
                       target: Target | None = None) -> str | None:
    """The app a package-less read of `path` narrows to
    (`session_stream_identity`), or None when it is read raw: a session
    without an app, an explicit `--path` to any other file, or an app
    nothing identifies."""
    identity = session_stream_identity(record, path, target=target)
    return identity[0] if identity else None


def session_stream_filter(record: dict[str, Any] | None, path: Path | str | None, *,
                          target: Target | None = None) -> Callable[[str], bool] | None:
    """The default line filter for a package-less read of an iOS session's
    own stream file — the session's app, by `session_stream_identity` — or
    None to read it raw. It carries the UUIDs it matches on as
    ``image_uuids``."""
    identity = session_stream_identity(record, path, target=target)
    if identity is None:
        return None
    app_id, executable, bundle_path, uuids = identity

    def matches(line: str) -> bool:
        return ios_simctl.log_line_matches(line, app_id, executable=executable,
                                           bundle_path=bundle_path, uuids=uuids)

    matches.image_uuids = sorted(uuids)  # type: ignore[attr-defined]
    return matches


def app_executable(target: Target, bundle_id: str | None, *,
                   app_path: str | Path | None = None) -> str | None:
    """The app's executable name (``Runner`` for a Flutter app), or None."""
    return app_image(target, bundle_id, app_path=app_path)[0]


def start_log_stream(target: Target, destination: Path, *, bundle_id: str | None = None,
                     executable: str | None = None,
                     app_path: str | Path | None = None,
                     record: dict[str, Any] | None = None,
                     identity: dict[str, Any] | None = None) -> int | None:
    """Start a session-long `log stream` writing ndjson to the artifacts dir.

    Returns the pid so `session stop` can reap it (INV-10). The stream runs
    under `ios_simctl.start_log_stream`: filtered by `ios_simctl.log_predicate`
    — the app's subsystem or its executable, so other apps with the same
    executable name are written too and are dropped when the file is read
    (`tail`, `ios_line_filter`, `recorded_line_filter`) — and capped on disk
    (AUTONOM_IOS_LOG_MAX_MB, one rotation), so a long session cannot fill
    the disk. The pid is the bounded writer's;
    stopping it stops the stream. With `record`, the writer is also entered in
    the machine process registry under that session, so `processes`,
    `cleanup --all` and `session stop` can all find it. Failure is not
    fatal: logs are supplementary evidence and must never block the UI loop.

    With `identity` (a dict), what the writer was started with is put there
    (`executable`, `bundle_path`, `image_uuids`): the answers of the lookups
    made while starting it, down to `ios_simctl.start_log_stream`'s own.
    """
    bundle_path = None
    uuids: frozenset[str] = frozenset()
    if bundle_id:
        found_executable, bundle_path, uuids = app_identity(target, bundle_id,
                                                            app_path=app_path)
        executable = executable or found_executable
    used: dict[str, Any] = {}
    pid = ios_simctl.start_log_stream(target.tool, target.target_id, destination,
                                      bundle_id=bundle_id, executable=executable,
                                      app_path=app_path, bundle_path=bundle_path,
                                      identity=used)
    # a lookup inside can succeed where the ones above missed a moment ago
    executable = used.get("executable") or executable
    bundle_path = used.get("bundle_path") or bundle_path
    uuids = uuids | ios_simctl.normalized_uuids(used.get("image_uuids"))
    if identity is not None:
        identity.update({"executable": executable, "bundle_path": bundle_path,
                         "image_uuids": sorted(uuids)})
    if pid and record is not None:
        from . import processes

        session_id = record.get("session_id")
        try:
            processes.register(
                "log_stream", pid, owner=session_id, session_id=session_id,
                artifacts_dir=record.get("artifacts_dir"), target_id=target.target_id,
                bundle_id=bundle_id, executable=executable,
                signature=writer_signature(destination),
            )
        except OSError:
            pass  # the registry is a safety net; the stream itself is running
    return pid


def start_session_log_stream(target: Target, record: dict[str, Any], *,
                             executable: str | None = None) -> int | None:
    """Start the session's `--log-stream` and record it everywhere it must
    be findable: `background.log_stream_pid`, the `streams[]` catalog and the
    process registry. The session's `install_path` helps resolve the
    executable. What the stream's records are matched by on read is kept
    too — what the writer was actually started with (`start_log_stream`'s
    `identity`), never a lookup of its own that may have missed a moment
    before: `log_stream_executable`, `log_stream_image_uuids` (the installed
    binary's UUIDs, added to those of earlier starts — a reinstall of
    another build keeps the old build's lines readable) and
    `log_stream_bundle_path` (where that binary was installed). The caller
    saves the record."""
    from . import session as session_mod

    bundle_id = record.get("app_id")
    app_path = record.get("install_path")
    destination = session_mod.artifact_path(record, "logs", "stream.ndjson")
    used: dict[str, Any] = {}
    pid = start_log_stream(target, destination, bundle_id=bundle_id,
                           executable=executable, app_path=app_path, record=record,
                           identity=used)
    executable = used.get("executable") or executable
    background = record.setdefault("background", {})
    background["log_stream_pid"] = pid
    if executable:
        background["log_stream_executable"] = executable
    if used.get("bundle_path"):
        background["log_stream_bundle_path"] = str(used["bundle_path"])
    known = recorded_image_uuids(record) | ios_simctl.normalized_uuids(used.get("image_uuids"))
    if known:
        background["log_stream_image_uuids"] = sorted(known)
    if pid:
        session_mod.register_stream(
            record, stream_id="log_stream", kind="device_log",
            path="logs/stream.ndjson", label="ios log stream", pid=pid)
    return pid


def stream_destination(record: dict[str, Any]) -> Path:
    """The session's `--log-stream` file — also the writer's registry
    signature, since the bounded writer carries it in its argv."""
    return Path(record["artifacts_dir"]) / "logs" / "stream.ndjson"


def _writer_mark() -> str:
    """A line of the bounded writer's own script (`ios_simctl.spawn_bounded`
    runs it as `python -c <script> <dest> <cap> <argv...>`). The script
    reaches `ps` verbatim except for its newlines (macOS prints them as
    `\\012`), and this line has none, so it identifies the writer on any
    host — a `tail -f` of the same file does not carry it."""
    return next(line.strip() for line in
                ios_simctl._BOUNDED_WRITER.splitlines()  # noqa: SLF001 - same writer
                if "sys.argv[1]" in line)


def writer_signature(destination: Path | str) -> list[str]:
    """The registry signature of the writer for `destination`: its script
    mark, and the file as a whole argument followed by the next one (the
    cap), so `<file>.bak` never matches (`processes.signature_matches`)."""
    return [_writer_mark(), f" {destination} "]


def is_writer_command(command: str, destination: Path | str) -> bool:
    """Is `command` (a `ps` command line) the bounded writer of `destination`?

    It must carry the writer's script mark and `destination` as a whole
    argument followed by the numeric cap — the argv shape
    `ios_simctl.spawn_bounded` produces. `tail -f <file>`, a writer of
    `<file>.bak` or `<file>.1`, or any process merely naming the file is
    not it."""
    if _writer_mark() not in command:
        return False
    token = re.compile(r"(?:^|\s)" + re.escape(str(destination)) + r"\s+\d+(?:\s|$)")
    return token.search(command) is not None


def _writer_state(record: dict[str, Any], pid: int | None) -> tuple[str, str | None]:
    """`log_writer_state` plus the command line it was judged on (None
    when `ps` was not asked or could not answer)."""
    from . import processes
    from . import session as session_mod

    if not pid or not session_mod.pid_alive(pid):
        return "gone", None
    command = processes.command_of(pid)
    if command is None:
        return ("unverified" if session_mod.pid_alive(pid) else "gone"), None
    destination = stream_destination(record)
    if is_writer_command(command, destination):
        return "ours", command
    text = str(destination)
    if _writer_mark() in command and not text.isascii() and text not in command:
        # A bounded writer whose file `ps` did not render as given: a
        # non-ASCII path shown as meta escapes (`cafM-CM-)` under LC_ALL=C)
        # cannot be told from another writer's file: it proves nothing.
        return "unverified", command
    return "pid_reused", command


def log_writer_state(record: dict[str, Any], pid: int | None) -> str:
    """Is `pid` still this session's log-stream writer?

    ``gone`` (not running), ``ours`` (its command line is this session's
    bounded writer, `is_writer_command`), ``pid_reused`` (alive, but running
    something else) or ``unverified`` (alive, and `ps` could not say — or
    rendered a bounded writer whose non-ASCII stream path it did not show
    as given). A pid recorded in a session file can be days old and belong
    to anything by now, so a live pid alone proves nothing."""
    return _writer_state(record, pid)[0]


def stop_log_writer(record: dict[str, Any], pid: int | None) -> dict[str, Any]:
    """Stop the session's log-stream writer `pid` — only once it is shown to be it.

    `log_writer_state` decides first, whatever the registry says; the stop
    then goes through the registry row when there is one
    (`processes.terminate_entry` checks the row's own signature too). Returns
    ``{"pid", "result"}`` with `terminate_entry`'s vocabulary:
    ``terminated``, ``already_exited``, ``pid_reused``,
    ``unverified_skipped`` (nothing is signalled for the last two), or
    ``none`` when there was no pid.

    The registry row (``kind: log_stream`` with this pid) is dropped only
    when it no longer names a live process it describes: the pid is gone
    (``already_exited``), it was stopped (``terminated``), or it is
    ``pid_reused`` *and* its command line does not carry the row's own
    signature (`processes.entry_matches`) — proven to be someone else's.
    It is kept when `ps` could not answer or render the command
    (``unverified_skipped``), when termination failed, and when the row's
    signature still matches the live process even though it is not this
    session's writer: the row still tracks a live writer, and dropping it
    would leave `processes` and `cleanup --all` blind to it."""
    from . import processes
    from . import session as session_mod

    if not pid:
        return {"pid": None, "result": "none"}
    row = next((entry for entry in processes.entries()
                if entry.get("pid") == pid and entry.get("kind") == "log_stream"), None)
    state, command = _writer_state(record, pid)
    if state == "ours":
        # through the row when there is one: it re-checks its own signature
        if row is not None:
            result = processes.terminate_entry(row)
        else:
            result = "terminated" if session_mod.terminate_pid(pid) else "already_exited"
    else:
        result = {"gone": "already_exited", "pid_reused": "pid_reused",
                  "unverified": "unverified_skipped"}[state]
    if row is not None:
        drop = result in ("terminated", "already_exited")
        if result == "pid_reused":
            if command is None:
                command = processes.command_of(int(pid))
            # ps silent now: gone in the meantime, or unverifiable (keep)
            drop = (not session_mod.pid_alive(int(pid))) if command is None else \
                not processes.entry_matches(row, command)
        if drop:
            processes.deregister(int(pid))  # the row no longer names a live writer
    return {"pid": pid, "result": result}


def ensure_log_stream(target: Target, record: dict[str, Any]) -> dict[str, Any]:
    """Restart the session's log stream when its writer is gone.

    A simulator reboot (`simulator keyboard pin` with a reboot) ends the
    `log stream` child, and with it the bounded writer, so the session kept
    reporting a stream that recorded nothing. After a reinstall the caller
    stops the old writer first (`stop_log_writer`) and this starts one that
    records the reinstalled binary's UUIDs next to the earlier ones (the
    predicate names the executable, so the old stream would have kept
    matching; a different build has different UUIDs). "Gone" is decided by
    `log_writer_state`, never by a live pid alone: a recorded pid that now
    runs something else is not the writer, and a new stream is started
    (``reason: pid_reused``). One that `ps` cannot vouch for is left alone
    and no second writer is started (``reason: unverified``). Only a session
    that had a stream gets one back. The caller saves the record.
    """
    background = record.get("background") or {}
    previous = background.get("log_stream_pid")
    had_stream = bool(previous) or any(
        stream.get("id") == "log_stream" for stream in record.get("streams") or [])
    if target.platform == ANDROID or not had_stream:
        return {"restarted": False, "reason": "no_log_stream"}
    state = log_writer_state(record, previous)
    if state == "ours":
        return {"restarted": False, "pid": previous, "reason": "alive"}
    if state == "unverified":
        return {"restarted": False, "pid": previous, "reason": "unverified"}
    pid = start_session_log_stream(target, record,
                                   executable=background.get("log_stream_executable"))
    outcome: dict[str, Any] = {"restarted": bool(pid), "pid": pid, "previous_pid": previous}
    if state == "pid_reused":
        outcome["previous_state"] = "pid_reused"
    return outcome


def ios_predicate(target: Target, bundle_id: str, *,
                  app_path: str | Path | None = None) -> str:
    """The `log` predicate for the installed app (`ios_simctl.app_log_predicate`)."""
    return ios_simctl.app_log_predicate(target.tool, target.target_id, bundle_id,
                                        app_path=app_path)


def ios_follow_argv(target: Target, package: str | None = None, *,
                    app_path: str | Path | None = None) -> list[str]:
    """The live `log stream` argv for `logs follow --source device` on iOS.

    `log_stream_argv` does not resolve the app itself, so the executable is
    resolved here (the same `app_image` the predicate helpers use) and
    passed in. The predicate admits every app with that executable name:
    pair it with `ios_line_filter(target, package)` to keep only this app's
    records."""
    executable, bundle_path = app_image(target, package, app_path=app_path)
    return ios_simctl.log_stream_argv(target.tool, target.target_id, bundle_id=package,
                                      executable=executable, bundle_path=bundle_path)


def ios_line_filter(target: Target, package: str | None = None, *,
                    app_path: str | Path | None = None,
                    record: dict[str, Any] | None = None) -> Callable[[str], bool]:
    """The line filter for following an iOS stream: never the `log` tool's
    own chatter (`ios_simctl.is_log_noise`), and — with a package — only the
    app's lines (`ios_simctl.app_log_filter`: the installed binary's UUIDs,
    plus those `record` — a session of that app — recorded for its stream)."""
    if package:
        return ios_simctl.app_log_filter(target.tool, target.target_id, package,
                                         app_path=app_path,
                                         extra_uuids=recorded_image_uuids(record, package))
    return lambda line: not ios_simctl.is_log_noise(line)


def _read_tail(path: Path, max_lines: int,
               keep: Callable[[str], bool] | None = None) -> list[str]:
    """The last `max_lines` lines of `path` (newlines stripped) that `keep`
    accepts. Filtering while reading matters for a session's stream file:
    it holds every same-named app's records, and the app's own may be few
    among the last lines."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        # Debug sessions produce modest files; a bounded deque keeps memory flat.
        from collections import deque

        lines = (line.rstrip("\n") for line in handle)
        if keep is not None:
            lines = (line for line in lines if keep(line))
        return list(deque(lines, maxlen=max(1, max_lines)))


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
    """The app's recent lines, from the session's stream file when there is
    one, else from `log show`. Both hold every record the predicate admits —
    any app with the same executable name — so with `package` both are
    narrowed here (`ios_simctl.log_line_matches`) by the installed binary's
    UUIDs plus those the stream file's session recorded
    (`log_stream_image_uuids`). Without `package`, the session's own stream
    file is narrowed to the session's app (`default_stream_app`); `log show`
    without one is the whole device log, as before."""
    pattern = compile_grep(grep)
    warnings: list[dict[str, Any]] = []
    lines: list[str] = []
    owner = _stream_record(stream_path) if stream_path is not None else None
    defaulted = (session_stream_identity(owner, stream_path, target=target)
                 if not package and stream_path is not None and stream_path.exists()
                 else None)
    if defaulted is not None:
        # the session's own stream without --package: the session's app
        wanted, executable, bundle_path, uuids = defaulted
    else:
        wanted = package
        executable, bundle_path, uuids = app_identity(target, wanted, app_path=app_path)
        if wanted:
            uuids = uuids | recorded_image_uuids(owner, wanted)
            if not executable and owner is not None and owner.get("app_id") == wanted:
                executable = (owner.get("background") or {}).get("log_stream_executable")

    def ours(line: str) -> bool:
        if not line.strip() or ios_simctl.is_log_noise(line):
            return False
        return not wanted or ios_simctl.log_line_matches(
            line, wanted, executable=executable, bundle_path=bundle_path, uuids=uuids)

    if stream_path and stream_path.exists():
        lines = _read_tail(stream_path, max_lines * 4, ours)
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
            lines = [line for line in (completed.stdout or "").splitlines() if ours(line)]

    if pattern:
        lines = [line for line in lines if pattern.search(line)]
    if max_lines > 0:
        lines = lines[-max_lines:]
    detail: dict[str, Any] = {
        "entries": [{"line": _compact_ndjson(line)} for line in lines],
        "warnings": warnings,
        "filter": None,
        "executable": executable,
    }
    if wanted and uuids:
        detail["image_uuids"] = sorted(uuids)
    return detail


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
    package) and, on iOS, the ``executable`` the predicate used and — when
    records were matched by binary UUID — the sorted ``image_uuids``."""
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
