"""Flow runs and artifacts (contract section 4): list_flows .. get_artifact, tools 17-22.

- ``list_flows`` walks the configured ``flow_roots``; a flow is named by
  ``fl_`` + the first 16 hex of sha256(realpath) and is only ever run by id.
- ``start_flow_run`` starts ``flow run <realpath> --session-id S --events`` as
  a supervised job in its own process group. The job holds the session guard
  until it ends, renews the agent lease every ``agent_lease_ttl_s / 3``
  seconds, and sooner when the lease it started on (a reused one) would
  otherwise expire first (a renew that fails without a refusal is retried
  about once a second; a refused one stops renewing; a lease at another
  generation is never renewed, cached or released), writes its events to
  ``runs/<run_id>.events.ndjson`` (at most 8 MiB) and keeps a durable record
  ``runs/<run_id>.json`` (0600, atomic replace, never changed once terminal).
- ``get_run`` / ``cancel_run`` read and stop runs by id. Cancel is SIGINT to
  the group, 10 s, then SIGKILL. A run whose owning server is another live
  process is never touched (``run_not_owned``).
- At start, records left ``pending``/``running`` by a server that is gone
  become ``outcome_unknown`` (reason ``server_restarted``). A still-live
  orphan whose command line carries the run's operation id is stopped first;
  its record is written only once its process group is gone (until then it
  stays active, so the next server tries again).
- ``list_artifacts`` / ``get_artifact`` scan the session's artifact directory
  without following symlinks and serve files by HMAC id only (the core
  ``artifact_id``), never by path. Text artifacts and run errors are free
  text: the known local roots in them become labels (DEC-027, ``scrub_text``)
  and text pages are pages of the cleaned text.

Event mapping (``scripts/autonom_lib/flow/executor.py`` with ``--events``):
``flow.run.started`` gives ``steps.total`` (its ``payload.steps``) and the CLI
run id; ``flow.step.started`` pushes the current step (``step_index``, the
``command`` name only, ``wall_time``); ``flow.step.finished`` pops the open
step of its ``step_index`` (and any left above it), counts ``steps.done`` and,
with ``status`` ``failed``, ``steps.failed`` (nested ``runFlow`` steps count
too, so ``total`` is raised to ``done`` when it is lower); a finish with
``status`` ``warning`` (the ``allowMutations`` notice of a retry block) or one
that matches no open step changes nothing; ``flow.step.refused`` (P2 control lost) names the refusing code;
``flow.run.finished`` carries the final status and its ``failure``. Final
status: a cancel request ends ``cancelled`` unless the flow had already
reported its end; exit 0 is ``passed``; exit 1 takes the reported ``failed``
or ``interrupted``; exit 130 is ``cancelled``; a CLI error envelope on stderr
is ``failed`` with its code; anything else is ``outcome_unknown``.

Python 3.9 compatible and standard library only.
"""
from __future__ import annotations

import base64
import calendar
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from mcp_cli import CONTROL_CODES, parse_cli_json
from mcp_common import (_PATH_END, ARTIFACT_ID_RE, FLOW_ID_RE, HOST_ID, HOST_ID_RE, OPERATION_ID_RE, RUN_ID_RE,
                        SESSION_ID, SESSION_ID_RE, CallContext, Reply, Services, Tool, ToolError,
                        bad_argument, integer, known_secrets, new_id, nullable, obj, redact, rejected, string)
from mcp_core_tools import write_private
from mcp_env import ensure_private_dir

try:
    import fcntl
except ImportError:  # pragma: no cover - not POSIX: only the in-process lock then
    fcntl = None  # type: ignore[assignment]

RECORD_SCHEMA = 1
STATUSES = ("pending", "running", "passed", "failed", "interrupted", "cancelled", "outcome_unknown")
ACTIVE = ("pending", "running")
EVENTS_CAP = 8 * 1024 * 1024
LINE_CAP = 1024 * 1024
STDERR_TAIL = 4096
CANCEL_GRACE_S = 10.0
CANCEL_WAIT_S = CANCEL_GRACE_S + 4.0
SHUTDOWN_GRACE_S = 6.0
SHUTDOWN_WAIT_S = 8.0
READER_JOIN_S = 2.0
RECOVERY_LOCK_WAIT_S = 5.0
PROGRESS_WRITE_S = 0.5
MONITOR_POLL_S = 0.2
MAX_ENV_KEYS = 16
MAX_ENV_VALUE = 500
MAX_REPORTS = 20
FLOW_DEPTH = 4
FLOW_FILES = 200
SCAN_DEPTH = 6
SCAN_ENTRIES = 2000
ARTIFACT_MAX_SIZE = 64 * 1024 * 1024
DEFAULT_MAX_BYTES = 1048576
ENV_KEY_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
EXCLUDED_PARTS = ("har", "mitm", "cookie", "secret", "token", ".env")
KINDS = ("image", "journal", "json", "report", "log")
TEXT_KINDS = ("journal", "json", "report", "log")
REPORT_KEYS = ("report", "manifest", "html_report", "report_html", "summary")
REPORT_NAMES = ("manifest.json", "report.json", "summary.json")
HEX = frozenset("0123456789abcdef")

READ = {"readOnlyHint": True, "openWorldHint": False}
WIDGET = {"openai/widgetAccessible": True}


# ---------------------------------------------------------------- small helpers

