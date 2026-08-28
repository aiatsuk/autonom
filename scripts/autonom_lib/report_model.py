"""Canonical Report Model v2 compiler.

The existing executor manifest remains a capture-oriented ledger.  This module
turns it into a portable, UI/exporter-neutral domain model with explicit test
identity, attempt identity, execution/proof axes, typed evidence, setup, Delta,
and replay metadata.
"""
from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

from . import errors
from .contracts import (
    EXECUTION_STATUSES,
    PROOF_VERDICTS,
    REPORT_SCHEMA,
    execution_status,
    fresh_id,
    history_id,
    proof_verdict,
    stable_id,
    utc_now,
)


def _attachment_type(path: str, ledger_kind: str | None = None) -> str:
    kind = ledger_kind or ""
    lower = path.lower()
    if "screenshot" in kind or lower.endswith((".png", ".jpg", ".jpeg", ".webp")):
        return "screenshot"
    if "hierarchy" in kind or lower.endswith((".xml", ".tree.json")):
        return "ui_tree"
    if "network" in kind or lower.endswith((".har", ".har.json")):
        return "network"
    if "log" in kind or lower.endswith((".log", ".txt")):
        return "log"
    if lower.endswith((".mp4", ".mov", ".webm")):
        return "video"
    if lower.endswith((".trace", ".perfetto-trace")):
        return "trace"
    if lower.endswith((".json", ".ndjson", ".jsonl")):
        return "structured_data"
    return "file"


