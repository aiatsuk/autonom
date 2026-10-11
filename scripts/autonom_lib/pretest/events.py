"""Analytics event checks against captured traffic (tp-026).

``run`` matches the app profile's ``events`` rules (``profile.py``) against the
requests the proxy captured (``network/flows.jsonl``) inside one window: the
``--check``'s ``began_at``..``ended_at`` (now for the open check), else the
open check's, else the whole pre-test's (``created_at``..now). The window's
start is taken to the whole second, because captured rows may carry
second-precision times.

A request matches a rule when every field the rule sets matches:

- ``method`` — the same HTTP method, any letter case;
- ``host`` — the same host, or the host matches the rule's glob
  (``*.example.com``), any letter case;
- ``path`` — the request path without its query matches the rule's glob
  (``fnmatch``: ``*`` crosses ``/``);
- ``body_key`` — the request body has that key: a top-level JSON key, a
  dotted path into nested JSON objects (a list on the way matches when any
  element does), or a form field (``a=1&b=2``). The body is the full
  captured body when the proxy kept one (``--capture-bodies``), else the
  captured preview;
- ``body_value`` — some value at ``body_key`` equals it as text (JSON
  ``true`` is ``"true"``).

The report lists ``matched`` rules with their request ids, ``missing``
rules, and ``unexpected`` requests: requests to an endpoint a rule names
(method, host and path all match) that match no rule in full, for example an
event name no rule expects. Each missing rule becomes an automatic finding
(``origin: "auto"``, ``source: "analytics"``, ``severity: "minor"``,
``confirmed: false``) attributed to the window's check, written once per
check and rule (``data.key``).

Bodies are read only to test keys and values: nothing of a body is ever
copied into the report, a finding or the journal except key NAMES (for
unexpected requests, at most ``MAX_KEYS`` top-level keys). Reported paths
drop their query string. Without any captured traffic (no
``network/flows.jsonl``) the report is ``state: "unavailable"`` and writes no
finding: "no request" cannot be told from "capture was off".
"""
from __future__ import annotations

import fnmatch
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl

from .. import errors
from ..network import store as network_store
from . import checks as checks_mod
from . import findings as findings_mod
from . import profile as profile_mod
from . import store

SOURCE = "analytics"
SEVERITY = "minor"
MAX_KEYS = 20
KEY_LIMIT = 64
MAX_IDS = 50
MAX_UNEXPECTED = 100
BODY_LIMIT = 1024 * 1024


# --- window -----------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def window(session: dict[str, Any], record: dict[str, Any],
           check_id: str | None) -> dict[str, Any]:
    """``{"check_id", "from", "to"}`` (datetimes) for the check, the open
    check, or the whole pre-test. ``unknown_check`` for an id the pre-test
    does not hold; ``invalid_value`` for a check that never began."""
    states = checks_mod.fold(session, record)
    chosen: dict[str, Any] | None = None
    if check_id is not None:
        chosen = next((item for item in states if item["check_id"] == check_id), None)
        if chosen is None:
            raise errors.AutonomError(
                errors.UNKNOWN_CHECK, f"no check {check_id!r} in this pre-test",
                hint="List checks with 'autonom pretest check list'.", check_id=check_id)
        if not chosen.get("began_at"):
            raise errors.AutonomError(
                errors.INVALID_VALUE, f"check {check_id} never began, so it has no window",
                hint=f"Begin it with 'autonom pretest check begin {check_id}', make the app "
                     "send its events, then run 'autonom pretest events' again.",
                check_id=check_id)
    else:
        chosen = next((item for item in states if item["state"] == "open"), None)
    now = _now()
    if chosen is not None:
        began = checks_mod.parse_ts(chosen.get("began_at"))
        ended = checks_mod.parse_ts(chosen.get("ended_at")) or now
        return {"check_id": chosen["check_id"], "from": began, "to": ended}
    began = checks_mod.parse_ts(record.get("created_at"))
    ended = checks_mod.parse_ts(record.get("ended_at")) or now
    return {"check_id": None, "from": began, "to": ended}


def _row_time(row: dict[str, Any]) -> datetime | None:
    millis = row.get("started_at_ms")
    if isinstance(millis, (int, float)) and not isinstance(millis, bool):
        try:
            return datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return checks_mod.parse_ts(row.get("started_at"))


def in_window(row: dict[str, Any], span: dict[str, Any]) -> bool:
    stamp = _row_time(row)
    began, ended = span["from"], span["to"]
    if stamp is None or began is None:
        return False
    return began.replace(microsecond=0) <= stamp <= ended


# --- bodies -----------------------------------------------------------------------------


