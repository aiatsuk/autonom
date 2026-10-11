"""Checks (tp-007): what the pre-test verifies, one at a time.

``checks.ndjson`` is append-only, written under the pre-test lock::

    {"event": "add",   "check_id": "chk-1", "title", "kind", "case", "why", "flow", ...}
    {"event": "begin", "check_id": "chk-1", ...}
    {"event": "end",   "check_id": "chk-1", "verdict", "note", "run_id", ...}

Every line also carries ``ts`` (ISO UTC, milliseconds) and ``journal_seq``
(the newest journal sequence at that moment), so a check's window can be
matched against the journal, logs and captured traffic later. ``fold``
turns the events into one state per check, in add order.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import errors
from .. import journal as journal_mod
from . import kit as kit_mod
from . import store

KINDS = ("ticket", "edge", "regression", "network", "a11y", "analytics")
VERDICTS = ("pass", "fail", "blocked", "not_covered", "inconclusive")
STATES = ("added", "open", "ended")
EVENTS = ("add", "begin", "end")
TITLE_LIMIT = 200
TEXT_LIMIT = 4000


def parse_ts(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _path(session: dict[str, Any], record: dict[str, Any]) -> Path:
    return store.pretest_dir(session, record["pretest_id"]) / store.CHECKS_FILE


def _stamp(session: dict[str, Any]) -> dict[str, Any]:
    return {"ts": store.now(), "journal_seq": journal_mod.last_seq(session)}


# --- validation -------------------------------------------------------------------


def event_failures(event: Any) -> list[str]:
    """Hand-written validator for ``schemas/pretest-check-v1.schema.json``."""
    if not isinstance(event, dict):
        return ["event must be an object"]
    failures: list[str] = []
    kind = event.get("event")
    if kind not in EVENTS:
        failures.append("event must be add, begin or end")
    if not isinstance(event.get("check_id"), str) or not store.CHECK_ID.match(
            event.get("check_id") or ""):
        failures.append("check_id must look like chk-N")
    if not isinstance(event.get("ts"), str) or parse_ts(event.get("ts")) is None:
        failures.append("ts must be an ISO timestamp")
    seq = event.get("journal_seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        failures.append("journal_seq must be an integer >= 0")
    if kind == "add":
        if not isinstance(event.get("title"), str) or not event.get("title"):
            failures.append("title must be a non-empty string")
        if event.get("kind") not in KINDS:
            failures.append("kind must be one of " + ", ".join(KINDS))
        for key in ("case", "why", "flow"):
            if key not in event:
                failures.append(f"missing {key}")
            elif event[key] is not None and not isinstance(event[key], str):
                failures.append(f"{key} must be a string or null")
        if event.get("case") is not None and event.get("case") not in kit_mod.ids():
            failures.append("case must be an edge-case kit id")
    if kind == "end":
        if event.get("verdict") not in VERDICTS:
            failures.append("verdict must be one of " + ", ".join(VERDICTS))
        for key in ("note", "run_id"):
            if key not in event:
                failures.append(f"missing {key}")
            elif event[key] is not None and not isinstance(event[key], str):
                failures.append(f"{key} must be a string or null")
    return failures


def validate_event(event: Any) -> dict[str, Any]:
    failures = event_failures(event)
    if failures:
        raise errors.AutonomError(
            errors.INVALID_VALUE, "check event does not satisfy the pre-test check schema",
            hint="This is a bug in the writer.", failures=failures)
    return event


# --- fold -------------------------------------------------------------------------


def events(session: dict[str, Any], record: dict[str, Any]) -> list[dict[str, Any]]:
    return [event for event in store.read_lines(_path(session, record))
            if not event_failures(event)]


def fold_events(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for event in items:
        check_id = event["check_id"]
        if event["event"] == "add":
            if check_id in states:
                continue
            states[check_id] = {
                "check_id": check_id, "title": event["title"], "kind": event["kind"],
                "case": event.get("case"), "why": event.get("why"),
                "flow": event.get("flow"), "state": "added", "verdict": None,
                "note": None, "run_ids": [], "added_at": event["ts"],
                "began_at": None, "ended_at": None, "duration_s": None,
                "seq_from": None, "seq_to": None,
            }
            continue
        state = states.get(check_id)
        if state is None:
            continue
        if event["event"] == "begin" and state["state"] == "added":
            state["state"] = "open"
            state["began_at"] = event["ts"]
            state["seq_from"] = event["journal_seq"] + 1
        elif event["event"] == "end" and state["state"] == "open":
            state["state"] = "ended"
            state["ended_at"] = event["ts"]
            state["verdict"] = event["verdict"]
            state["note"] = event.get("note")
            state["seq_to"] = event["journal_seq"]
            if event.get("run_id") and event["run_id"] not in state["run_ids"]:
                state["run_ids"].append(event["run_id"])
            began, ended = parse_ts(state["began_at"]), parse_ts(state["ended_at"])
            if began and ended:
                state["duration_s"] = round(max(0.0, (ended - began).total_seconds()), 3)
    return list(states.values())


def fold(session: dict[str, Any], record: dict[str, Any]) -> list[dict[str, Any]]:
    """One state per check, in add order."""
    return fold_events(events(session, record))


def open_check(session: dict[str, Any], record: dict[str, Any]) -> dict[str, Any] | None:
    return next((item for item in fold(session, record) if item["state"] == "open"), None)


def _find(states: list[dict[str, Any]], check_id: str) -> dict[str, Any]:
    for state in states:
        if state["check_id"] == check_id:
            return state
    raise errors.AutonomError(
        errors.UNKNOWN_CHECK, f"no check {check_id!r} in this pre-test",
        hint="List checks with 'autonom pretest check list'.", check_id=check_id)


# --- verbs ------------------------------------------------------------------------
#
# ``begin`` and ``end`` append the event first and move ``active.json`` second.
# A crash between the two leaves the pointer behind the events; the events
# are the truth. Attribution already reads the open check from the folded
# events (``store.attribution``), and the next ``begin``/``end`` puts the
# pointer right before deciding anything. Retrying an interrupted ``begin`` is
# idempotent: ``begin`` of the check that is already open returns its state
# instead of refusing (contract 8.2). ``end`` is not: ``end`` of a check that
# is not open (already ended, or never begun) always refuses with
# ``no_open_check``, whatever the verdict, so a retried ``end`` never silently
# drops its note (fix round 3, item 12).


def _repair_pointer(session: dict[str, Any], record: dict[str, Any],
                    states: list[dict[str, Any]]) -> None:
    """Point ``active.json`` at the folded open check (or none) when it
    names something else. Only this pre-test's own pointer is touched."""
    ids = store.active_ids(session)
    if not ids or ids[0] != record["pretest_id"]:
        return
    opened = next((item["check_id"] for item in states if item["state"] == "open"), None)
    if ids[1] != opened:
        store.set_open_check(session, record["pretest_id"], opened)


