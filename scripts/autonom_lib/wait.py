"""Waiting inside a call instead of sleeping between calls.

Three things can be waited for, each bounded by a timeout:

- ``visible``: at least one node on screen matches a selector (the fields and
  match modes of `ui find`; only on-screen nodes count, as in a flow's
  `assertVisible`);
- ``gone``: no node on screen matches it;
- ``request``: the session's capture proxy recorded a matching request (the
  filters of `network requests list`) after the wait began, or after the
  cursor its caller took before sending an input.

`ui wait` takes one element condition from its selector flags, `network
requests wait` one request condition from its filters, and the action verbs
any number of them through `--until` (``parse_until``): all must hold.

``poll`` only reads: it never sends an input. A tree read that fails the way
a screen in transition fails (`backend_failed`, `ui_not_idle`) is read again
until the deadline; the last such failure is reported in the result, never
raised. Any other failure is the caller's to handle.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from . import errors, selector

VISIBLE = "visible"
GONE = "gone"
REQUEST = "request"

# How long `--until` waits when `--until-timeout-ms` is not given: the default
# a flow step waits for (`flow run --default-timeout-ms`).
DEFAULT_UNTIL_TIMEOUT_MS = 10_000
INTERVAL_MS = 200

# A tree read that failed like this is read again: the screen was mid-change.
_TRANSIENT = (errors.BACKEND_FAILED, errors.UI_NOT_IDLE)
# `--until` keys and the selector field each one fills.
_UNTIL_FIELDS = {"text": "text", "desc": "desc", "id": "resource_id"}
_FIELD_WORDS = {"resource_id": "id", "class_name": "class"}
_REQUEST_KEYS = ("id", "method", "host", "path", "status", "mocked", "duration_ms")

HINT_UNTIL = ("Pass text=VALUE, desc=VALUE or id=VALUE (a node on screen), the same after "
              "'gone:' (no such node on screen), or request=GLOB (a request the proxy "
              "records, matched like 'network requests list --path').")


@dataclass(frozen=True)
class Condition:
    """One thing to wait for (see the module docstring)."""

    kind: str
    selector: dict[str, Any] = field(default_factory=dict)
    mode: str = "contains"
    case_sensitive: bool = False
    index: int | None = None
    filters: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        """The condition as the reply names it."""
        if self.kind == REQUEST:
            named = {("path" if key == "path_glob" else key): value
                     for key, value in self.filters.items()}
            return {"wait": REQUEST, **named}
        described: dict[str, Any] = {"wait": self.kind, "selector": dict(self.selector)}
        if self.mode != "contains":
            described["mode"] = self.mode
        if self.case_sensitive:
            described["case_sensitive"] = True
        if self.index is not None:
            described["index"] = self.index
        return described

    def label(self) -> str:
        """The condition in a few words, for a message or a text reply."""
        return label_of(self.describe())


def label_of(described: Mapping[str, Any]) -> str:
    """A condition as ``Condition.describe`` names it, in a few words:
    ``text "Orders"``, ``gone: id "com.app:id/spinner"``, ``request POST
    */v1/orders* status 201``."""
    if described.get("wait") == REQUEST:
        words = [str(described[key]) for key in ("method", "host", "path")
                 if described.get(key) is not None]
        if described.get("status") is not None:
            words.append(f"status {described['status']}")
        if described.get("mocked") is not None:
            words.append("mocked" if described["mocked"] else "not mocked")
        return " ".join(["request", *words])
    words = [f'{_FIELD_WORDS.get(name, name)} {_quoted(value)}'
             for name, value in (described.get("selector") or {}).items()]
    return ("gone: " if described.get("wait") == GONE else "") + " ".join(words)


def _quoted(value: Any) -> str:
    return f'"{value}"' if isinstance(value, str) else str(value).lower()


def element(fields: Mapping[str, Any], *, gone: bool = False, mode: str = "contains",
            case_sensitive: bool = False, index: int | None = None) -> Condition:
    """A node on screen (or, with ``gone``, no such node), by the selector
    fields that are not None."""
    return Condition(GONE if gone else VISIBLE,
                     selector={name: value for name, value in fields.items()
                               if value is not None},
                     mode=mode, case_sensitive=case_sensitive, index=index)


def request(**filters: Any) -> Condition:
    """A recorded request, by the `store.filter_flows` filters that are not None."""
    return Condition(REQUEST, filters={name: value for name, value in filters.items()
                                       if value is not None})


def parse_until(spec: str) -> Condition:
    """One `--until` value: ``text=``, ``desc=`` or ``id=`` (matched like the
    selector flags by default: contains, any case), the same after ``gone:``,
    or ``request=GLOB``. Anything else is `invalid_value`."""
    text = str(spec).strip()
    gone = text.startswith("gone:")
    key, equals, value = (text[len("gone:"):] if gone else text).partition("=")
    key = key.strip()
    if not equals or not value:
        raise _until_error(spec, "it has no value" if key in (*_UNTIL_FIELDS, REQUEST)
                           else "it is not KEY=VALUE")
    if key == REQUEST:
        if gone:
            raise _until_error(spec, "a request cannot be waited to be gone")
        return request(path_glob=value)
    if key not in _UNTIL_FIELDS:
        raise _until_error(spec, f"{key!r} is not text, desc, id or request")
    return element({_UNTIL_FIELDS[key]: value}, gone=gone)


def _until_error(spec: str, why: str) -> errors.AutonomError:
    return errors.AutonomError(errors.INVALID_VALUE, f"--until {spec!r}: {why}", HINT_UNTIL,
                               flag="--until", value=str(spec))


def matches(nodes: list[dict[str, Any]], condition: Condition) -> list[dict[str, Any]]:
    """The on-screen nodes an element condition selects (``index`` narrows to
    that occurrence; one that is not there selects nothing)."""
    found = selector.select(nodes, condition.selector, mode=condition.mode,
                            case_sensitive=condition.case_sensitive, all_matches=True,
                            visible_only=True)
    if condition.index is None:
        return found
    try:
        return [found[condition.index]]
    except IndexError:
        return []


def poll(conditions: list[Condition], *, timeout_ms: int,
         read_nodes: Callable[[], list[dict[str, Any]]] | None = None,
         fresh_requests: Callable[[], list[dict[str, Any]]] | None = None,
         interval_ms: int = INTERVAL_MS,
         clock: Callable[[], float] = time.monotonic,
         sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Wait until every condition holds, at most ``timeout_ms``.

    ``read_nodes`` reads the screen (called once per round, only when an
    element condition is waited for); ``fresh_requests`` returns the requests
    recorded since its last call (only for request conditions). A request
    condition stays met once a matching request was seen; element conditions
    are judged on each read, and all of them must hold in the same read.
    Every condition is checked at least once, and once more at the deadline.

    The result: ``met``, ``elapsed_ms`` (the real clock: a slow last read is
    reported, not hidden), ``timeout_ms``, ``reads`` (tree reads made),
    ``conditions`` (each as ``describe`` names it, with ``met``, for an
    element ``count`` — the on-screen matches of the last read — and for a
    request the one that met it), and ``error`` (``code``, ``message``) when
    the last tree read failed.
    """
    from .network import store  # the flow filters; not needed by an element wait

    started = clock()
    deadline = started + max(timeout_ms, 0) / 1000
    states: list[dict[str, Any]] = [{"met": False} for _ in conditions]
    elements = [index for index, item in enumerate(conditions) if item.kind != REQUEST]
    requests = [index for index, item in enumerate(conditions) if item.kind == REQUEST]
    reads = 0
    failure: errors.AutonomError | None = None
    while True:
        if requests:
            assert fresh_requests is not None
            recorded = fresh_requests()
            for index in requests:
                if states[index]["met"]:
                    continue
                found = store.filter_flows(recorded, **conditions[index].filters)
                if found:
                    states[index].update(met=True, request=_request_summary(found[0]))
        if elements:
            assert read_nodes is not None
            try:
                nodes: list[dict[str, Any]] | None = read_nodes()
                failure = None
            except errors.AutonomError as exc:
                if exc.code not in _TRANSIENT:
                    raise
                nodes, failure = None, exc
            reads += 1
            for index in elements:
                if nodes is None:
                    states[index].update(met=False, count=None)
                    continue
                count = len(matches(nodes, conditions[index]))
                states[index].update(
                    met=(count == 0) if conditions[index].kind == GONE else count > 0,
                    count=count)
        now = clock()
        met = all(state["met"] for state in states)
        if met or now >= deadline:
            result: dict[str, Any] = {
                "met": met, "elapsed_ms": int((now - started) * 1000),
                "timeout_ms": timeout_ms, "reads": reads,
                "conditions": [{**item.describe(), **state}
                               for item, state in zip(conditions, states)],
            }
            if failure is not None:
                result["error"] = {"code": failure.code, "message": failure.message}
            return result
        sleep(min(max(interval_ms, 10) / 1000, deadline - now))