def _body_text(session: dict[str, Any], row: dict[str, Any]) -> str:
    """The full captured request body when kept, else the preview. Never
    returned to a caller outside this module's matching."""
    flow_id = row.get("id")
    if isinstance(flow_id, str) and flow_id and "/" not in flow_id and ".." not in flow_id:
        try:
            raw = network_store.body(session, flow_id, "request")
        except OSError:
            raw = None
        if raw is not None:
            return raw[:BODY_LIMIT].decode("utf-8", "replace")
    preview = row.get("request_body_preview")
    return preview if isinstance(preview, str) else ""


def parse_body(text: str) -> Any:
    """JSON (object or list), else form fields as a dict, else None."""
    text = (text or "").strip()
    if not text:
        return None
    if text[0] in "{[":
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
    if "=" in text and "\n" not in text:
        try:
            pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=False)
        except ValueError:
            return None
        if pairs:
            out: dict[str, Any] = {}
            for key, value in pairs:
                out.setdefault(key, value)
            return out
    return None


def _values_at(body: Any, dotted: str) -> list[Any] | None:
    """Every value at the dotted key (None when the key is absent everywhere)."""
    current: list[Any] = [body]
    for part in dotted.split("."):
        following: list[Any] = []
        for item in current:
            pending = [item]
            while pending:
                node = pending.pop()
                if isinstance(node, list):
                    pending.extend(node)
                elif isinstance(node, dict) and part in node:
                    following.append(node[part])
        if not following:
            return None
        current = following
    flat: list[Any] = []
    for item in current:
        if isinstance(item, list):
            flat.extend(item)
        else:
            flat.append(item)
    return flat


def _as_text(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float, str)):
        return str(value)
    return None


def body_keys(body: Any) -> list[str]:
    """Top-level key NAMES only (of the object, or of each object in a list)."""
    names: list[str] = []
    items = body if isinstance(body, list) else [body]
    for item in items:
        if isinstance(item, dict):
            for key in item:
                name = store.clip(str(key), KEY_LIMIT) or ""
                if name and name not in names:
                    names.append(name)
                if len(names) >= MAX_KEYS:
                    return names
    return names


# --- matching ---------------------------------------------------------------------------


def _path(row: dict[str, Any]) -> str:
    return str(row.get("path") or "").split("?", 1)[0].split("#", 1)[0]


def endpoint_matches(rule: dict[str, Any], row: dict[str, Any]) -> bool:
    if rule.get("method") and str(row.get("method") or "").upper() != rule["method"]:
        return False
    if rule.get("host"):
        host = str(row.get("host") or "").lower()
        if host != rule["host"] and not fnmatch.fnmatchcase(host, rule["host"]):
            return False
    if rule.get("path") and not fnmatch.fnmatchcase(_path(row), rule["path"]):
        return False
    return True


def body_matches(rule: dict[str, Any], body: Any) -> bool:
    key = rule.get("body_key")
    if not key:
        return True
    if body is None:
        return False
    values = _values_at(body, key)
    if values is None:
        return False
    expected = rule.get("body_value")
    if expected is None:
        return True
    return any(_as_text(value) == expected for value in values)


def match(session: dict[str, Any], rules: list[dict[str, Any]],
          rows: list[dict[str, Any]]) -> dict[str, Any]:
    """``{"matched", "missing", "unexpected"}`` for rows already in the window."""
    hits: dict[str, list[str]] = {rule["name"]: [] for rule in rules}
    unexpected: list[dict[str, Any]] = []
    for row in rows:
        endpoint = [rule for rule in rules if endpoint_matches(rule, row)]
        if not endpoint:
            continue
        needs_body = any(rule.get("body_key") for rule in endpoint)
        body = parse_body(_body_text(session, row)) if needs_body else None
        full = [rule for rule in endpoint if body_matches(rule, body)]
        flow_id = str(row.get("id")) if row.get("id") is not None else None
        for rule in full:
            if flow_id is not None:
                hits[rule["name"]].append(flow_id)
        if not full and len(unexpected) < MAX_UNEXPECTED:
            unexpected.append({
                "request_id": flow_id,
                "method": store.clip(row.get("method"), 16),
                "host": store.clip(row.get("host"), 255),
                "path": store.clip(_path(row), 500),
                "body_keys": body_keys(body),
                "endpoint_of": [rule["name"] for rule in endpoint]})
    matched = [{"name": name, "count": len(ids), "request_ids": ids[:MAX_IDS]}
               for name, ids in hits.items() if ids]
    missing = [{key: rule.get(key) for key in ("name", "method", "host", "path", "body_key")}
               for rule in rules if not hits[rule["name"]]]
    return {"matched": matched, "missing": missing, "unexpected": unexpected}


# --- findings ---------------------------------------------------------------------------


