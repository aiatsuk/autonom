"""Flow store: read, filter, and bound what an agent sees (CAP-NET-003, CAP-NET-005).

The store is append-only JSONL, so a proxy crash costs at most the final partial
line — which the reader skips with a warning rather than failing. A line that
is JSON but not an object is skipped the same way, and counted.

Listing is capped (default 50) and reports `total_matched` plus `truncated`, so an
agent is never silently shown a subset and left to conclude the rest do not exist.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .. import errors
from . import redact

DEFAULT_MAX = 50
# A store line nested deeper than this is no record the tool wrote: json.loads
# may still read it, but str(), repr() or the scrubbers then raise
# RecursionError, so readers skip it like a line that is not a record.
MAX_LINE_DEPTH = 200


def too_deep(value: Any, limit: int = MAX_LINE_DEPTH) -> bool:
    """Whether lists and objects in ``value`` nest more than ``limit`` levels.
    Walks without recursion, so it never raises RecursionError itself."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if not isinstance(item, (dict, list)):
            continue
        if depth > limit:
            return True
        children = item.values() if isinstance(item, dict) else item
        stack.extend((child, depth + 1) for child in children)
    return False


def flows_path(record: dict[str, Any]) -> Path:
    return Path(record["artifacts_dir"]) / "network" / "flows.jsonl"


def read_all_counted(
    record: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Every readable flow record, the warnings, and how many lines were skipped.

    A line is skipped, never fatal, when it is not JSON (a torn final write)
    or is JSON but not an object (`[]`, `"x"`, `3`): readers such as
    `pretest events` and `network requests` index the records as mappings.
    An object nested more than `MAX_LINE_DEPTH` levels is counted with the
    non-objects (printing or scrubbing it would raise RecursionError), and so
    is a line `json.loads` itself refuses with RecursionError: only a
    `json.JSONDecodeError` counts as a torn write.
    """
    path = flows_path(record)
    warnings: list[dict[str, Any]] = []
    if not path.exists():
        return [], warnings, 0
    flows: list[dict[str, Any]] = []
    truncated = 0
    not_objects = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            truncated += 1  # not JSON: a torn final write
            continue
        except (ValueError, RecursionError):
            # Valid JSON the parser refuses: nested past its own limit (on
            # older Pythons json.loads raises RecursionError where newer ones
            # parse and `too_deep` refuses) or a number past the digit limit.
            # Not a torn write — counted with the non-objects on every version.
            not_objects += 1
            continue
        if not isinstance(parsed, dict) or too_deep(parsed):
            not_objects += 1
            continue
        try:
            flows.append(redact.scrub_flow(parsed))
        except (TypeError, ValueError, AttributeError, RecursionError):
            not_objects += 1
    if truncated:
        warnings.append({
            "code": "truncated_flow_record",
            "error": f"skipped {truncated} unparseable line(s)",
            "hint": "The proxy was probably interrupted mid-write; earlier flows are intact.",
            "skipped": truncated,
        })
    if not_objects:
        warnings.append({
            "code": "invalid_flow_record",
            "error": f"skipped {not_objects} line(s) that are not flow records (JSON objects)",
            "hint": "network/flows.jsonl was edited or written by something other than "
                    "the proxy; the other flows are intact.",
            "skipped": not_objects,
        })
    return flows, warnings, truncated + not_objects


def read_all(record: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`read_all_counted` without the count; each skip warning carries `skipped`."""
    flows, warnings, _skipped = read_all_counted(record)
    return flows, warnings


def skipped_count(warnings: list[dict[str, Any]]) -> int:
    """Lines `read_all` skipped, from its warnings."""
    return sum(int(item.get("skipped") or 0) for item in warnings
               if isinstance(item, dict) and item.get("code") in SKIP_WARNING_CODES)


SKIP_WARNING_CODES = ("truncated_flow_record", "invalid_flow_record")


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
        if host and str(flow.get("host") or "").lower() != host.lower():
            continue
        if method and str(flow.get("method") or "").upper() != method.upper():
            continue
        if status is not None and flow.get("status") != status:
            continue
        if path_glob and not (
            fnmatch.fnmatch(str(flow.get("path") or ""), path_glob)
            or fnmatch.fnmatch(str(flow.get("url") or ""), path_glob)
        ):
            continue
        if cutoff and str(flow.get("started_at") or "") < cutoff:
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
    flows, warnings, skipped = read_all_counted(record)
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
    if skipped:
        payload["skipped"] = skipped
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


COPY_BODY_LIMIT = 4 * 1024 * 1024


def copy_body(record: dict[str, Any], flow_id: str, which: str) -> dict[str, Any]:
    """On-demand redacted full text. Never substitute a truncated preview."""
    flow = find(record, flow_id)
    copies = flow.get("body_copy")
    state = copies.get(which) if isinstance(copies, dict) else None
    if state == "saved" and re.fullmatch(r"f_[0-9]+", flow_id):
        suffix = "req" if which == "request" else "res"
        path = Path(record["artifacts_dir"]) / "network" / "copy-bodies" / f"{flow_id}.{suffix}"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as handle:
                content = handle.read(COPY_BODY_LIMIT + 1)
            if len(content) > COPY_BODY_LIMIT:
                return {"complete": False, "reason": "too_large"}
            return {"complete": True, "body": redact.scrub_body(content.decode("utf-8"))}
        except (OSError, UnicodeError, ValueError, RecursionError):
            return {"complete": False, "reason": "unavailable"}
    # Older captures can still copy complete small bodies, but not lost prefixes.
    preview = flow.get(f"{which}_body_preview")
    if isinstance(preview, str) and not preview.endswith(redact.TRUNCATION_MARKER):
        return {"complete": True, "body": redact.scrub_body(preview)}
    return {"complete": False, "reason": state or "not_captured"}
