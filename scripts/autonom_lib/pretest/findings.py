"""Findings (tp-009): what went wrong, with evidence that exists.

``findings.ndjson`` is append-only, written under the pre-test lock::

    {"event": "add", "finding_id": "f-1", "check_id", "origin", "source",
     "severity", "title", "expected", "actual", "evidence": [refs],
     "confirmed", "data", "ts", "journal_seq"}
    {"event": "update", "finding_id": "f-1", "fields": {...}, "ts", "journal_seq"}

Evidence refs are checked against the session when the finding is written,
so a finding can never point at a screenshot that was not taken:

    shot:<row number in shots/index.jsonl (at most 9 digits), or the file name>
    tree:<file under trees/>        action:<file stem under actions/>
    request:<flow id in network/flows.jsonl>
    log:<from-ISO>..<to-ISO>        run:<run id under flows/>
    crash:<name>                    (a file under crashes/, or a crash health found)
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .. import contracts, errors
from .. import journal as journal_mod
from . import checks as checks_mod
from . import store

SEVERITIES = ("blocker", "major", "minor", "info")
ORIGINS = ("agent", "auto")
SOURCES = ("crash", "log", "request", "metrics", "a11y", "analytics")
STABILITIES = ("stable", "intermittent", "not_reproduced")
_SHOT_NUMBER = re.compile(r"[0-9]+")
# A row number has at most this many digits; a longer one names no row (and
# int() of thousands of digits raises ValueError).
SHOT_NUMBER_DIGITS = 9
REF_KINDS = ("shot", "tree", "action", "request", "log", "run", "crash")
UPDATE_FIELDS = ("reproduced", "stability", "repro_runs", "draft_flow")
FINDING_ID = re.compile(r"^f-[1-9]\d*$")
_REPRODUCED = re.compile(r"^\d+/[1-9]\d*$")
TITLE_LIMIT = 200
TEXT_LIMIT = 4000
MAX_EVIDENCE = 50


def _path(session: dict[str, Any], record: dict[str, Any]) -> Path:
    return store.pretest_dir(session, record["pretest_id"]) / store.FINDINGS_FILE


def _stamp(session: dict[str, Any]) -> dict[str, Any]:
    return {"ts": store.now(), "journal_seq": journal_mod.last_seq(session)}


# --- evidence refs ----------------------------------------------------------------


def _unknown(ref: Any, why: str) -> errors.AutonomError:
    return errors.AutonomError(
        errors.EVIDENCE_REF_UNKNOWN, f"evidence {ref!r} does not resolve: {why}",
        hint="Use refs to artifacts of this session: shot:<n|file>, tree:<file>, "
             "action:<stem>, request:<id>, log:<from>..<to>, run:<run_id>, crash:<name>. "
             "'autonom shots list' and 'autonom network requests list' show the ids.",
        ref=ref)


def _inside(base: Path, value: str) -> Path | None:
    """``base/value`` when it stays inside ``base``; None for a traversal."""
    if not value or "\0" in value or Path(value).is_absolute():
        return None
    candidate = (base / value).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        return None
    return candidate


def _artifacts(session: dict[str, Any]) -> Path:
    return Path(session["artifacts_dir"])


def _resolve_shot(session: dict[str, Any], ref: str, value: str) -> dict[str, Any]:
    from .. import screenshot as screenshot_mod

    rows = screenshot_mod.load_index(session)
    chosen: tuple[int, dict[str, Any]] | None = None
    if _SHOT_NUMBER.fullmatch(value) and len(value) > SHOT_NUMBER_DIGITS:
        raise _unknown(ref, "no such screenshot in shots/index.jsonl")
    if _SHOT_NUMBER.fullmatch(value):
        # ASCII digits only: str.isdigit() also takes "²" or "１", which
        # int() refuses or reads as another row
        number = int(value)
        if 1 <= number <= len(rows):
            chosen = (number, rows[number - 1])
    else:
        for number, row in enumerate(rows, start=1):
            file = str(row.get("file") or "")
            if value in (file, Path(file).name, Path(file).stem):
                chosen = (number, row)
    if chosen is None:
        raise _unknown(ref, "no such screenshot in shots/index.jsonl")
    number, row = chosen
    path = _inside(_artifacts(session), str(row.get("file") or ""))
    if path is None or not path.is_file():
        raise _unknown(ref, "the screenshot file is missing")
    data = {key: row.get(key) for key in ("file", "label", "task", "captured_at",
                                          "mocks_active", "width", "height",
                                          "pretest_id", "check_id") if key in row}
    data["index"] = number
    return {"ref": ref, "kind": "shot", "path": str(path), "data": data}


def _resolve_file(session: dict[str, Any], ref: str, kind: str, folder: str,
                  value: str, suffixes: Iterable[str]) -> dict[str, Any]:
    base = _artifacts(session) / folder
    for suffix in ("", *suffixes):
        path = _inside(base, value + suffix)
        if path is not None and path.is_file():
            return {"ref": ref, "kind": kind, "path": str(path),
                    "data": {"file": str(path.relative_to(_artifacts(session).resolve()))}}
    raise _unknown(ref, f"no such file under {folder}/")


def _resolve_request(session: dict[str, Any], ref: str, value: str) -> dict[str, Any]:
    from ..network import store as network_store

    path = network_store.flows_path(session)
    for row in store.read_lines(path):
        ident = row.get("id")
        # A flow id is text (or a number); anything else is no id, and str()
        # of a deeply nested one would raise RecursionError.
        if isinstance(ident, (str, int)) and not isinstance(ident, bool) \
                and str(ident) == value:
            request = row.get("request") if isinstance(row.get("request"), dict) else {}
            response = row.get("response") if isinstance(row.get("response"), dict) else {}
            data = {"id": value,
                    "method": row.get("method") or request.get("method"),
                    "host": row.get("host") or request.get("host"),
                    "path": row.get("path") or request.get("path"),
                    "status": row.get("status") if "status" in row
                    else response.get("status_code")}
            return {"ref": ref, "kind": "request", "path": str(path), "data": data}
    raise _unknown(ref, "no such flow id in network/flows.jsonl")


def _resolve_log(session: dict[str, Any], ref: str, value: str) -> dict[str, Any]:
    start, sep, finish = value.partition("..")
    began, ended = checks_mod.parse_ts(start), checks_mod.parse_ts(finish)
    if not sep or began is None or ended is None:
        raise _unknown(ref, "a log window is <from-ISO>..<to-ISO>")
    if ended < began:
        raise _unknown(ref, "the log window ends before it starts")
    out: dict[str, Any] = {"ref": ref, "kind": "log",
                           "data": {"from": start, "to": finish}}
    stream = _artifacts(session) / "logs" / "stream.ndjson"
    if stream.is_file():
        out["path"] = str(stream)
    return out


def _resolve_run(session: dict[str, Any], ref: str, value: str) -> dict[str, Any]:
    if "/" in value or "\\" in value:
        raise _unknown(ref, "a run id is a single name")
    path = _inside(_artifacts(session) / "flows", value)
    if path is None or not path.is_dir():
        raise _unknown(ref, "no such flow run under flows/")
    return {"ref": ref, "kind": "run", "path": str(path), "data": {"run_id": value}}


def _resolve_crash(session: dict[str, Any], ref: str, value: str) -> dict[str, Any]:
    try:
        return _resolve_file(session, ref, "crash", "crashes", value, (".txt", ".ips"))
    except errors.AutonomError:
        pass
    root = store.pretests_root(session)
    if root.is_dir() and "/" not in value:
        for health in sorted(root.glob("pt_*/health.json")):
            try:
                doc = json.loads(health.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for crash in ((doc.get("crashes") or {}).get("new") or []):
                if isinstance(crash, dict) and crash.get("name") == value:
                    return {"ref": ref, "kind": "crash", "path": str(health),
                            "data": {"name": value, "time": crash.get("time")}}
    raise _unknown(ref, "no such crash under crashes/ or in a health report")


def resolve_ref(session: dict[str, Any], ref: str) -> dict[str, Any]:
    """``{"ref", "kind", "path"?, "data"}`` or ``evidence_ref_unknown``."""
    if not isinstance(ref, str) or ":" not in ref:
        raise _unknown(ref, "a ref is <kind>:<id>")
    kind, _, value = ref.partition(":")
    value = value.strip()
    if kind not in REF_KINDS:
        raise _unknown(ref, f"unknown kind {kind!r}")
    if not value:
        raise _unknown(ref, "the id is empty")
    if kind == "shot":
        return _resolve_shot(session, ref, value)
    if kind == "tree":
        return _resolve_file(session, ref, "tree", "trees", value, (".json",))
    if kind == "action":
        return _resolve_file(session, ref, "action", "actions", value, (".json",))
    if kind == "request":
        return _resolve_request(session, ref, value)
    if kind == "log":
        return _resolve_log(session, ref, value)
    if kind == "run":
        return _resolve_run(session, ref, value)
    return _resolve_crash(session, ref, value)


# --- validation -------------------------------------------------------------------


def event_failures(event: Any) -> list[str]:
    """Hand-written validator for ``schemas/pretest-finding-v1.schema.json``."""
    if not isinstance(event, dict):
        return ["event must be an object"]
    failures: list[str] = []
    kind = event.get("event")
    if kind not in ("add", "update"):
        failures.append("event must be add or update")
    if not isinstance(event.get("finding_id"), str) or not FINDING_ID.match(
            event.get("finding_id") or ""):
        failures.append("finding_id must look like f-N")
    if not isinstance(event.get("ts"), str) or checks_mod.parse_ts(event.get("ts")) is None:
        failures.append("ts must be an ISO timestamp")
    seq = event.get("journal_seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        failures.append("journal_seq must be an integer >= 0")
    if kind == "add":
        check_id = event.get("check_id")
        if check_id is not None and (not isinstance(check_id, str)
                                     or not store.CHECK_ID.match(check_id)):
            failures.append("check_id must be null or look like chk-N")
        if event.get("origin") not in ORIGINS:
            failures.append("origin must be agent or auto")
        if event.get("source") is not None and event.get("source") not in SOURCES:
            failures.append("source must be null or one of " + ", ".join(SOURCES))
        if event.get("severity") not in SEVERITIES:
            failures.append("severity must be one of " + ", ".join(SEVERITIES))
        for key in ("title", "actual"):
            if not isinstance(event.get(key), str) or not event.get(key):
                failures.append(f"{key} must be a non-empty string")
        if "expected" not in event or (event["expected"] is not None
                                       and not isinstance(event["expected"], str)):
            failures.append("expected must be a string or null")
        evidence = event.get("evidence")
        if not isinstance(evidence, list) or not all(isinstance(i, str) for i in evidence):
            failures.append("evidence must be a list of refs")
        if not isinstance(event.get("confirmed"), bool):
            failures.append("confirmed must be a boolean")
        if not isinstance(event.get("data"), dict):
            failures.append("data must be an object")
    if kind == "update":
        fields = event.get("fields")
        if not isinstance(fields, dict) or not fields:
            failures.append("fields must be a non-empty object")
        else:
            failures.extend(_field_failures(fields))
    return failures


def _field_failures(fields: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for key, value in fields.items():
        if key not in UPDATE_FIELDS:
            failures.append(f"{key} cannot be updated (only {', '.join(UPDATE_FIELDS)})")
        elif key == "reproduced" and (not isinstance(value, str)
                                      or not _REPRODUCED.match(value)
                                      or int(value.split("/")[0]) > int(value.split("/")[1])):
            failures.append("reproduced must look like k/N with k <= N")
        elif key == "stability" and value not in STABILITIES:
            failures.append("stability must be one of " + ", ".join(STABILITIES))
        elif key == "repro_runs" and (not isinstance(value, list)
                                      or not all(isinstance(i, str) for i in value)):
            failures.append("repro_runs must be a list of run ids")
        elif key == "draft_flow" and not (value is None or isinstance(value, str)):
            failures.append("draft_flow must be a path or null")
    return failures


def validate_event(event: Any) -> dict[str, Any]:
    failures = event_failures(event)
    if failures:
        raise errors.AutonomError(
            errors.INVALID_VALUE, "finding event does not satisfy the pre-test finding schema",
            hint="This is a bug in the writer.", failures=failures)
    return event


# --- fold -------------------------------------------------------------------------


def events(session: dict[str, Any], record: dict[str, Any]) -> list[dict[str, Any]]:
    return [event for event in store.read_lines(_path(session, record))
            if not event_failures(event)]


def fold_events(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for event in items:
        finding_id = event["finding_id"]
        if event["event"] == "add":
            if finding_id in found:
                continue
            found[finding_id] = {
                "finding_id": finding_id, "check_id": event.get("check_id"),
                "origin": event["origin"], "source": event.get("source"),
                "severity": event["severity"], "title": event["title"],
                "expected": event.get("expected"), "actual": event["actual"],
                "evidence": list(event["evidence"]), "confirmed": event["confirmed"],
                "data": dict(event["data"]), "created_at": event["ts"],
                "journal_seq": event["journal_seq"],
                "reproduced": None, "stability": None, "repro_runs": [], "draft_flow": None,
            }
        elif finding_id in found:
            found[finding_id].update(event["fields"])
    return list(found.values())


def fold(session: dict[str, Any], record: dict[str, Any]) -> list[dict[str, Any]]:
    """One state per finding, in add order, with updates applied."""
    return fold_events(events(session, record))


# --- verbs ------------------------------------------------------------------------


def _text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    # Mask first, then cut (contract 8.2): a cut never splits the marker.
    return store.clip(value, limit) or None


def add(session: dict[str, Any], record: dict[str, Any], *, check_id: str | None,
        severity: str, title: str, actual: str, expected: str | None = None,
        evidence: Iterable[str] = (), origin: str = "agent", source: str | None = None,
        confirmed: bool = True, data: dict[str, Any] | None = None) -> dict[str, Any]:
    store.ensure_mutable(record)
    if severity not in SEVERITIES:
        raise errors.AutonomError(
            errors.INVALID_SEVERITY, f"unknown severity {severity!r}",
            hint="Severities: " + ", ".join(SEVERITIES) + ".", severity=severity)
    if origin not in ORIGINS:
        raise errors.AutonomError(errors.INVALID_VALUE, f"unknown origin {origin!r}",
                                  hint="Origins: agent, auto.")
    if source is not None and source not in SOURCES:
        raise errors.AutonomError(errors.INVALID_VALUE, f"unknown source {source!r}",
                                  hint="Sources: " + ", ".join(SOURCES) + ".")
    if not isinstance(confirmed, bool):
        raise errors.AutonomError(errors.INVALID_VALUE, "confirmed must be true or false",
                                  hint="Internal: pass a boolean.")
    if data is not None and not isinstance(data, dict):
        raise errors.AutonomError(errors.INVALID_VALUE, "finding data must be an object",
                                  hint="Internal: pass a dict.")
    clean_title, clean_actual = _text(title, TITLE_LIMIT), _text(actual, TEXT_LIMIT)
    if not clean_title or not clean_actual:
        raise errors.AutonomError(
            errors.INVALID_VALUE, "a finding needs a title and the actual behaviour",
            hint="Pass --title and --actual (and --expected for what should happen).")
    refs = [evidence] if isinstance(evidence, str) else [str(ref) for ref in evidence]
    if len(refs) > MAX_EVIDENCE:
        raise errors.AutonomError(errors.INVALID_VALUE,
                                  f"at most {MAX_EVIDENCE} evidence refs per finding",
                                  hint="Keep the refs that show the problem best.")
    deduped: list[str] = []
    for ref in refs:
        if ref not in deduped:
            deduped.append(ref)
    with store.lock(session, record["pretest_id"]):
        current = store.load(session, record["pretest_id"])
        store.ensure_mutable(current)
        if check_id is not None:
            known = {item["check_id"] for item in checks_mod.fold(session, current)}
            if check_id not in known:
                raise errors.AutonomError(
                    errors.UNKNOWN_CHECK, f"no check {check_id!r} in this pre-test",
                    hint="List checks with 'autonom pretest check list'.", check_id=check_id)
        for ref in deduped:
            resolve_ref(session, ref)
        existing = fold(session, current)
        finding_id = f"f-{len(existing) + 1}"
        taken = {item["finding_id"] for item in existing}
        while finding_id in taken:
            finding_id = f"f-{int(finding_id[2:]) + 1}"
        safe_data = json.loads(json.dumps(contracts.redact_value(data or {}),
                                          ensure_ascii=False, default=str))
        event = {"event": "add", "finding_id": finding_id, "check_id": check_id,
                 "origin": origin, "source": source, "severity": severity,
                 "title": clean_title, "expected": _text(expected, TEXT_LIMIT),
                 "actual": clean_actual, "evidence": deduped, "confirmed": confirmed,
                 "data": safe_data, **_stamp(session)}
        validate_event(event)
        store.append_line(_path(session, current), event)
    return next(item for item in fold(session, record) if item["finding_id"] == finding_id)


def update(session: dict[str, Any], record: dict[str, Any], finding_id: str,
           fields: dict[str, Any]) -> dict[str, Any]:
    store.ensure_mutable(record)
    if not isinstance(fields, dict) or not fields:
        raise errors.AutonomError(errors.INVALID_VALUE, "nothing to update",
                                  hint="Fields: " + ", ".join(UPDATE_FIELDS) + ".")
    failures = _field_failures(fields)
    if failures:
        raise errors.AutonomError(errors.INVALID_VALUE, "finding update is not valid",
                                  hint="Fields: " + ", ".join(UPDATE_FIELDS) + ".",
                                  failures=failures)
    with store.lock(session, record["pretest_id"]):
        current = store.load(session, record["pretest_id"])
        store.ensure_mutable(current)
        if finding_id not in {item["finding_id"] for item in fold(session, current)}:
            raise errors.AutonomError(
                errors.UNKNOWN_FINDING, f"no finding {finding_id!r} in this pre-test",
                hint="List findings with 'autonom pretest finding list'.",
                finding_id=finding_id)
        event = {"event": "update", "finding_id": finding_id,
                 "fields": json.loads(json.dumps(fields)), **_stamp(session)}
        validate_event(event)
        store.append_line(_path(session, current), event)
    return next(item for item in fold(session, record) if item["finding_id"] == finding_id)
