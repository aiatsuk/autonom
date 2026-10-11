"""Warm-up (tp-024, DEC-008): bring a new app to a known state before checks.

``run`` does, in order:

1. reads the app profile (``profile.load``; ``profile_invalid`` refuses) and
   validates its ``login_flow`` and ``warmup_flow`` files before anything
   touches the device (``flow_file_not_found`` or the flow's own error);
2. runs ``login_flow``, then ``warmup_flow``, through the flow executor. The
   environment variables the profile's ``accounts`` name (only
   ``AUTONOM_``/``PRETEST_`` names, ``profile.ACCOUNT_VAR``; no other variable
   is ever read) are handed to the runs as secrets when they are set (``--secret`` and ``--env`` add more), so
   no account value reaches a file. When a flow does not pass, the later
   flow and the walk are skipped;
3. updates the app's Runtime Map from the session, the same ingestion as
   ``autonom atlas update`` (every flow run's step fingerprints and every tap
   detail);
4. walks the Runtime Map screen hints: the hints in ``context.json`` plus the
   hints the updated map now gives for the same diff. Each hint is reported:

   - ``reached`` — the screen is on display, either already or after
     replaying the recorded transitions of the shortest known path from the
     current screen (each a ``tapOn`` with a recorded selector; after each
     tap the screen settles and its fingerprint must be the edge's target);
   - ``unreplayable`` — a known path exists but one of its transitions is not
     a selector tap (a swipe, a key, a coordinate tap, a relational selector);
   - ``known`` — the screen is in the map but was not reached: no recorded
     path from the current screen, the walk was skipped or stopped, or the
     replay ended on another screen (``reason`` says which);
   - ``unknown`` — the screen is not in the Runtime Map.

No exploration happens beyond recorded transitions. Every flow step and
every replayed tap goes through the pre-test guard: it counts against the
budget and forbidden text is refused. A guard refusal stops the walk; the
hints left are reported ``known`` with the refusal's code as their reason.
Warm-up refuses while a check is open (``check_already_open``): its actions
would land in that check's window.
"""
from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from typing import Any

from .. import actions as actions_mod
from .. import errors
from .. import ui as ui_mod
from ..atlas import fingerprint as fingerprint_mod
from ..atlas import graph as graph_mod
from ..flow import executor as flow_executor
from ..flow import schema as flow_schema
from ..flow import validator as flow_validator
from . import checks as checks_mod
from . import context as context_mod
from . import profile as profile_mod
from . import store

STATUSES = ("known", "reached", "unreplayable", "unknown")
ROLES = ("login_flow", "warmup_flow")
SETTLE_TIMEOUT_MS = 4000
SETTLE_QUIET_MS = 400
MAX_PATH = 12
GUARD_CODES = frozenset({errors.BUDGET_EXHAUSTED, errors.FORBIDDEN_ACTION,
                         errors.APP_NOT_ALLOWED})
# action-detail selector keys (``ui tap``) and flow selector keys (``describe``)
_DETAIL_FIELDS = ("text", "desc", "resource_id", "class_name", "package", "role",
                  "clickable", "enabled", "focusable", "focused", "scrollable",
                  "selected", "checked")
_FLOW_FIELDS = {"id": "resource_id", "text": "text", "visibleText": "visible_text",
                "description": "desc", "role": "role", "enabled": "enabled",
                "checked": "checked", "selected": "selected", "focused": "focused"}


# --- flows -------------------------------------------------------------------------------


def _flows(profile: dict[str, Any] | None) -> list[tuple[str, Path, Any]]:
    """``[(role, path, parsed flow)]``, validated before any device action."""
    out: list[tuple[str, Path, Any]] = []
    for role in ROLES:
        value = (profile or {}).get(role)
        if not value:
            continue
        path = Path(value)
        if not path.is_file():
            raise errors.AutonomError(
                errors.FLOW_FILE_NOT_FOUND, f"the profile's {role} {path} does not exist",
                hint=f"Fix '{role}' in {profile.get('path')} (a path relative to the "
                     "profile's folder), or remove it.", path=str(path), role=role)
        out.append((role, path, flow_validator.validate_tree(path)))
    return out


