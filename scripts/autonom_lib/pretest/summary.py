"""Pre-test summaries (tp-014): ``summary.json`` and the paste-ready ``summary.md``.

``build_summary`` assembles ``autonom.pretest-summary/v1`` (contract section 3)
from the folded checks and findings, the health report, the kit coverage, the
ticket verdict, the timing and the pack ``layout`` (which evidence ended up
where in the pack). It writes nothing and reads no file; ``pack.build`` calls
it and writes the result. ``validate_summary`` is the hand-written validator
for ``schemas/pretest-summary-v1.schema.json``.

``layout`` keys, all optional:

- ``evidence``          — ``[{"id": "e-1", "kind", "path", "label", "check_id",
                           "width"?, "height"?, "mocked"?, "mocks_active"?}]``,
                           paths relative to the pack (``mocked: true`` marks a
                           screenshot taken while mocks were active);
- ``check_evidence``    — ``{check_id: ["e-1", ...]}``;
- ``finding_evidence``  — ``{finding_id: ["e-3", ...]}``;
- ``check_repro``       — ``{check_id: [steps]}`` (``repro.steps``);
- ``finding_repro``     — ``{finding_id: [steps]}``;
- ``before`` / ``after``— ``{check_id: {"verdict", "evidence": [...]}}`` (rerun);
- ``context``           — the pre-test's ``context.json`` (changed files);
- ``check_requests``    — ``{check_id: {"requests": [...], "truncated": N}}``
                           (``pack.check_requests``): each check's ``requests``
                           and ``requests_truncated``;
- ``parent_checks``     — the parent pack's checks (a more-checks or rerun child);
- ``actions``           — device actions counted against the budget.

``render_markdown`` turns a summary into Markdown with the same facts. It
names evidence by pack-relative path only, marks mocked screenshots
" (mocked data)", lists each check's captured requests compactly (at most
``MD_REQUESTS`` per check) and gives every not covered, blocked or open
check with its note (or why it has none). In free text (a note, a repro step
argument, a mock rule) every occurrence of a known machine root becomes
``<host-path>`` (``strip_host_paths``, contract section 13.1: ``$HOME``, the
temporary directory, ``AUTONOM_HOME``, the session, the pack, the record's
repository and flows directory, the current user's home); nothing else is
rewritten, so app routes such as ``/home`` or ``/media/42`` stay. Host-path
fields of the record (parent pack, draft flow) are shown by their last name
only.
"""
from __future__ import annotations

import getpass
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .. import errors

SUMMARY_SCHEMA = "autonom.pretest-summary/v1"
VERDICTS = ("pass", "fail", "blocked", "not_covered", "inconclusive")
CHECK_VERDICTS = VERDICTS
EVIDENCE_KINDS = ("screenshot", "tree", "action", "request", "log", "run", "crash",
                  "recording")
_EVIDENCE_ID = re.compile(r"^e-[1-9]\d*$")
_PACK_PATH = re.compile(r"^(?:blobs/sha256/[0-9a-f]{2}/[0-9a-f]{64}|runs/[A-Za-z0-9._-]+/)$")
REVIEW_PLACEHOLDER = "<pack>"
MD_REQUESTS = 30
# Global flags whose value is a path on the machine that ran the pre-test, not
# part of the step: a repro command drops the flag and its value.
HOST_ONLY_FLAGS = ("--adb",)


def drop_host_flags(args: Any) -> list[Any]:
    """``args`` of a repro step without ``--adb <path>`` / ``--adb=<path>``
    (``HOST_ONLY_FLAGS``); everything else is kept in order. Anything that is
    not a list gives an empty list."""
    if not isinstance(args, list):
        return []
    out: list[Any] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        text = arg if isinstance(arg, str) else None
        if text in HOST_ONLY_FLAGS:
            skip = True
            continue
        if text is not None and any(text.startswith(flag + "=") for flag in HOST_ONLY_FLAGS):
            continue
        out.append(arg)
    return out


def repro_steps(steps: Any) -> list[Any]:
    """Repro steps as kept in a summary: each step's ``args`` without the
    host-only flags (``drop_host_flags``); other fields unchanged."""
    out: list[Any] = []
    for step in steps if isinstance(steps, list) else []:
        if isinstance(step, dict) and "args" in step:
            step = {**step, "args": drop_host_flags(step.get("args"))}
        out.append(step)
    return out


