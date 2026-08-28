"""Flow store: read, filter, and bound what an agent sees (CAP-NET-003, CAP-NET-005).

The store is append-only JSONL, so a proxy crash costs at most the final partial
line — which the reader skips with a warning rather than failing.

Listing is capped (default 50) and reports `total_matched` plus `truncated`, so an
agent is never silently shown a subset and left to conclude the rest do not exist.
"""
from __future__ import annotations

import fnmatch
import json
import time
from pathlib import Path
from typing import Any

from .. import errors
from . import redact

DEFAULT_MAX = 50


def flows_path(record: dict[str, Any]) -> Path:
    return Path(record["artifacts_dir"]) / "network" / "flows.jsonl"


def read_all(record: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    path = flows_path(record)
    warnings: list[dict[str, Any]] = []
    if not path.exists():
        return [], warnings
    flows: list[dict[str, Any]] = []
    truncated = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            flows.append(redact.scrub_flow(json.loads(line)))
        except json.JSONDecodeError:
            truncated += 1
    if truncated:
        warnings.append({
            "code": "truncated_flow_record",
            "error": f"skipped {truncated} unparseable line(s)",
            "hint": "The proxy was probably interrupted mid-write; earlier flows are intact.",
        })
    return flows, warnings


def _since_cutoff(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))


def filter_flows(
    flows: list[dict[str, Any]],
    *,
    host: str | None = None,
    method: str | None = None,
    status: int | None = None,
    path_glob: str | None = None,
    since_seconds: float | None = None,
    mocked: bool | None = None,
) -> list[dict[str, Any]]:
    cutoff = _since_cutoff(since_seconds) if since_seconds else None
    result = []
    for flow in flows:
        if host and (flow.get("host") or "").lower() != host.lower():
            continue
        if method and (flow.get("method") or "").upper() != method.upper():
            continue
        if status is not None and flow.get("status") != status:
            continue
        if path_glob and not (
            fnmatch.fnmatch(flow.get("path") or "", path_glob)
            or fnmatch.fnmatch(flow.get("url") or "", path_glob)
        ):
            continue
        if cutoff and (flow.get("started_at") or "") < cutoff:
            continue
        if mocked is not None and bool(flow.get("mocked")) is not mocked:
            continue
        result.append(flow)
    return result


def after_id(flows: list[dict[str, Any]], since_id: str,
             warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flows appended after `since_id` (store order). An unknown id returns
    everything with a warning — silently returning nothing would read as
    'no new traffic' when the truth is 'your cursor is gone'."""
    for index, flow in enumerate(flows):
        if flow.get("id") == since_id:
            return flows[index + 1:]
    warnings.append({
        "code": "since_id_not_found",
        "error": f"no recorded flow with id {since_id}; returning all flows",
        "hint": "The store may have been cleared; take a new cursor from the "
                "latest listed id.",
    })
    return flows


def listing(
    record: dict[str, Any],
    *,
    max_items: int = DEFAULT_MAX,
    since_id: str | None = None,
    **filters: Any,
) -> dict[str, Any]:
    flows, warnings = read_all(record)
    if since_id:
        flows = after_id(flows, since_id, warnings)
    matched = filter_flows(flows, **filters)
    limited = matched[-max_items:][::-1] if max_items and max_items > 0 else matched[::-1]
    payload: dict[str, Any] = {
        "count": len(limited),
        "total_matched": len(matched),
        "truncated": len(limited) < len(matched),
        "requests": limited,
    }
    if warnings:
        payload["warnings"] = warnings
    return payload


def find(record: dict[str, Any], flow_id: str) -> dict[str, Any]:
    flows, _warnings = read_all(record)
    for flow in flows:
        if flow.get("id") == flow_id:
            return flow
    raise errors.AutonomError(
        errors.FLOW_NOT_FOUND,
        f"no recorded flow with id {flow_id}",
        "List them with 'autonom network requests list'.",
    )


def body(record: dict[str, Any], flow_id: str, which: str) -> bytes | None:
    path = Path(record["artifacts_dir"]) / "network" / "bodies" / f"{flow_id}.{which}"
    return path.read_bytes() if path.exists() else None


def require_bodies(record: dict[str, Any]) -> None:
    directory = Path(record["artifacts_dir"]) / "network" / "bodies"
    if not directory.exists():
        raise errors.AutonomError(
            errors.BODIES_NOT_CAPTURED,
            "full bodies were not captured for this session",
            "Restart the proxy with 'autonom network start --capture-bodies'. "
            "Bodies are off by default because they are the densest source of "
            "credentials and personal data.",
        )