def _secrets(profile: dict[str, Any] | None, config: Any) -> Any:
    config = config if config is not None else flow_executor.RunConfig()
    for name in profile_mod.account_variables(profile):
        if not profile_mod.ACCOUNT_VAR.match(name):
            continue  # never read a variable outside the account rule
        if name not in config.secrets and name in os.environ:
            config.secrets[name] = os.environ[name]
    return config


def _execution(result: Any) -> str:
    from .regress import execution

    return execution(result.status, result.failure, result.steps)


def _run_flow(target: Any, session: dict[str, Any], config: Any, role: str,
              path: Path, flow: Any) -> dict[str, Any]:
    run: dict[str, Any] = {"role": role, "flow": str(path), "run_id": None,
                           "status": "broken", "failure": None}
    runner = flow_executor.Executor(target, session, config)
    try:
        result = runner.run(flow)
    except errors.AutonomError as exc:
        run["run_id"] = getattr(runner, "_writer_run_id", None)
        run["failure"] = {"error_code": exc.code,
                          "failure_class": flow_schema.failure_class(exc.code),
                          "error": exc.message}
        return run
    run.update(run_id=result.run_id, failure=result.failure, status=_execution(result))
    return run


# --- Runtime Map -------------------------------------------------------------------------


def _read_graph(app_id: str) -> dict[str, Any]:
    """The map as stored, keeping only well-shaped screens and transitions."""
    try:
        data = json.loads((graph_mod.apps_home() / app_id / "atlas" / "graph.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    screens = data.get("screens") if isinstance(data.get("screens"), dict) else {}
    transitions = data.get("transitions") if isinstance(data.get("transitions"), dict) else {}
    return {"screens": screens, "transitions": transitions}


def update_map(session: dict[str, Any], app_id: str) -> dict[str, Any]:
    """``atlas update`` for the session: flow step fingerprints and tap details.

    A map file that does not parse, or whose entries are not shaped as the
    Runtime Map writes them (hand edited, or from another version), is left
    untouched: the walk then uses what it can read, and the block says
    ``state: "unavailable"``."""
    artifacts = Path(session["artifacts_dir"])
    runs = edges = 0
    try:
        stored = graph_mod.apps_home() / app_id / "atlas" / "graph.json"
        if stored.exists():
            json.loads(stored.read_text(encoding="utf-8"))  # unreadable: never overwritten
        graph = graph_mod.load(app_id)
        if not isinstance(graph, dict) or not isinstance(graph.get("screens"), dict) \
                or not isinstance(graph.get("transitions"), dict):
            raise TypeError("graph.json is not a Runtime Map")
        for events_path in sorted(artifacts.glob("flows/*/events.ndjson")):
            events = []
            try:
                lines = events_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    events.append(item)
            edges += graph_mod.ingest_flow_events(graph, events, session.get("session_id"))
            runs += 1
        details = list(actions_mod.read_details(session).values())
        edges += graph_mod.ingest_action_details(graph, details, session.get("session_id"))
        path = graph_mod.save(app_id, graph)
    except (KeyError, TypeError, AttributeError, ValueError, OSError) as exc:
        graph = _read_graph(app_id)
        return {"state": "unavailable",
                "reason": f"the Runtime Map could not be updated ({type(exc).__name__})",
                "graph": str(graph_mod.apps_home() / app_id / "atlas" / "graph.json"),
                "screens": len(graph["screens"]), "transitions": len(graph["transitions"]),
                "_graph": graph}
    return {"state": "updated", "graph": str(path), "runs_ingested": runs,
            "transitions_touched": edges, "screens": len(graph.get("screens") or {}),
            "transitions": len(graph.get("transitions") or {}), "_graph": graph}


def hints(session: dict[str, Any], record: dict[str, Any], app_id: str) -> list[dict[str, Any]]:
    """The context's hints, then the ones the updated map adds for the diff."""
    try:
        context = store.read_context(session, record["pretest_id"])
    except (OSError, ValueError):
        context = {}
    context = context if isinstance(context, dict) else {}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    stored = [item for item in context.get("runtime_map_hints") or [] if isinstance(item, dict)]
    names = [str(item.get("path")) for item in context.get("changed_files") or []
             if isinstance(item, dict) and item.get("path")]
    fresh = context_mod.runtime_map_hints(app_id, names,
                                          list(context.get("changed_areas") or []))
    for item in [*stored, *fresh]:
        screen_id = str(item.get("screen_id") or "")
        if screen_id and screen_id not in seen:
            seen.add(screen_id)
            out.append({"screen_id": screen_id, "labels": list(item.get("labels") or []),
                        "matched_area": item.get("matched_area"), "hint": True})
    return out


def _successes(edge: dict[str, Any]) -> int:
    value = edge.get("success")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def shortest_path(graph: dict[str, Any], source: str, target: str) -> list[dict[str, Any]] | None:
    """The fewest recorded transitions (with at least one success) from
    ``source`` to ``target``; ``[]`` when they are the same; None when none."""
    if source == target:
        return []
    adjacency: dict[str, list[dict[str, Any]]] = {}
    for edge in (graph.get("transitions") or {}).values():
        if isinstance(edge, dict) and isinstance(edge.get("from"), str) \
                and isinstance(edge.get("to"), str) and _successes(edge) > 0:
            adjacency.setdefault(edge["from"], []).append(edge)
    for edges in adjacency.values():
        # a replayable tap first, then the most successful edge
        edges.sort(key=lambda edge: (selector_of(edge) is None, -_successes(edge)))
    queue: deque[tuple[str, list[dict[str, Any]]]] = deque([(source, [])])
    seen = {source}
    while queue:
        current, walked = queue.popleft()
        if len(walked) >= MAX_PATH:
            continue
        for edge in adjacency.get(current, []):
            if edge["to"] in seen:
                continue
            step = walked + [edge]
            if edge["to"] == target:
                return step
            seen.add(edge["to"])
            queue.append((edge["to"], step))
    return None


def selector_of(edge: dict[str, Any]) -> dict[str, Any] | None:
    """``{"fields", "mode", "case_sensitive", "index"}`` for a replayable
    selector tap, else None (another command, no selector, or a relation)."""
    if edge.get("command") != "tapOn" or not edge.get("detail"):
        return None
    try:
        raw = json.loads(edge["detail"])
    except (TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    index = raw.get("index")
    if index is not None and (isinstance(index, bool) or not isinstance(index, int)):
        return None
    if "match" in raw:  # a flow selector (flow.selectors.describe)
        modes = flow_schema.MATCH_MODES
        if raw.get("match") not in modes:
            return None
        extra = set(raw) - set(_FLOW_FIELDS) - {"match", "index"}
        if extra:
            return None  # relational anchors are not replayed
        fields = {_FLOW_FIELDS[key]: value for key, value in raw.items() if key in _FLOW_FIELDS}
        mode, case_sensitive = modes[raw["match"]]
    else:  # a `ui tap` action detail
        extra = set(raw) - set(_DETAIL_FIELDS) - {"mode", "case_sensitive", "index"}
        if extra:
            return None
        fields = {key: value for key, value in raw.items()
                  if key in _DETAIL_FIELDS and value is not None}
        mode = raw.get("mode") or "contains"
        case_sensitive = bool(raw.get("case_sensitive"))
        if mode not in ("exact", "contains", "regex"):
            return None
    if not fields or not all(isinstance(value, (str, bool)) for value in fields.values()):
        return None
    return {"fields": fields, "mode": mode, "case_sensitive": case_sensitive, "index": index}


def _screen(target: Any) -> tuple[str, list[dict[str, Any]]]:
    nodes = ui_mod.snapshot(target)
    return fingerprint_mod.fingerprint(nodes)["structure"], nodes


def _replay_tap(session: dict[str, Any], target: Any, edge: dict[str, Any],
                selector: dict[str, Any]) -> str:
    """Tap the edge's selector on the current screen; the screen id after it."""
    _current, nodes = _screen(target)
    node = ui_mod.select_for_action(nodes, selector["fields"], mode=selector["mode"],
                                    case_sensitive=selector["case_sensitive"],
                                    index=selector["index"])
    if node is None:
        raise errors.AutonomError(errors.NO_MATCHING_NODE,
                                  "the recorded selector matched nothing on this screen",
                                  hint="The screen changed since the map recorded it.")
    x, y = ui_mod.center_of(node)
    ui_mod.tap(target, x, y, node=node)
    actions_mod.record_detail(session, "tap", {
        "kind": "tap", "coordinate": False, "origin": "warmup",
        "selector": {**selector["fields"], "mode": selector["mode"],
                     "case_sensitive": selector["case_sensitive"],
                     "index": selector["index"]},
        "node": node, "x": x, "y": y, "duration_ms": None, "nodes": nodes})
    ui_mod.settle(target, timeout_ms=SETTLE_TIMEOUT_MS, quiet_ms=SETTLE_QUIET_MS)
    return _screen(target)[0]


def _edge_view(edge: dict[str, Any]) -> dict[str, Any]:
    selector = selector_of(edge)
    return {"from": edge.get("from"), "to": edge.get("to"), "command": edge.get("command"),
            "selector": selector["fields"] if selector else None}


def _blocked(path: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((edge for edge in path if selector_of(edge) is None), None)


def _replay(session: dict[str, Any], target: Any, path: list[dict[str, Any]],
            ) -> tuple[str, str | None, int, str | None, errors.AutonomError | None]:
    """Replay a path: ``(status, reason, replayed, screen now, guard refusal)``."""
    replayed = 0
    current: str | None = None
    for edge in path:
        try:
            current = _replay_tap(session, target, edge, selector_of(edge))  # type: ignore[arg-type]
        except errors.AutonomError as exc:
            try:
                current = _screen(target)[0]
            except errors.AutonomError:
                current = None
            refusal = exc if exc.code in GUARD_CODES else None
            return "known", f"replay stopped: {exc.code}", replayed, current, refusal
        replayed += 1
        if current != edge.get("to"):
            return "known", "the replay ended on another screen", replayed, current, None
    return "reached", None, replayed, current, None


def walk(session: dict[str, Any], target: Any, graph: dict[str, Any],
         items: list[dict[str, Any]], *, skip_reason: str | None = None) -> dict[str, Any]:
    """Each hint with its status (see the module text).

    Statuses that need no replay are decided from the screen the walk starts
    on, so a hint's status does not depend on the order of the hints; the
    hints with a replayable path are then walked one after another, each from
    the screen the previous replay left."""
    screens = graph.get("screens") or {}
    current: str | None = None
    stop = skip_reason
    if stop is None:
        try:
            current = _screen(target)[0]
        except errors.AutonomError as exc:
            stop = f"the screen could not be read ({exc.code})"
    start = current
    results: list[dict[str, Any]] = []
    walkable: list[dict[str, Any]] = []
    for hint in items:
        screen_id = hint["screen_id"]
        out = dict(hint)
        results.append(out)
        if screen_id not in screens:
            out.update(status="unknown", reason="not in the Runtime Map")
            continue
        if stop is not None or start is None:
            out.update(status="known", reason=stop or "the screen could not be read")
            continue
        path = shortest_path(graph, start, screen_id)
        if path is None:
            out.update(status="known", reason="no recorded path from the current screen")
            continue
        out["path"] = [_edge_view(edge) for edge in path]
        if not path:
            out.update(status="reached", reason="already on screen", replayed=0)
            continue
        blocked = _blocked(path)
        if blocked is not None:
            out.update(status="unreplayable",
                       reason=f"the transition {blocked.get('command')} "
                              f"{blocked.get('from')}->{blocked.get('to')} is not a "
                              "recorded selector tap")
            continue
        walkable.append(out)
    refusals: list[dict[str, Any]] = []
    for out in walkable:
        screen_id = out["screen_id"]
        if stop is not None or current is None:
            out.update(status="known", reason=stop or "the screen could not be read")
            continue
        path = shortest_path(graph, current, screen_id)
        if path is None or _blocked(path) is not None:
            out.update(status="known",
                       reason="no replayable path from the screen the walk had reached")
            continue
        out["path"] = [_edge_view(edge) for edge in path]
        status, reason, replayed, current, refusal = _replay(session, target, path)
        out.update(status=status, replayed=replayed)
        if reason:
            out["reason"] = reason
        if refusal is not None:
            refusals.append({"error_code": refusal.code, "error": refusal.message,
                             "screen_id": screen_id})
            stop = f"walk stopped by {refusal.code}"
        elif current is None:
            stop = "the screen could not be read"
    counts = {name: sum(1 for item in results if item["status"] == name) for name in STATUSES}
    payload: dict[str, Any] = {"hints": results, "counts": counts, "current_screen": current}
    if refusals:
        payload["refusals"] = refusals
    return payload


# --- entry point -------------------------------------------------------------------------


def run(session: dict[str, Any], record: dict[str, Any], target: Any, *,
        config: Any = None) -> dict[str, Any]:
    """Warm the app up (see the module text)."""
    store.ensure_mutable(record)
    opened = checks_mod.open_check(session, record)
    if opened is not None:
        raise errors.AutonomError(
            errors.CHECK_ALREADY_OPEN, f"check {opened['check_id']} is still open",
            hint=f"End it first with 'autonom pretest check end {opened['check_id']} "
                 "--verdict <verdict>'; warm-up actions must not land in a check.",
            open_check_id=opened["check_id"])
    app_id = record.get("app_id")
    if not app_id:
        raise errors.AutonomError(
            errors.NO_TARGET, "the pre-test has no app id",
            hint="Start the session (or the pre-test) with --app-id.")
    profile = profile_mod.load(record.get("repo"), app_id)
    flows = _flows(profile)
    config = _secrets(profile, config)
    runs: list[dict[str, Any]] = []
    skip: str | None = None
    for role, path, flow in flows:
        if skip is not None:
            runs.append({"role": role, "flow": str(path), "run_id": None, "status": "skipped",
                         "failure": None, "reason": skip})
            continue
        run = _run_flow(target, session, config, role, path, flow)
        runs.append(run)
        if run["status"] != "passed":
            code = (run["failure"] or {}).get("error_code")
            skip = f"{role} did not pass ({run['status']}" + (f", {code})" if code else ")")
    runtime_map = update_map(session, app_id)
    graph = runtime_map.pop("_graph")
    walked = walk(session, target, graph, hints(session, record, app_id), skip_reason=skip)
    payload: dict[str, Any] = {
        "profile": {"path": profile["path"], "source": profile["source"]} if profile else None,
        "runs": runs, "runtime_map": runtime_map, **walked,
    }
    warnings: list[dict[str, Any]] = []
    if profile is None:
        warnings.append({
            "code": "no_app_profile",
            "error": "this app has no profile, so no login or warm-up flow ran",
            "hint": "Add .autonom/apps/<app-id>/profile.yaml with login_flow (see "
                    "docs/USAGE.md, 'App profile and warm-up')."})
    if runtime_map.get("state") == "unavailable":
        warnings.append({"code": "runtime_map_not_updated", "error": runtime_map["reason"],
                         "hint": "Check the map with 'autonom atlas show'; export it, then "
                                 "remove or repair graph.json so it can be rebuilt."})
    if skip is not None:
        warnings.append({"code": "warmup_incomplete", "error": skip,
                         "hint": "Read the run with 'autonom report model --run <run-id>', fix "
                                 "the flow or the device state, then warm up again."})
    if not walked["hints"]:
        warnings.append({"code": "no_screen_hints",
                         "error": "the Runtime Map gives no screen hint for this diff",
                         "hint": "Hints come from map screens whose labels match changed "
                                 "areas; run flows or taps first so the map knows screens."})
    if warnings:
        payload["warnings"] = warnings
    return payload