def _request_summary(flow: Mapping[str, Any]) -> dict[str, Any]:
    summary = {key: flow.get(key) for key in _REQUEST_KEYS if flow.get(key) is not None}
    if flow.get("completed") is False:
        summary["completed"] = False
    return summary


def unmet_message(conditions: list[Condition], result: Mapping[str, Any],
                  timeout_name: str = "--timeout-ms") -> tuple[str, str]:
    """``(error, hint)`` for a wait that ended unmet: which conditions did
    not hold, and what to look at."""
    unmet = [item for item, state in zip(conditions, result.get("conditions") or ())
             if not state.get("met")]
    named = "; ".join(item.label() for item in unmet) or "the condition"
    error = f"not met within {result.get('timeout_ms')} ms: {named}"
    failed = result.get("error")
    if isinstance(failed, Mapping):
        error += f" (the last screen read failed: {failed.get('message')})"
    hints = []
    if any(item.kind == VISIBLE for item in unmet):
        hints.append("No such node came on screen: read it with 'autonom ui tree' (on iOS "
                     "a visible label is in desc).")
    if any(item.kind == GONE for item in unmet):
        hints.append("The node is still on screen.")
    if any(item.kind == REQUEST for item in unmet):
        hints.append("No matching request was recorded: check the capture with 'autonom "
                     "network status' and the filter with 'autonom network requests list'.")
    hints.append(f"Raise {timeout_name} if it only needs longer.")
    return error, " ".join(hints)