def _describe(rule: dict[str, Any]) -> str:
    where = " ".join(part for part in (rule.get("method"), (rule.get("host") or "")
                                       + (rule.get("path") or "")) if part)
    if rule.get("body_key"):
        where += f" with body key {rule['body_key']}"
        if rule.get("body_value") is not None:
            where += " set to the rule's value"
    return where


def _missing_finding(rule: dict[str, Any], span: dict[str, Any],
                     in_window: int) -> dict[str, Any]:
    scope = f"check {span['check_id']}" if span["check_id"] else "the pre-test"
    return {
        "title": f"Expected analytics event missing: {rule['name']}",
        "expected": f"A request matching rule {rule['name']} ({_describe(rule)}) during {scope}.",
        "actual": f"No captured request during {scope} matched it ({in_window} request(s) "
                  "were captured in that window).",
    }


def _rules(profile: dict[str, Any] | None, record: dict[str, Any],
           app_id: str | None) -> list[dict[str, Any]]:
    rules = list((profile or {}).get("events") or [])
    if rules:
        return rules
    searched = [str(path) for _source, path in
                profile_mod.candidates(record.get("repo"), app_id)]
    raise errors.AutonomError(
        errors.INVALID_VALUE,
        "the app profile names no analytics events" if profile else "this app has no profile",
        hint="Add an 'events:' list (name, method, host, path, body_key, body_value) to "
             "the app's profile.yaml: " + (" or ".join(searched) or
                                           ".autonom/apps/<app-id>/profile.yaml") + ".",
        profile=(profile or {}).get("path"), searched=searched)


def run(session: dict[str, Any], record: dict[str, Any], *,
        check_id: str | None = None) -> dict[str, Any]:
    """Match the profile's rules in the window and write findings for the
    missing ones (see the module text)."""
    store.ensure_mutable(record)
    app_id = record.get("app_id")
    profile = profile_mod.load(record.get("repo"), app_id)
    rules = _rules(profile, record, app_id)
    span = window(session, record, check_id)
    report: dict[str, Any] = {
        "check_id": span["check_id"],
        "window": {"from": _iso(span["from"]) if span["from"] else None,
                   "to": _iso(span["to"]) if span["to"] else None},
        "profile": profile["path"] if profile else None,
        "rules": len(rules),
    }
    path = network_store.flows_path(session)
    if not path.is_file():
        report.update({
            "state": "unavailable", "reason": "no captured traffic in this session",
            "requests_in_window": 0, "matched": [], "missing": [], "unexpected": [],
            "findings": [],
            "warnings": [{"code": "network_not_captured",
                          "error": "no request was captured, so events cannot be checked",
                          "hint": "Start capture with 'autonom network start' and attach the "
                                  "device (consent required), repeat the check, then run "
                                  "'autonom pretest events' again."}]})
        return report
    try:
        rows, warnings = network_store.read_all(session)
    except OSError as exc:
        report.update({
            "state": "unavailable", "reason": f"captured traffic is unreadable "
                                              f"({exc.strerror or type(exc).__name__})",
            "requests_in_window": 0, "matched": [], "missing": [], "unexpected": [],
            "findings": [],
            "warnings": [{"code": "network_unreadable", "error": "network/flows.jsonl could "
                          "not be read", "hint": "Check the session's network folder "
                          "permissions, then run 'autonom pretest events' again."}]})
        return report
    rows = [row for row in rows if isinstance(row, dict) and in_window(row, span)]
    result = match(session, rules, rows)
    written: list[dict[str, Any]] = []
    by_name = {rule["name"]: rule for rule in rules}
    with store.lock(session, record["pretest_id"]):
        current = store.load(session, record["pretest_id"])
        store.ensure_mutable(current)
        seen = {str((item.get("data") or {}).get("key"))
                for item in findings_mod.fold(session, current)
                if (item.get("data") or {}).get("key")}
        for missing in result["missing"]:
            rule = by_name[missing["name"]]
            key = f"analytics:{span['check_id'] or '-'}:{rule['name']}"
            if key in seen:
                continue
            text = _missing_finding(rule, span, len(rows))
            data = {"key": key, "rule": rule["name"], "method": rule.get("method"),
                    "host": rule.get("host"), "path": rule.get("path"),
                    "body_key": rule.get("body_key"),
                    "window_from": report["window"]["from"],
                    "window_to": report["window"]["to"], "requests_in_window": len(rows)}
            written.append(findings_mod.add(
                session, current, check_id=span["check_id"], severity=SEVERITY,
                title=text["title"], actual=text["actual"], expected=text["expected"],
                evidence=[], origin="auto", source=SOURCE, confirmed=False, data=data))
            seen.add(key)
    report.update({"state": "available", "requests_in_window": len(rows), **result,
                   "findings": written})
    if warnings:
        report["warnings"] = warnings
    return report