# --- build ---------------------------------------------------------------------------


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def review_commands(findings: list[dict[str, Any]]) -> dict[str, str]:
    """The three review commands, with ``<pack>`` standing for the pack path
    (the path itself is a host path and never goes into a summary)."""
    gating = [item.get("finding_id") for item in findings
              if item.get("confirmed") is True and item.get("finding_id")]
    finding = gating[0] if gating else "<finding-id>"
    base = f"autonom pretest review {REVIEW_PLACEHOLDER}"
    return {
        "approve": f"{base} approve",
        "more_checks": f'{base} more-checks --check "<title>"',
        "return_to_dev": f'{base} return-to-dev --finding {finding} --note "<why>"',
    }


def _health_block(health: Any) -> dict[str, Any]:
    if not isinstance(health, dict):
        return {}
    return {key: value for key, value in health.items() if key != "schema"}


def build_summary(record: dict[str, Any], checks: list[dict[str, Any]],
                  findings: list[dict[str, Any]], health: dict[str, Any] | None,
                  kit_cov: list[dict[str, Any]], verdict: dict[str, Any],
                  timing: dict[str, Any], layout: dict[str, Any] | None = None
                  ) -> dict[str, Any]:
    """``autonom.pretest-summary/v1`` for one pre-test (see the module text)."""
    if not isinstance(record, dict) or not record.get("pretest_id"):
        raise errors.AutonomError(
            errors.INVALID_VALUE, "a summary needs the pre-test record",
            hint="Pass the record from 'store.load'.")
    if not isinstance(verdict, dict) or verdict.get("verdict") not in VERDICTS:
        raise errors.AutonomError(
            errors.INVALID_VALUE, "a summary needs the ticket verdict",
            hint="Pass the dict 'verdict.ticket_verdict' returned.")
    layout = _dict(layout)
    check_evidence = _dict(layout.get("check_evidence"))
    finding_evidence = _dict(layout.get("finding_evidence"))
    check_repro = _dict(layout.get("check_repro"))
    finding_repro = _dict(layout.get("finding_repro"))
    before, after = _dict(layout.get("before")), _dict(layout.get("after"))
    context = _dict(layout.get("context"))
    check_requests = _dict(layout.get("check_requests"))

    out_checks = []
    for check in _list(checks):
        cid = check.get("check_id")
        out_checks.append({
            "check_id": cid, "title": check.get("title"), "kind": check.get("kind"),
            "case": check.get("case"), "why": check.get("why"),
            "verdict": check.get("verdict"), "state": check.get("state"),
            "note": check.get("note"), "began_at": check.get("began_at"),
            "ended_at": check.get("ended_at"), "duration_s": check.get("duration_s"),
            "run_ids": list(check.get("run_ids") or []),
            "evidence": list(check_evidence.get(cid) or []),
            "repro": repro_steps(list(check_repro.get(cid) or [])),
            "before": before.get(cid), "after": after.get(cid),
            "requests": [dict(item) for item in
                         _list(_dict(check_requests.get(cid)).get("requests"))
                         if isinstance(item, dict)],
            "requests_truncated": _count(_dict(check_requests.get(cid)).get("truncated")),
        })
    out_findings = []
    for finding in _list(findings):
        fid = finding.get("finding_id")
        out_findings.append({
            "finding_id": fid, "check_id": finding.get("check_id"),
            "origin": finding.get("origin"), "source": finding.get("source"),
            "severity": finding.get("severity"), "title": finding.get("title"),
            "expected": finding.get("expected"), "actual": finding.get("actual"),
            "confirmed": finding.get("confirmed"),
            "evidence": list(finding_evidence.get(fid) or []),
            "refs": list(finding.get("evidence") or []),
            "repro": repro_steps(list(finding_repro.get(fid) or [])),
            "reproduced": finding.get("reproduced"), "stability": finding.get("stability"),
            "repro_runs": list(finding.get("repro_runs") or []),
            "draft_flow": finding.get("draft_flow"),
        })
    changed = []
    for item in _list(context.get("changed_files")):
        if isinstance(item, dict) and item.get("path"):
            row = {"path": str(item["path"]), "status": item.get("status") or "M"}
            if item.get("renamed_from"):
                row["renamed_from"] = str(item["renamed_from"])
            changed.append(row)
    budget = _dict(record.get("budget"))
    summary = {
        "schema": SUMMARY_SCHEMA,
        "pretest_id": record["pretest_id"],
        "title": record.get("title"),
        "ticket_ref": record.get("ticket_ref"),
        "verdict": verdict["verdict"],
        "verdict_reasons": list(verdict.get("reasons") or []),
        "target": dict(_dict(record.get("target"))),
        "app_id": record.get("app_id"),
        "base": record.get("base"),
        "head": record.get("head"),
        "created_at": record.get("created_at"),
        "ended_at": record.get("ended_at"),
        "parent_pack": record.get("parent_pack"),
        "kind": record.get("kind"),
        "rerun_of": record.get("rerun_of"),
        "budget": {"max_actions": budget.get("max_actions"),
                   "max_minutes": budget.get("max_minutes")},
        "changed_files": changed,
        "diff_truncated": bool(context.get("diff_truncated")),
        "checks": out_checks,
        "parent_checks": [
            {key: item.get(key) for key in ("check_id", "title", "kind", "case", "verdict",
                                            "state")}
            for item in _list(layout.get("parent_checks")) if isinstance(item, dict)],
        "findings": out_findings,
        "not_covered": list(verdict.get("not_covered") or []),
        "blocked": list(verdict.get("blocked") or []),
        "open": list(verdict.get("open") or []),
        "kit": [dict(item) for item in _list(kit_cov) if isinstance(item, dict)],
        "health": _health_block(health),
        "evidence": [dict(item) for item in _list(layout.get("evidence"))
                     if isinstance(item, dict)],
        "timing": dict(_dict(timing)),
        "review_commands": review_commands(out_findings),
    }
    validate(summary)
    return summary