def _typed_attachments(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    by_path = {str(item.get("path")): item
               for item in manifest.get("artifact_steps") or []
               if item.get("path")}
    attachments: list[dict[str, Any]] = []
    paths = set(str(path) for path in manifest.get("artifacts") or [])
    paths.update(by_path)
    for path in sorted(paths):
        ledger = by_path.get(path, {})
        mime, _encoding = mimetypes.guess_type(path)
        attachment = {
            "attachment_id": stable_id("att", manifest.get("run_id"), path),
            "type": _attachment_type(path, ledger.get("kind")),
            "path": path,
            "name": Path(path).name,
            "mime_type": mime or "application/octet-stream",
            "availability": "available",
        }
        for source, target in (
            ("kind", "capture_kind"), ("step_index", "step_index"),
            ("step_id", "step_id"), ("phase", "phase"),
        ):
            if ledger.get(source) is not None:
                attachment[target] = ledger[source]
        attachments.append(attachment)
    return attachments


def _delta_for_step(step: dict[str, Any], attachments: list[dict[str, Any]],
                    manifest: dict[str, Any]) -> dict[str, Any]:
    index = step.get("index")
    owned = [item for item in attachments
             if item.get("step_index") == index]
    before = [item["attachment_id"] for item in owned
              if str(item.get("capture_kind", "")).endswith("-before")]
    after = [item["attachment_id"] for item in owned
             if str(item.get("capture_kind", "")).endswith("-after")]
    ui_before = [item for item in before if _by_id(attachments, item)["type"]
                 in ("screenshot", "ui_tree")]
    ui_after = [item for item in after if _by_id(attachments, item)["type"]
                in ("screenshot", "ui_tree")]
    logs = [item["attachment_id"] for item in owned if item["type"] == "log"]
    requests = [item["attachment_id"] for item in owned
                if item["type"] == "network"]
    network = manifest.get("network") or {}
    collected = set(manifest.get("evidence_collect") or [])
    precondition = step.get("precondition_fingerprint")
    postcondition = step.get("postcondition_fingerprint")
    ui_available = bool(ui_before and ui_after)
    return {
        "before": before,
        "after": after,
        "ui": {
            "before": ui_before,
            "after": ui_after,
            "availability": (
                "available" if ui_available else
                "partial" if ui_before or ui_after else "unavailable"),
            "changed": (
                precondition != postcondition
                if precondition is not None and postcondition is not None
                else None),
        },
        "logs": {"attachment_ids": logs, "count": len(logs),
                 "availability": (
                     "available" if logs else "zero" if "logs" in collected
                     else "unavailable")},
        "requests": {
            "attachment_ids": requests, "count": len(requests),
            "availability": (
                "available" if requests else
                "zero" if network.get("captured") or network.get("available")
                else "unavailable"),
        },
        "selector_receipt": step.get("selector"),
        "target": step.get("target"),
        "precondition_fingerprint": precondition,
        "postcondition_fingerprint": postcondition,
        "complete": True,
    }


def _by_id(attachments: list[dict[str, Any]], attachment_id: str) -> dict[str, Any]:
    return next(item for item in attachments
                if item["attachment_id"] == attachment_id)


def compile_manifest(manifest: dict[str, Any], *, revision: int = 1,
                     created_at: str | None = None) -> dict[str, Any]:
    if not manifest.get("run_id"):
        raise errors.AutonomError(
            errors.REPORT_MODEL_INVALID, "manifest has no run_id")
    app_id = manifest.get("app_id")
    flow_id = manifest.get("flow_id") or stable_id(
        "flow", app_id or "unknown", manifest.get("flow_path") or manifest.get("flow_name"))
    case_id = stable_id("case", app_id or "unknown", flow_id)
    attempt_id = manifest.get("attempt_id") or stable_id(
        "attempt", manifest.get("session_id"), manifest["run_id"])
    status = manifest.get("execution_status") or execution_status(
        manifest.get("status"), manifest.get("primary_error"))
    verdict = manifest.get("proof_verdict") or proof_verdict(status)
    attachments = _typed_attachments(manifest)
    steps: list[dict[str, Any]] = []
    action_attempts: list[dict[str, Any]] = []
    for raw in manifest.get("steps") or []:
        step_status = execution_status(raw.get("status"), {
            "failure_class": raw.get("failure_class")})
        step = {
            "step_id": raw.get("step_id") or stable_id(
                "step", attempt_id, raw.get("index")),
            "source_id": raw.get("source_id"),
            "index": raw.get("index"),
            "name": raw.get("label") or raw.get("command") or "step",
            "command": raw.get("command"),
            "status": step_status,
            "started_at_ms": raw.get("started_at_ms"),
            "finished_at_ms": raw.get("finished_at_ms"),
            "duration_ms": raw.get("duration_ms", 0),
            "attempt_count": raw.get("attempts", 1),
            "retry_attempt": raw.get("retry_attempt"),
            "parent_index": raw.get("parent_index"),
            "depth": raw.get("depth", 0),
            "error": ({
                "code": raw.get("error_code"),
                "class": raw.get("failure_class"),
                "message": raw.get("error"),
            } if raw.get("error_code") or raw.get("error") else None),
        }
        step["delta"] = _delta_for_step(raw, attachments, manifest)
        step["attachment_ids"] = [item["attachment_id"] for item in attachments
                                  if item.get("step_index") == raw.get("index")]
        steps.append(step)
        action_attempts.append({
            "action_attempt_id": stable_id(
                "action-attempt", attempt_id, step["step_id"],
                raw.get("retry_attempt"), raw.get("index")),
            "attempt_id": attempt_id,
            "step_id": step["step_id"],
            "sequence": raw.get("index"),
            "retry_attempt": raw.get("retry_attempt"),
            "status": step_status,
            "started_at_ms": raw.get("started_at_ms"),
            "finished_at_ms": raw.get("finished_at_ms"),
            "duration_ms": raw.get("duration_ms", 0),
            "error": step["error"],
            "attachment_ids": list(step["attachment_ids"]),
        })

    safe_parameters = manifest.get("history_parameters") or []
    parameters = manifest.get("properties") or {}
    replay = manifest.get("replay") or {
        "mode": "baseline",
        "portable_restore": "replay-from-flow-start",
        "source_run_id": manifest.get("parent_run_id"),
    }
    setup = manifest.get("setup") or {}
    model = {
        "schema": REPORT_SCHEMA,
        "revision": revision,
        "created_at": created_at or utc_now(),
        "launch": {
            "launch_id": stable_id("launch", manifest.get("session_id")),
            "session_id": manifest.get("session_id"),
            "campaign_id": manifest.get("campaign_id"),
            "shard_id": manifest.get("shard_id"),
        },
        "test_case": {
            "case_id": case_id,
            "history_id": history_id(app_id, flow_id, parameters, safe_parameters),
            "flow_id": flow_id,
            "app_id": app_id,
            "name": manifest.get("flow_name") or flow_id,
            "description": manifest.get("description"),
            "tags": manifest.get("tags") or [],
            "parameters": {name: parameters[name] for name in safe_parameters
                           if name in parameters},
        },
        "attempt": {
            "attempt_id": attempt_id,
            "run_id": manifest["run_id"],
            "parent_attempt_id": manifest.get("parent_attempt_id"),
            "retry_of": manifest.get("retry_of"),
            "status": status,
            "proof_verdict": verdict,
            "started_at_ms": manifest.get("started_at_ms"),
            "finished_at_ms": manifest.get("finished_at_ms"),
            "primary_error": manifest.get("primary_error"),
            "reproduction": manifest.get("reproduction"),
            "execution_command": manifest.get("execution_command"),
        },
        "environment": manifest.get("environment") or {},
        "capability_snapshot": manifest.get("capability_snapshot"),
        "setup_catalog": {
            "available": setup.get("available") or [],
            "selected": setup.get("selected") or [],
            "applied": setup.get("applied") or [],
            "verified": setup.get("verified") or [],
            "used": setup.get("used") or [],
        },
        "fixtures": manifest.get("fixtures") or [],
        "blocks": manifest.get("blocks") or [],
        "steps": steps,
        "action_attempts": action_attempts,
        "attachments": attachments,
        "replay": replay,
        "gates": manifest.get("gates") or [],
        "annotations": [],
    }
    validate(model)
    return model


def validate(model: dict[str, Any]) -> None:
    failures: list[str] = []
    if model.get("schema") != REPORT_SCHEMA:
        failures.append(f"schema must be {REPORT_SCHEMA}")
    attempt = model.get("attempt") or {}
    if attempt.get("status") not in EXECUTION_STATUSES:
        failures.append("attempt.status is invalid")
    if attempt.get("proof_verdict") not in PROOF_VERDICTS:
        failures.append("attempt.proof_verdict is invalid")
    for path in ("launch", "test_case", "attempt", "steps",
                 "action_attempts", "attachments"):
        if path not in model:
            failures.append(f"missing {path}")
    step_ids = [item.get("step_id") for item in model.get("steps") or []]
    if None in step_ids or len(step_ids) != len(set(step_ids)):
        failures.append("step ids must be present and unique")
    attachment_ids = [item.get("attachment_id")
                      for item in model.get("attachments") or []]
    if None in attachment_ids or len(attachment_ids) != len(set(attachment_ids)):
        failures.append("attachment ids must be present and unique")
    action_ids = [item.get("action_attempt_id")
                  for item in model.get("action_attempts") or []]
    if None in action_ids or len(action_ids) != len(set(action_ids)):
        failures.append("action attempt ids must be present and unique")
    known_steps = set(step_ids)
    if any(item.get("step_id") not in known_steps
           for item in model.get("action_attempts") or []):
        failures.append("action attempts must reference a known step")
    if failures:
        raise errors.AutonomError(
            errors.REPORT_MODEL_INVALID,
            "report model does not satisfy the v2 contract",
            failures=failures,
        )


def summary(model: dict[str, Any]) -> dict[str, Any]:
    steps = model.get("steps") or []
    status_counts = {status: sum(1 for step in steps if step.get("status") == status)
                     for status in EXECUTION_STATUSES}
    attempt = model["attempt"]
    return {
        "schema": "autonom.summary/v1",
        "run_id": attempt["run_id"],
        "attempt_id": attempt["attempt_id"],
        "case_id": model["test_case"]["case_id"],
        "history_id": model["test_case"]["history_id"],
        "status": attempt["status"],
        "proof_verdict": attempt["proof_verdict"],
        "steps": len(steps),
        "step_status": status_counts,
        "attachments": len(model.get("attachments") or []),
        "first_causal_failure": next(
            (step["step_id"] for step in steps if step.get("status")
             in ("failed", "broken")), None),
    }


def load(path: Path) -> dict[str, Any]:
    model = json.loads(path.read_text(encoding="utf-8"))
    validate(model)
    return model
