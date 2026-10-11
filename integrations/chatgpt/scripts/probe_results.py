#!/usr/bin/env python3
"""Check ChatGPT probe results files and the P0 results doc.

Contract section 7.1. A results file is the JSON the probe page saves
(contract 5.6). This script checks it, checks that the results doc
(``docs/plans/CHATGPT_P0_RESULTS.md``) reports the same statuses, checks the
empty doc template, and writes an honest stub file for a run that could not
happen at all.

Usage:
  probe_results.py check --results [LABEL=]FILE ... [--profile harness|live]
                         [--doc FILE] [--evidence-dir DIR] [--json]
  probe_results.py check-template --doc FILE [--json]
  probe_results.py stub --reason TEXT --out FILE [--host-name NAME]
                        [--host-version VERSION] [--json]

``check`` reports each problem as ``{"file", "path", "problem"}``.
Exit codes: 0 no problems, 1 problems found (or a refused stub),
2 usage error or input that cannot be read.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

PROBE_VERSION = "0.1.0"
SCHEMA_VERSION = 1

# The 27 checks of contract 5.3 in display order: (id, group, route, depends_on).
# tests/test_chatgpt_probe_results.py compares this table with probe-core.mjs CHECKS.
CHECKS: Tuple[Tuple[str, str, Optional[str], Tuple[str, ...]], ...] = (
    ("net.fetch.local", "routes", "local", ()),
    ("net.fetch.remote", "routes", "remote", ()),
    ("net.ws.local", "routes", "local", ("net.fetch.local",)),
    ("net.ws.remote", "routes", "remote", ("net.fetch.remote",)),
    ("input.rtt.local", "routes", "local", ("net.ws.local",)),
    ("input.rtt.remote", "routes", "remote", ("net.ws.remote",)),
    ("media.decode.local", "routes", "local", ("net.ws.local", "media.webcodecs")),
    ("media.decode.remote", "routes", "remote", ("net.ws.remote", "media.webcodecs")),
    ("net.reconnect.local", "routes", "local", ("net.ws.local", "media.webcodecs")),
    ("net.reconnect.remote", "routes", "remote", ("net.ws.remote", "media.webcodecs")),
    ("net.csp.local", "routes", "local", ()),
    ("net.csp.remote", "routes", "remote", ()),
    ("host.init", "host", None, ()),
    ("host.entry", "host", None, ("host.init",)),
    ("host.theme", "host", None, ("host.init",)),
    ("host.size", "host", None, ("host.init",)),
    ("bridge.read", "host", None, ()),
    ("bridge.write", "host", None, ("bridge.read",)),
    ("bridge.error", "host", None, ()),
    ("bridge.stale", "host", None, ("bridge.read",)),
    ("bridge.context", "host", None, ()),
    ("bridge.message", "host", None, ()),
    ("bridge.media", "host", None, ("media.webcodecs",)),
    ("model.tool", "host", None, ()),
    ("media.webcodecs", "host", None, ()),
    ("device.android.local", "devices", "local", ("net.ws.local", "media.webcodecs")),
    ("device.ios.remote", "devices", "remote", ("net.ws.remote", "media.webcodecs")),
)
CHECK_IDS: Tuple[str, ...] = tuple(row[0] for row in CHECKS)
CHECK_BY_ID = {row[0]: row for row in CHECKS}

STATUSES = ("pass", "fail", "not_exercised")
STATUS_WORDS = {"pass": "pass", "fail": "fail", "not_exercised": "not exercised"}
OBSERVERS = ("probe", "operator")
ROUTE_ROWS = ("net.fetch", "net.ws", "input.rtt", "media.decode", "net.reconnect", "net.csp")

HARNESS_REQUIRED = (
    "host.init", "host.theme", "host.size", "bridge.read", "bridge.write", "bridge.error",
    "bridge.stale", "bridge.message", "bridge.media", "media.webcodecs",
) + tuple("%s.local" % row for row in ROUTE_ROWS)
HARNESS_REMOTE = tuple("%s.remote" % row for row in ROUTE_ROWS)

DOC_HEADINGS = (
    "# ChatGPT P0 results: desktop host contract",
    "## 1. Environment",
    "## 2. Capability matrix",
    "## 3. Routes",
    "## 4. Transport and topology decision for P3 and P4",
    "## 5. Installation requirements",
    "## 6. Revised estimates",
    "## 7. Evidence",
    "## 8. Not exercised and why",
)
MATRIX_HEADING = "## 2. Capability matrix"
DOC_LABELS = ("harness-chromium", "harness-webkit", "harness-devices", "harness-forwarded",
              "chatgpt-global", "chatgpt-thread", "chatgpt-devices")
CELL_WORDS = ("pass", "fail", "not exercised", "pending")

RUN_ID = re.compile(r"^probe-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
BARE_ORIGIN = re.compile(r"^https?://([a-z0-9.-]+|\[[0-9a-f:]+\])(:[0-9]{1,5})?$")
LABEL = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SECRET_KEYS = frozenset({"media_token", "token", "authorization"})
SECRET_TEXT = "bearer."

HINTS = {
    "invalid_arguments": "Fix the named option and run the command again.",
    "results_unreadable": "Pass an existing results JSON file saved by the probe page or the harness.",
    "doc_unreadable": "Pass an existing Markdown file, for example docs/plans/CHATGPT_P0_RESULTS.md.",
    "out_exists": "Choose a new --out path; stub never overwrites a results file.",
    "out_unwritable": "Create the parent directory of --out or choose a writable path.",
    "reason_contains_secret": "Remove the token text from --reason, --host-name and --host-version.",
}


class Refusal(Exception):
    """A refused command: one JSON payload, exit 1 (or 2 for usage and unreadable input)."""

    def __init__(self, error_code: str, error: str, exit_code: int = 1, **extras: Any) -> None:
        super().__init__(error)
        self.error_code = error_code
        self.error = error
        self.exit_code = exit_code
        self.extras = extras

    def payload(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ok": False, "error_code": self.error_code, "error": self.error,
                               "hint": HINTS.get(self.error_code, "")}
        out.update(self.extras)
        return out


Problem = Dict[str, str]


def problem(file: str, path: str, text: str) -> Problem:
    return {"file": file, "path": path, "problem": text}


# ---------------------------------------------------------------- small checks

def parse_iso(value: Any) -> Optional[_dt.datetime]:
    if not isinstance(value, str) or not ISO_UTC.match(value):
        return None
    text = value[:-1]
    if "." in text:
        head, frac = text.split(".", 1)
        text = head + "." + (frac + "000000")[:6]
        fmt = "%Y-%m-%dT%H:%M:%S.%f"
    else:
        fmt = "%Y-%m-%dT%H:%M:%S"
    try:
        return _dt.datetime.strptime(text, fmt)
    except ValueError:
        return None


def is_bare_origin(value: Any) -> bool:
    match = BARE_ORIGIN.match(value) if isinstance(value, str) else None
    if not match:
        return False
    port = match.group(2)
    return port is None or 1 <= int(port[1:]) <= 65535


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def secret_problems(value: Any, file: str, path: str = "") -> List[Problem]:
    """No object key named like a token and no ``bearer.`` text anywhere (keys or strings)."""
    found: List[Problem] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            here = "%s.%s" % (path, key) if path else str(key)
            if str(key).lower() in SECRET_KEYS:
                found.append(problem(file, here, "key %r must not appear in results" % key))
            if SECRET_TEXT in str(key).lower():
                found.append(problem(file, here, "key contains %r" % SECRET_TEXT))
            found.extend(secret_problems(inner, file, here))
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            found.extend(secret_problems(inner, file, "%s[%d]" % (path, index)))
    elif isinstance(value, str) and SECRET_TEXT in value.lower():
        found.append(problem(file, path or "$", "text contains %r" % SECRET_TEXT))
    return found


# ---------------------------------------------------------------- results files

def check_results(data: Any, file: str) -> List[Problem]:
    """Every rule of contract 5.6 for one results object (the base check)."""
    out: List[Problem] = []
    if not isinstance(data, dict):
        return [problem(file, "$", "results must be a JSON object")]

    if data.get("schema_version") != SCHEMA_VERSION or isinstance(data.get("schema_version"), bool):
        out.append(problem(file, "schema_version", "must be %d" % SCHEMA_VERSION))
    if not isinstance(data.get("probe_version"), str) or not data.get("probe_version"):
        out.append(problem(file, "probe_version", "must be a non-empty string"))
    if not isinstance(data.get("run_id"), str) or not RUN_ID.match(data.get("run_id", "")):
        out.append(problem(file, "run_id", "must match probe-YYYYMMDD-HHMMSS-xxxx"))
    started = parse_iso(data.get("started_at"))
    finished = parse_iso(data.get("finished_at"))
    if started is None:
        out.append(problem(file, "started_at", "must be an ISO-8601 UTC time ending in Z"))
    if finished is None:
        out.append(problem(file, "finished_at", "must be an ISO-8601 UTC time ending in Z"))
    if started is not None and finished is not None and finished < started:
        out.append(problem(file, "finished_at", "is before started_at"))

    host = data.get("host")
    if not isinstance(host, dict):
        out.append(problem(file, "host", "must be an object"))
    else:
        if "entry" in host and host["entry"] not in ("global", "thread", "unknown"):
            out.append(problem(file, "host.entry", "must be global, thread or unknown"))
        if "theme" in host and host["theme"] not in ("light", "dark", None):
            out.append(problem(file, "host.theme", "must be light, dark or null"))

    out.extend(_check_routes(data.get("routes"), file))
    counts = {status: 0 for status in STATUSES}
    out.extend(_check_checks(data.get("checks"), file, counts))

    summary = data.get("summary")
    if not isinstance(summary, dict):
        out.append(problem(file, "summary", "must be an object"))
    else:
        for status in STATUSES:
            value = summary.get(status)
            if not is_int(value):
                out.append(problem(file, "summary.%s" % status, "must be an integer"))
            elif value != counts[status]:
                out.append(problem(file, "summary.%s" % status,
                                   "is %d but the checks hold %d" % (value, counts[status])))
    out.extend(secret_problems(data, file))
    return out


def _check_routes(routes: Any, file: str) -> List[Problem]:
    out: List[Problem] = []
    if not isinstance(routes, dict):
        return [problem(file, "routes", "must be an object")]
    for name in ("local", "remote"):
        route = routes.get(name)
        if not isinstance(route, dict):
            out.append(problem(file, "routes.%s" % name, "must be an object"))
            continue
        origin = route.get("origin")
        if origin is not None and not is_bare_origin(origin):
            out.append(problem(file, "routes.%s.origin" % name,
                               "must be a bare lowercase origin (scheme://host[:port]) or null"))
    remote = routes.get("remote")
    if isinstance(remote, dict):
        if not isinstance(remote.get("selected"), bool):
            out.append(problem(file, "routes.remote.selected", "must be true or false"))
        elif remote["selected"] and not remote.get("origin"):
            out.append(problem(file, "routes.remote.origin", "is required when the remote route is selected"))
    return out


def _check_checks(checks: Any, file: str, counts: Dict[str, int]) -> List[Problem]:
    out: List[Problem] = []
    if not isinstance(checks, list):
        return [problem(file, "checks", "must be a list")]
    seen: Dict[str, int] = {}
    for index, record in enumerate(checks):
        at = "checks[%d]" % index
        if not isinstance(record, dict):
            out.append(problem(file, at, "must be an object"))
            continue
        cid = record.get("id")
        if not isinstance(cid, str) or cid not in CHECK_BY_ID:
            out.append(problem(file, at + ".id", "unknown check id %r" % (cid,)))
            continue
        if cid in seen:
            out.append(problem(file, at + ".id", "duplicate id %s (first at checks[%d])" % (cid, seen[cid])))
            continue
        seen[cid] = index
        _, group, route, _deps = CHECK_BY_ID[cid]
        if record.get("group") != group:
            out.append(problem(file, at + ".group", "must be %r for %s" % (group, cid)))
        if record.get("route") != route:
            out.append(problem(file, at + ".route", "must be %r for %s" % (route, cid)))
        status = record.get("status")
        if status not in STATUSES:
            out.append(problem(file, at + ".status", "%s: status must be pass, fail or not_exercised, got %r"
                               % (cid, status)))
        else:
            counts[status] += 1
        if record.get("observed_by") not in OBSERVERS:
            out.append(problem(file, at + ".observed_by", "%s: must be probe or operator" % cid))
        if status == "not_exercised":
            reason = record.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                out.append(problem(file, at + ".reason", "%s: not_exercised needs a non-empty reason" % cid))
        if status == "fail":
            error = record.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, str) or not code.strip():
                out.append(problem(file, at + ".error.code", "%s: fail needs error.code" % cid))
        deps = record.get("depends_on", [])
        if not isinstance(deps, list) or not all(isinstance(dep, str) for dep in deps):
            out.append(problem(file, at + ".depends_on", "%s: must be a list of check ids" % cid))
        started = record.get("started_at")
        if started is not None and parse_iso(started) is None:
            out.append(problem(file, at + ".started_at", "%s: must be null or an ISO-8601 UTC time" % cid))
        duration = record.get("duration_ms")
        if duration is not None and (not is_int(duration) or duration < 0):
            out.append(problem(file, at + ".duration_ms", "%s: must be null or an integer >= 0" % cid))
    for cid in CHECK_IDS:
        if cid not in seen:
            out.append(problem(file, "checks", "missing check %s" % cid))
    return out


def status_by_id(data: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(data, dict) and isinstance(data.get("checks"), list):
        for record in data["checks"]:
            if isinstance(record, dict) and isinstance(record.get("id"), str) and record["id"] in CHECK_BY_ID and record["id"] not in out:
                out[record["id"]] = record.get("status")
    return out


def index_by_id(data: Any) -> Dict[str, int]:
    out: Dict[str, int] = {}
    if isinstance(data, dict) and isinstance(data.get("checks"), list):
        for index, record in enumerate(data["checks"]):
            if isinstance(record, dict) and isinstance(record.get("id"), str) and record["id"] in CHECK_BY_ID and record["id"] not in out:
                out[record["id"]] = index
    return out


def check_harness_profile(data: Any, file: str) -> List[Problem]:
    statuses = status_by_id(data)
    where = index_by_id(data)
    required = list(HARNESS_REQUIRED)
    routes = data.get("routes") if isinstance(data, dict) else None
    remote = routes.get("remote") if isinstance(routes, dict) else None
    if isinstance(remote, dict) and remote.get("selected") is True:
        required.extend(HARNESS_REMOTE)
    out: List[Problem] = []
    for cid in required:
        if cid not in statuses:
            continue  # already reported as missing by the base check
        if statuses[cid] != "pass":
            out.append(problem(file, "checks[%d].status" % where[cid],
                               "harness profile requires pass for %s, got %s" % (cid, statuses[cid])))
    return out


def check_evidence_dir(directory: Path) -> List[Problem]:
    shots = directory / "screenshots"
    name = str(directory)
    if not shots.is_dir():
        return [problem(name, "screenshots", "directory missing")]
    pngs = [p.name for p in shots.iterdir() if p.is_file() and p.suffix.lower() == ".png"]
    out: List[Problem] = []
    for word in ("global", "thread"):
        if not any(word in png for png in pngs):
            out.append(problem(name, "screenshots", "no *.png whose name contains %r" % word))
    return out


# ---------------------------------------------------------------- results doc

def headings(text: str) -> List[str]:
    """Level 1 and 2 headings outside code fences, in order."""
    out: List[str] = []
    fenced = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
            continue
        if not fenced and re.match(r"^#{1,2} ", line):
            out.append(line.rstrip())
    return out


def split_row(line: str) -> List[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in body.split("|")]


def matrix(text: str) -> Tuple[Optional[List[str]], List[Tuple[int, List[str]]]]:
    """The first table of section 2: (header cells, [(line number, cells)])."""
    lines = text.splitlines()
    start = None
    for number, line in enumerate(lines):
        if line.rstrip() == MATRIX_HEADING:
            start = number + 1
            break
    if start is None:
        return None, []
    header: Optional[List[str]] = None
    rows: List[Tuple[int, List[str]]] = []
    for number in range(start, len(lines)):
        line = lines[number]
        if re.match(r"^#{1,2} ", line):
            break
        if not line.strip().startswith("|"):
            if header is not None:
                break
            continue
        cells = split_row(line)
        if header is None:
            header = cells
        elif all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        else:
            rows.append((number + 1, cells))
    return header, rows


def cell_word(cell: str) -> Optional[str]:
    """The status word of a matrix cell: the text before an optional `` · value``."""
    word = cell.split(" · ", 1)[0].strip()
    return word if word in CELL_WORDS else None


def parse_matrix(text: str, file: str) -> Tuple[List[Problem], Optional[List[str]], Dict[str, Dict[str, str]]]:
    """Problems, column labels and {id: {label: word}} for the doc matrix."""
    out: List[Problem] = []
    header, rows = matrix(text)
    if header is None:
        return [problem(file, "matrix", "no table under %r" % MATRIX_HEADING)], None, {}
    if not header or header[0] != "Check":
        out.append(problem(file, "matrix.header", "first column must be 'Check'"))
    labels = header[1:]
    cells: Dict[str, Dict[str, str]] = {}
    for number, row in rows:
        at = "matrix line %d" % number
        match = re.fullmatch(r"`([^`]+)`", row[0]) if row else None
        if not match:
            out.append(problem(file, at, "first cell must be a check id in backticks"))
            continue
        cid = match.group(1)
        if cid not in CHECK_BY_ID:
            out.append(problem(file, at, "unknown check id %s" % cid))
            continue
        if cid in cells:
            out.append(problem(file, "matrix[%s]" % cid, "listed more than once"))
            continue
        if len(row) != len(header):
            out.append(problem(file, "matrix[%s]" % cid, "has %d cells, the header has %d" % (len(row), len(header))))
        words: Dict[str, str] = {}
        for label, cell in zip(labels, row[1:]):
            word = cell_word(cell)
            if word is None:
                out.append(problem(file, "matrix[%s][%s]" % (cid, label),
                                   "cell %r must be pass, fail, not exercised or pending" % cell))
                continue
            words[label] = word
        cells[cid] = words
    for cid in CHECK_IDS:
        if cid not in cells:
            out.append(problem(file, "matrix", "missing row for %s" % cid))
    return out, labels, cells


def doc_secret_problems(text: str, file: str) -> List[Problem]:
    for number, line in enumerate(text.splitlines(), 1):
        if SECRET_TEXT in line.lower():
            return [problem(file, "line %d" % number, "text contains %r" % SECRET_TEXT)]
    return []


def check_headings(text: str, file: str) -> List[Problem]:
    found = headings(text)
    if found == list(DOC_HEADINGS):
        return []
    for index, wanted in enumerate(DOC_HEADINGS):
        got = found[index] if index < len(found) else None
        if got != wanted:
            return [problem(file, "headings[%d]" % index, "expected %r, found %r" % (wanted, got))]
    return [problem(file, "headings[%d]" % len(DOC_HEADINGS), "unexpected extra heading %r"
                    % found[len(DOC_HEADINGS)])]


def check_doc(text: str, file: str, results: List[Tuple[str, str, Any]]) -> List[Problem]:
    """The doc reports each labelled results file's status word for every id; no pending cell."""
    out = check_headings(text, file) + doc_secret_problems(text, file)
    found, labels, cells = parse_matrix(text, file)
    out.extend(found)
    if labels is None:
        return out
    if labels != list(DOC_LABELS):
        out.append(problem(file, "matrix.header", "labels must be %s" % ", ".join(DOC_LABELS)))
    for cid, words in cells.items():
        for label, word in words.items():
            if word == "pending":
                out.append(problem(file, "matrix[%s][%s]" % (cid, label), "cell is still pending"))
    for label, _path, data in results:
        if label not in labels:
            out.append(problem(file, "matrix.header", "no column for results label %r" % label))
            continue
        statuses = status_by_id(data)
        for cid in CHECK_IDS:
            if cid not in cells or label not in cells[cid] or cid not in statuses:
                continue
            want = STATUS_WORDS.get(statuses[cid])
            got = cells[cid][label]
            if want is not None and got != "pending" and got != want:
                out.append(problem(file, "matrix[%s][%s]" % (cid, label),
                                   "doc says %r but the results file says %r" % (got, want)))
    return out


