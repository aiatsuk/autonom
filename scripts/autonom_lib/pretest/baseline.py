"""Device baseline at pre-test start (tp-005).

Health deltas are only honest against a recorded starting point: the newest
crash, the log cursor, how many network flows were already captured, and one
metrics snapshot. A capability that cannot be read is recorded as
``{"state": "unavailable", "reason": ...}`` — never as zero or empty, which
would later read as "nothing happened".
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .. import contracts, device_state, logs
from ..metrics import snapshot as snapshot_mod
from ..network import store as network_store
from . import BASELINE_SCHEMA

MAX_CRASH_NAMES = 500


def _unavailable(reason: str) -> dict[str, Any]:
    from . import store

    # A device error can echo a URL or a header: mask it, then cut it.
    return {"state": "unavailable", "reason": store.clip(reason, 300) or "unavailable"}


def _reason(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return f"{code}: {message}" if code else message


def crash_name(entry: dict[str, Any]) -> str:
    """A stable id for a crash entry. Android's crash buffer has no file
    names, so its line stands in, hashed."""
    name = entry.get("name")
    if name:
        return str(name)
    raw = str(entry.get("raw") or "")
    return "logcat:" + hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:12]


def _crashes(target: Any, app_id: str | None) -> dict[str, Any]:
    if target is None or not hasattr(target, "platform"):
        return _unavailable("no device target to read crashes from")
    try:
        entries = device_state.crash_list(target, app_id)
    except Exception as exc:  # noqa: BLE001 — recorded, never fatal
        return _unavailable(_reason(exc))
    names = [crash_name(entry) for entry in entries]
    latest = None
    if entries:
        newest = max(enumerate(entries),
                     key=lambda pair: (str(pair[1].get("date") or ""), pair[0]))[1]
        latest = {"name": crash_name(newest), "time": newest.get("date")}
    return {"state": "available", "latest": latest, "names": names[-MAX_CRASH_NAMES:]}


def _line_count(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def _logs(session: dict[str, Any] | None) -> dict[str, Any]:
    block: dict[str, Any] = {"state": "available", "cursor_ts": contracts.utc_now()}
    if session:
        try:
            stream = logs.stream_destination(session)
            if stream.is_file():
                block["stream"] = "logs/stream.ndjson"
                block["stream_lines"] = _line_count(stream)
        except Exception:  # noqa: BLE001
            pass
    return block


def _network(session: dict[str, Any] | None) -> dict[str, Any]:
    if not session:
        return _unavailable("no session")
    try:
        path = network_store.flows_path(session)
        if path.is_file():
            return {"state": "available", "flows_line_count": _line_count(path)}
    except Exception as exc:  # noqa: BLE001
        return _unavailable(_reason(exc))
    if (session.get("network") or {}).get("enabled"):
        return {"state": "available", "flows_line_count": 0}
    return _unavailable("network capture is not running in this session "
                        "(start it with 'autonom network start')")


def _metrics(target: Any, app_id: str | None) -> dict[str, Any]:
    if not app_id:
        return _unavailable("no app id; metrics are per app")
    if target is None or not hasattr(target, "platform"):
        return _unavailable("no device target to read metrics from")
    try:
        payload, _raw = snapshot_mod.take(target, app_id)
    except Exception as exc:  # noqa: BLE001
        return _unavailable(_reason(exc))
    snapshot = {key: payload.get(key) for key in
                ("captured_at", "platform", "pid", "metric_semantics", "memory", "cpu")
                if key in payload}
    return {"state": "available", "snapshot": contracts.redact_value(snapshot)}


def capture(target: Any, session: dict[str, Any] | None, app_id: str | None) -> dict[str, Any]:
    """``autonom.pretest-baseline/v1``. Never raises: each part is recorded
    as available or unavailable on its own."""
    return {
        "schema": BASELINE_SCHEMA,
        "taken_at": contracts.utc_now(),
        "crashes": _crashes(target, app_id),
        "logs": _logs(session),
        "network": _network(session),
        "metrics": _metrics(target, app_id),
    }