def _text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    # Mask first, then cut (contract 8.2): a cut never splits the marker.
    return store.clip(value, limit) or None


def add(session: dict[str, Any], record: dict[str, Any], *, title: str, kind: str,
        case: str | None = None, why: str | None = None,
        flow: str | Path | None = None) -> dict[str, Any]:
    store.ensure_mutable(record)
    clean_title = _text(title, TITLE_LIMIT)
    if not clean_title:
        raise errors.AutonomError(errors.INVALID_VALUE, "a check needs a title",
                                  hint="Say what the check verifies, e.g. "
                                       "'Cart total updates after removing an item'.")
    if kind not in KINDS:
        raise errors.AutonomError(errors.INVALID_VALUE, f"unknown check kind {kind!r}",
                                  hint="Kinds: " + ", ".join(KINDS) + ".", kind=kind)
    if case is not None and case not in kit_mod.ids():
        raise errors.AutonomError(errors.INVALID_VALUE, f"unknown edge case {case!r}",
                                  hint="See 'autonom pretest kit' for the case ids.",
                                  case=case)
    flow_path: str | None = None
    if flow is not None:
        resolved = Path(flow).expanduser().resolve()
        if not resolved.is_file():
            raise errors.AutonomError(errors.FLOW_FILE_NOT_FOUND, f"no flow file at {flow}",
                                      hint="Pass the path of an existing flow YAML.",
                                      path=str(flow))
        flow_path = str(resolved)
    with store.lock(session, record["pretest_id"]):
        current = store.load(session, record["pretest_id"])
        store.ensure_mutable(current)
        existing = fold(session, current)
        check_id = f"chk-{len(existing) + 1}"
        taken = {item["check_id"] for item in existing}
        while check_id in taken:
            check_id = f"chk-{int(check_id[4:]) + 1}"
        event = {"event": "add", "check_id": check_id, "title": clean_title, "kind": kind,
                 "case": case, "why": _text(why, TEXT_LIMIT), "flow": flow_path,
                 **_stamp(session)}
        validate_event(event)
        store.append_line(_path(session, current), event)
    return _find(fold(session, record), check_id)


