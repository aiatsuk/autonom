"""The observed graph: storage, ingestion, queries (§10.1, §10.3, §10.4).

Stored machine-level under ``~/.autonom/apps/<app_id>/atlas/graph.json``
(0600, atomic writes) — the same knowledge home mobile-memory already uses.
Every screen and edge carries evidence references (session, run, step), and
coverage reports only what was observed; the absence of an edge means
*unknown*, never "impossible".
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from . import ATLAS_SCHEMA_VERSION
from .. import errors
from . import fingerprint as fingerprint_mod


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def apps_home() -> Path:
    home = os.environ.get("AUTONOM_HOME")
    base = Path(home) if home else Path.home() / ".autonom"
    return base / "apps"


def atlas_dir(app_id: str) -> Path:
    path = apps_home() / app_id / "atlas"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    os.chmod(path, 0o700)
    return path


def graph_path(app_id: str) -> Path:
    return atlas_dir(app_id) / "graph.json"


def load(app_id: str) -> dict[str, Any]:
    path = graph_path(app_id)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"schema_version": ATLAS_SCHEMA_VERSION, "app_id": app_id,
            "screens": {}, "transitions": {}, "updated_at": None}


def save(app_id: str, graph: dict[str, Any]) -> Path:
    graph["updated_at"] = _now()
    path = graph_path(app_id)
    scratch = path.with_suffix(".tmp")
    scratch.write_text(json.dumps(graph, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    os.chmod(scratch, 0o600)
    os.replace(scratch, path)
    return path


# --- ingestion ---------------------------------------------------------------


def _touch_screen(graph: dict[str, Any], screen: dict[str, Any],
                  evidence: dict[str, Any]) -> str:
    screen_id = screen["structure"]
    entry = graph["screens"].setdefault(screen_id, {
        "screen_id": screen_id, "labels": screen.get("labels", []),
        "first_seen": _now(), "last_seen": None,
        "variants": {}, "sessions": [],
    })
    entry["last_seen"] = _now()
    for label in screen.get("labels", []):
        if label not in entry["labels"]:
            entry["labels"].append(label)
    entry["labels"] = entry["labels"][:5]
    variant = entry["variants"].setdefault(screen["state"], {
        "first_seen": _now(), "last_seen": None, "count": 0})
    variant["last_seen"] = _now()
    variant["count"] += 1
    session_id = evidence.get("session_id")
    if session_id and session_id not in entry["sessions"]:
        entry["sessions"] = (entry["sessions"] + [session_id])[-10:]
    return screen_id


def _touch_transition(graph: dict[str, Any], source: str, target: str,
                      command: str, detail: str | None,
                      ok: bool, evidence: dict[str, Any]) -> None:
    if source == target:
        return  # an action that did not change the screen is not an edge
    key = f"{source}->{target}::{command}"
    entry = graph["transitions"].setdefault(key, {
        "from": source, "to": target, "command": command, "detail": detail,
        "success": 0, "failure": 0,
        "first_seen": _now(), "last_seen": None, "evidence": [],
    })
    entry["last_seen"] = _now()
    entry["success" if ok else "failure"] += 1
    reference = {k: v for k, v in evidence.items() if v is not None}
    if reference and reference not in entry["evidence"]:
        entry["evidence"] = (entry["evidence"] + [reference])[-10:]


def ingest_flow_events(graph: dict[str, Any], events: list[dict[str, Any]],
                       session_id: str | None) -> int:
    """Consecutive step fingerprints become screens and edges."""
    added = 0
    previous_screen: str | None = None
    pending: dict[str, Any] | None = None  # the step leaving previous_screen
    for event in events:
        if event.get("kind") != "flow.step.finished":
            continue
        payload = event.get("payload") or {}
        screen = payload.get("screen")
        evidence = {"session_id": session_id,
                    "run_id": event.get("run_id"),
                    "step_index": payload.get("step_index")}
        if pending is not None and screen:
            target = _touch_screen(graph, screen, evidence)
            _touch_transition(
                graph, pending["from"], target, pending["command"],
                pending.get("detail"), pending["ok"], evidence)
            added += 1
            pending = None
        if screen:
            previous_screen = _touch_screen(graph, screen, evidence)
        if previous_screen and payload.get("command") in (
                "tapOn", "longPressOn", "doubleTapOn", "swipe", "back",
                "openLink", "pressKey", "scrollUntilVisible", "launchApp"):
            pending = {
                "from": previous_screen,
                "command": payload.get("command"),
                "detail": json.dumps(payload.get("selector"),
                                     ensure_ascii=False)
                if payload.get("selector") else None,
                "ok": payload.get("status") == "passed",
            }
    return added


def ingest_action_details(graph: dict[str, Any],
                          details: list[dict[str, Any]],
                          session_id: str | None) -> int:
    """Manual sessions: each tap detail carries the tree seen *before* it."""
    added = 0
    pending: dict[str, Any] | None = None
    for detail in details:
        if detail.get("kind") != "tap" or not detail.get("nodes"):
            continue
        screen = fingerprint_mod.fingerprint(detail["nodes"])
        evidence = {"session_id": session_id}
        screen_id = _touch_screen(graph, screen, evidence)
        if pending is not None:
            _touch_transition(graph, pending["from"], screen_id,
                              pending["command"], pending.get("detail"),
                              True, evidence)
            added += 1
        pending = {
            "from": screen_id,
            "command": "tapOn",
            "detail": json.dumps(detail.get("selector"), ensure_ascii=False)
            if detail.get("selector") else None,
        }
    return added


# --- queries -----------------------------------------------------------------


def summary(graph: dict[str, Any]) -> dict[str, Any]:
    screens = graph.get("screens", {})
    transitions = graph.get("transitions", {})
    return {
        "app_id": graph.get("app_id"),
        "screens": len(screens),
        "variants": sum(len(s.get("variants", {})) for s in screens.values()),
        "transitions": len(transitions),
        "updated_at": graph.get("updated_at"),
        "screen_list": [
            {"screen_id": sid, "labels": s.get("labels", []),
             "variants": len(s.get("variants", {})),
             "last_seen": s.get("last_seen")}
            for sid, s in sorted(screens.items())
        ],
    }


def coverage(graph: dict[str, Any]) -> dict[str, Any]:
    edges = [
        {"from": t["from"], "to": t["to"], "command": t["command"],
         "success": t["success"], "failure": t["failure"],
         "last_seen": t["last_seen"],
         "evidence": t.get("evidence", [])[-3:]}
        for t in graph.get("transitions", {}).values()
    ]
    lonely = [sid for sid, s in graph.get("screens", {}).items()
              if not any(e["from"] == sid or e["to"] == sid for e in edges)]
    return {
        "observed_screens": len(graph.get("screens", {})),
        "observed_transitions": len(edges),
        "edges": sorted(edges, key=lambda e: (e["from"], e["to"])),
        "screens_without_observed_edges": lonely,
        "note": "absence of an edge means unobserved, not impossible",
    }


def _match_screen(graph: dict[str, Any], query: str) -> list[str]:
    query_low = query.lower()
    out = []
    for sid, screen in graph.get("screens", {}).items():
        if sid == query or any(query_low in label.lower()
                               for label in screen.get("labels", [])):
            out.append(sid)
    return out


def paths(graph: dict[str, Any], source_query: str,
          target_query: str) -> dict[str, Any]:
    sources = _match_screen(graph, source_query)
    targets = set(_match_screen(graph, target_query))
    if not sources or not targets:
        raise errors.AutonomError(
            errors.FLOW_NO_FLOWS_FOUND,
            f"no observed screen matches "
            f"{source_query if not sources else target_query!r}",
            hint="Match by screen id or a label substring; see 'atlas show'.",
        )
    adjacency: dict[str, list[dict[str, Any]]] = {}
    for transition in graph.get("transitions", {}).values():
        adjacency.setdefault(transition["from"], []).append(transition)
    best: list[list[dict[str, Any]]] = []
    for start in sources:
        queue: list[tuple[str, list[dict[str, Any]]]] = [(start, [])]
        seen = {start}
        while queue:
            current, walked = queue.pop(0)
            if current in targets and walked:
                best.append(walked)
                break
            for edge in adjacency.get(current, []):
                if edge["to"] in seen:
                    continue
                seen.add(edge["to"])
                queue.append((edge["to"], walked + [edge]))
    return {
        "from": sources, "to": sorted(targets),
        "paths": [
            [{"from": e["from"], "to": e["to"], "command": e["command"]}
             for e in path]
            for path in best
        ],
    }


def diff(base: dict[str, Any], head: dict[str, Any]) -> dict[str, Any]:
    base_screens = set(base.get("screens", {}))
    head_screens = set(head.get("screens", {}))
    base_edges = set(base.get("transitions", {}))
    head_edges = set(head.get("transitions", {}))
    return {
        "screens_added": sorted(head_screens - base_screens),
        "screens_removed": sorted(base_screens - head_screens),
        "transitions_added": sorted(head_edges - base_edges),
        "transitions_removed": sorted(base_edges - head_edges),
    }
