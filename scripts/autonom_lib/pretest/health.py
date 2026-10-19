"""Device health since the baseline, and automatic findings (tp-010).

``compute`` compares the device with ``baseline.json`` and returns
``autonom.pretest-health/v1``::

    {"schema", "computed_at",
     "crashes":  {"state", "new":    [{"name", "time", "check_id"}]},
     "logs":     {"state", "groups": [{"pattern", "count", "first_ts", "check_id", "level"}]},
     "requests": {"state", "failed": [{"id", "method", "host", "path", "status",
                                       "reason": "5xx|4xx-unmocked|incomplete", "check_id"}]},
     "metrics":  {"state", "leads":  [{"metric", "before", "after", "delta", "check_id"}]}}

Each block is ``available`` or ``unavailable`` with a ``reason``. A block
whose baseline was unavailable stays unavailable: without a starting point
"nothing new" cannot be told from "could not look". Its list is then empty
and means nothing. The one exception is ``requests``: the baseline records
no traffic only when ``network/flows.jsonl`` did not exist yet, so every row
in it now is newer than the baseline. Such a block is available with
``"basis": "clock"`` and keeps only rows started at or after the baseline's
``taken_at``; a block read past the baseline's line count says
``"basis": "line_count"``. With neither a line count nor a baseline time the
block is unavailable.

- crashes: crash entries whose name was not in the baseline. Android's crash
  buffer has one entry per line, so consecutive new lines of one process are
  one crash. When the baseline's name list was capped, an unknown entry is
  old only if it is provably older: on Android it comes before a known line
  in the buffer, or its day is before the newest baseline crash's day;
  elsewhere its time is before the newest baseline crash's time. An entry on
  the same day or second is reported rather than dropped.
- logs: the app's error-level lines since the baseline's log cursor, grouped
  by message with quoted values, hex ids and digits replaced; at most
  ``MAX_LOG_GROUPS`` groups (the most frequent), never the lines themselves.
  An error-level line is a logcat ``E``/``F``/``A`` line (iOS ``error`` or
  ``fault``), or an app line of any level whose message is a structured log
  record at error level (``app_level``: JSON with ``"level"`` ``error``,
  ``fatal`` or ``severe``, any case, or the same as ``level=error``), or a
  Flutter error marker (``FLUTTER_ERROR``, ``══╡ EXCEPTION CAUGHT``).
  Android reads logcat for the app, error lines only, before any cut; iOS
  reads the session's log stream file narrowed to the app the same way every
  other reader of that file is (``logs.recorded_line_filter``). When more
  than ``MAX_LOG_LINES`` error lines were read, the oldest are left out and
  the block says how many (``truncated_lines``).
- requests: flows appended to ``network/flows.jsonl`` after the baseline
  that answered 5xx, answered 4xx without a mock, or never completed. A flow
  the proxy answered or held from a mock (``mocked: true``) is never a
  failure: the test asked for that answer. That includes a request a
  ``hang`` mock held until the cap (``error_kind: hang_cap``): the proxy marks
  the mock before the hold, so such a row is always mocked. Nor is a request
  the app itself cancelled (``completed: false``, ``error_kind:
  client_abort``). An unmocked request that never completed for another
  reason (an upstream error, a timeout) is a failure ("Request never
  completed"). A line that is not a JSON object, or is nested too deep to
  read, is skipped. Each row
  carries its ``line`` in the file; flow ids restart with every proxy
  process, so a row whose id is not unique in the file says
  ``duplicate_id: true`` and gets no ``request:<id>`` evidence ref (it would
  resolve to another row).
- metrics: one fresh snapshot against the baseline's. Memory is a lead when
  it grew by both 20 % and 20 MB, CPU when it rose by 50 points. One snapshot
  proves nothing, so these are only ever called leads.

Attribution is by wall clock on the host: an item belongs to the check whose
``began_at``..``ended_at`` window (open end for the open check) contains its
time; otherwise to no check. Device clocks are read as host-local time, so a
device whose clock disagrees with the host attributes by its own error.

``auto_findings`` turns each item into one finding (``origin: "auto"``,
``severity: "info"``, ``confirmed: false``), at most once per ``data.key``
(``crash:<name>``, ``log:<digest>``, ``request:<id>@<started at>``,
``metrics:<metric>``), so health can run any number of times. Automatic findings never touch a
check verdict; the ticket verdict ignores them until a reviewer confirms one.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .. import device_state, errors, logs
from ..metrics import snapshot as snapshot_mod
from ..network import redact
from ..network import store as network_store
from . import baseline as baseline_mod
from . import checks as checks_mod
from . import findings as findings_mod
from . import store

HEALTH_SCHEMA = "autonom.pretest-health/v1"
HEALTH_FILE = "health.json"

MAX_LOG_GROUPS = 50
MAX_TRACKED_GROUPS = 5000
MAX_LOG_LINES = 20000
MAX_PATTERN = 300
MAX_NEW_CRASHES = 100
MAX_FAILED_REQUESTS = 200
MEMORY_RATIO = 0.20
MEMORY_MIN_KB = 20 * 1024
CPU_POINTS = 50.0
LEVELS_ANDROID = {"E": "error", "F": "fatal", "A": "assert"}
LEVELS_IOS = {"error": "error", "fault": "fault"}
REASONS = ("5xx", "4xx-unmocked", "incomplete")


class _Unavailable(Exception):
    """A part of the device that cannot be read right now."""


def _unavailable(reason: str, key: str) -> dict[str, Any]:
    return {"state": "unavailable", "reason": store.clip(reason, 300) or "unavailable",
            key: []}


def _reason(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return f"{code}: {message}" if code else message


def _baseline_reason(block: Any) -> str:
    why = block.get("reason") if isinstance(block, dict) else None
    return f"unavailable at the baseline: {why or 'not recorded'}"


# --- time and attribution ---------------------------------------------------------


def _iso(stamp: datetime) -> str:
    stamp = stamp.astimezone(timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


def _to_datetime(ts: Any) -> datetime | None:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(ts, str):
        return checks_mod.parse_ts(ts)
    return None


def attribute(ts: Any, checks: Iterable[dict[str, Any]]) -> str | None:
    """The id of the check whose window contains ``ts`` (ISO text, datetime
    or epoch seconds), else None. A window runs from ``began_at`` to
    ``ended_at``; the open check's window has no end. A check that never
    began has no window."""
    stamp = _to_datetime(ts)
    if stamp is None:
        return None
    for check in checks or ():
        if not isinstance(check, dict):
            continue
        began = checks_mod.parse_ts(check.get("began_at"))
        if began is None or stamp < began:
            continue
        ended = checks_mod.parse_ts(check.get("ended_at"))
        if ended is None:
            if check.get("state") == "open":
                return check.get("check_id")
            continue
        if stamp <= ended:
            return check.get("check_id")
    return None


def _local_device_time(text: str) -> datetime | None:
    """``MM-DD HH:MM:SS.mmm`` (logcat) or ``YYYY-MM-DD HH:MM:SS[.f]`` read as
    host-local time; a logcat stamp more than a day ahead is last year's."""
    text = " ".join(text.split())
    now = datetime.now().astimezone()
    for fmt, with_year in (("%Y-%m-%d %H:%M:%S.%f", False), ("%Y-%m-%d %H:%M:%S", False),
                           ("%Y-%m-%d %H:%M:%S.%f", True), ("%Y-%m-%d %H:%M:%S", True)):
        try:
            candidate = f"{now.year}-{text}" if with_year else text
            naive = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        stamp = naive.astimezone()
        if with_year and stamp - now > timedelta(days=1):
            try:
                stamp = naive.replace(year=naive.year - 1).astimezone()
            except ValueError:  # 29 February of last year
                return None
        return stamp.astimezone(timezone.utc)
    return None