def _iso_of(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".%03dZ" % int((epoch % 1) * 1000)


def _now_iso() -> str:
    return _iso_of(time.time())


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _short(value: Any, limit: int) -> Optional[str]:
    return value[:limit] if isinstance(value, str) else None


def _word(value: Any, limit: int = 80) -> Optional[str]:
    """``value`` when it is a short ``[A-Za-z0-9_.-]`` word, else None."""
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    return value if all(char.isascii() and (char.isalnum() or char in "_.-") for char in value) else None


def _command_name(value: Any) -> Optional[str]:
    """A Flow command name (``tapOn``, ``runFlow``), never its arguments."""
    word = _word(value)
    return word if word is not None and word[0].isalpha() else None


# ---------------------------------------------------------------- free text (DEC-027)
#
# Free text in a result (the text of journal, json, log and report artifacts,
# a run's error) shows no known local root: every occurrence of one is
# replaced by its label wherever it appears, longest root first, with no
# guess about where a path starts or ends. The roots: the user's home (``~``),
# the Autonom state base (``<state>``, and ``<autonom-home>``/``<xdg-state>``
# from the core labels), the session folder (``<session>``), each configured
# flow root (``<flow-root>``), the temp folders (``TMPDIR``,
# ``tempfile.gettempdir()``, ``/tmp``, ``/private/tmp`` and any
# ``/var/folders/..`` or ``/private/var/folders/..`` -> ``<tmp>``) and any
# ``/Users/<name>`` (-> ``/Users/<user>``); each also in its resolved form.
# Other absolute paths (``/opt``, ``/Volumes``) are left as they are, and
# nothing else is rewritten (base64 text, URLs). Structured fields still refuse
# absolute paths. Secrets and env values are redacted after the roots.

# One path part after ``/Users/`` or ``/var/folders/``.
_PART_CHAR = r"[^/\s\"'`<>&;,:|()\[\]{}\\]"
_VAR_FOLDERS = r"(?:/private)?/var/folders(?:/%s+){0,2}(?:/[0-9A-Z](?!%s))?" % (_PART_CHAR, _PART_CHAR)
_USER_DIR = r"/Users/%s+" % _PART_CHAR
TEMP_LABEL = "<tmp>"
USER_LABEL = "/Users/<user>"
_FIXED_TEMPS = ("/tmp", "/private/tmp")


def local_roots(services: Any, labels: Sequence[Tuple[str, str]] = ()) -> List[Tuple[str, str]]:
    """The ``(root, label)`` pairs of DEC-027: ``labels`` first (they win a
    tie), then the server's own labels (home, state base), the configured
    flow roots and the temp folders; each root also in its resolved form, without a trailing ``/``,
    at least 2 characters, the first label of a root kept."""
    pairs: List[Tuple[Any, str]] = list(labels)
    base = getattr(services, "scrub", None)
    pairs.extend(getattr(base, "labels", None) or [])
    config = getattr(services, "config", None) or {}
    try:
        flow_roots = list(config.get("flow_roots") or [])
    except (AttributeError, TypeError):
        flow_roots = []
    pairs.extend((root, "<flow-root>") for root in flow_roots)
    env = getattr(services, "env", None) or {}
    temps: List[Any] = [env.get("TMPDIR"), os.environ.get("TMPDIR")]
    try:
        temps.append(tempfile.gettempdir())
    except (OSError, ValueError):  # pragma: no cover - no usable temp folder
        pass
    pairs.extend((temp, TEMP_LABEL) for temp in list(temps) + list(_FIXED_TEMPS))
    seen: Dict[str, str] = {}
    for root, label in pairs:
        if not isinstance(root, str) or not isinstance(label, str) or not root.startswith("/"):
            continue
        forms = [root]
        try:
            forms.append(os.path.realpath(root))
        except (OSError, ValueError):
            pass
        for form in forms:
            form = form.rstrip("/")
            if len(form) >= 2 and form not in seen:
                seen[form] = label
    return list(seen.items())


class RootScrubber:
    """One pass over a text that replaces every occurrence of the known roots
    (longest first), then any ``/var/folders/..`` and ``/Users/<name>``."""

    def __init__(self, roots: Sequence[Tuple[str, str]]) -> None:
        self.labels = dict(roots)
        ordered = sorted(self.labels, key=len, reverse=True)
        # A root ends at a path boundary (the core _PATH_END rule), so a home
        # root that prefixes a sibling folder (/Users/al vs /Users/alice) does
        # not eat part of it.
        alternatives = [re.escape(root) + _PATH_END for root in ordered]
        alternatives.append("(?P<tmp>%s)" % _VAR_FOLDERS)
        alternatives.append("(?P<user>%s)" % _USER_DIR)
        self.pattern = re.compile("|".join(alternatives))
        self.names = frozenset(self.labels.values()) | {TEMP_LABEL, USER_LABEL}

    def _label(self, match: Any) -> str:
        if match.group("tmp") is not None:
            return TEMP_LABEL
        if match.group("user") is not None:
            return USER_LABEL
        return self.labels[match.group(0)]

    def sub(self, text: str) -> str:
        return self.pattern.sub(self._label, text)


def scrub_text(services: Any, text: str, labels: Sequence[Tuple[str, str]] = (),
               values: Sequence[str] = ()) -> str:
    """``text`` fit for a tool result (DEC-027, contract 4.5): every known local
    root replaced by its label, then secrets and ``values`` redacted (each value
    also as it reads after its own roots were replaced, unless that is a bare
    label)."""
    roots = RootScrubber(local_roots(services, labels))
    out = roots.sub(str(text))
    extra: List[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        extra.append(value)
        cleaned = roots.sub(value)
        if cleaned != value and cleaned not in roots.names:
            extra.append(cleaned)
    return redact(out, extra)


def _ps(fields: str, pid: int) -> Optional[str]:
    """``ps -o <fields> -p PID`` in the C locale and the UTC zone (so a start
    time reads the same in every server, whatever its own ``TZ``); None when
    unreadable."""
    command = "/bin/ps" if os.path.exists("/bin/ps") else "ps"
    try:
        proc = subprocess.run([command, "-o", fields, "-p", str(int(pid))], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
                              env={"PATH": "/bin:/usr/bin", "LC_ALL": "C", "LANG": "C", "TZ": "UTC0"},
                              check=False, close_fds=True)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", "replace").strip()
    return text or None


def process_info(pid: Any) -> Tuple[bool, Optional[str]]:
    """(alive, start time as ISO UTC or None) of ``pid``; a zombie is not alive.

    ``ps`` prints the start time in UTC (``_ps``) and it is read back as UTC
    (``calendar.timegm``), never in this server's own zone: two servers with
    different ``TZ`` give one pid the same start time (contract 4.3)."""
    if _int(pid) is None or pid <= 0:
        return False, None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, None
    except PermissionError:
        pass
    except OSError:
        return False, None
    out = _ps("stat=,lstart=", pid)
    if out is None:
        return True, None
    parts = out.split()
    if parts and parts[0].startswith("Z"):
        return False, None
    try:
        parsed = time.strptime(" ".join(parts[1:6]), "%a %b %d %H:%M:%S %Y")
        started: Optional[str] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(calendar.timegm(parsed)))
    except (ValueError, OverflowError, IndexError):
        started = None
    return True, started


def process_command(pid: int) -> Optional[str]:
    return _ps("command=", pid)


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def signal_group(pgid: int, signum: int) -> None:
    try:
        os.killpg(pgid, signum)
    except OSError:
        pass


def stop_group(pgid: int, grace_s: float = CANCEL_GRACE_S,
               cutoff: Optional[Callable[[], Optional[float]]] = None) -> bool:
    """SIGINT to the group, wait ``grace_s``, then SIGKILL. True when SIGKILL was needed.
    ``cutoff`` may return an earlier monotonic deadline for the SIGKILL (the
    server is stopping)."""
    signal_group(pgid, signal.SIGINT)
    deadline = time.monotonic() + grace_s
    while True:
        limit = deadline
        early = cutoff() if cutoff is not None else None
        if early is not None:
            limit = min(limit, early)
        if time.monotonic() >= limit:
            break
        if not group_alive(pgid):
            return False
        time.sleep(0.1)
    if not group_alive(pgid):
        return False
    signal_group(pgid, signal.SIGKILL)
    return True


def wait_group_gone(pgid: int, timeout: float) -> bool:
    """True once no process of the group is left (within ``timeout``)."""
    deadline = time.monotonic() + timeout
    while group_alive(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


# ---------------------------------------------------------------- flows

def flow_id_of(realpath: str) -> str:
    return "fl_" + hashlib.sha256(realpath.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def walk_flows(roots: List[str]) -> Tuple[List[Dict[str, str]], List[Tuple[str, str]]]:
    """Every flow under ``roots``: entries ``{flow_id, name, root, path}`` and
    warnings ``(code, message)``. At most 4 directory levels below a root,
    ``.yaml``/``.yml`` only, at most 200 files; symlinks (files or directories)
    are skipped. Hidden directories are walked like any other, so the usual
    ``.maestro/`` folder under a root is listed."""
    flows: List[Dict[str, str]] = []
    warnings: List[Tuple[str, str]] = []
    seen: set = set()
    for root in roots:
        root_name = os.path.basename(root.rstrip("/")) or "/"
        if not os.path.isdir(root):
            warnings.append(("flow_root_unavailable", "Flow root %s is not a directory" % root_name[:80]))
            continue
        stack: List[Tuple[str, str, int]] = [(root, "", 0)]
        while stack and len(flows) < FLOW_FILES:
            directory, rel, depth = stack.pop()
            try:
                with os.scandir(directory) as iterator:
                    items = sorted(iterator, key=lambda item: item.name)
            except OSError:
                continue
            subdirs: List[Tuple[str, str, int]] = []
            for entry in items:
                name = entry.name
                relpath = rel + "/" + name if rel else name
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if depth < FLOW_DEPTH:
                            subdirs.append((entry.path, relpath, depth + 1))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if not name.lower().endswith((".yaml", ".yml")):
                    continue
                real = os.path.realpath(entry.path)
                flow_id = flow_id_of(real)
                if flow_id in seen:
                    continue
                seen.add(flow_id)
                flows.append({"flow_id": flow_id, "name": relpath[:1024], "root": root_name[:255],
                              "path": real})
                if len(flows) >= FLOW_FILES:
                    warnings.append(("flows_truncated", "Only the first %d flow files are listed" % FLOW_FILES))
                    break
            stack.extend(reversed(subdirs))
    return flows, warnings


# ---------------------------------------------------------------- artifacts

def artifact_kind(name: str) -> Optional[str]:
    lower = name.lower()
    if lower == "journal.ndjson":
        return "journal"
    if lower.endswith((".png", ".jpg", ".jpeg")):
        return "image"
    if lower.endswith((".json", ".ndjson")):
        return "json"
    if lower.endswith(".html"):
        return "report"
    if lower.endswith((".log", ".txt")):
        return "log"
    return None


def artifact_mime(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".ndjson"):
        return "application/x-ndjson"
    if lower.endswith(".json"):
        return "application/json"
    if lower.endswith(".html"):
        return "text/html"
    return "text/plain"


def excluded_part(name: str) -> bool:
    """True for a path part that is never listed or served (contract 4.4)."""
    lower = name.lower()
    if lower in ("session.json", "network"):
        return True
    return any(item in lower for item in EXCLUDED_PARTS)


def scan_artifacts(base: Path) -> Tuple[List[Dict[str, Any]], bool]:
    """Files under ``base`` as ``{relpath, kind, name, group, size, mtime, mime}``,
    newest first, and whether the scan stopped early.

    ``os.scandir`` without following symlinks (a symlink is never listed or
    entered), at most 6 directory levels below ``base``, at most 2000 entries
    visited, files over 64 MiB and excluded names skipped."""
    found: List[Dict[str, Any]] = []
    truncated = False
    visited = 0
    stack: List[Tuple[str, str, int]] = [(str(base), "", 0)]
    while stack and not truncated:
        directory, rel, depth = stack.pop()
        try:
            iterator = os.scandir(directory)
        except OSError:
            continue
        with iterator:
            for entry in iterator:
                visited += 1
                if visited > SCAN_ENTRIES:
                    truncated = True
                    break
                name = entry.name
                if excluded_part(name):
                    continue
                relpath = rel + "/" + name if rel else name
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if depth < SCAN_DEPTH:
                            stack.append((entry.path, relpath, depth + 1))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    kind = artifact_kind(name)
                    if kind is None:
                        continue
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if info.st_size > ARTIFACT_MAX_SIZE:
                    continue
                found.append({"relpath": relpath, "kind": kind, "name": name[:255],
                              "group": relpath.split("/", 1)[0][:255] if "/" in relpath else None,
                              "size": int(info.st_size), "mtime": float(info.st_mtime),
                              "mime": artifact_mime(name)})
    found.sort(key=lambda item: (-item["mtime"], item["relpath"]))
    return found, truncated


def allowed_relpath(base: Path, relpath: str) -> bool:
    """True when ``relpath`` (posix) under ``base`` is a file the scan would list:
    no excluded part, no symlink on the way, a known kind, at most 64 MiB."""
    parts = [part for part in relpath.split("/") if part]
    if not parts or any(part in (".", "..") or excluded_part(part) for part in parts):
        return False
    if len(parts) - 1 > SCAN_DEPTH or artifact_kind(parts[-1]) is None:
        return False
    current = str(base)
    for index, part in enumerate(parts):
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode):
            return False
        last = index == len(parts) - 1
        if last and (not stat.S_ISREG(info.st_mode) or info.st_size > ARTIFACT_MAX_SIZE):
            return False
        if not last and not stat.S_ISDIR(info.st_mode):
            return False
    return True


# ---------------------------------------------------------------- progress and final status

class Progress:
    """Run progress from the executor's ``--events`` lines (mapping in the module doc)."""

    def __init__(self) -> None:
        self.total = 0
        self.done = 0
        self.failed = 0
        self.stack: List[Dict[str, Any]] = []
        self.cli_run_id: Optional[str] = None
        self.finished: Optional[Dict[str, Any]] = None
        self.refused: Optional[Dict[str, Any]] = None
        self.events = 0

    def feed(self, event: Any) -> bool:
        """Apply one parsed event; True when it was a flow event."""
        if not isinstance(event, dict):
            return False
        kind = event.get("kind")
        if not isinstance(kind, str) or not kind.startswith("flow."):
            return False
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        self.events += 1
        if self.cli_run_id is None:
            self.cli_run_id = _word(event.get("run_id"), 64)
        if kind == "flow.run.started":
            steps = _int(payload.get("steps"))
            if steps is not None and steps >= 0:
                self.total = steps
        elif kind == "flow.step.started":
            self.stack.append({"index": _int(payload.get("step_index")),
                               "command": _command_name(payload.get("command")),
                               "started_at": _short(event.get("wall_time"), 64)})
        elif kind == "flow.step.finished":
            if payload.get("status") == "warning":
                return True  # a notice (retry with allowMutations), the step goes on
            index = _int(payload.get("step_index"))
            for position in range(len(self.stack) - 1, -1, -1):
                if self.stack[position]["index"] == index:
                    del self.stack[position:]
                    break
            else:
                return True  # no open step of that index: nothing ends, nothing counts
            self.done += 1
            if payload.get("status") == "failed":
                self.failed += 1
        elif kind == "flow.step.refused":
            self.refused = dict(payload)
            self.stack = []
        elif kind == "flow.run.finished":
            self.finished = dict(payload)
            self.stack = []
        return True

    def steps(self) -> Dict[str, int]:
        return {"total": max(self.total, self.done), "done": self.done, "failed": self.failed}

    def current(self) -> Optional[Dict[str, Any]]:
        return dict(self.stack[-1]) if self.stack else None


def final_status(progress: Progress, exit_code: Optional[int], cancel_reason: Optional[str],
                 stderr_error: Optional[Mapping[str, Any]]) -> Tuple[str, Optional[str], Optional[str]]:
    """(status, error_code, error) of an ended run (mapping in the module doc)."""
    finished = progress.finished or {}
    reported = finished.get("status") if isinstance(finished.get("status"), str) else None
    failure = finished.get("failure") if isinstance(finished.get("failure"), dict) else {}
    if cancel_reason is not None and not (reported is not None and exit_code in (0, 1)):
        return "cancelled", None, None
    if exit_code == 0:
        return "passed", None, None
    if exit_code == 1 and reported in ("failed", "interrupted"):
        code = _word(failure.get("error_code"))
        if reported == "interrupted" and progress.refused:
            code = _word(progress.refused.get("error_code")) or code
        index = _int(failure.get("step_index"))
        command = _command_name(failure.get("command"))
        where = "" if index is None else " at step %d%s" % (index, " (%s)" % command if command else "")
        message = "The flow ended %s%s%s" % (reported, where, ": %s" % code if code else "")
        return reported, code, message[:500]
    if exit_code == 1 and reported in ("passed", "replayed", "planned"):
        return "passed", None, None
    if exit_code == 130:
        return "cancelled", None, None
    if stderr_error and _word(stderr_error.get("error_code")):
        message = _short(stderr_error.get("error"), 500) or "The flow run failed"
        return "failed", _word(stderr_error.get("error_code")), message
    if exit_code is not None and exit_code < 0:
        return "outcome_unknown", None, "The flow process ended on signal %d" % -exit_code
    return "outcome_unknown", None, "The flow process ended with exit code %s and no result" % exit_code


# ---------------------------------------------------------------- record view

def _hex_id(value: Any, prefix: str, length: int) -> bool:
    return isinstance(value, str) and len(value) == len(prefix) + length and value.startswith(prefix) \
        and all(char in HEX for char in value[len(prefix):])


def record_view(record: Mapping[str, Any]) -> Dict[str, Any]:
    """The ``run`` object of the tools. Every value is checked, so a damaged
    record on disk never breaks the output schema; process ids, the holder id
    and env values are never part of it."""
    steps = record.get("steps") if isinstance(record.get("steps"), dict) else {}
    current = record.get("current_step") if isinstance(record.get("current_step"), dict) else None
    reports = record.get("report_artifacts") if isinstance(record.get("report_artifacts"), list) else []
    env_keys = record.get("env_keys") if isinstance(record.get("env_keys"), list) else []

    def count(value: Any) -> int:
        number = _int(value)
        return number if number is not None and number >= 0 else 0

    generation = _int(record.get("generation"))
    return {
        "run_id": str(record.get("run_id")),
        "host_id": str(record.get("host_id")),
        "session_id": str(record.get("session_id")),
        "flow_id": str(record.get("flow_id")),
        "flow_name": _short(record.get("flow_name"), 1024) or "",
        "status": record.get("status") if record.get("status") in STATUSES else "outcome_unknown",
        "created_at": _short(record.get("created_at"), 64),
        "started_at": _short(record.get("started_at"), 64),
        "ended_at": _short(record.get("ended_at"), 64),
        "operation_id": str(record.get("operation_id")),
        "generation": generation if generation is not None and generation >= 0 else None,
        "env_keys": [key for key in env_keys if _word(key, 64)][:MAX_ENV_KEYS],
        "steps": {"total": count(steps.get("total")), "done": count(steps.get("done")),
                  "failed": count(steps.get("failed"))},
        "current_step": None if current is None else {
            "index": _int(current.get("index")),
            "command": _command_name(current.get("command")),
            "started_at": _short(current.get("started_at"), 64)},
        "exit_code": _int(record.get("exit_code")),
        "error_code": _word(record.get("error_code")),
        "error": _short(record.get("error"), 500),
        "reason": _short(record.get("reason"), 200),
        "report_artifacts": [item for item in reports if _hex_id(item, "art_", 24)][:MAX_REPORTS],
        "events_dropped": count(record.get("events_dropped")),
        "forced": record.get("forced") is True,
        "last_step_outcome": "unknown" if record.get("last_step_outcome") == "unknown" else None,
    }


# ---------------------------------------------------------------- jobs

class Job:
    """One supervised ``flow run`` child and its record."""

    def __init__(self, store: "RunStore", record: Dict[str, Any], ref: Dict[str, Any],
                 flow_path: str, env_values: Dict[str, str]) -> None:
        self.store = store
        self.record = record
        self.ref = ref
        self.flow_path = flow_path
        self.env_values = env_values
        self.run_id: str = record["run_id"]
        self.session_id: str = record["session_id"]
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.done = threading.Event()
        self.ended = threading.Event()
        self.proc: Optional[subprocess.Popen] = None
        self.progress = Progress()
        self.cancel_reason: Optional[str] = None
        self.kill_at: Optional[float] = None
        self.forced = False
        self.step_at_cancel = False
        self.renew_problem: Optional[str] = None
        self.stderr_tail = b""
        self.events_bytes = 0
        self.events_dropped = 0
        self.events_fd: Optional[int] = None
        self.last_write = 0.0
        self.write_pending = False  # a progress write was throttled; the monitor tick flushes it
        self.deadline = time.monotonic() + float(store.services.config.get("run_max_s") or 1800)
        self.readers: List[threading.Thread] = []
        self.monitored = False

    @property
    def events_path(self) -> Path:
        return self.store.runs_dir / ("%s.events.ndjson" % self.run_id)

    def view(self) -> Dict[str, Any]:
        with self.lock:
            return record_view(self.record)

    def write(self, force: bool = False) -> None:
        """Persist the record (progress at most every 0.5 s; transitions always).

        A throttled progress write is not lost: it is marked pending and the
        monitor tick (every 0.2 s) writes it once the 0.5 s window has passed,
        so the record on disk never keeps a stale step for longer than that.

        Snapshot and file write happen under ``write_lock`` together, so writes
        land in snapshot order and a progress snapshot taken before the end can
        never replace the terminal record on disk."""
        with self.write_lock:
            with self.lock:
                now = time.monotonic()
                if not force and now - self.last_write < PROGRESS_WRITE_S:
                    self.write_pending = True
                    return
                self.last_write = now
                self.write_pending = False
                snapshot = json.loads(json.dumps(self.record))
            try:
                self.store.write_record(snapshot)
            except (OSError, ValueError, TypeError):
                pass  # logged by write_record; the in-memory record still answers get_run

    def _sync_progress(self) -> None:
        self.record["steps"] = self.progress.steps()
        self.record["current_step"] = self.progress.current()
        self.record["events_dropped"] = self.events_dropped

    # -- launch

    def launch(self, claim: Mapping[str, Any]) -> None:
        """Write the pending record, spawn the CLI and start the job threads.
        Raises a ``rejected`` ToolError when nothing was spawned."""
        services = self.store.services
        refusal = self.cancelled_before_start()
        if refusal is not None:
            raise refusal
        try:
            ensure_private_dir(self.store.runs_dir)
            self.events_fd = os.open(str(self.events_path),
                                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
            self.store.write_record(dict(self.record))
        except (OSError, ValueError, TypeError) as exc:
            self._discard_files()
            raise rejected("write_failed", "The run record could not be written: %s" % type(exc).__name__)
        try:
            proc = services.runner.spawn(
                "flow_run", {"path": self.flow_path, "session_id": self.session_id, "env": self.env_values},
                operation_id=self.record["operation_id"], claim=claim,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except ToolError:
            self._discard_files()
            raise
        except ValueError as exc:
            self._discard_files()
            raise rejected("cli_unavailable", "The flow run could not be started: %s" % type(exc).__name__)
        with self.lock:
            self.proc = proc
            self.record["pid"] = proc.pid
            self.record["started_at"] = _now_iso()
            cancelled = self.cancel_reason is not None
        if cancelled:
            signal_group(proc.pid, signal.SIGINT)  # cancelled while it was being spawned
        self.write(force=True)
        self.readers = [
            threading.Thread(target=self._read_stdout, name="run-stdout-%s" % self.run_id, daemon=True),
            threading.Thread(target=self._read_stderr, name="run-stderr-%s" % self.run_id, daemon=True),
        ]
        try:
            for thread in self.readers:
                thread.start()
            threading.Thread(target=self._monitor, name="run-monitor-%s" % self.run_id, daemon=True).start()
            self.monitored = True
        except RuntimeError as exc:
            # No supervision without the threads: the child must not drive the
            # device unsupervised. It was spawned, so the outcome is unknown.
            signal_group(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            with self.lock:
                self.ended.set()
                self.record.update(status="outcome_unknown", ended_at=_now_iso(), reason="supervision_failed",
                                   exit_code=proc.returncode)
            self.write(force=True)
            self.done.set()
            raise ToolError("run_unsupervised", "The flow run could not be supervised: %s" % type(exc).__name__,
                            "Check the screen and get_operation before starting the flow again.")
        try:
            threading.Thread(target=self._keep_alive, name="run-lease-%s" % self.run_id, daemon=True).start()
        except RuntimeError:
            self.store.services.log("run %s has no lease keep-alive" % self.run_id)

    def _discard_files(self) -> None:
        if self.events_fd is not None:
            try:
                os.close(self.events_fd)
            except OSError:
                pass
            self.events_fd = None
        for path in (self.events_path, self.store.record_path(self.run_id)):
            try:
                os.unlink(str(path))
            except OSError:
                pass

    # -- readers

    def _append_event_line(self, line: bytes) -> None:
        if self.events_fd is None:
            return
        if self.events_bytes + len(line) > EVENTS_CAP:
            self.events_dropped += 1
            return
        try:
            os.write(self.events_fd, line)
            self.events_bytes += len(line)
        except OSError:
            self.events_dropped += 1

    def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        stream = self.proc.stdout
        oversized = False
        try:
            while True:
                line = stream.readline(LINE_CAP)
                if not line:
                    break
                if not line.endswith(b"\n") and len(line) >= LINE_CAP:
                    oversized = True
                    continue
                if oversized:
                    oversized = False  # the tail of a line over 1 MiB: dropped whole
                    with self.lock:
                        self.events_dropped += 1
                    continue
                force = False
                with self.lock:
                    if self.ended.is_set():
                        continue
                    self._append_event_line(line if line.endswith(b"\n") else line + b"\n")
                    try:
                        event = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError, RecursionError):
                        event = None
                    changed = self.progress.feed(event)
                    if self.record["status"] == "pending":
                        self.record["status"] = "running"
                        changed = force = True
                    if changed:
                        self._sync_progress()
                if changed:
                    self.write(force=force)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        stream = self.proc.stderr
        try:
            while True:
                chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
                if not chunk:
                    break
                with self.lock:
                    self.stderr_tail = (self.stderr_tail + chunk)[-STDERR_TAIL:]
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def stderr_text(self) -> str:
        """The stderr tail (4 KiB) with env values and token shapes redacted."""
        with self.lock:
            text = self.stderr_tail.decode("utf-8", "replace")
        return redact(text, list(self.env_values.values()))

    def clean(self, text: str) -> str:
        """``text`` (a CLI error message) for the record and every result: the
        flow's real path reads as its name, then ``scrub_text`` (the known
        local roots as their labels, env values and secrets redacted; DEC-027)."""
        labels: List[Tuple[str, str]] = []
        if self.flow_path:
            labels.append((self.flow_path, str(self.record.get("flow_name") or "<flow>")))
        return scrub_text(self.store.services, text, labels, list(self.env_values.values()))

    # -- control

    def cancel(self, reason: str, grace_s: float = CANCEL_GRACE_S) -> bool:
        """SIGINT the group now; the monitor sends SIGKILL after ``grace_s``.
        A run still launching (no child yet) is marked: it is never spawned, or
        its child is interrupted the moment it exists. False when the run
        already ended."""
        with self.lock:
            if self.done.is_set() or self.ended.is_set():
                return False
            if self.cancel_reason is None:
                self.cancel_reason = reason
                self.step_at_cancel = bool(self.progress.stack)
                self.kill_at = time.monotonic() + grace_s
            elif self.kill_at is not None:
                self.kill_at = min(self.kill_at, time.monotonic() + grace_s)
            proc = self.proc
        if proc is not None:
            signal_group(proc.pid, signal.SIGINT)
        return True

    def cancelled_before_start(self) -> Optional[ToolError]:
        """The refusal for a run cancelled before its child was spawned."""
        with self.lock:
            reason = self.cancel_reason
        if reason is None:
            return None
        if reason == "server_stopped":
            return rejected("server_stopping", "The server is stopping; flow run %s was not started" % self.run_id,
                            "Call start_flow_run again once the adapter server runs.")
        if reason == "session_stopped":
            return rejected("session_stopped", "Session %s is stopping; flow run %s was not started"
                            % (self.session_id, self.run_id))
        return rejected("run_cancelled", "Flow run %s was cancelled before it started" % self.run_id,
                        "Call start_flow_run again to run the flow.")

    def abandon(self) -> None:
        """A launch that failed: nobody waits on this job any longer."""
        with self.lock:
            self.ended.set()
        self.done.set()

    def _renew_wait(self, period: float, floor: float) -> float:
        """Seconds to the next renew: one period, but early enough that one
        period is still left before the cached lease expires (a run may start
        on a reused lease with little time left), and never under ``floor``."""
        keeper = self.store.services.keeper
        try:
            entry = keeper.cached(str(self.ref["platform"]), str(self.ref["target_id"]))
            now = float(keeper.clock())
        except Exception:  # noqa: BLE001 - the plain period then
            return period
        expires = entry.get("expires_at") if entry else None
        if not isinstance(expires, (int, float)) or isinstance(expires, bool):
            return period
        return max(floor, min(period, float(expires) - now - period))

    def _renew_once(self) -> None:
        """One keep-alive renew: ``control renew`` at the run's own generation.

        The agent holder id (``chatgpt-agent:<host_id>``) is shared by every
        adapter server of the host, so a lease at another generation may be
        another server's grant. Such a lease is never renewed, never put into
        this server's keeper cache and so never released by it (the keeper
        releases what its cache holds at shutdown): a cached entry at another
        generation, or a renew answered at another generation, is a refusal
        ``control_generation_changed`` and renewing stops (contract 4.3). Only
        an answer at the run's own generation goes into the cache. It never
        acquires. A failure that is no refusal (a CLI timeout) keeps the cached
        entry and is retried by ``_keep_alive``; a refusal drops the run's own
        entry."""
        keeper = self.store.services.keeper
        platform, target_id = str(self.ref["platform"]), str(self.ref["target_id"])
        with self.lock:
            wanted = self.record.get("generation")
        if not isinstance(wanted, int) or isinstance(wanted, bool):
            raise rejected("control_not_held", "Flow run %s has no agent lease generation" % self.run_id)
        entry = keeper.cached(platform, target_id)
        if entry is not None and entry.get("generation") != wanted:
            raise rejected("control_generation_changed",
                           "The agent lease of flow run %s is now held at another generation" % self.run_id)
        result = keeper.runner.run("control_renew", {
            "session_id": self.ref["session_id"], "holder": keeper.holder_id(), "generation": wanted,
            "ttl": keeper.ttl()})
        if not result.ok:
            if result.rejected or result.cli_error_code in CONTROL_CODES:
                self._drop_own_lease(wanted)
            raise result.to_error()
        data = result.data if isinstance(result.data, dict) else {}
        lease = data.get("lease") if isinstance(data.get("lease"), dict) else {}
        answered = data.get("generation")
        if not isinstance(answered, int) or isinstance(answered, bool):
            answered = lease.get("generation")
        if answered != wanted or isinstance(answered, bool):
            # Renewed in place at another generation: not the run's grant.
            self._drop_own_lease(wanted)
            raise rejected("control_generation_changed",
                           "The agent lease of flow run %s was renewed at another generation" % self.run_id)
        keeper._store(self.ref, data)  # noqa: SLF001 - the keeper keeps it as its own renew would

    def _drop_own_lease(self, generation: int) -> None:
        """Forget the cached lease when it is still the run's own generation."""
        keeper = self.store.services.keeper
        platform, target_id = str(self.ref["platform"]), str(self.ref["target_id"])
        entry = keeper.cached(platform, target_id)
        if entry is not None and entry.get("generation") == generation:
            keeper.drop(platform, target_id)

    def _keep_alive(self) -> None:
        """Renew the agent lease every TTL/3 while the run is active (contract 4.3);
        the first renew comes at once when the lease the run started on has
        less than one period left beyond the next renew.

        Only a refused renew (a ``rejected`` answer or a control code) stops
        renewing; it is recorded in the run's ``reason``. Any other failure is
        retried about once a second until a renew succeeds or the run ends, and
        is named in ``reason`` only when the run ends before a renew succeeds."""
        services = self.store.services
        ttl = float(services.config.get("agent_lease_ttl_s") or 120)
        period = max(1.0, ttl / 3.0)
        retry_s = min(1.0, period)
        floor = 0.0
        retrying = False
        while not self.done.wait(retry_s if retrying else self._renew_wait(period, floor)):
            floor = retry_s  # a renew is never repeated more than once a second
            with self.lock:
                if self.record["status"] not in ACTIVE or self.cancel_reason is not None or self.ended.is_set():
                    return
            try:
                self._renew_once()
            except ToolError as exc:
                code = str(exc.extras.get("cli_error_code") or exc.error_code)
                if exc.rejected or code in CONTROL_CODES:
                    with self.lock:
                        if self.ended.is_set():
                            return  # a terminal record is never changed
                        self.renew_problem = ("lease_renew_refused: %s" % code)[:200]
                        if self.record.get("reason") is None:
                            self.record["reason"] = self.renew_problem
                    self.write(force=True)
                    return  # the flow then ends interrupted at its next step; never --takeover
                retrying = True
                with self.lock:
                    self.renew_problem = ("lease_renew_failed: %s" % code)[:200]
                services.log("run %s lease renew failed: %s" % (self.run_id, code[:80]))
                continue
            except Exception as exc:  # noqa: BLE001 - the keep-alive never ends the job
                retrying = True
                with self.lock:
                    self.renew_problem = ("lease_renew_failed: %s" % type(exc).__name__)[:200]
                services.log("run %s lease renew failed: %s" % (self.run_id, type(exc).__name__))
                continue
            retrying = False
            with self.lock:
                if self.renew_problem is not None and self.renew_problem.startswith("lease_renew_failed"):
                    self.renew_problem = None  # the run's lease is held again

    def _monitor(self) -> None:
        """Wait for the child, enforce the SIGKILL deadline and ``run_max_s``,
        then finalise. The guard is released whatever happens."""
        assert self.proc is not None
        proc = self.proc
        try:
            self._supervise(proc)
        except Exception as exc:  # noqa: BLE001 - never leave the session held
            self.store.services.log("run %s supervision failed: %s" % (self.run_id, type(exc).__name__))
            signal_group(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        deadline = time.monotonic() + READER_JOIN_S
        for thread in self.readers:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        try:
            self._finish(proc.returncode)
        except Exception as exc:  # noqa: BLE001 - the guard is always released
            self.store.services.log("run %s finalisation failed: %s" % (self.run_id, type(exc).__name__))
            with self.lock:
                self.record["status"] = "outcome_unknown"
                self.record["ended_at"] = self.record.get("ended_at") or _now_iso()
            self.write(force=True)
        finally:
            self.ended.set()
            self.store.services.guard.release(self.session_id)
            self.done.set()

    def _supervise(self, proc: subprocess.Popen) -> None:
        while True:
            try:
                proc.wait(timeout=MONITOR_POLL_S)
                break
            except subprocess.TimeoutExpired:
                pass
            if self.write_pending:
                self.write()  # the progress a throttled write left behind
            now = time.monotonic()
            if now >= self.deadline:
                self.deadline = float("inf")
                self.cancel("run_timeout")
            with self.lock:
                kill_now = self.kill_at is not None and now >= self.kill_at and not self.forced
                if kill_now:
                    self.forced = True
            if kill_now:
                signal_group(proc.pid, signal.SIGKILL)
        with self.lock:
            cancelled = self.cancel_reason is not None
        if cancelled:
            # What the group left behind goes too. The CLI is reaped by now, so
            # only the group is signalled, never the bare pid.
            signal_group(proc.pid, signal.SIGKILL)

    def _finish(self, exit_code: Optional[int]) -> None:
        """Turn the ended process into the terminal record: in memory and on disk
        together, before the guard is released."""
        self.ended.set()
        stderr_error = parse_cli_json(self.stderr_text())
        reports = self._report_artifacts()
        with self.lock:
            status, error_code, error = final_status(self.progress, exit_code, self.cancel_reason,
                                                     stderr_error)
            self._sync_progress()
            self.record["status"] = status
            self.record["exit_code"] = exit_code
            self.record["error_code"] = error_code
            self.record["error"] = None if error is None else self.clean(error)[:500]
            self.record["reason"] = self.cancel_reason or self.renew_problem or self.record.get("reason")
            if status == "cancelled":
                self.record["forced"] = self.forced
                if self.step_at_cancel or self.progress.stack:
                    self.record["last_step_outcome"] = "unknown"
            self.record["current_step"] = None
            if self.events_fd is not None:
                try:
                    os.close(self.events_fd)
                except OSError:
                    pass
                self.events_fd = None
            self.record["report_artifacts"] = reports
            self.record["ended_at"] = _now_iso()
        self.write(force=True)

    def _report_artifacts(self) -> List[str]:
        """Artifact ids of the report files the final event names (``report``,
        ``manifest``, ``reports``...) and of the report/manifest files in the CLI
        run directory ``flows/<cli run id>/``; only files the scan would list."""
        progress = self.progress
        if progress.finished is None and progress.cli_run_id is None:
            return []
        try:
            base = self.store.services.refs.session_artifacts_dir(self.ref)
        except Exception:  # noqa: BLE001 - report ids are best effort
            return []
        base_real = os.path.realpath(str(base))
        candidates: List[str] = []
        finished = progress.finished or {}
        for key in REPORT_KEYS:
            value = finished.get(key)
            if isinstance(value, str) and os.path.isabs(value):
                candidates.append(value)
        listed = finished.get("reports")
        if isinstance(listed, list):
            candidates.extend(item for item in listed[:MAX_REPORTS]
                              if isinstance(item, str) and os.path.isabs(item))
        if progress.cli_run_id:
            try:
                with os.scandir(os.path.join(base_real, "flows", progress.cli_run_id)) as iterator:
                    for entry in sorted(iterator, key=lambda item: item.name):
                        if artifact_kind(entry.name) == "report" or entry.name in REPORT_NAMES:
                            candidates.append(entry.path)
            except OSError:
                pass
        ids: List[str] = []
        for candidate in candidates:
            real = os.path.realpath(candidate)
            try:
                if real == base_real or os.path.commonpath([real, base_real]) != base_real:
                    continue
            except ValueError:
                continue
            relpath = os.path.relpath(real, base_real).replace(os.sep, "/")
            if not allowed_relpath(Path(base_real), relpath):
                continue
            artifact = self.store.services.refs.artifact_id(self.record["host_id"], self.session_id, relpath)
            if artifact not in ids:
                ids.append(artifact)
            if len(ids) >= MAX_REPORTS:
                break
        return ids


class RunStore:
    """The job registry of this server and the durable records of every server."""

    def __init__(self, services: Services) -> None:
        self.services = services
        self.runs_dir = Path(services.state_dir) / "runs"
        self.lock = threading.Lock()
        self.jobs: Dict[str, Job] = {}
        self.closing = False
        self.stop_by: Optional[float] = None  # SIGKILL deadline for orphans once the server stops
        self.recovery_locks: Dict[str, threading.Lock] = {}
        self.recovering: List[threading.Thread] = []
        self.server_pid = os.getpid()
        self.server_started_at = process_info(self.server_pid)[1]
        self.text_lock = threading.Lock()
        self.text_cache: Optional[Tuple[str, bytes]] = None  # (key, cleaned text) of the last text artifact

    # -- records

    def record_path(self, run_id: str) -> Path:
        return self.runs_dir / ("%s.json" % run_id)

    def write_record(self, record: Mapping[str, Any]) -> None:
        """Atomic 0600 write of one record; raises (after logging) on failure."""
        try:
            ensure_private_dir(self.runs_dir)
            data = json.dumps(record, sort_keys=True, ensure_ascii=True).encode("ascii") + b"\n"
            write_private(self.record_path(str(record["run_id"])), data)
        except (OSError, ValueError, TypeError) as exc:
            self.services.log("run record %s could not be written: %s"
                              % (str(record.get("run_id"))[:40], type(exc).__name__))
            raise

    def load(self, run_id: str) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(self.record_path(run_id).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, RecursionError):
            return None
        if not isinstance(data, dict) or data.get("run_id") != run_id or data.get("schema") != RECORD_SCHEMA:
            return None
        for key, pattern in (("host_id", HOST_ID_RE), ("session_id", SESSION_ID_RE), ("flow_id", FLOW_ID_RE),
                             ("operation_id", OPERATION_ID_RE)):
            if not (isinstance(data.get(key), str) and re.fullmatch(pattern[1:-1], data[key])):
                return None  # a damaged record reads as missing, never as a schema violation
        return data

    def owner_alive(self, record: Mapping[str, Any]) -> bool:
        """True while the server that started the run still runs: its pid is
        alive and, when both are known, its start time is the recorded one."""
        pid = _int(record.get("server_pid"))
        if pid is None or pid <= 0:
            return False
        stored = record.get("server_started_at")
        if pid == self.server_pid:
            return stored is None or stored == self.server_started_at
        alive, started = process_info(pid)
        if not alive:
            return False
        return not (isinstance(stored, str) and started is not None and stored != started)

    # -- restart recovery

    def orphan_of(self, record: Mapping[str, Any]) -> Optional[int]:
        """The run's flow pid (its process group id) when that process is
        still alive and its command line carries the run's operation id."""
        pid = _int(record.get("pid"))
        operation_id = record.get("operation_id")
        if pid is None or pid <= 0 or not isinstance(operation_id, str) or not operation_id:
            return None
        alive, _started = process_info(pid)
        command = process_command(pid) if alive else None
        if alive and command is not None and operation_id in command:
            return pid
        return None

    def _recovery_lock(self, run_id: str) -> threading.Lock:
        with self.lock:
            lock = self.recovery_locks.get(run_id)
            if lock is None:
                lock = self.recovery_locks[run_id] = threading.Lock()
            return lock

    def _stop_cutoff(self) -> Optional[float]:
        return self.stop_by

    def _claim_record(self, run_id: str) -> Tuple[bool, Optional[int]]:
        """Take the cross-process lock of one run record: ``flock`` on
        ``runs/<run_id>.lock`` (0600), so two servers recovering the same
        gone server's run never both write its terminal record (contract 4.1).
        Returns ``(taken, fd)``; close ``fd`` to release. Not taken within
        ``RECOVERY_LOCK_WAIT_S`` (the other holder only reads and writes one
        small file) -> ``(False, None)``. Without ``fcntl`` -> ``(True, None)``."""
        if fcntl is None:
            return True, None
        try:
            ensure_private_dir(self.runs_dir)
            fd = os.open(str(self.runs_dir / ("%s.lock" % run_id)),
                         os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o600)
        except OSError as exc:
            self.services.log("run %s lock could not be opened: %s" % (run_id, type(exc).__name__))
            return False, None
        deadline = time.monotonic() + RECOVERY_LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True, fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            except OSError as exc:
                self.services.log("run %s lock failed: %s" % (run_id, type(exc).__name__))
                break
        os.close(fd)
        return False, None

    def recover_run(self, run_id: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Recover one run of a gone server: its live orphan is stopped first
        (SIGINT, 10 s, SIGKILL to the process group; sooner once this server
        stops) and only when the group is gone does the record become
        ``outcome_unknown`` (reason ``server_restarted``). Until then the record
        stays active, so a later server tries again. One recovery per run at a
        time in this server (a thread lock) and across servers (``flock`` on
        the run's lock file around the reload and the write); the record is
        read again under that lock, so a terminal record is never rewritten. Returns ``(outcome, record)`` with outcome
        ``missing``, ``ended``, ``owned``, ``recovered`` or ``still_running``."""
        with self._recovery_lock(run_id):
            record = self.load(run_id)
            if record is None:
                return "missing", None
            if record.get("status") not in ACTIVE:
                return "ended", record
            if self.owner_alive(record):
                return "owned", record
            orphan = self.orphan_of(record)
            forced = False
            if orphan is not None:
                forced = stop_group(orphan, CANCEL_GRACE_S, self._stop_cutoff)
                if not wait_group_gone(orphan, READER_JOIN_S):
                    self.services.log("orphaned flow run %s did not stop; its record stays active" % run_id)
                    return "still_running", record
            # Another server may be recovering the same run: the reload and the
            # write happen under the cross-process record lock, so only the
            # first writes the terminal record and the next reads it back.
            taken, fd = self._claim_record(run_id)
            if not taken:
                self.services.log("flow run %s is being recovered by another server" % run_id)
                return "still_running", record
            try:
                current = self.load(run_id)
                if current is None:
                    return "missing", None
                if current.get("status") not in ACTIVE:
                    return "ended", current
                updated = dict(current)
                updated["status"] = "outcome_unknown"
                updated["reason"] = "server_restarted"
                updated["ended_at"] = _now_iso()
                if current.get("current_step") is not None:
                    updated["last_step_outcome"] = "unknown"
                updated["current_step"] = None
                if forced:
                    updated["forced"] = True
                try:
                    self.write_record(updated)
                except (OSError, ValueError, TypeError):
                    pass
                return "recovered", updated
            finally:
                if fd is not None:
                    os.close(fd)  # releases the flock

    def _recover_in_background(self, run_id: str) -> None:
        try:
            self.recover_run(run_id)
        except Exception as exc:  # noqa: BLE001 - a background thread only logs
            self.services.log("orphaned flow run %s could not be recovered: %s" % (run_id, type(exc).__name__))

    def recover(self) -> List[int]:
        """Startup: active records of gone servers become ``outcome_unknown``;
        a live orphan is stopped in the background first and its record is
        written once its process group is gone (shutdown waits for these
        threads). Returns the orphan pids."""
        try:
            names = sorted(os.listdir(str(self.runs_dir)))
        except OSError:
            return []
        orphans: List[Tuple[str, int]] = []
        for name in names:
            if not name.startswith("run_") or not name.endswith(".json"):
                continue
            run_id = name[:-len(".json")]
            record = self.load(run_id)
            if record is None or record.get("status") not in ACTIVE:
                continue
            if self.owner_alive(record):
                continue  # another live server's run: never touched
            orphan = self.orphan_of(record)
            if orphan is None:
                self.recover_run(run_id)
            else:
                orphans.append((run_id, orphan))
        if orphans:
            self.services.log("stopping %d orphaned flow run(s)" % len(orphans))
            for run_id, _pid in orphans:
                thread = threading.Thread(target=self._recover_in_background, args=(run_id,),
                                          name="run-orphan", daemon=True)
                with self.lock:
                    self.recovering.append(thread)
                thread.start()
        return [pid for _run_id, pid in orphans]

    # -- registry

    def active_job(self, session_id: str) -> Optional[Job]:
        with self.lock:
            for job in self.jobs.values():
                if job.session_id == session_id and not job.done.is_set():
                    return job
        return None

    def get_job(self, run_id: str) -> Optional[Job]:
        with self.lock:
            return self.jobs.get(run_id)

    def start(self, ctx: CallContext, ref: Dict[str, Any], flow: Mapping[str, str],
              env_values: Dict[str, str]) -> Dict[str, Any]:
        """Register the job, take the session, get the lease, spawn.

        The job is in ``jobs`` before the guard is taken, so the session hook
        and the shutdown hook see (and cancel) a run that is still launching:
        cancelled before the spawn it is never spawned, cancelled during it its
        child is interrupted at once."""
        services = self.services
        run_id = new_id("run_", 8)
        limit = int(services.config.get("max_runs") or 2)
        record: Dict[str, Any] = {
            "schema": RECORD_SCHEMA, "run_id": run_id, "host_id": ref["host_id"],
            "session_id": ref["session_id"], "flow_id": flow["flow_id"], "flow_name": flow["name"],
            "status": "pending", "created_at": _now_iso(), "started_at": None, "ended_at": None,
            "pid": None, "server_pid": self.server_pid, "server_started_at": self.server_started_at,
            "operation_id": ctx.operation_id, "holder_id": None, "generation": None,
            "env_keys": sorted(env_values),
            "steps": {"total": 0, "done": 0, "failed": 0}, "current_step": None,
            "exit_code": None, "error_code": None, "error": None, "reason": None,
            "report_artifacts": [], "events_dropped": 0, "forced": False,
            "last_step_outcome": None,
        }
        job = Job(self, record, ref, flow["path"], env_values)
        with self.lock:
            if self.closing:
                raise rejected("server_stopping", "The server is stopping; no flow run is started",
                               "Call start_flow_run again once the adapter server runs.")
            active = sum(1 for item in self.jobs.values() if not item.done.is_set())
            if active >= limit:
                raise rejected("run_limit", "This server already runs %d flow run(s), its max_runs limit"
                               % active, "Wait for a run to end, or cancel one with cancel_run.")
            self.jobs[run_id] = job
        held = False
        try:
            services.guard.hold(ref["session_id"], {"run_id": run_id})
            held = True
            refusal = job.cancelled_before_start()
            if refusal is not None:
                raise refusal
            claim = services.keeper.ensure(ref)
            with job.lock:
                job.record["holder_id"] = claim["holder"]
                job.record["generation"] = claim["generation"]
            job.launch(claim)
        except BaseException:
            if held and not job.monitored:
                services.guard.release(ref["session_id"])  # else the monitor releases it
            with self.lock:
                if job.proc is None:
                    self.jobs.pop(run_id, None)  # nothing was spawned: no run exists
            job.abandon()
            raise
        return job.view()

    # -- hooks

    def session_stopping(self, session_id: str) -> Optional[str]:
        """Session hook: cancel the session's run and wait for its end."""
        job = self.active_job(session_id)
        if job is None:
            return None
        job.cancel("session_stopped")
        if job.done.wait(CANCEL_WAIT_S):
            return "cancelled %s" % job.run_id
        return "%s is still stopping" % job.run_id

    def shutdown(self) -> List[str]:
        """Shutdown hook: cancel every running run (reason ``server_stopped``)
        inside the server's 10 s hook budget. Orphans of a gone server that
        are still being stopped get their SIGKILL by the same grace, and their
        recovery threads are waited for, so a record is never left terminal
        with its orphan alive."""
        with self.lock:
            self.closing = True  # no run starts after the snapshot
            if self.stop_by is None:
                self.stop_by = time.monotonic() + SHUTDOWN_GRACE_S
            jobs = [job for job in self.jobs.values() if not job.done.is_set()]
            recovering = list(self.recovering)
        for job in jobs:
            job.cancel("server_stopped", grace_s=SHUTDOWN_GRACE_S)
        deadline = time.monotonic() + SHUTDOWN_WAIT_S
        for job in jobs:
            job.done.wait(max(0.0, deadline - time.monotonic()))
        for thread in recovering:
            thread.join(max(0.0, deadline - time.monotonic()))
        return [job.run_id for job in jobs]


def run_store(services: Services) -> RunStore:
    store = services.extensions.get("mcp_runs")
    if store is None:
        store = RunStore(services)
        services.extensions["mcp_runs"] = store
    return store


def setup(services: Services) -> None:
    store = run_store(services)
    store.recover()
    services.hooks.register("runs", store.session_stopping)
    services.on_shutdown("runs", store.shutdown, 10)


# ---------------------------------------------------------------- handlers

def _local_host(ctx: CallContext, host_id: Optional[str]) -> str:
    local = ctx.services.refs.check_host(host_id)
    ctx.host_id = local["host_id"]
    return local["host_id"]


def list_flows(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    _local_host(ctx, args.get("host_id"))
    roots = list(ctx.services.config.get("flow_roots") or [])
    if not roots:
        ctx.warn("no_flow_roots", "No flow_roots are configured in the adapter config")
        return {"flows": []}
    flows, warnings = walk_flows(roots)
    for code, message in warnings:
        ctx.warn(code, message)
    return {"flows": [{"flow_id": item["flow_id"], "name": item["name"], "root": item["root"]}
                      for item in flows]}


def check_env(value: Any) -> Dict[str, str]:
    """The ``env`` argument: at most 16 keys ``^[A-Z][A-Z0-9_]{0,63}$``,
    values of 0..500 printable characters."""
    env = value or {}
    if len(env) > MAX_ENV_KEYS:
        raise bad_argument("env", "must have at most %d keys" % MAX_ENV_KEYS)
    for key, item in env.items():
        if not (1 <= len(key) <= 64 and "A" <= key[0] <= "Z" and all(char in ENV_KEY_CHARS for char in key)):
            raise bad_argument("env", "keys must match ^[A-Z][A-Z0-9_]{0,63}$")
        if not isinstance(item, str) or len(item) > MAX_ENV_VALUE or not item.isprintable():
            raise bad_argument("env.%s" % key, "must be 0..%d printable characters" % MAX_ENV_VALUE)
    return {key: env[key] for key in sorted(env)}


def start_flow_run(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    ref = ctx.resolve(args["host_id"], args["session_id"])
    env_values = check_env(args.get("env"))
    roots = list(ctx.services.config.get("flow_roots") or [])
    flows = walk_flows(roots)[0] if roots else []
    flow = next((item for item in flows if item["flow_id"] == args["flow_id"]), None)
    if flow is None:
        raise rejected("flow_not_found", "Flow %s is not under the configured flow_roots" % args["flow_id"],
                       "Call list_flows for the flow ids of this host.")
    return {"run": _safe(ctx, run_store(ctx.services).start(ctx, ref, flow, env_values))}


def _record_of(ctx: CallContext, host_id: str, run_id: str) -> Tuple[Optional[Job], Dict[str, Any]]:
    local = _local_host(ctx, host_id)
    store = run_store(ctx.services)
    job = store.get_job(run_id)
    if job is not None:
        with job.lock:
            record = dict(job.record)
    else:
        loaded = store.load(run_id)
        if loaded is None:
            raise rejected("run_not_found", "Run %s was not found on this host" % run_id,
                           "Call get_run with a run_id from start_flow_run.")
        record = loaded
    if record.get("host_id") != local:
        raise rejected("run_not_found", "Run %s was not found on this host" % run_id,
                       "Call get_run with a run_id from start_flow_run.")
    return job, record


def _safe(ctx: CallContext, view: Dict[str, Any]) -> Dict[str, Any]:
    """A run view for a successful result: its error text is scrubbed again
    (a record written by an older server may hold a local path)."""
    if isinstance(view.get("error"), str):
        view["error"] = scrub_text(ctx.services, view["error"])[:500]
    return view


def get_run(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    job, record = _record_of(ctx, args["host_id"], args["run_id"])
    return {"run": _safe(ctx, job.view() if job is not None else record_view(record))}


def cancel_run(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    run_id = args["run_id"]
    job, record = _record_of(ctx, args["host_id"], run_id)
    store = run_store(ctx.services)
    if job is not None:
        if not job.cancel("cancel_requested"):
            job.done.wait(READER_JOIN_S + 1.0)
            raise rejected("run_not_running", "Run %s already ended (%s)" % (run_id, job.view()["status"]),
                           "Read its result with get_run.", run=job.view())
        if not job.done.wait(CANCEL_WAIT_S):
            raise ToolError("cancel_unconfirmed", "Run %s did not confirm its end in time" % run_id,
                            "Call get_run and check the screen before acting on the session again.",
                            run=job.view())
        return {"run": _safe(ctx, job.view())}
    if record.get("status") not in ACTIVE:
        raise rejected("run_not_running", "Run %s already ended (%s)" % (run_id, record.get("status")),
                       "Read its result with get_run.", run=record_view(record))
    if store.owner_alive(record):
        raise rejected("run_not_owned", "Run %s belongs to another live Autonom adapter server" % run_id,
                       "Cancel it from the conversation that started it.", run=record_view(record))
    # The owning server is gone: the same recovery as at startup, done now
    # (serialized per run, so a second cancel waits and then reads the end).
    outcome, current = store.recover_run(run_id)
    if outcome == "missing" or current is None or current.get("host_id") != record.get("host_id"):
        raise rejected("run_not_found", "Run %s was not found on this host" % run_id,
                       "Call get_run with a run_id from start_flow_run.")
    if outcome == "ended":
        raise rejected("run_not_running", "Run %s already ended (%s)" % (run_id, current.get("status")),
                       "Read its result with get_run.", run=record_view(current))
    if outcome == "owned":
        raise rejected("run_not_owned", "Run %s belongs to another live Autonom adapter server" % run_id,
                       "Cancel it from the conversation that started it.", run=record_view(current))
    if outcome == "still_running":
        raise ToolError("cancel_unconfirmed", "Run %s did not confirm its end in time" % run_id,
                        "Call get_run and check the screen before acting on the session again.",
                        run=record_view(current))
    return {"run": _safe(ctx, record_view(current))}


def _entry_view(ctx: CallContext, ref: Mapping[str, Any], item: Mapping[str, Any]) -> Dict[str, Any]:
    return {"artifact_id": ctx.services.refs.artifact_id(ref["host_id"], ref["session_id"], item["relpath"]),
            "kind": item["kind"], "name": item["name"], "group": item["group"], "size": item["size"],
            "modified_at": _iso_of(item["mtime"]), "mime": item["mime"]}


def list_artifacts(ctx: CallContext, args: Dict[str, Any]) -> Dict[str, Any]:
    ref = ctx.resolve(args["host_id"], args["session_id"], live=False)
    found, truncated = scan_artifacts(ctx.services.refs.session_artifacts_dir(ref))
    kind = args.get("kind")
    if kind is not None:
        found = [item for item in found if item["kind"] == kind]
    limit = int(args.get("limit") or 50)
    if truncated:
        ctx.warn("artifacts_truncated", "The scan stopped after %d entries" % SCAN_ENTRIES)
    return {"artifacts": [_entry_view(ctx, ref, item) for item in found[:limit]],
            "truncated": truncated or len(found) > limit}


def open_inside(base: Path, relpath: str) -> Tuple[int, os.stat_result]:
    """An fd of ``base/relpath`` opened with ``O_NOFOLLOW`` whose real path is
    inside ``base`` and is the very file that was opened."""
    if not allowed_relpath(base, relpath):
        raise FileNotFoundError(relpath)
    target = os.path.join(str(base), *relpath.split("/"))
    fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        info = os.fstat(fd)
        base_real = os.path.realpath(str(base))
        real = os.path.realpath(target)
        if real == base_real or os.path.commonpath([real, base_real]) != base_real:
            raise FileNotFoundError(relpath)
        check = os.stat(real)
        if not stat.S_ISREG(info.st_mode) or (check.st_dev, check.st_ino) != (info.st_dev, info.st_ino):
            raise FileNotFoundError(relpath)
        if info.st_size > ARTIFACT_MAX_SIZE:
            raise FileNotFoundError(relpath)
    except BaseException:
        os.close(fd)
        raise
    return fd, info


# Text artifacts are cleaned like every other result text (DEC-027): a journal
# line names the screenshot's absolute path, for example. The whole file is
# cleaned at once and paged as the UTF-8 bytes of the cleaned text, so
# ``offset``/``length``/``next_offset`` count bytes of the cleaned text, a
# page never ends inside a character (a page shorter than one character
# holds that one character), and the pages of a file join to exactly the
# cleaned text of the whole file. The last cleaned file is kept per server
# while the file and the cleaning inputs stay the same.

def _continuation(data: bytes, at: int) -> bool:
    return 0 <= at < len(data) and (data[at] & 0xC0) == 0x80


def text_page(data: bytes, offset: int, max_bytes: int) -> Tuple[int, int]:
    """``(start, end)`` of the page of the UTF-8 ``data`` asked for by
    ``offset`` and ``max_bytes``: ``start`` moves forward to a character
    start, ``end`` back to one; a page that would hold no whole character
    holds the one at ``start``."""
    total = len(data)
    if offset >= total:
        return offset, offset
    start = offset
    while _continuation(data, start):
        start += 1
    end = min(total, start + max(1, max_bytes))
    while end > start and _continuation(data, end):
        end -= 1
    if end == start and start < total:
        end = start + 1
        while _continuation(data, end):
            end += 1
    return start, end


def clean_text_bytes(services: Any, raw: bytes, labels: Sequence[Tuple[str, str]]) -> bytes:
    """The UTF-8 bytes of ``raw`` (decoded with replacement) after ``scrub_text``."""
    return scrub_text(services, raw.decode("utf-8", "replace"), labels).encode("utf-8")


def _read_all(fd: int, size: int) -> bytes:
    chunks: List[bytes] = []
    remaining = size
    os.lseek(fd, 0, os.SEEK_SET)
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 1048576))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _text_key(services: Any, base: Path, relpath: str, info: os.stat_result,
              labels: Sequence[Tuple[str, str]]) -> str:
    """What the cleaned text of one file depends on: the file (device, inode,
    size, times), the roots and the secrets. A digest, so no secret is kept
    in the key."""
    material = json.dumps([os.path.realpath(str(base)), relpath, info.st_dev, info.st_ino, info.st_size,
                           getattr(info, "st_mtime_ns", info.st_mtime), getattr(info, "st_ctime_ns", info.st_ctime),
                           local_roots(services, labels), sorted(known_secrets())])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def cleaned_text(services: Any, fd: int, info: os.stat_result, base: Path, relpath: str,
                 labels: Sequence[Tuple[str, str]]) -> bytes:
    """The cleaned text of the open file ``fd`` (from the server's one-entry
    cache when nothing it depends on changed)."""
    store = run_store(services)
    key = _text_key(services, base, relpath, info, labels)
    with store.text_lock:
        if store.text_cache is not None and store.text_cache[0] == key:
            return store.text_cache[1]
    data = clean_text_bytes(services, _read_all(fd, int(info.st_size)), labels)
    with store.text_lock:
        store.text_cache = (key, data)
    return data


def _not_found(artifact_id: str) -> ToolError:
    return rejected("artifact_not_found", "Artifact %s is not in this session" % artifact_id,
                    "Call list_artifacts for the artifact ids of this session.")


def get_artifact(ctx: CallContext, args: Dict[str, Any]) -> Reply:
    ref = ctx.resolve(args["host_id"], args["session_id"], live=False)
    base = ctx.services.refs.session_artifacts_dir(ref)
    wanted = args["artifact_id"]
    found, _truncated = scan_artifacts(base)
    item = next((entry for entry in found
                 if ctx.services.refs.artifact_id(ref["host_id"], ref["session_id"], entry["relpath"]) == wanted),
                None)
    if item is None:
        raise _not_found(wanted)
    offset = int(args.get("offset") or 0)
    max_bytes = int(args.get("max_bytes") or DEFAULT_MAX_BYTES)
    try:
        fd, info = open_inside(base, item["relpath"])
    except OSError:
        raise _not_found(wanted)
    text_kind = item["kind"] in TEXT_KINDS
    try:
        size = int(info.st_size)
        if text_kind:
            cleaned = cleaned_text(ctx.services, fd, info, base, item["relpath"], [(str(base), "<session>")])
            start, end = text_page(cleaned, offset, max_bytes)
            data = cleaned[start:end]
            total = len(cleaned)
        else:
            start, total = offset, size
            remaining = max(0, min(max_bytes, size - offset))
            chunks: List[bytes] = []
            if remaining:
                os.lseek(fd, offset, os.SEEK_SET)
                while remaining > 0:
                    chunk = os.read(fd, min(remaining, 1048576))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
            data = b"".join(chunks)
            end = offset + len(data)
    except OSError as exc:
        raise ToolError("artifact_unreadable", "Artifact %s could not be read: %s" % (wanted, type(exc).__name__),
                        "Call get_artifact again.")
    finally:
        os.close(fd)
    artifact: Dict[str, Any] = {
        "artifact_id": wanted, "kind": item["kind"], "name": item["name"], "size": size,
        "mime": item["mime"], "offset": start, "length": len(data),
        "next_offset": end if data and end < total else None,
    }
    content: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {}
    if text_kind:
        artifact["text"] = data.decode("utf-8")
    else:
        encoded = base64.b64encode(data).decode("ascii")
        meta["autonom/artifact"] = {"data_b64": encoded}
        inline_max = int(ctx.services.config.get("inline_image_max_bytes") or 0)
        if offset == 0 and len(data) == size and 0 < size <= inline_max:
            content.append({"type": "image", "data": encoded, "mimeType": item["mime"]})
    return Reply({"artifact": artifact}, content, meta)


# ---------------------------------------------------------------- schemas and catalog entries

RUN_ID = string(RUN_ID_RE, description="Run id from start_flow_run.")
FLOW_ID = string(FLOW_ID_RE, description="Flow id from list_flows.")
ARTIFACT_ID = string(ARTIFACT_ID_RE, description="Artifact id from list_artifacts or a run.")
TIME = nullable(string(max_length=64))
STEPS = obj({"total": integer(minimum=0), "done": integer(minimum=0), "failed": integer(minimum=0)},
            ("total", "done", "failed"))
CURRENT_STEP = obj({"index": nullable(integer()), "command": nullable(string(max_length=80)),
                    "started_at": TIME}, ("index", "command", "started_at"))
RUN_VIEW = obj({
    "run_id": string(RUN_ID_RE),
    "host_id": HOST_ID,
    "session_id": SESSION_ID,
    "flow_id": string(FLOW_ID_RE),
    "flow_name": string(max_length=1024),
    "status": string(enum=STATUSES),
    "created_at": TIME,
    "started_at": TIME,
    "ended_at": TIME,
    "operation_id": string(OPERATION_ID_RE),
    "generation": nullable(integer(minimum=0)),
    "env_keys": {"type": "array", "maxItems": MAX_ENV_KEYS, "items": string(max_length=64)},
    "steps": STEPS,
    "current_step": nullable(CURRENT_STEP),
    "exit_code": nullable(integer()),
    "error_code": nullable(string(max_length=80)),
    "error": nullable(string(max_length=500)),
    "reason": nullable(string(max_length=200)),
    "report_artifacts": {"type": "array", "maxItems": MAX_REPORTS, "items": string(ARTIFACT_ID_RE)},
    "events_dropped": integer(minimum=0),
    "forced": {"type": "boolean"},
    "last_step_outcome": nullable(string(enum=("unknown",))),
}, ("run_id", "host_id", "session_id", "flow_id", "flow_name", "status", "created_at", "started_at",
    "ended_at", "operation_id", "generation", "env_keys", "steps", "current_step", "exit_code",
    "error_code", "error", "reason", "report_artifacts", "events_dropped", "forced", "last_step_outcome"))
FLOW_ENTRY = obj({"flow_id": string(FLOW_ID_RE), "name": string(max_length=1024),
                  "root": string(max_length=255)}, ("flow_id", "name", "root"))
ARTIFACT_ENTRY = obj({
    "artifact_id": string(ARTIFACT_ID_RE),
    "kind": string(enum=KINDS),
    "name": string(max_length=255),
    "group": nullable(string(max_length=255)),
    "size": integer(minimum=0),
    "modified_at": string(max_length=64),
    "mime": string(max_length=80),
}, ("artifact_id", "kind", "name", "group", "size", "modified_at", "mime"))
ARTIFACT_CHUNK = obj({
    "artifact_id": string(ARTIFACT_ID_RE),
    "kind": string(enum=KINDS),
    "name": string(max_length=255),
    "size": integer(minimum=0),
    "mime": string(max_length=80),
    "offset": integer(minimum=0),
    "length": integer(minimum=0),
    "next_offset": nullable(integer(minimum=0)),
    "text": string(),
}, ("artifact_id", "kind", "name", "size", "mime", "offset", "length", "next_offset"))

TOOLS = (
    Tool(
        name="list_flows",
        title="List flows",
        description=("List the Flow files under the adapter's configured flow_roots with their flow_id, "
                     "name (path relative to its root) and root folder name. Pass host_id to filter."),
        input_schema=obj({"host_id": HOST_ID}),
        output_fields={"flows": {"type": "array", "maxItems": FLOW_FILES, "items": FLOW_ENTRY}},
        annotations=dict(READ),
        meta=dict(WIDGET),
        mutating=False,
        handler=list_flows,
    ),
    Tool(
        name="start_flow_run",
        title="Start a flow run",
        description=("Start a Flow from list_flows on a session as a background run and return at once "
                     "with its run_id. The run holds the session (other agent actions are refused "
                     "session_busy) and the agent control lease until it ends. Follow it with get_run and "
                     "stop it with cancel_run. env sets Flow variables; only their names are kept."),
        input_schema=obj({
            "host_id": HOST_ID, "session_id": SESSION_ID, "flow_id": FLOW_ID,
            "env": {"type": "object", "additionalProperties": string(max_length=MAX_ENV_VALUE),
                    "description": ("Flow variables: at most 16 keys ^[A-Z][A-Z0-9_]{0,63}$, values of up "
                                    "to 500 printable characters.")},
        }, ("host_id", "session_id", "flow_id")),
        output_fields={"run": RUN_VIEW},
        annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        meta=dict(WIDGET),
        mutating=True,
        handler=start_flow_run,
    ),
    Tool(
        name="get_run",
        title="Get a flow run",
        description=("Read a flow run by run_id: status (pending, running, passed, failed, interrupted, "
                     "cancelled, outcome_unknown), step progress, the current step's command name, the "
                     "error code and the report artifact ids."),
        input_schema=obj({"host_id": HOST_ID, "run_id": RUN_ID}, ("host_id", "run_id")),
        output_fields={"run": RUN_VIEW},
        annotations=dict(READ),
        meta=dict(WIDGET),
        mutating=False,
        handler=get_run,
    ),
    Tool(
        name="cancel_run",
        title="Cancel a flow run",
        description=("Stop a running flow run: interrupt it, and force it after 10 s. A step that was "
                     "running may or may not have reached the device (last_step_outcome unknown), so check "
                     "the screen before acting again."),
        input_schema=obj({"host_id": HOST_ID, "run_id": RUN_ID}, ("host_id", "run_id")),
        output_fields={"run": RUN_VIEW},
        annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
        meta=dict(WIDGET),
        mutating=True,
        handler=cancel_run,
    ),
    Tool(
        name="list_artifacts",
        title="List session artifacts",
        description=("List a session's evidence files (screenshots, journal, JSON, reports, logs) by "
                     "artifact_id, newest first. Network captures and secrets are never listed."),
        input_schema=obj({"host_id": HOST_ID, "session_id": SESSION_ID,
                          "kind": string(enum=KINDS), "limit": integer(1, 200, default=50)},
                         ("host_id", "session_id")),
        output_fields={"artifacts": {"type": "array", "maxItems": 200, "items": ARTIFACT_ENTRY},
                       "truncated": {"type": "boolean"}},
        annotations=dict(READ),
        meta=dict(WIDGET),
        mutating=False,
        handler=list_artifacts,
    ),
    Tool(
        name="get_artifact",
        title="Get a session artifact",
        description=("Read one artifact by artifact_id in pages (offset, max_bytes; next_offset is null at "
                     "the end). Text kinds return text; images return their bytes in _meta and, when small "
                     "enough, an image block."),
        input_schema=obj({"host_id": HOST_ID, "session_id": SESSION_ID, "artifact_id": ARTIFACT_ID,
                          "offset": integer(minimum=0),
                          "max_bytes": integer(1, 4194304, default=DEFAULT_MAX_BYTES)},
                         ("host_id", "session_id", "artifact_id")),
        output_fields={"artifact": ARTIFACT_CHUNK},
        annotations=dict(READ),
        meta=dict(WIDGET),
        mutating=False,
        handler=get_artifact,
    ),
)
