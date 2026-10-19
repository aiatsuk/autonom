"""Checks (tp-007): what the pre-test verifies, one at a time.

``checks.ndjson`` is append-only, written under the pre-test lock::

    {"event": "add",   "check_id": "chk-1", "title", "kind", "case", "why", "flow", ...}
    {"event": "begin", "check_id": "chk-1", ...}
    {"event": "end",   "check_id": "chk-1", "verdict", "note", "run_id",
                                            ["observed_count"], ...}
    {"event": "amended", "check_id": "chk-1", "old_verdict", "new_verdict", "note",
                                              "at", ["observed_count"], ...}

Every line also carries ``ts`` (ISO UTC, milliseconds) and ``journal_seq``
(the newest journal sequence at that moment), so a check's window can be
matched against the journal, logs and captured traffic later. ``fold``
turns the events into one state per check, in add order.

``amend`` (A.9) corrects the verdict of an ended check of the active
pre-test: it appends an ``amended`` event and the folded state carries the
latest verdict, its note and the list of ``amendments``, so the summary and
the pack use the latest verdict. A check of a race kit case
(``RACE_CASES``: the double tap) passes only with an observed request count
(``observed_count``, A.12 / RISK-038), when the caller asks for that rule
(``pretest check end`` and ``check amend`` do). The count is stored on the
check and, so the summary shows it, leads the folded ``note``.
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
EVENTS = ("add", "begin", "end", "amended")
# Kit cases whose pass depends on how many requests a fast repeat really sent
# (edge-case-kit.md: `double-tap`); device input can be slower than the race.
RACE_CASES = ("double-tap",)
RACE_HINT = ("count the requests (network requests) and pass --observed-count, or end "
             "with --verdict inconclusive")
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
        failures.append("event must be add, begin, end or amended")
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
    if kind == "amended":
        for key in ("old_verdict", "new_verdict"):
            if event.get(key) not in VERDICTS:
                failures.append(f"{key} must be one of " + ", ".join(VERDICTS))
        if not isinstance(event.get("note"), str) or not event.get("note"):
            failures.append("note must be a non-empty string")
        if not isinstance(event.get("at"), str) or parse_ts(event.get("at")) is None:
            failures.append("at must be an ISO timestamp")
    if kind in ("end", "amended") and "observed_count" in event \
            and not _is_count(event["observed_count"]):
        failures.append("observed_count must be an integer >= 0")
    return failures


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


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
            if "observed_count" in event:
                state["observed_count"] = event["observed_count"]
            began, ended = parse_ts(state["began_at"]), parse_ts(state["ended_at"])
            if began and ended:
                state["duration_s"] = round(max(0.0, (ended - began).total_seconds()), 3)
        elif event["event"] == "amended" and state["state"] == "ended":
            # keys appear only when used, so the fold (and a pack's digest) of
            # a pre-test without amendments or counts is what it always was
            state.setdefault("amendments", []).append({
                "old_verdict": state["verdict"], "new_verdict": event["new_verdict"],
                "note": event["note"], "previous_note": state.get("note"),
                "at": event["at"]})
            state["verdict"] = event["new_verdict"]
            state["note"] = event["note"]
            if "observed_count" in event:
                state["observed_count"] = event["observed_count"]
    return [_shown(state) for state in states.values()]


def _shown(state: dict[str, Any]) -> dict[str, Any]:
    """The folded state as readers see it: a stored request count
    (``observed requests: N``) and the last amendment (``amended from V``)
    lead the note, so summary.md and summary.json, which print the note,
    show both. A state with neither is returned unchanged."""
    parts: list[str] = []
    if "observed_count" in state:
        parts.append(f"observed requests: {state['observed_count']}")
    if state.get("amendments"):
        parts.append(f"amended from {state['amendments'][-1]['old_verdict']}")
    if not parts:
        return state
    if state.get("note"):
        parts.append(state["note"])
    return {**state, "note": "; ".join(parts)}


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


def _check_verdict(verdict: str) -> None:
    if verdict not in VERDICTS:
        raise errors.AutonomError(
            errors.INVALID_VERDICT, f"unknown verdict {verdict!r}",
            hint="Verdicts: " + ", ".join(VERDICTS) + ".", verdict=verdict)


def _check_count(observed_count: Any) -> None:
    if observed_count is not None and not _is_count(observed_count):
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            f"--observed-count must be a whole number >= 0, got {observed_count!r}",
            hint="Count the requests in 'autonom network requests list', e.g. "
                 "--observed-count 1.", observed_count=str(observed_count))


def _race_rule(state: dict[str, Any], verdict: str, observed_count: int | None) -> None:
    """A pass of a race kit case needs the observed request count (A.12)."""
    if verdict != "pass" or state.get("case") not in RACE_CASES or observed_count is not None:
        return
    raise errors.AutonomError(
        errors.RACE_NEEDS_COUNT,
        f"check {state['check_id']} is the {state['case']} case: a pass needs the observed "
        "request count", hint=RACE_HINT, check_id=state["check_id"], case=state["case"])


def end(session: dict[str, Any], record: dict[str, Any], check_id: str, *,
        verdict: str, note: str | None = None, run_id: str | None = None,
        observed_count: int | None = None, race_rule: bool = False) -> dict[str, Any]:
    """End the open check. ``observed_count`` (an integer >= 0) is stored on
    the check; with ``race_rule`` (the CLI's ``check end``) a ``pass`` of a
    race kit case without it is refused with ``race_needs_count``. Library
    callers that end checks from flow runs keep the old behaviour."""
    _check_verdict(verdict)
    _check_count(observed_count)
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
        if race_rule:
            _race_rule(state, verdict, observed_count)
        event = {"event": "end", "check_id": check_id, "verdict": verdict,
                 "note": _text(note, TEXT_LIMIT),
                 "run_id": str(run_id) if run_id else None, **_stamp(session)}
        if observed_count is not None:
            event["observed_count"] = observed_count
        validate_event(event)
        store.append_line(_path(session, current), event)
        store.set_open_check(session, current["pretest_id"], None)
    return _find(fold(session, record), check_id)


def amend(session: dict[str, Any], record: dict[str, Any], check_id: str, *,
          verdict: str, note: str, observed_count: int | None = None) -> dict[str, Any]:
    """Correct the verdict of an ended check of the active pre-test (A.9).

    Appends ``{"event": "amended", "old_verdict", "new_verdict", "note",
    "at"}`` (and ``observed_count`` when given); the fold then carries the new
    verdict. Refusals: ``invalid_verdict``, ``invalid_value`` (no note, a bad
    count), ``pretest_finalized`` (packed), ``no_active_pretest`` (stopped),
    ``unknown_check``, ``check_not_ended`` and, for a pass of a race kit case
    with no count stored or given, ``race_needs_count``."""
    _check_verdict(verdict)
    _check_count(observed_count)
    clean_note = _text(note, TEXT_LIMIT)
    if not clean_note or not clean_note.strip():
        raise errors.AutonomError(
            errors.INVALID_VALUE, "an amendment needs --note",
            hint="Say why the verdict changes, e.g. --note 'the 500 came from the "
                 "test backend, not the app'.", check_id=check_id)
    store.check_open_record(record)
    with store.lock(session, record["pretest_id"]):
        current = store.load(session, record["pretest_id"])
        store.check_open_record(current)
        states = fold_events(events(session, current))
        state = next((item for item in states if item["check_id"] == check_id), None)
        if state is None:
            _find(states, check_id)  # raises unknown_check
        if state["state"] != "ended":
            hint = (f"End it with 'autonom pretest check end {check_id} --verdict <verdict>'."
                    if state["state"] == "open" else
                    f"Begin it with 'autonom pretest check begin {check_id}', then end it.")
            raise errors.AutonomError(
                errors.CHECK_NOT_ENDED,
                f"check {check_id} is {state['state']}, not ended: only an ended check "
                "can be amended", hint=hint, check_id=check_id, state=state["state"])
        _race_rule(state, verdict, observed_count if observed_count is not None
                   else state.get("observed_count"))
        stamp = _stamp(session)
        event = {"event": "amended", "check_id": check_id, "old_verdict": state["verdict"],
                 "new_verdict": verdict, "note": clean_note, "at": stamp["ts"], **stamp}
        if observed_count is not None:
            event["observed_count"] = observed_count
        validate_event(event)
        store.append_line(_path(session, current), event)
    return _find(fold(session, record), check_id)