def _any_time(value: Any) -> datetime | None:
    """An ISO time with a zone as given; one without a zone (a device's
    wall clock) as host-local time."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        stamp = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return _local_device_time(text)
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return stamp.astimezone(timezone.utc)


# --- crashes ------------------------------------------------------------------------


def _crash_pid(entry: dict[str, Any]) -> str | None:
    raw = str(entry.get("raw") or "")
    _stamp, pid, _tag = logs.parse_logcat_line(raw)
    return pid


def _crash_time(entry: dict[str, Any]) -> tuple[Any, datetime | None]:
    raw = str(entry.get("raw") or "")
    if entry.get("name") is None and raw:
        stamp, _pid, _tag = logs.parse_logcat_line(raw)
        if stamp:
            parsed = _local_device_time(stamp)
            return (_iso(parsed) if parsed else stamp), parsed
    date = entry.get("date")
    parsed = _any_time(date)
    return (_iso(parsed) if parsed else date), parsed


_DAY = re.compile(r"(?:(\d{4})-)?(\d\d)-(\d\d)")


def _watermark(value: Any) -> tuple[str, Any] | None:
    """The newest baseline crash's time: ``("day", date)`` for Android's
    ``MM-DD`` (a day more than one ahead of today is last year's), else
    ``("exact", datetime)``; None when it cannot be read."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    match = _DAY.fullmatch(text)
    if match:
        today = datetime.now().astimezone().date()
        year = int(match.group(1)) if match.group(1) else today.year
        try:
            day = date(year, int(match.group(2)), int(match.group(3)))
            if not match.group(1) and day - today > timedelta(days=1):
                day = day.replace(year=year - 1)
        except ValueError:
            return None
        return "day", day
    stamp = _any_time(text)
    return ("exact", stamp) if stamp else None


def _older_than_baseline(entry: dict[str, Any], index: int, last_known: int,
                         mark: tuple[str, Any] | None) -> bool:
    """Only for a capped baseline: is this unknown entry provably older
    than the baseline? Ties and unreadable times are not, so a new crash is
    never dropped for sharing a day or a second with an old one."""
    if entry.get("name") is None and index < last_known:
        return True  # the crash buffer is in order: before a known line is older
    if mark is None:
        return False
    _shown, parsed = _crash_time(entry)
    if parsed is None:
        return False
    if mark[0] == "day":
        return parsed.astimezone().date() < mark[1]
    return parsed < mark[1]


def _crashes(baseline: dict[str, Any], record: dict[str, Any], target: Any,
             checks: list[dict[str, Any]]) -> dict[str, Any]:
    base = baseline.get("crashes")
    if not isinstance(base, dict) or base.get("state") != "available":
        return _unavailable(_baseline_reason(base), "new")
    if target is None or not hasattr(target, "platform"):
        return _unavailable("no device target to read crashes from", "new")
    try:
        entries = device_state.crash_list(target, record.get("app_id"))
    except Exception as exc:  # noqa: BLE001 — recorded, never fatal
        return _unavailable(_reason(exc), "new")
    entries = [entry for entry in entries or [] if isinstance(entry, dict)]
    names = [str(name) for name in base.get("names") or []]
    known = set(names)
    latest = base.get("latest") if isinstance(base.get("latest"), dict) else None
    capped = len(names) >= baseline_mod.MAX_CRASH_NAMES
    mark = _watermark(latest.get("time")) if capped and latest else None
    last_known = max((index for index, entry in enumerate(entries)
                      if entry.get("name") is None
                      and baseline_mod.crash_name(entry) in known), default=-1)
    groups: list[tuple[dict[str, Any], str | None]] = []
    for index, entry in enumerate(entries):
        if baseline_mod.crash_name(entry) in known:
            continue
        if capped and _older_than_baseline(entry, index, last_known, mark):
            continue  # older than the baseline, only missing from the capped names
        pid = _crash_pid(entry) if entry.get("name") is None else None
        if pid is not None and groups and groups[-1][1] == pid:
            continue  # one Android crash spans many buffer lines of one process
        groups.append((entry, pid))
    new: list[dict[str, Any]] = []
    for entry, _pid in groups[:MAX_NEW_CRASHES]:
        shown, parsed = _crash_time(entry)
        new.append({"name": store.clip(baseline_mod.crash_name(entry), 200),
                    "time": store.clip(shown, 100) if shown is not None else None,
                    "check_id": attribute(parsed, checks) if parsed else None})
    block: dict[str, Any] = {"state": "available", "new": new}
    if len(groups) > MAX_NEW_CRASHES:
        block["omitted"] = len(groups) - MAX_NEW_CRASHES
    return block


# --- logs ---------------------------------------------------------------------------

_QUOTED_DOUBLE = re.compile(r'"[^"\n]*+"')
_QUOTED_SINGLE = re.compile(r"(?<![A-Za-z])'[^'\n]*+'")
_UUID = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{8}+-[0-9A-Fa-f]{4}+-[0-9A-Fa-f]{4}+"
                   r"-[0-9A-Fa-f]{4}+-[0-9A-Fa-f]{12}+(?![0-9A-Za-z])")
