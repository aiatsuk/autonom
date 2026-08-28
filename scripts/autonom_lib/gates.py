"""Explicit quality gates and history aggregation for Report Model v2."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import errors
from .contracts import canonical_json, utc_now
from . import report_model


DEFAULT_RULES = {
    "allowed_statuses": ["passed"],
    "allowed_proof_verdicts": ["pass", "not_applicable"],
    "max_failed_steps": 0,
    "max_broken_steps": 0,
    "require_evidence_for_failures": True,
}


def evaluate(model: dict[str, Any], rules: dict[str, Any] | None = None) -> dict[str, Any]:
    report_model.validate(model)
    selected = {**DEFAULT_RULES, **(rules or {})}
    failures: list[dict[str, Any]] = []
    attempt = model["attempt"]
    steps = model.get("steps") or []
    if attempt["status"] not in selected["allowed_statuses"]:
        failures.append({"rule": "allowed_statuses", "actual": attempt["status"]})
    if attempt["proof_verdict"] not in selected["allowed_proof_verdicts"]:
        failures.append({"rule": "allowed_proof_verdicts",
                         "actual": attempt["proof_verdict"]})
    for status, key in (("failed", "max_failed_steps"),
                        ("broken", "max_broken_steps")):
        count = sum(1 for step in steps if step.get("status") == status)
        if count > int(selected[key]):
            failures.append({"rule": key, "actual": count,
                             "maximum": int(selected[key])})
    if selected.get("require_evidence_for_failures"):
        missing = [step["step_id"] for step in steps
                   if step.get("status") in ("failed", "broken")
                   and not step.get("attachment_ids")]
        if missing:
            failures.append({"rule": "require_evidence_for_failures",
                             "steps": missing})
    return {
        "schema": "autonom.gate-result/v1", "evaluated_at": utc_now(),
        "passed": not failures, "rules": selected, "failures": failures,
        "run_id": attempt["run_id"], "attempt_id": attempt["attempt_id"],
    }


def history(models: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for model in models:
        report_model.validate(model)
        grouped[model["test_case"]["history_id"]].append(model)
    cases = []
    for history_id, attempts in sorted(grouped.items()):
        attempts.sort(key=lambda item: item["attempt"].get("started_at_ms") or 0)
        latest = attempts[-1]
        statuses = [item["attempt"]["status"] for item in attempts]
        cases.append({
            "history_id": history_id,
            "case_id": latest["test_case"]["case_id"],
            "name": latest["test_case"]["name"],
            "attempts": len(attempts),
            "latest_status": statuses[-1],
            "retried": len(attempts) > 1,
            "flaky": "passed" in statuses and any(
                status in ("failed", "broken") for status in statuses),
            "statuses": statuses,
            "attempt_ids": [item["attempt"]["attempt_id"] for item in attempts],
        })
    return {"schema": "autonom.history/v1", "cases": cases,
            "attempts": len(models)}


def load_rules(path: Path | None) -> dict[str, Any]:
    if path is None:
        return dict(DEFAULT_RULES)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise errors.AutonomError(errors.REPORT_MODEL_INVALID,
                                  "gate rules must be a JSON object")
    return value