def timing_block(record: dict[str, Any], checks: list[dict[str, Any]],
                 findings: list[dict[str, Any]], actions: int | None) -> dict[str, Any]:
    """``{"wall_s", "checks_s", "actions", "checks", "findings", "auto_findings"}``."""
    from .checks import parse_ts

    began, ended = parse_ts(record.get("created_at")), parse_ts(record.get("ended_at"))
    wall = round(max(0.0, (ended - began).total_seconds()), 3) if began and ended else None
    return {
        "wall_s": wall,
        "checks_s": {item["check_id"]: item["duration_s"] for item in checks
                     if isinstance(item.get("duration_s"), (int, float))},
        "actions": actions,
        "checks": len(checks),
        "findings": len(findings),
        "auto_findings": sum(1 for item in findings if item.get("origin") == "auto"),
    }


# --- validation ------------------------------------------------------------------------


def _is_str(value: Any, nullable: bool = False) -> bool:
    return (value is None and nullable) or isinstance(value, str)


def _is_num(value: Any, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# Fields a request row of a check must never carry into a pack.
_REQUEST_SECRETS = ("url", "request_headers_preview", "response_headers_preview",
                    "request_body_preview", "response_body_preview", "request", "response",
                    "headers", "body")


def summary_failures(summary: Any) -> list[str]:
    """Hand-written validator for ``schemas/pretest-summary-v1.schema.json``."""
    if not isinstance(summary, dict):
        return ["summary must be an object"]
    failures: list[str] = []
    required = ("schema", "pretest_id", "title", "ticket_ref", "verdict", "verdict_reasons",
                "target", "app_id", "base", "head", "created_at", "ended_at",
                "parent_pack", "kind", "changed_files", "diff_truncated", "checks",
                "findings", "not_covered", "blocked", "open", "kit", "health",
                "evidence", "timing", "review_commands")
    for key in required:
        if key not in summary:
            failures.append(f"missing {key}")
    if summary.get("schema") != SUMMARY_SCHEMA:
        failures.append(f"schema must be {SUMMARY_SCHEMA}")
    if summary.get("verdict") not in VERDICTS:
        failures.append("verdict must be one of " + ", ".join(VERDICTS))
    for key in ("pretest_id", "title"):
        if not isinstance(summary.get(key), str) or not summary.get(key):
            failures.append(f"{key} must be a non-empty string")
    for key in ("ticket_ref", "app_id", "base", "head", "created_at", "ended_at",
                "parent_pack"):
        if key in summary and not _is_str(summary[key], nullable=True):
            failures.append(f"{key} must be a string or null")
    if summary.get("kind") not in ("pretest", "more-checks", "rerun"):
        failures.append("kind must be pretest, more-checks or rerun")
    target = summary.get("target")
    if not isinstance(target, dict) or not isinstance(target.get("platform"), str) \
            or not isinstance(target.get("target_id"), str):
        failures.append("target must be {platform, target_id}")
    if not isinstance(summary.get("diff_truncated"), bool):
        failures.append("diff_truncated must be a boolean")
    for key in ("verdict_reasons", "not_covered", "blocked", "open"):
        value = summary.get(key)
        if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
            failures.append(f"{key} must be a list of strings")
    files = summary.get("changed_files")
    if not isinstance(files, list) or not all(
            isinstance(i, dict) and isinstance(i.get("path"), str)
            and i.get("status") in ("M", "A", "D", "R") for i in files):
        failures.append("changed_files must be a list of {path, status: M|A|D|R}")
    evidence_ids: set[str] = set()
    evidence = summary.get("evidence")
    if not isinstance(evidence, list):
        failures.append("evidence must be a list")
        evidence = []
    for index, item in enumerate(evidence):
        where = f"evidence[{index}]"
        if not isinstance(item, dict):
            failures.append(f"{where} must be an object")
            continue
        eid = item.get("id")
        if not isinstance(eid, str) or not _EVIDENCE_ID.match(eid):
            failures.append(f"{where}.id must look like e-N")
        elif eid in evidence_ids:
            failures.append(f"{where}.id {eid} is repeated")
        else:
            evidence_ids.add(eid)
        if item.get("kind") not in EVIDENCE_KINDS:
            failures.append(f"{where}.kind must be one of " + ", ".join(EVIDENCE_KINDS))
        path = item.get("path")
        if not isinstance(path, str) or not _PACK_PATH.match(path):
            failures.append(f"{where}.path must be blobs/sha256/<ab>/<digest> or runs/<id>/")
        if not _is_str(item.get("label"), nullable=True):
            failures.append(f"{where}.label must be a string or null")
        if not _is_str(item.get("check_id"), nullable=True):
            failures.append(f"{where}.check_id must be a string or null")
        for key in ("width", "height"):
            if key in item and not _is_num(item[key], nullable=True):
                failures.append(f"{where}.{key} must be a number or null")
        if "mocked" in item and not isinstance(item["mocked"], bool):
            failures.append(f"{where}.mocked must be a boolean")
        if "mocks_active" in item and not _is_count(item["mocks_active"]):
            failures.append(f"{where}.mocks_active must be an integer >= 0")
    checks = summary.get("checks")
    if not isinstance(checks, list):
        failures.append("checks must be a list")
        checks = []
    check_ids: set[str] = set()
    for index, item in enumerate(checks):
        where = f"checks[{index}]"
        if not isinstance(item, dict):
            failures.append(f"{where} must be an object")
            continue
        if not isinstance(item.get("check_id"), str):
            failures.append(f"{where}.check_id must be a string")
        else:
            check_ids.add(item["check_id"])
        if not isinstance(item.get("title"), str):
            failures.append(f"{where}.title must be a string")
        if item.get("state") not in ("added", "open", "ended"):
            failures.append(f"{where}.state must be added, open or ended")
        if item.get("verdict") is not None and item.get("verdict") not in CHECK_VERDICTS:
            failures.append(f"{where}.verdict must be a check verdict or null")
        if not _is_num(item.get("duration_s"), nullable=True):
            failures.append(f"{where}.duration_s must be a number or null")
        for key in ("run_ids", "evidence", "repro"):
            if not isinstance(item.get(key), list):
                failures.append(f"{where}.{key} must be a list")
        for ref in item.get("evidence") or []:
            if ref not in evidence_ids:
                failures.append(f"{where}.evidence names unknown {ref!r}")
        for key in ("before", "after"):
            if item.get(key) is not None and not isinstance(item.get(key), dict):
                failures.append(f"{where}.{key} must be an object or null")
        if "requests" in item:
            requests = item["requests"]
            if not isinstance(requests, list) or not all(
                    isinstance(req, dict) and isinstance(req.get("mocked"), bool)
                    and _is_str(req.get("method"), nullable=True)
                    and _is_str(req.get("host"), nullable=True)
                    and _is_str(req.get("path"), nullable=True)
                    and (req.get("status") is None or _is_count(req.get("status")))
                    and ("completed" not in req or isinstance(req.get("completed"), bool))
                    and ("error_kind" not in req
                         or _is_str(req.get("error_kind"), nullable=True))
                    and not any(key in req for key in _REQUEST_SECRETS)
                    for req in requests):
                failures.append(f"{where}.requests must be a list of {{method, host, path, "
                                "status, mocked, duration_ms?, started_at?, completed?, "
                                "error_kind?}")
        if "requests_truncated" in item and not _is_count(item["requests_truncated"]):
            failures.append(f"{where}.requests_truncated must be an integer >= 0")
    findings = summary.get("findings")
    if not isinstance(findings, list):
        failures.append("findings must be a list")
        findings = []
    for index, item in enumerate(findings):
        where = f"findings[{index}]"
        if not isinstance(item, dict):
            failures.append(f"{where} must be an object")
            continue
        if not isinstance(item.get("finding_id"), str):
            failures.append(f"{where}.finding_id must be a string")
        if item.get("check_id") is not None and item.get("check_id") not in check_ids:
            failures.append(f"{where}.check_id names an unknown check")
        if item.get("origin") not in ("agent", "auto"):
            failures.append(f"{where}.origin must be agent or auto")
        if item.get("severity") not in ("blocker", "major", "minor", "info"):
            failures.append(f"{where}.severity must be blocker, major, minor or info")
        if not isinstance(item.get("confirmed"), bool):
            failures.append(f"{where}.confirmed must be a boolean")
        for key in ("title", "actual"):
            if not isinstance(item.get(key), str):
                failures.append(f"{where}.{key} must be a string")
        if not isinstance(item.get("evidence"), list):
            failures.append(f"{where}.evidence must be a list")
        for ref in item.get("evidence") or []:
            if ref not in evidence_ids:
                failures.append(f"{where}.evidence names unknown {ref!r}")
    kit = summary.get("kit")
    if not isinstance(kit, list) or not all(
            isinstance(i, dict) and isinstance(i.get("id"), str)
            and isinstance(i.get("covered"), bool) for i in kit):
        failures.append("kit must be a list of {id, covered, ...}")
    if not isinstance(summary.get("health"), dict):
        failures.append("health must be an object")
    timing = summary.get("timing")
    if not isinstance(timing, dict):
        failures.append("timing must be an object")
    else:
        for key in ("wall_s", "actions"):
            if not _is_num(timing.get(key), nullable=True):
                failures.append(f"timing.{key} must be a number or null")
        for key in ("checks", "findings", "auto_findings"):
            if not isinstance(timing.get(key), int) or isinstance(timing.get(key), bool):
                failures.append(f"timing.{key} must be an integer")
        if not isinstance(timing.get("checks_s"), dict):
            failures.append("timing.checks_s must be an object")
    commands = summary.get("review_commands")
    if not isinstance(commands, dict) or not all(
            isinstance(commands.get(key), str)
            for key in ("approve", "more_checks", "return_to_dev")):
        failures.append("review_commands must hold approve, more_checks and return_to_dev")
    return failures


def validate(summary: Any) -> dict[str, Any]:
    failures = summary_failures(summary)
    if failures:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"summary does not satisfy {SUMMARY_SCHEMA}",
            hint="This is a bug in the pack writer or a hand-edited summary.json.",
            failures=failures)
    return summary


