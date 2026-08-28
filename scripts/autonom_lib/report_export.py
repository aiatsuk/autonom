"""Exporter fan-out from Report Model v2: Allure, agent JSON, CSV, metrics."""
from __future__ import annotations

import csv
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .contracts import canonical_json, stable_id
from . import report_model


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")
    os.chmod(path, 0o600)


def agent(model: dict[str, Any], out: Path) -> dict[str, Any]:
    """Compact machine view: decisions and evidence refs without HTML."""
    report_model.validate(model)
    payload = {
        "schema": "autonom.agent-report/v1",
        "summary": report_model.summary(model),
        "failure": model["attempt"].get("primary_error"),
        "setup": model.get("setup_catalog") or {},
        "capabilities": model.get("capability_snapshot"),
        "replay": model.get("replay") or {},
        "steps": [
            {
                "step_id": step["step_id"], "index": step.get("index"),
                "name": step.get("name"), "command": step.get("command"),
                "status": step.get("status"), "error": step.get("error"),
                "delta": step.get("delta"),
                "attachment_ids": step.get("attachment_ids") or [],
            }
            for step in model.get("steps") or []
        ],
        "attachments": model.get("attachments") or [],
    }
    _write_json(out, payload)
    return {"format": "agent", "out": str(out)}


def csv_steps(model: dict[str, Any], out: Path) -> dict[str, Any]:
    report_model.validate(model)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow((
            "run_id", "attempt_id", "case_id", "history_id", "step_id",
            "index", "name", "command", "status", "duration_ms",
            "retry_attempt", "attachment_count", "log_count", "request_count",
        ))
        for step in model.get("steps") or []:
            delta = step.get("delta") or {}
            writer.writerow((
                model["attempt"]["run_id"], model["attempt"]["attempt_id"],
                model["test_case"]["case_id"], model["test_case"]["history_id"],
                step["step_id"], step.get("index"), step.get("name"),
                step.get("command"), step.get("status"), step.get("duration_ms", 0),
                step.get("retry_attempt"), len(step.get("attachment_ids") or []),
                (delta.get("logs") or {}).get("count", 0),
                (delta.get("requests") or {}).get("count", 0),
            ))
    os.chmod(out, 0o600)
    return {"format": "csv", "out": str(out)}


def metrics(model: dict[str, Any], out: Path) -> dict[str, Any]:
    report_model.validate(model)
    summary = report_model.summary(model)
    attempt = model["attempt"]
    duration = max(0, (attempt.get("finished_at_ms") or 0)
                   - (attempt.get("started_at_ms") or 0))
    labels = {
        "case_id": model["test_case"]["case_id"],
        "history_id": model["test_case"]["history_id"],
        "status": attempt["status"],
        "proof_verdict": attempt["proof_verdict"],
    }
    payload = {
        "schema": "autonom.metrics/v1", "labels": labels,
        "values": {
            "run_duration_ms": duration,
            "step_count": summary["steps"],
            "attachment_count": summary["attachments"],
            **{f"steps_{name}": count
               for name, count in summary["step_status"].items()},
        },
    }
    _write_json(out, payload)
    return {"format": "metrics", "out": str(out)}


def allure(model: dict[str, Any], out: Path, *, bundle_root: Path | None = None) -> dict[str, Any]:
    """Write an Allure 2/3 compatible results directory.

    Allure consumes the same result envelope in Report 2 and Report 3.  Stable
    case/history ids make retries and history merge instead of appearing as new
    tests; the native model remains the source of truth.
    """
    report_model.validate(model)
    out.mkdir(parents=True, exist_ok=True)
    attempt = model["attempt"]
    test_case = model["test_case"]
    status = attempt["status"]
    allure_status = {
        "passed": "passed", "failed": "failed", "broken": "broken",
        "skipped": "skipped", "unknown": "unknown",
    }[status]
    uuid = attempt["attempt_id"]

    attachment_by_id = {item["attachment_id"]: item
                        for item in model.get("attachments") or []}

    def attachment_refs(ids: list[str]) -> list[dict[str, Any]]:
        refs = []
        for attachment_id in ids:
            item = attachment_by_id.get(attachment_id)
            if not item:
                continue
            source_name = f"{attachment_id}-{Path(item['name']).name}"
            source = None
            if bundle_root and item.get("blob"):
                source = bundle_root / item["blob"]
            if source and source.is_file():
                shutil.copyfile(source, out / source_name)
            else:
                # A pointer remains useful when exporting directly from a live
                # manifest; unavailable data is explicit, never synthesized.
                _write_json(out / f"{source_name}.reference.json", item)
                source_name += ".reference.json"
            refs.append({"name": item["name"], "source": source_name,
                         "type": item.get("mime_type")})
        return refs

    steps = []
    for step in model.get("steps") or []:
        item = {
            "name": step.get("name") or step.get("command"),
            "status": step.get("status"),
            "stage": "finished",
            "start": step.get("started_at_ms"),
            "stop": step.get("finished_at_ms"),
            "parameters": [
                {"name": "step_id", "value": step["step_id"]},
                {"name": "command", "value": step.get("command")},
            ],
            "attachments": attachment_refs(step.get("attachment_ids") or []),
            "steps": [],
        }
        if step.get("error"):
            item["statusDetails"] = {
                "message": step["error"].get("message"),
                "trace": json.dumps(step["error"], ensure_ascii=False),
            }
        steps.append(item)

    labels = [
        {"name": "framework", "value": "autonom"},
        {"name": "suite", "value": test_case.get("app_id") or "Autonom"},
        {"name": "testCaseId", "value": test_case["case_id"]},
        {"name": "historyId", "value": test_case["history_id"]},
        {"name": "proofVerdict", "value": attempt["proof_verdict"]},
    ]
    labels.extend({"name": "tag", "value": tag}
                  for tag in test_case.get("tags") or [])
    result = {
        "uuid": uuid,
        "historyId": test_case["history_id"],
        "testCaseId": test_case["case_id"],
        "name": test_case["name"],
        "fullName": f"{test_case.get('app_id') or 'app'}::{test_case['flow_id']}",
        "status": allure_status,
        "stage": "finished",
        "start": attempt.get("started_at_ms"),
        "stop": attempt.get("finished_at_ms"),
        "labels": labels,
        "parameters": [{"name": name, "value": str(value)}
                       for name, value in test_case.get("parameters", {}).items()],
        "steps": steps,
        "attachments": attachment_refs([
            item["attachment_id"] for item in model.get("attachments") or []
            if item.get("step_index") is None
        ]),
        "links": [],
    }
    if attempt.get("primary_error"):
        result["statusDetails"] = {
            "message": attempt["primary_error"].get("error"),
            "trace": json.dumps(attempt["primary_error"], ensure_ascii=False),
        }
    _write_json(out / f"{uuid}-result.json", result)
    _write_json(out / "environment.properties.json", model.get("environment") or {})
    _write_json(out / "autonom-summary.json", report_model.summary(model))
    return {"format": "allure", "out": str(out), "results": 1}


def export(model: dict[str, Any], format_name: str, out: Path,
           *, bundle_root: Path | None = None) -> dict[str, Any]:
    table = {
        "agent": lambda: agent(model, out),
        "csv": lambda: csv_steps(model, out),
        "metrics": lambda: metrics(model, out),
        "allure": lambda: allure(model, out, bundle_root=bundle_root),
    }
    return table[format_name]()
