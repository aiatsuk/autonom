"""Teach recorder state, marker ranges, review, validation, and approval."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from . import errors, journal
from .contracts import canonical_json, fresh_id, utc_now
from .flow import compiler, validator

STATE_FILE = "teach.json"


def _path(session: dict[str, Any]) -> Path:
    return Path(session["artifacts_dir"]) / STATE_FILE


def _write(session: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    path = _path(session)
    path.write_bytes(canonical_json(state) + b"\n")
    os.chmod(path, 0o600)
    return state


def load(session: dict[str, Any]) -> dict[str, Any]:
    path = _path(session)
    if not path.is_file():
        return {"schema": "autonom.teach/v1", "recordings": []}
    return json.loads(path.read_text(encoding="utf-8"))


def start(session: dict[str, Any], name: str) -> dict[str, Any]:
    state = load(session)
    if any(item.get("status") == "recording" for item in state["recordings"]):
        raise errors.AutonomError(errors.TEACH_RANGE_INVALID,
                                  "a Teach recording is already active")
    before, _ = journal.read(session, max_entries=100_000)
    recording = {
        "recording_id": fresh_id("teach"), "name": name,
        "status": "recording", "started_at": utc_now(),
        "start_seq": (before[-1]["seq"] + 1) if before else 1,
        "markers": [],
    }
    state["recordings"].append(recording)
    journal.append(session, {"kind": "teach", "event": "start",
                             "recording_id": recording["recording_id"],
                             "name": name, "origin": "human"})
    # The marker itself is metadata, not part of the compilable range.
    recording["start_seq"] += 1
    _write(session, state)
    return recording


def _active(state: dict[str, Any]) -> dict[str, Any]:
    item = next((item for item in reversed(state.get("recordings") or [])
                 if item.get("status") == "recording"), None)
    if not item:
        raise errors.AutonomError(errors.TEACH_RANGE_INVALID,
                                  "there is no active Teach recording",
                                  hint="Start one with 'autonom teach start <name>'.")
    return item


def mark(session: dict[str, Any], name: str) -> dict[str, Any]:
    state = load(session)
    recording = _active(state)
    journal.append(session, {"kind": "teach", "event": "marker",
                             "recording_id": recording["recording_id"],
                             "name": name, "origin": "human"})
    entries, _ = journal.read(session, max_entries=100_000)
    marker = {"name": name, "seq": entries[-1]["seq"], "at": utc_now()}
    recording["markers"].append(marker)
    _write(session, state)
    return marker


def stop(session: dict[str, Any]) -> dict[str, Any]:
    state = load(session)
    recording = _active(state)
    entries, _ = journal.read(session, max_entries=100_000)
    recording["end_seq"] = entries[-1]["seq"] if entries else recording["start_seq"]
    recording["stopped_at"] = utc_now()
    recording["status"] = "recorded"
    journal.append(session, {"kind": "teach", "event": "stop",
                             "recording_id": recording["recording_id"],
                             "origin": "human"})
    _write(session, state)
    return recording


def resolve_range(session: dict[str, Any], recording_id: str | None = None,
                  from_marker: str | None = None,
                  to_marker: str | None = None) -> tuple[dict[str, Any], int, int]:
    state = load(session)
    candidates = [item for item in state.get("recordings") or []
                  if item.get("status") != "recording"]
    if recording_id:
        candidates = [item for item in candidates
                      if item.get("recording_id") == recording_id]
    if not candidates:
        raise errors.AutonomError(errors.TEACH_RANGE_INVALID,
                                  "no completed Teach recording matches")
    recording = candidates[-1]
    markers = {item["name"]: item["seq"] for item in recording.get("markers") or []}
    start_seq = markers.get(from_marker, recording["start_seq"])
    end_seq = markers.get(to_marker, recording["end_seq"])
    if from_marker and from_marker not in markers:
        raise errors.AutonomError(errors.TEACH_RANGE_INVALID,
                                  f"unknown start marker {from_marker!r}")
    if to_marker and to_marker not in markers:
        raise errors.AutonomError(errors.TEACH_RANGE_INVALID,
                                  f"unknown end marker {to_marker!r}")
    if start_seq > end_seq:
        raise errors.AutonomError(errors.TEACH_RANGE_INVALID,
                                  "Teach range starts after it ends")
    return recording, start_seq, end_seq


def compile_recording(session: dict[str, Any], *, out: Path,
                      recording_id: str | None = None,
                      from_marker: str | None = None,
                      to_marker: str | None = None) -> dict[str, Any]:
    recording, start_seq, end_seq = resolve_range(
        session, recording_id, from_marker, to_marker)
    text, quality = compiler.compile_to_text(
        session, name=recording["name"], task=recording["name"],
        start_seq=start_seq, end_seq=end_seq)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    validator.validate_tree(out)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    state = load(session)
    target = next(item for item in state["recordings"]
                  if item["recording_id"] == recording["recording_id"])
    target.update({"status": "compiled", "flow": str(out),
                   "flow_sha256": digest, "quality": quality})
    _write(session, state)
    return {"recording_id": recording["recording_id"], "out": str(out),
            "flow_sha256": digest, **quality}


def approve(session: dict[str, Any], flow_path: Path, *, minimum_runs: int = 3) -> dict[str, Any]:
    flow = validator.validate_tree(flow_path)
    manifests = []
    for path in sorted((Path(session["artifacts_dir"]) / "flows").glob("*/manifest.json"),
                       key=lambda item: item.stat().st_mtime, reverse=True):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("flow_id") == flow.flow_id:
            manifests.append(value)
    consecutive = []
    for manifest in manifests:
        if manifest.get("status") != "passed":
            break
        consecutive.append(manifest)
    if len(consecutive) < minimum_runs:
        raise errors.AutonomError(
            errors.TEACH_APPROVAL_BLOCKED,
            f"Teach approval requires {minimum_runs} consecutive clean replays",
            hint=f"Run the compiled flow until it has {minimum_runs} consecutive passes.",
            flow_id=flow.flow_id, clean_replays=len(consecutive), required=minimum_runs,
        )
    receipt = {
        "schema": "autonom.teach-approval/v1", "approved_at": utc_now(),
        "flow": str(flow_path), "flow_id": flow.flow_id,
        "clean_replays": [item["run_id"] for item in consecutive[:minimum_runs]],
    }
    receipt_path = flow_path.with_suffix(flow_path.suffix + ".approved.json")
    receipt_path.write_bytes(canonical_json(receipt) + b"\n")
    return {**receipt, "receipt": str(receipt_path)}