# --- Markdown ------------------------------------------------------------------------------

HOST_PATH = "<host-path>"
# Characters that end a path token in Markdown text.
_STOP = r"\s`'\"|()\[\]<>{},;"
# Sentence punctuation that may trail a path token and is kept in the text.
_TRAILING = ".,:;!?"
# Parents of the current user's home directory on macOS and Linux.
_USER_PARENTS = ("/Users", "/home")
_VERDICT_WORDS = {"pass": "PASS", "fail": "FAIL", "blocked": "BLOCKED",
                  "not_covered": "NOT COVERED", "inconclusive": "INCONCLUSIVE"}


def _home() -> str | None:
    try:
        return str(Path.home())
    except (RuntimeError, KeyError, OSError):
        return None


def _user() -> str | None:
    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name is no root, never a failure
        return None
    return name if name and "/" not in name and "\0" not in name else None


def _forms(value: Any) -> list[str]:
    """``value`` as written and resolved, without a trailing slash; nothing
    for a relative path, the file-system root or an unusable value."""
    if not isinstance(value, (str, os.PathLike)):
        return []
    try:
        text = os.fspath(value)
    except TypeError:
        return []
    if not isinstance(text, str) or not text.startswith("/") or "\0" in text \
            or len(text) > 4096:
        return []
    out = []
    try:
        resolved = os.path.realpath(text)
    except (OSError, ValueError):
        resolved = text
    for form in (os.path.normpath(text), resolved):
        form = form.rstrip("/")
        if form and form not in out:
            out.append(form)
    return out