_HEX_PREFIXED = re.compile(r"(?<![0-9A-Za-z])0[xX][0-9A-Fa-f]++(?![0-9A-Za-z])")
_HEX_LONG = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{8,}+(?![0-9A-Za-z])")
_DIGITS = re.compile(r"\d++")
_SPACES = re.compile(r"\s++")
_LOGCAT_HEAD = re.compile(r"^(\d\d-\d\d\s\d\d:\d\d:\d\d\.\d{3})\s++\d++\s++\d++\s++([VDIWEFA])\s")

PATTERNS = {"_QUOTED_DOUBLE": _QUOTED_DOUBLE, "_QUOTED_SINGLE": _QUOTED_SINGLE,
            "_UUID": _UUID, "_HEX_PREFIXED": _HEX_PREFIXED, "_HEX_LONG": _HEX_LONG,
            "_DIGITS": _DIGITS, "_SPACES": _SPACES, "_LOGCAT_HEAD": _LOGCAT_HEAD}


def _hex_id(match: re.Match[str]) -> str:
    text = match.group(0)
    return "<hex>" if any(ch.isdigit() for ch in text) else text


def normalize_message(message: str) -> str:
    """The group key of a log message: credentials masked first, then quoted
    values, ids, hex and digits replaced, whitespace collapsed, and the
    result cut to ``MAX_PATTERN`` characters."""
    text = store.redact_text(str(message)) or ""
    text = _QUOTED_DOUBLE.sub('"<v>"', text)
    text = _QUOTED_SINGLE.sub("'<v>'", text)
    text = _UUID.sub("<id>", text)
    text = _HEX_PREFIXED.sub("<hex>", text)
    text = _HEX_LONG.sub(_hex_id, text)
    text = _DIGITS.sub("N", text)
    text = _SPACES.sub(" ", text).strip()
    return store.clip(text, MAX_PATTERN) or ""