def begin(session: dict[str, Any], record: dict[str, Any], check_id: str) -> dict[str, Any]:
    store.ensure_mutable(record)
    with store.lock(session, record["pretest_id"]):
        current = store.load(session, record["pretest_id"])
        store.check_open_record(current)
        states = fold(session, current)
        state = _find(states, check_id)
        _repair_pointer(session, current, states)
        already = next((item for item in states if item["state"] == "open"), None)
        if already is not None and already["check_id"] == check_id:
            return state  # a retried begin: already recorded, pointer repaired
        if already is not None:
            raise errors.AutonomError(
                errors.CHECK_ALREADY_OPEN,
                f"check {already['check_id']} is still open",
                hint=f"End it first with 'autonom pretest check end {already['check_id']} "
                     "--verdict <verdict>'.",
                open_check_id=already["check_id"], check_id=check_id)
        if state["state"] == "ended":
            raise errors.AutonomError(
                errors.INVALID_VALUE, f"check {check_id} already ended ({state['verdict']})",
                hint="Add a new check to try again; an ended check keeps its verdict.",
                check_id=check_id)
        event = {"event": "begin", "check_id": check_id, **_stamp(session)}
        validate_event(event)
        store.append_line(_path(session, current), event)
        store.set_open_check(session, current["pretest_id"], check_id)
    return _find(fold(session, record), check_id)


def end(session: dict[str, Any], record: dict[str, Any], check_id: str, *,
        verdict: str, note: str | None = None, run_id: str | None = None) -> dict[str, Any]:
    if verdict not in VERDICTS:
        raise errors.AutonomError(
            errors.INVALID_VERDICT, f"unknown verdict {verdict!r}",
            hint="Verdicts: " + ", ".join(VERDICTS) + ".", verdict=verdict)
    store.ensure_mutable(record)
    with store.lock(session, record["pretest_id"]):
        current = store.load(session, record["pretest_id"])
        store.ensure_mutable(current)
        states = fold(session, current)
        state = _find(states, check_id)
        _repair_pointer(session, current, states)
        if state["state"] != "open":
            # An ended check is never ended again, whatever the verdict: a
            # silent ok would drop the new note (fix round 3, item 12).
            opened = next((item["check_id"] for item in states if item["state"] == "open"),
                          None)
            if state["state"] == "ended":
                hint = (f"{check_id} already ended '{state['verdict']}'; its verdict and note "
                        "cannot change. Record what you saw with 'autonom pretest finding "
                        f"add --check {check_id}'.")
            elif opened:
                hint = f"The open check is {opened}."
            else:
                hint = f"Begin it first with 'autonom pretest check begin {check_id}'."
            raise errors.AutonomError(
                errors.NO_OPEN_CHECK, f"check {check_id} is not open ({state['state']})",
                hint=hint, check_id=check_id, open_check_id=opened, state=state["state"],
                verdict=state.get("verdict"))
        event = {"event": "end", "check_id": check_id, "verdict": verdict,
                 "note": _text(note, TEXT_LIMIT),
                 "run_id": str(run_id) if run_id else None, **_stamp(session)}
        validate_event(event)
        store.append_line(_path(session, current), event)
        store.set_open_check(session, current["pretest_id"], None)
    return _find(fold(session, record), check_id)