def host_roots(extra: Any = ()) -> list[str]:
    """The known machine roots whose paths never go into ``summary.md``
    (contract section 13.1): ``$HOME``, ``tempfile.gettempdir()``,
    ``AUTONOM_HOME``, the current user's home under ``/Users/<user>`` and
    ``/home/<user>``, and ``extra`` (the session artifacts directory, the
    pack, the record's ``repo`` and ``flows_dir``), each as written and
    resolved (``os.path.realpath``), longest first."""
    user = _user()
    candidates: list[Any] = [os.environ.get("HOME"), _home(), tempfile.gettempdir(),
                             os.environ.get("AUTONOM_HOME")]
    if user:
        candidates += [f"{parent}/{user}" for parent in _USER_PARENTS]
    if isinstance(extra, (str, os.PathLike)):
        extra = [extra]
    candidates += list(extra or ())
    roots: set[str] = set()
    for value in candidates:
        roots.update(_forms(value))
    return sorted(roots, key=len, reverse=True)


def strip_host_paths(text: str, roots: Any = ()) -> str:
    """Replace every occurrence of a known machine root (``host_roots``) in
    ``text``, with the rest of its path token, by ``<host-path>``.

    This is substring replacement (contract sections 13.1 and 14.1): a root
    counts wherever it occurs in a line, also glued to a flag
    (``-L/Users/me/lib``) or inside a ``file://`` URL. Only a following
    path-name character (a letter, digit, ``_`` or ``-``) continues the name,
    so ``/Users/me`` does not match ``/Users/meg`` or ``/Users/me-old``; any
    other character after the root (``.``, ``:``, ``!``, ``?``, ``=``, ``#``,
    a quote, a bracket, a space) or the end of the text ends it and the root
    is replaced. A ``/`` after the root continues the path, and the rest of
    that path token is replaced with it. Nothing else is rewritten: app routes
    (``/home``, ``/media/42``), API paths, other absolute paths and ``~``
    paths stay as written. Sentence punctuation after a replaced path is kept,
    and so is whatever follows a bare root (``<root>:/bin`` keeps ``:/bin``).
    Each root is one literal followed by one possessive character class or a
    one-character lookahead, so the work is linear in the text length."""

    def keep_tail(match: re.Match[str]) -> str:
        whole = match.group(0)
        return HOST_PATH + whole[len(whole.rstrip(_TRAILING)):]

    for root in host_roots(roots):
        # the root and the rest of its path after a "/", or the bare root when
        # no path-name character follows; a root that holds a space
        # ("/Users/Ann Lee") is matched whole
        pattern = re.compile(rf"{re.escape(root)}(?:/[^{_STOP}]*+|(?![\w-]))")
        text = pattern.sub(keep_tail, text)
    return text