# Structured app log records at error level, and Flutter's error markers:
# counted whatever logcat priority (or iOS level) carried them.
APP_ERROR_LEVELS = {"error": "error", "fatal": "fatal", "severe": "error"}
_JSON_LEVEL = re.compile(r'"level"\s*+:\s*+"(error|fatal|severe)"', re.IGNORECASE)
_PAIR_LEVEL = re.compile(r'(?<![\w.-])level\s*+=\s*+"?(error|fatal|severe)\b', re.IGNORECASE)
FLUTTER_MARKERS = ("FLUTTER_ERROR", "\u2550\u2550\u2561 EXCEPTION CAUGHT")

# Asked of logcat so the line cap applies to error lines only: a chatty app's
# debug lines must never push an earlier error out of the window. The second
# half keeps app lines of any priority that carry an error record or marker
# (``app_level`` decides; the regex only narrows what is read).
ANDROID_ERROR_GREP = (r"^\d\d-\d\d\s+\d\d:\d\d:\d\d\.\d{3}\s+\d+\s+\d+\s+[EFA]\s"
                      r"|level[\"']?\s*[:=]\s*\"?(?:error|fatal|severe)"
                      r"|FLUTTER_ERROR|EXCEPTION CAUGHT")


def app_level(message: str) -> str | None:
    """``error`` or ``fatal`` when ``message`` is an app log record at that
    level (JSON ``"level"`` or ``level=``, case-insensitive; ``severe`` counts
    as ``error``) or a Flutter error marker; None otherwise."""
    if not isinstance(message, str) or not message:
        return None
    if any(marker in message for marker in FLUTTER_MARKERS):
        return "error"
    brace = message.find("{")
    if brace >= 0:
        try:
            record = json.loads(message[brace:])
        except (ValueError, RecursionError):  # not JSON, or nested past the limit
            record = None
        if isinstance(record, dict):
            level = record.get("level")
            if isinstance(level, str) and level.strip().lower() in APP_ERROR_LEVELS:
                return APP_ERROR_LEVELS[level.strip().lower()]
            return None
    match = _JSON_LEVEL.search(message) or _PAIR_LEVEL.search(message)
    return APP_ERROR_LEVELS[match.group(1).lower()] if match else None


def _keep_newest(lines: list[tuple]) -> tuple[list[tuple], int]:
    """At most ``MAX_LOG_LINES`` lines (the newest) and how many were cut."""
    cut = max(0, len(lines) - MAX_LOG_LINES)
    return (lines[cut:] if cut else lines), cut


def _android_lines(target: Any, app_id: str, cursor: datetime | None) -> list[tuple]:
    since = None
    if cursor is not None:
        since = max(1.0, (datetime.now(timezone.utc) - cursor).total_seconds() + 5.0)
    # max_lines=0: no cut inside the reader; the grep keeps only error levels.
    detail = logs.tail_logcat_detailed(target.tool, target.target_id, package=app_id,
                                       since_seconds=since, max_lines=0,
                                       grep=ANDROID_ERROR_GREP)
    out: list[tuple] = []
    for entry in detail.get("entries") or []:
        line = entry.get("line") if isinstance(entry, dict) else None
        if not isinstance(line, str):
            continue
        match = _LOGCAT_HEAD.match(line)
        if not match:
            continue
        rest = line[match.end():].lstrip()
        tag, sep, message = rest.partition(": ")
        text = f"{tag.strip()}: {message}" if sep else rest
        level = LEVELS_ANDROID.get(match.group(2)) or app_level(message if sep else rest)
        if not level:
            continue
        out.append((_local_device_time(match.group(1)), level, text))
    return out