def check_template(text: str, file: str) -> List[Problem]:
    out = check_headings(text, file) + doc_secret_problems(text, file)
    found, labels, cells = parse_matrix(text, file)
    out.extend(found)
    if labels is None:
        return out
    if labels != list(DOC_LABELS):
        out.append(problem(file, "matrix.header", "labels must be %s" % ", ".join(DOC_LABELS)))
    for cid, words in cells.items():
        for label, word in words.items():
            if word != "pending":
                out.append(problem(file, "matrix[%s][%s]" % (cid, label), "template cell must be pending, got %r"
                                   % word))
    return out


# ---------------------------------------------------------------- input

def read_text(path: str, code: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise Refusal(code, "Cannot read %s: %s" % (path, type(exc).__name__), exit_code=2, path=path)


def read_results(path: str) -> Any:
    text = read_text(path, "results_unreadable")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise Refusal("results_unreadable", "%s is not valid JSON: %s" % (path, exc.msg), exit_code=2, path=path)
    except RecursionError:
        raise Refusal("results_unreadable", "%s is not valid JSON: nested too deeply" % path, exit_code=2,
                      path=path)


def split_label(spec: str) -> Tuple[str, str]:
    """``LABEL=FILE`` or ``FILE`` (label = file stem)."""
    label, sep, rest = spec.partition("=")
    if sep and LABEL.match(label) and rest:
        return label, rest
    if sep and not rest:
        raise Refusal("invalid_arguments", "--results %s: the file part is empty" % spec, exit_code=2)
    return Path(spec).stem, spec


# ---------------------------------------------------------------- commands

def cmd_check(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    if not args.results:
        raise Refusal("invalid_arguments", "check needs at least one --results [LABEL=]FILE", exit_code=2)
    if args.evidence_dir and args.profile != "live":
        raise Refusal("invalid_arguments", "--evidence-dir is used only with --profile live", exit_code=2)
    loaded: List[Tuple[str, str, Any]] = []
    labels_seen: Dict[str, str] = {}
    for spec in args.results:
        label, path = split_label(spec)
        if label in labels_seen:
            raise Refusal("invalid_arguments", "results label %r is given twice" % label, exit_code=2)
        labels_seen[label] = path
        loaded.append((label, path, read_results(path)))
    doc_text = read_text(args.doc, "doc_unreadable") if args.doc else None

    problems: List[Problem] = []
    files: List[Dict[str, Any]] = []
    for label, path, data in loaded:
        found = check_results(data, path)
        if args.profile == "harness":
            found.extend(check_harness_profile(data, path))
        problems.extend(found)
        summary = data.get("summary") if isinstance(data, dict) else None
        files.append({"label": label, "file": path, "problems": len(found),
                      "summary": summary if isinstance(summary, dict) else None})
    if args.profile == "live" and args.evidence_dir:
        problems.extend(check_evidence_dir(Path(args.evidence_dir)))
    if doc_text is not None:
        problems.extend(check_doc(doc_text, args.doc, loaded))
    result = {"ok": not problems, "command": "check", "profile": args.profile or "base",
              "files": files, "doc": args.doc, "problems": problems}
    return result, 0 if not problems else 1


def cmd_check_template(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    text = read_text(args.doc, "doc_unreadable")
    problems = check_template(text, args.doc)
    return {"ok": not problems, "command": "check-template", "doc": args.doc, "problems": problems}, \
        0 if not problems else 1


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def stub_results(reason: str, host_name: Optional[str] = None, host_version: Optional[str] = None,
                 now: Optional[_dt.datetime] = None) -> Dict[str, Any]:
    """Every check not_exercised with ``reason``: for a run that could not happen at all."""
    moment = now or utc_now()
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    run_id = "probe-%s-%s" % (moment.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(2))
    checks = [{
        "id": cid, "group": group, "route": route, "status": "not_exercised", "observed_by": "probe",
        "value": None, "unit": None, "error": None, "reason": reason, "depends_on": list(deps),
        "started_at": None, "duration_ms": None,
    } for cid, group, route, deps in CHECKS]
    return {
        "schema_version": SCHEMA_VERSION, "probe_version": PROBE_VERSION, "run_id": run_id,
        "started_at": stamp, "finished_at": stamp,
        "host": {"name": host_name, "version": host_version, "protocol": None, "entry": "unknown",
                 "display_mode": None, "theme": None, "container": {"width": None, "height": None},
                 "platform": None, "user_agent": None, "token_path": None, "mcp": None, "stub": True},
        "routes": {"local": {"origin": None}, "remote": {"origin": None, "label": None, "selected": False}},
        "checks": checks,
        "summary": {"pass": 0, "fail": 0, "not_exercised": len(checks)},
    }


def cmd_stub(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    reason = (args.reason or "").strip()
    if not reason:
        raise Refusal("invalid_arguments", "--reason must not be empty", exit_code=2)
    if len(reason) > 300:
        raise Refusal("invalid_arguments", "--reason must be at most 300 characters", exit_code=2)
    data = stub_results(reason, args.host_name, args.host_version)
    if secret_problems(data, args.out):
        raise Refusal("reason_contains_secret", "The stub would contain %r text" % SECRET_TEXT)
    out = Path(args.out)
    if out.exists() or out.is_symlink():
        raise Refusal("out_exists", "%s already exists" % args.out, path=args.out)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise Refusal("out_exists", "%s already exists" % args.out, path=args.out)
    except OSError as exc:
        raise Refusal("out_unwritable", "Cannot create %s: %s" % (args.out, type(exc).__name__), path=args.out)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return {"ok": True, "command": "stub", "out": args.out, "run_id": data["run_id"],
            "summary": data["summary"]}, 0


# ---------------------------------------------------------------- output

def human(result: Dict[str, Any]) -> str:
    if result["command"] == "stub":
        return "Wrote %s (run %s, %d checks not exercised)." % (
            result["out"], result["run_id"], result["summary"]["not_exercised"])
    lines = ["%s: %s: %s" % (p["file"], p["path"], p["problem"]) for p in result["problems"]]
    if result["command"] == "check":
        checked = ", ".join("%s=%s" % (f["label"], f["file"]) for f in result["files"])
        what = "%d results file(s) (%s), profile %s%s" % (
            len(result["files"]), checked, result["profile"], ", doc %s" % result["doc"] if result["doc"] else "")
    else:
        what = "template %s" % result["doc"]
    if result["ok"]:
        lines.append("OK: %s, no problems." % what)
    else:
        lines.append("FAILED: %s, %d problem(s)." % (what, len(result["problems"])))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="probe_results.py",
                                     description="Check ChatGPT probe results files and the P0 results doc.")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="validate results files (and optionally the results doc)")
    check.add_argument("--results", action="extend", nargs="+", default=[], metavar="[LABEL=]FILE",
                       help="a results JSON file, optionally labelled as a doc column")
    check.add_argument("--profile", choices=("harness", "live"), help="extra rules for a harness or live run")
    check.add_argument("--doc", help="results doc whose matrix must agree with the files")
    check.add_argument("--evidence-dir", help="live evidence directory holding screenshots/")
    check.add_argument("--json", action="store_true", help="print one JSON object")

    template = sub.add_parser("check-template", help="validate the empty results doc template")
    template.add_argument("--doc", required=True, help="the template Markdown file")
    template.add_argument("--json", action="store_true", help="print one JSON object")

    stub = sub.add_parser("stub", help="write a results file for a run that could not happen")
    stub.add_argument("--reason", required=True, help="why nothing could be exercised")
    stub.add_argument("--out", required=True, help="new results file to write (never overwritten)")
    stub.add_argument("--host-name", help="host name to record, for example ChatGPT")
    stub.add_argument("--host-version", help="host version to record")
    stub.add_argument("--json", action="store_true", help="print one JSON object")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(None if argv is None else list(argv))
    handlers = {"check": cmd_check, "check-template": cmd_check_template, "stub": cmd_stub}
    try:
        result, code = handlers[args.command](args)
    except Refusal as exc:
        payload = exc.payload()
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print("error: %s (%s)" % (exc.error, exc.error_code), file=sys.stderr)
            if payload["hint"]:
                print("hint: %s" % payload["hint"], file=sys.stderr)
        return exc.exit_code
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(human(result))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