def _cell(value: Any) -> str:
    if value is None or value == "":
        return "—"
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text.replace("|", "\\|")


def _line(value: Any) -> str:
    if value is None:
        return "—"
    return " ".join(str(value).split())


def _duration(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    seconds = int(round(value))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _name_only(value: Any) -> str | None:
    if not value:
        return None
    parts = [part for part in re.split(r"[\\/]+", str(value)) if part]
    return parts[-1] if parts else None


def _step_text(step: Any) -> str:
    if isinstance(step, str):
        return _line(step)
    if not isinstance(step, dict):
        return "—"
    parts = [str(step.get("verb") or "step")]
    parts += [str(arg) for arg in drop_host_flags(step.get("args")) if arg is not None]
    selector = step.get("selector")
    if isinstance(selector, dict):
        parts += [f"{key}={value}" for key, value in selector.items()]
    text = " ".join(parts)
    extras = []
    if step.get("origin") == "human":
        extras.append("by hand")
    mocks = step.get("mocks_active")
    if isinstance(mocks, list) and mocks:
        shown = []
        for rule in mocks:
            if isinstance(rule, dict):
                shown.append(" ".join(f"{k}={v}" for k, v in rule.items()))
            else:
                shown.append(str(rule))
        extras.append("mocks: " + "; ".join(shown))
    if extras:
        text += f" ({', '.join(extras)})"
    return _line(text)


def _evidence_lines(ids: list[str], by_id: dict[str, dict[str, Any]]) -> list[str]:
    out = []
    for eid in ids:
        item = by_id.get(eid)
        if not item:
            continue
        label = item.get("label") or item.get("kind")
        mocked = " (mocked data)" if item.get("mocked") is True else ""
        out.append(f"  - {item.get('kind')}: `{item.get('path')}` — {_line(label)}{mocked}")
    return out


def incomplete_text(item: dict[str, Any]) -> str | None:
    """``never completed (hang_cap)`` for a request whose row says
    ``completed: false``; None for any other request."""
    if item.get("completed") is not False:
        return None
    kind = item.get("error_kind")
    kind = kind.strip() if isinstance(kind, str) else ""
    return f"never completed ({kind})" if kind else "never completed"


def request_text(item: dict[str, Any]) -> str:
    """One captured request in one line: ``GET host/path → 200 (mocked, 120 ms)``;
    a request that never completed reads ``→ never completed (hang_cap)``."""
    text = f"{item.get('method') or '?'} {item.get('host') or ''}{item.get('path') or ''}"
    status = item.get("status")
    incomplete = incomplete_text(item)
    if incomplete:
        text += f" → {incomplete}"
    else:
        text += f" → {status}" if status is not None else " → no status"
    extras = []
    if item.get("mocked"):
        extras.append("mocked")
    duration = item.get("duration_ms")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        extras.append(f"{int(duration)} ms")
    return _line(text + (f" ({', '.join(extras)})" if extras else ""))


def _request_lines(check: dict[str, Any]) -> list[str]:
    requests = [item for item in check.get("requests") or [] if isinstance(item, dict)]
    more = _count(check.get("requests_truncated"))
    if not requests and not more:
        return []
    out = [f"- Requests ({len(requests) + more}):"]
    out += [f"  - {request_text(item)}" for item in requests[:MD_REQUESTS]]
    hidden = max(0, len(requests) - MD_REQUESTS) + more
    if hidden:
        where = (f" (see `checks/{check['check_id']}.json`)"
                 if len(requests) > MD_REQUESTS else "")
        out.append(f"  - … and {hidden} more{where}")
    return out


def _why_listed(check: dict[str, Any] | None) -> str:
    """The note of a not covered, blocked or open check, or why it has none."""
    if not check:
        return "not in this pack"
    if check.get("note"):
        return _line(check["note"])
    if check.get("state") == "added":
        return "never ran"
    if check.get("state") == "open":
        return "still open"
    return "no note"


def render_markdown(summary: dict[str, Any], *, roots: Any = ()) -> str:
    """Paste-ready Markdown with the facts of ``summary`` (see the module text).

    ``roots`` are extra machine directories (the session, the pack, the
    repository, the flows directory) replaced like the home and temporary
    roots."""
    validate(summary)
    verdict = summary["verdict"]
    by_id = {item["id"]: item for item in summary["evidence"]}
    checks, findings = summary["checks"], summary["findings"]
    autos = [item for item in findings if item.get("origin") == "auto"]
    lines = [f"# Pre-test {_VERDICT_WORDS.get(verdict, verdict)}: {_line(summary['title'])}", ""]
    lines.append(f"- Verdict: **{verdict}**")
    for reason in summary["verdict_reasons"]:
        lines.append(f"  - {_line(reason)}")
    lines.append(f"- Ticket: {_line(summary.get('ticket_ref'))}")
    lines.append(f"- Pre-test: {summary['pretest_id']} ({summary.get('kind')})")
    target = summary.get("target") or {}
    lines.append(f"- Device: {target.get('platform')} {_line(target.get('target_id'))}"
                 f" · app {_line(summary.get('app_id'))}")
    lines.append(f"- Diff: {_line(summary.get('base'))} → {_line(summary.get('head') or 'working tree')}"
                 f" · changed files: {len(summary['changed_files'])}"
                 + (" (diff truncated)" if summary.get("diff_truncated") else ""))
    parent = _name_only(summary.get("parent_pack"))
    if parent:
        lines.append(f"- Parent pack: {parent}")
    lines.append("")

    lines += [f"## Checks ({len(checks)})", ""]
    if checks:
        lines += ["| Check | Title | Kind | Case | Verdict | Time |",
                  "|---|---|---|---|---|---|"]
        for check in checks:
            shown = check.get("verdict") or check.get("state")
            lines.append(f"| {_cell(check['check_id'])} | {_cell(check.get('title'))} | "
                         f"{_cell(check.get('kind'))} | {_cell(check.get('case'))} | "
                         f"{_cell(shown)} | {_duration(check.get('duration_s'))} |")
        lines.append("")
        for check in checks:
            details = []
            if check.get("note"):
                details.append(f"- Note: {_line(check['note'])}")
            if check.get("before") or check.get("after"):
                before = (check.get("before") or {}).get("verdict")
                after = (check.get("after") or {}).get("verdict")
                details.append(f"- Before: {_line(before)} · after: {_line(after)}")
            if check.get("repro"):
                details.append("- Steps:")
                details += [f"  {n}. {_step_text(step)}"
                            for n, step in enumerate(check["repro"], start=1)]
            details += _request_lines(check)
            evidence = _evidence_lines(check.get("evidence") or [], by_id)
            if evidence:
                details.append("- Evidence:")
                details += evidence
            if details:
                lines += [f"### {check['check_id']} — {_line(check.get('title'))}", ""]
                lines += details + [""]
    else:
        lines += ["No checks were added.", ""]
    if summary.get("parent_checks"):
        lines += ["Parent pack checks:", ""]
        for item in summary["parent_checks"]:
            lines.append(f"- {item.get('check_id')} — {_line(item.get('title'))}: "
                         f"{_line(item.get('verdict') or item.get('state'))}")
        lines.append("")

    lines += [f"## Findings ({len(findings)})", "",
              f"{len(findings) - len(autos)} by the agent, {len(autos)} automatic "
              "(automatic findings never change the verdict until a reviewer confirms one).",
              ""]
    for finding in findings:
        status = "confirmed" if finding.get("confirmed") else "unconfirmed"
        lines.append(f"### {finding['finding_id']} · {finding.get('severity')} · "
                     f"{_line(finding.get('title'))}")
        lines.append("")
        lines.append(f"- Check: {_line(finding.get('check_id'))} · origin: "
                     f"{finding.get('origin')}"
                     + (f" ({finding.get('source')})" if finding.get("source") else "")
                     + f" · {status}")
        lines.append(f"- Expected: {_line(finding.get('expected'))}")
        lines.append(f"- Actual: {_line(finding.get('actual'))}")
        if finding.get("reproduced"):
            lines.append(f"- Reproduced: {finding['reproduced']}"
                         + (f" ({finding.get('stability')})" if finding.get("stability") else ""))
        draft = _name_only(finding.get("draft_flow"))
        if draft:
            lines.append(f"- Draft flow: {draft}")
        if finding.get("repro"):
            lines.append("- Steps to reproduce:")
            lines += [f"  {n}. {_step_text(step)}"
                      for n, step in enumerate(finding["repro"], start=1)]
        evidence = _evidence_lines(finding.get("evidence") or [], by_id)
        if evidence:
            lines.append("- Evidence:")
            lines += evidence
        lines.append("")

    by_check = {check["check_id"]: check for check in checks}
    for key, heading in (("not_covered", "Not covered"), ("blocked", "Blocked"),
                         ("open", "Open at pack time")):
        ids = summary[key]
        lines.append(f"## {heading}: {', '.join(ids) if ids else 'none'}")
        lines.append("")
        for cid in ids:
            check = by_check.get(cid)
            lines.append(f"- {cid} — {_line((check or {}).get('title'))}: "
                         f"{_why_listed(check)}")
        if ids:
            lines.append("")

    lines += ["## Device health", ""]
    health = summary.get("health") or {}
    for name, key, word in (("crashes", "new", "new crash(es)"),
                            ("logs", "groups", "error log group(s)"),
                            ("requests", "failed", "failed request(s)"),
                            ("metrics", "leads", "metric lead(s)")):
        block = health.get(name) if isinstance(health.get(name), dict) else {}
        if block.get("state") != "available":
            lines.append(f"- {name.capitalize()}: unavailable"
                         + (f" ({_line(block.get('reason'))})" if block.get("reason") else ""))
            continue
        items = [item for item in block.get(key) or [] if isinstance(item, dict)]
        lines.append(f"- {name.capitalize()}: {len(items)} {word}")
        for item in items[:20]:
            if name == "crashes":
                text = f"{item.get('name')} at {item.get('time')}"
            elif name == "logs":
                text = f"{item.get('count')}× {item.get('pattern')}"
            elif name == "requests":
                text = (f"{item.get('method')} {item.get('host')}{item.get('path')} "
                        f"{item.get('status')} ({item.get('reason')})")
            else:
                text = (f"{item.get('metric')}: {item.get('before')} → {item.get('after')} "
                        f"(lead, delta {item.get('delta')})")
            owner = item.get("check_id")
            lines.append(f"  - {_line(text)}" + (f" [{owner}]" if owner else ""))
    lines.append("")

    kit = summary.get("kit") or []
    covered = [item for item in kit if item.get("covered")]
    skipped = [item for item in kit if not item.get("covered")]
    lines += ["## Edge-case kit", ""]
    if covered:
        lines.append("- Covered: " + ", ".join(
            f"{item['id']} ({'/'.join(str(v) for v in item.get('verdicts') or [] if v) or 'open'})"
            for item in covered))
    else:
        lines.append("- Covered: none")
    lines.append(f"- Skipped ({len(skipped)}): "
                 + (", ".join(item["id"] for item in skipped) or "none"))
    lines.append("")

    timing = summary.get("timing") or {}
    lines += ["## Timing", ""]
    lines.append(f"- Wall time: {_duration(timing.get('wall_s'))}")
    per_check = timing.get("checks_s") or {}
    if per_check:
        lines.append("- Per check: " + ", ".join(
            f"{cid} {_duration(value)}" for cid, value in per_check.items()))
    actions = timing.get("actions")
    lines.append(f"- Actions: {actions if actions is not None else '—'} · checks: "
                 f"{timing.get('checks')} · findings: {timing.get('findings')} "
                 f"({timing.get('auto_findings')} automatic)")
    lines.append("")

    commands = summary["review_commands"]
    lines += ["## Review", "",
              f"- Approve: `{commands['approve']}`",
              f"- More checks: `{commands['more_checks']}`",
              f"- Return to dev: `{commands['return_to_dev']}`", "",
              "Evidence paths are relative to the pack directory; open `index.html` "
              "for the screenshots. Verify with `autonom report verify <pack>`.", ""]
    return strip_host_paths("\n".join(lines), roots)