def _ios_lines(session: dict[str, Any], base: dict[str, Any], app_id: str,
               target: Any) -> list[tuple]:
    stream = logs.stream_destination(session)
    if not stream.is_file():
        raise _Unavailable("iOS app logs are read from the session log stream; "
                           "start the session with --log-stream")
    # The stream file holds every same-named app's records (every Flutter
    # Runner): narrow to this app before anything is read from a line.
    keep = logs.recorded_line_filter(session, app_id, target=target)
    skip = base.get("stream_lines") if base.get("stream") else 0
    skip = skip if isinstance(skip, int) and not isinstance(skip, bool) and skip > 0 else 0
    with stream.open("r", encoding="utf-8", errors="replace") as handle:
        raw_lines = handle.readlines()
    if len(raw_lines) < skip:
        skip = 0  # rotated since the baseline: the cursor time decides alone
    out: list[tuple] = []
    for line in raw_lines[skip:]:
        line = line.rstrip("\r\n")
        if not line.strip() or not keep(line):
            continue
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(item, dict):
            continue
        level = (LEVELS_IOS.get(str(item.get("messageType") or "").lower())
                 or app_level(str(item.get("eventMessage") or "")))
        if not level:
            continue
        stamp = None
        raw_ts = item.get("timestamp")
        if isinstance(raw_ts, str):
            text = raw_ts.strip()
            fixed = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", text, count=1)
            stamp = checks_mod.parse_ts(fixed.replace(" ", "T", 1)) or _local_device_time(text)
        out.append((stamp, level, str(item.get("eventMessage") or "")))
    return out


def _logs(session: dict[str, Any], baseline: dict[str, Any], record: dict[str, Any],
          target: Any, checks: list[dict[str, Any]]) -> dict[str, Any]:
    base = baseline.get("logs")
    if not isinstance(base, dict) or base.get("state") != "available":
        return _unavailable(_baseline_reason(base), "groups")
    app_id = record.get("app_id")
    if not app_id:
        return _unavailable("no app id; app logs are read per app", "groups")
    if target is None or not hasattr(target, "platform"):
        return _unavailable("no device target to read logs from", "groups")
    cursor = checks_mod.parse_ts(base.get("cursor_ts"))
    try:
        if target.platform == "android":
            lines = _android_lines(target, app_id, cursor)
        elif target.platform == "ios":
            lines = _ios_lines(session, base, app_id, target)
        else:
            raise _Unavailable(f"no log reader for platform {target.platform!r}")
    except Exception as exc:  # noqa: BLE001 — recorded, never fatal
        return _unavailable(_reason(exc), "groups")
    lines = [line for line in lines
             if cursor is None or line[0] is None or line[0] >= cursor]
    lines, truncated = _keep_newest(lines)
    groups: dict[str, dict[str, Any]] = {}
    total = untracked = 0
    for stamp, level, message in lines:
        total += 1
        pattern = normalize_message(message)
        if not pattern:
            continue
        group = groups.get(pattern)
        if group is None:
            if len(groups) >= MAX_TRACKED_GROUPS:
                untracked += 1
                continue
            group = groups[pattern] = {
                "pattern": pattern, "count": 0,
                "first_ts": _iso(stamp) if stamp else None, "last_ts": None,
                "check_id": attribute(stamp, checks) if stamp else None,
                "level": level}
        group["count"] += 1
        if stamp is not None:
            group["last_ts"] = _iso(stamp)
            if group["first_ts"] is None:
                group["first_ts"] = _iso(stamp)
                group["check_id"] = attribute(stamp, checks)
    ranked = sorted(groups.values(), key=lambda g: (-g["count"], g["first_ts"] or ""))
    block: dict[str, Any] = {"state": "available", "groups": ranked[:MAX_LOG_GROUPS],
                             "lines": total}
    omitted = max(0, len(ranked) - MAX_LOG_GROUPS)
    if omitted or untracked:
        block["omitted_groups"] = omitted
        block["omitted_lines"] = untracked + sum(g["count"] for g in ranked[MAX_LOG_GROUPS:])
    if truncated:
        # The oldest error lines were never grouped: the window is incomplete.
        block["truncated_lines"] = truncated
    return block


# --- requests -----------------------------------------------------------------------


def _flow_time(row: dict[str, Any]) -> datetime | None:
    millis = row.get("started_at_ms")
    if isinstance(millis, (int, float)) and not isinstance(millis, bool):
        return _to_datetime(millis / 1000.0)
    return _to_datetime(row.get("started_at"))


def _failure_reason(row: dict[str, Any]) -> str | None:
    if row.get("mocked") or row.get("mock_id"):
        return None  # the proxy answered from a mock: the test asked for it
    if row.get("completed") is False and row.get("error_kind") == "client_abort":
        return None  # the app cancelled it (left a screen, a second tap)
    status = row.get("status")
    if row.get("error") or not isinstance(status, int) or isinstance(status, bool):
        return "incomplete"
    if status >= 500:
        return "5xx"
    if 400 <= status < 500:
        return "4xx-unmocked"
    return None


def _requests(session: dict[str, Any], baseline: dict[str, Any],
              checks: list[dict[str, Any]]) -> dict[str, Any]:
    base = baseline.get("network")
    base_ok = isinstance(base, dict) and base.get("state") == "available"
    path = network_store.flows_path(session)
    if not path.is_file():
        if base_ok:
            return {"state": "available", "failed": []}
        return _unavailable(_baseline_reason(base) + "; no traffic was captured since",
                            "failed")
    skip = base.get("flows_line_count") if base_ok else 0
    skip = skip if isinstance(skip, int) and not isinstance(skip, bool) and skip > 0 else 0
    since = checks_mod.parse_ts(baseline.get("taken_at"))
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            raw_lines = handle.readlines()
    except OSError as exc:
        return _unavailable(_reason(exc), "failed")
    by_time = not base_ok or len(raw_lines) < skip
    if by_time and since is None:
        return _unavailable(_baseline_reason(base) + "; nothing tells old traffic from new",
                            "failed")
    if by_time:
        skip = 0  # no line watermark (or the store was cleared): the clock decides
    rows: list[tuple[int, dict[str, Any]]] = []
    id_counts: dict[str, int] = {}
    for number, line in enumerate(raw_lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            # Too deep is skipped whether the parser refused it (RecursionError
            # on older Pythons) or read it: the same line, the same outcome.
            if not isinstance(row, dict) or network_store.too_deep(row):
                continue
            # Ids restart with every proxy process: count them over the whole file.
            flow_id = str(row["id"]) if row.get("id") is not None else None
        except (ValueError, RecursionError):  # not JSON, or nested past the limit
            continue
        if flow_id is not None:
            id_counts[flow_id] = id_counts.get(flow_id, 0) + 1
        if number > skip:
            rows.append((number, row))
    failed: list[dict[str, Any]] = []
    total = 0
    for number, row in rows:
        try:
            row = redact.scrub_flow(row)
            stamp = _flow_time(row)
            if by_time and since is not None and (stamp is None or stamp < since):
                continue
            reason = _failure_reason(row)
            if reason is None:
                continue
            status = row.get("status")
            flow_id = str(row.get("id")) if row.get("id") is not None else None
            item = {
                "id": flow_id,
                "method": store.clip(row.get("method"), 16),
                "host": store.clip(row.get("host"), 255),
                "path": store.clip(row.get("path"), 500),
                "status": status if isinstance(status, int) and not isinstance(status, bool)
                else None,
                "reason": reason,
                "started_at": _iso(stamp) if stamp else None,
                "check_id": attribute(stamp, checks) if stamp else None,
                "line": number,
            }
        except RecursionError:
            continue  # a value nested too deep to print: skip the line
        total += 1
        if len(failed) >= MAX_FAILED_REQUESTS:
            continue
        if flow_id is not None and id_counts.get(flow_id, 0) > 1:
            item["duplicate_id"] = True
        failed.append(item)
    block: dict[str, Any] = {"state": "available", "failed": failed,
                             "basis": "clock" if by_time else "line_count"}
    if total > len(failed):
        block["omitted"] = total - len(failed)
    return block


# --- metrics ------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _memory_kb(snapshot: Any) -> tuple[str, float] | None:
    memory = snapshot.get("memory") if isinstance(snapshot, dict) else None
    if not isinstance(memory, dict):
        return None
    pss = _number(memory.get("total_pss_kb"))
    if pss is not None:
        return "memory_pss_kb", pss
    rss = _number(memory.get("rss_bytes"))
    if rss is not None:
        return "memory_rss_kb", rss / 1024.0
    return None


def _cpu(snapshot: Any) -> float | None:
    cpu = snapshot.get("cpu") if isinstance(snapshot, dict) else None
    return _number(cpu.get("process_percent")) if isinstance(cpu, dict) else None


def _tidy(value: float) -> int | float:
    return int(value) if float(value).is_integer() else round(value, 2)


def leads_between(before: Any, after: Any, *, check_id: str | None = None
                  ) -> list[dict[str, Any]]:
    """Metric leads between two snapshots: memory up by both 20 % and 20 MB,
    CPU up by 50 points."""
    leads: list[dict[str, Any]] = []
    old, new = _memory_kb(before), _memory_kb(after)
    if old and new and old[0] == new[0]:
        delta = new[1] - old[1]
        if delta >= MEMORY_MIN_KB and delta >= old[1] * MEMORY_RATIO:
            leads.append({"metric": old[0], "before": _tidy(old[1]), "after": _tidy(new[1]),
                          "delta": _tidy(delta), "check_id": check_id,
                          "threshold": "+20 % and +20 MB", "kind": "lead"})
    cpu_old, cpu_new = _cpu(before), _cpu(after)
    if cpu_old is not None and cpu_new is not None and cpu_new - cpu_old >= CPU_POINTS:
        leads.append({"metric": "cpu_percent", "before": _tidy(cpu_old),
                      "after": _tidy(cpu_new), "delta": _tidy(cpu_new - cpu_old),
                      "check_id": check_id, "threshold": "+50 points", "kind": "lead"})
    return leads


def _metrics(baseline: dict[str, Any], record: dict[str, Any], target: Any,
             checks: list[dict[str, Any]]) -> dict[str, Any]:
    base = baseline.get("metrics")
    if not isinstance(base, dict) or base.get("state") != "available":
        return _unavailable(_baseline_reason(base), "leads")
    app_id = record.get("app_id")
    if not app_id:
        return _unavailable("no app id; metrics are per app", "leads")
    if target is None or not hasattr(target, "platform"):
        return _unavailable("no device target to read metrics from", "leads")
    try:
        payload, _raw = snapshot_mod.take(target, app_id)
    except Exception as exc:  # noqa: BLE001 — recorded, never fatal
        return _unavailable(_reason(exc), "leads")
    taken = _to_datetime(payload.get("captured_at")) if isinstance(payload, dict) else None
    taken = taken or datetime.now(timezone.utc)
    leads = leads_between(base.get("snapshot"), payload,
                          check_id=attribute(taken, checks))
    return {"state": "available", "leads": leads, "measured_at": _iso(taken)}


# --- compute, write, findings -----------------------------------------------------


def _read_baseline(session: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    try:
        doc = store.read_baseline(session, record["pretest_id"])
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _require_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or not isinstance(record.get("pretest_id"), str):
        raise errors.AutonomError(
            errors.INVALID_VALUE, "health needs a pre-test record",
            hint="Pass the record from 'store.require_active' or 'store.load'.")
    return record


def compute(session: dict[str, Any], record: dict[str, Any], target: Any) -> dict[str, Any]:
    """``autonom.pretest-health/v1`` for this pre-test, now. Never raises for
    a device problem: each block is available or unavailable on its own."""
    record = _require_record(record)
    if not store.pretest_dir(session, record["pretest_id"]).is_dir():
        raise errors.AutonomError(
            errors.NO_ACTIVE_PRETEST, f"no pre-test {record['pretest_id']} in this session",
            hint="List this session's pre-tests with 'autonom pretest list'.",
            pretest_id=record["pretest_id"])
    baseline = _read_baseline(session, record)
    if not baseline:
        missing = {"state": "unavailable", "reason": "baseline.json is missing or unreadable"}
        baseline = {"crashes": missing, "logs": missing, "network": missing,
                    "metrics": missing}
    checks = checks_mod.fold(session, record)
    return {
        "schema": HEALTH_SCHEMA,
        "computed_at": store.now(),
        "crashes": _crashes(baseline, record, target, checks),
        "logs": _logs(session, baseline, record, target, checks),
        "requests": _requests(session, baseline, checks),
        "metrics": _metrics(baseline, record, target, checks),
    }


def write(session: dict[str, Any], record: dict[str, Any], health: dict[str, Any]) -> Path:
    """Write ``health.json`` (owner-only, atomic) under the pre-test lock.
    Refuses a pre-test that is packed by then (``pretest_finalized``)."""
    record = _require_record(record)
    path = store.pretest_dir(session, record["pretest_id"]) / HEALTH_FILE
    with store.lock(session, record["pretest_id"]):
        # Reload under the lock: a pack may have finished since the caller
        # loaded its record, and a packed pre-test never changes.
        store.ensure_mutable(store.load(session, record["pretest_id"]))
        store.write_json(path, health)
    return path


def _key_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def candidates(health: dict[str, Any]) -> list[dict[str, Any]]:
    """One would-be automatic finding per health item, each with its
    ``data.key``: ``crash:<name>``, ``log:<digest of the pattern>``,
    ``request:<flow id>@<started at, else line N>``, ``metrics:<metric>``.
    A flow id repeats across proxy restarts, so it alone is no key; a
    repeated id also gets no evidence ref, which would resolve to the first
    row with that id."""
    out: list[dict[str, Any]] = []

    def block(name: str, key: str) -> list[dict[str, Any]]:
        part = health.get(name) if isinstance(health, dict) else None
        if not isinstance(part, dict) or part.get("state") != "available":
            return []
        return [item for item in part.get(key) or [] if isinstance(item, dict)]

    for crash in block("crashes", "new"):
        name = str(crash.get("name") or "")
        if not name:
            continue
        out.append({
            "key": f"crash:{name}", "source": "crash", "check_id": crash.get("check_id"),
            "title": f"Crash after the baseline: {name}",
            "actual": f"The device recorded a new crash ({name}) at "
                      f"{crash.get('time') or 'an unknown time'}.",
            "evidence": [f"crash:{name}"],
            "data": {"name": name, "time": crash.get("time")}})
    for group in block("logs", "groups"):
        pattern = str(group.get("pattern") or "")
        if not pattern:
            continue
        evidence = []
        if group.get("first_ts") and group.get("last_ts"):
            evidence.append(f"log:{group['first_ts']}..{group['last_ts']}")
        out.append({
            "key": f"log:{_key_digest(pattern)}", "source": "log",
            "check_id": group.get("check_id"),
            "title": f"App {group.get('level') or 'error'} log: {pattern}",
            "actual": f"{group.get('count')} {group.get('level') or 'error'}-level line(s) "
                      f"matching: {pattern}",
            "evidence": evidence,
            "data": {"pattern": pattern, "count": group.get("count"),
                     "level": group.get("level"), "first_ts": group.get("first_ts")}})
    for flow in block("requests", "failed"):
        flow_id = flow.get("id")
        if not flow_id:
            continue
        status = flow.get("status")
        what = {"5xx": f"answered {status}",
                "4xx-unmocked": f"answered {status} without a mock",
                "incomplete": "never completed"}.get(flow.get("reason"), "failed")
        where = f"{flow.get('method') or '?'} {flow.get('host') or ''}{flow.get('path') or ''}"
        when = flow.get("started_at") or f"line {flow.get('line')}"
        out.append({
            "key": f"request:{flow_id}@{when}", "source": "request",
            "check_id": flow.get("check_id"),
            "title": f"Request {what}: {where}",
            "actual": f"{where} {what}.",
            "evidence": [] if flow.get("duplicate_id") else [f"request:{flow_id}"],
            "data": {"id": flow_id, "method": flow.get("method"), "host": flow.get("host"),
                     "path": flow.get("path"), "status": status,
                     "reason": flow.get("reason"), "started_at": flow.get("started_at"),
                     "line": flow.get("line")}})
    for lead in block("metrics", "leads"):
        metric = str(lead.get("metric") or "")
        if not metric:
            continue
        out.append({
            "key": f"metrics:{metric}", "source": "metrics", "check_id": lead.get("check_id"),
            "title": f"Lead: {metric} rose from {lead.get('before')} to {lead.get('after')}",
            "actual": f"A lead, not a proven defect: {metric} moved by {lead.get('delta')} "
                      f"({lead.get('threshold') or 'past the threshold'}) between the baseline "
                      "and one later snapshot.",
            "evidence": [],
            "data": {"metric": metric, "before": lead.get("before"),
                     "after": lead.get("after"), "delta": lead.get("delta")}})
    return out


def _resolves(session: dict[str, Any], ref: str) -> bool:
    try:
        findings_mod.resolve_ref(session, ref)
    except errors.AutonomError:
        return False
    return True


_FATAL_CODES = (errors.PRETEST_FINALIZED, errors.NO_ACTIVE_PRETEST)


def auto_findings(session: dict[str, Any], record: dict[str, Any],
                  health: dict[str, Any]) -> list[dict[str, Any]]:
    """Write one automatic finding per health item not already written (by
    ``data.key``) and return the ones written now. Refuses a packed
    pre-test (``pretest_finalized``). An evidence ref that does not resolve
    is dropped, never the finding."""
    record = _require_record(record)
    store.ensure_mutable(record)
    if not isinstance(health, dict) or health.get("schema") != HEALTH_SCHEMA:
        raise errors.AutonomError(
            errors.INVALID_VALUE, "not a pre-test health report",
            hint="Pass the dict 'health.compute' returned.")
    written: list[dict[str, Any]] = []
    with store.lock(session, record["pretest_id"]):
        current = store.load(session, record["pretest_id"])
        store.ensure_mutable(current)
        known_checks = {item["check_id"] for item in checks_mod.fold(session, current)}
        seen = {str((item.get("data") or {}).get("key"))
                for item in findings_mod.fold(session, current)
                if (item.get("data") or {}).get("key")}
        for item in candidates(health):
            if item["key"] in seen:
                continue
            check_id = item["check_id"] if item["check_id"] in known_checks else None
            evidence = [ref for ref in item["evidence"] if _resolves(session, ref)]
            try:
                finding = findings_mod.add(
                    session, current, check_id=check_id, severity="info",
                    title=item["title"], actual=item["actual"], expected=None,
                    evidence=evidence, origin="auto", source=item["source"],
                    confirmed=False, data={"key": item["key"], **item["data"]})
            except errors.AutonomError as exc:
                if exc.code in _FATAL_CODES:
                    raise
                continue  # one unwritable item never costs the others
            seen.add(item["key"])
            written.append(finding)
    return written


def run(session: dict[str, Any], record: dict[str, Any], target: Any) -> dict[str, Any]:
    """``compute``, ``write`` and ``auto_findings`` in that order (crash refs
    resolve against the written report). ``{"health", "path", "auto_findings"}``."""
    store.ensure_mutable(_require_record(record))
    health = compute(session, record, target)
    path = write(session, record, health)
    return {"health": health, "path": str(path),
            "auto_findings": auto_findings(session, record, health)}
