"""Deterministic, content-addressed, integrity-checked Report Bundle v2."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from . import errors
from .contracts import (BUNDLE_SCHEMA, REPLAY_SCHEMA, canonical_json, fresh_id,
                        redact_value, sha256_bytes, sha256_file, utc_now)
from . import report_model


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise errors.AutonomError(
            errors.PATH_FORBIDDEN,
            "artifact path escapes the session evidence root", path=value)
    return path


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")
    os.chmod(path, 0o600)


def _copy_blob(source: Path, root: Path) -> tuple[str, Path]:
    digest = sha256_file(source)
    relative = Path("blobs") / "sha256" / digest[:2] / digest
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o600)
    return digest, relative


def replay_manifest(model: dict[str, Any]) -> dict[str, Any]:
    attempt = model["attempt"]
    return {
        "schema": REPLAY_SCHEMA,
        "source_run_id": attempt["run_id"],
        "source_attempt_id": attempt["attempt_id"],
        "flow_id": model["test_case"]["flow_id"],
        "app_id": model["test_case"].get("app_id"),
        "command": attempt.get("reproduction"),
        "environment": model.get("environment") or {},
        "setup": model.get("setup_catalog") or {},
        "capability_snapshot": model.get("capability_snapshot"),
        "strategy": "baseline",
        "checkpoints": [
            {"step_id": step["step_id"], "index": step.get("index"),
             "name": step.get("name")}
            for step in model.get("steps") or []
            if step.get("command") == "checkpoint"
        ],
        "verification": [
            {"step_id": step["step_id"], "expected_status": step["status"],
             "postcondition_fingerprint": (step.get("delta") or {}).get(
                 "postcondition_fingerprint")}
            for step in model.get("steps") or []
        ],
    }


def build(manifest: dict[str, Any], *, artifacts_root: Path, out: Path,
          force: bool = False) -> dict[str, Any]:
    """Finalize via a sibling staging dir and atomic rename.

    An already finalized directory is immutable.  ``force`` is only accepted
    when its integrity hash is identical, making retries idempotent rather than
    an overwrite escape hatch.
    """
    safe_manifest = redact_value(manifest)
    sources = []
    for item in manifest.get("flow_sources") or []:
        source = Path(str(item.get("path") or ""))
        relative = _safe_relative(str(item.get("relative") or source.name))
        if source.is_file():
            sources.append((source, relative))
    if not sources:
        source = Path(str(manifest.get("flow_path") or ""))
        if source.is_file():
            sources.append((source, Path(source.name)))
    if sources:
        root_relative = next(
            (relative for source, relative in sources
             if source.resolve() == Path(str(manifest.get("flow_path"))).resolve()),
            sources[0][1])
        safe_manifest["flow_path"] = (Path("flow") / root_relative).as_posix()
        safe_manifest["workspace_root"] = "flow"
        safe_manifest["flow_sources"] = [
            {"path": (Path("flow") / relative).as_posix(),
             "relative": relative.as_posix()}
            for _source, relative in sources]
        safe_manifest["reproduction"] = "autonom replay --bundle <bundle>"
    model = report_model.compile_manifest(safe_manifest)
    finalized = out / "finalized.json"
    if finalized.is_file():
        descriptor_path = out / "manifest.json"
        descriptor = (json.loads(descriptor_path.read_text(encoding="utf-8"))
                      if descriptor_path.is_file() else {})
        if descriptor.get("run_id") == manifest.get("run_id"):
            current = json.loads(finalized.read_text(encoding="utf-8"))
            return {"bundle": str(out), "finalized": True, "idempotent": True,
                    "model_sha256": current.get("model_sha256")}
        raise errors.AutonomError(
            errors.REPORT_BUNDLE_FINALIZED,
            "report bundle is finalized and differs from this run",
            hint="Choose a new output directory; finalized raw evidence is immutable.",
            bundle=str(out),
        )
    if out.exists() and any(out.iterdir()):
        raise errors.AutonomError(
            errors.REPORT_BUNDLE_FINALIZED,
            "output directory is non-empty and not an Autonom finalized bundle",
            hint="Choose an empty output directory.", bundle=str(out))

    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out.name}.staging-", dir=out.parent))
    try:
        attachment_catalog: list[dict[str, Any]] = []
        (staging / "flow/.autonom").mkdir(parents=True, exist_ok=True)
        for source, relative in sources:
            destination = staging / "flow" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o600)
        for attachment in model.get("attachments") or []:
            relative_source = _safe_relative(attachment["path"])
            source = artifacts_root / relative_source
            row = dict(attachment)
            if source.is_file():
                digest, blob_path = _copy_blob(source, staging)
                row.update({"sha256": digest, "size": source.stat().st_size,
                            "blob": blob_path.as_posix()})
            else:
                row["availability"] = "unavailable"
                row["reason"] = "artifact was listed but is not present"
            attachment_catalog.append(row)
        model["attachments"] = attachment_catalog
        model_hash = sha256_bytes(canonical_json(model))
        _write_json(staging / "model/report.json", model)
        _write_json(staging / "summary.json", report_model.summary(model))
        _write_json(staging / "catalog.json", {"attachments": attachment_catalog})
        _write_json(staging / "run.json", safe_manifest)
        _write_json(staging / "replay.json", replay_manifest(model))
        for step in model.get("steps") or []:
            _write_json(staging / "steps" / f"{step['step_id']}.json", step)

        source_events = artifacts_root / "flows" / str(manifest["run_id"]) / "events.ndjson"
        if source_events.is_file():
            stream = staging / "streams/events.ndjson"
            stream.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_events, stream)
            os.chmod(stream, 0o600)

        files = []
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                files.append({"path": path.relative_to(staging).as_posix(),
                              "sha256": sha256_file(path),
                              "size": path.stat().st_size})
        integrity = {"algorithm": "sha256", "files": files}
        _write_json(staging / "integrity.json", integrity)
        descriptor = {
            "schema": BUNDLE_SCHEMA,
            "run_id": manifest["run_id"],
            "attempt_id": model["attempt"]["attempt_id"],
            "model": "model/report.json",
            "summary": "summary.json",
            "catalog": "catalog.json",
            "replay": "replay.json",
            "integrity": "integrity.json",
            "finalized": True,
        }
        _write_json(staging / "manifest.json", descriptor)
        _write_json(staging / "finalized.json", {
            "schema": "autonom.finalized/v1",
            "model_sha256": model_hash,
            "integrity_sha256": sha256_file(staging / "integrity.json"),
        })
        if out.exists():
            out.rmdir()
        staging.replace(out)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"bundle": str(out), "finalized": True, "idempotent": False,
            "model_sha256": model_hash,
            "attachments": len(attachment_catalog)}


def verify(bundle: Path) -> dict[str, Any]:
    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file():
        raise errors.AutonomError(
            errors.REPORT_INTEGRITY_FAILED, "bundle has no manifest.json")
    descriptor = json.loads(manifest_path.read_text(encoding="utf-8"))
    if descriptor.get("schema") != BUNDLE_SCHEMA:
        raise errors.AutonomError(
            errors.REPORT_INTEGRITY_FAILED, "unsupported bundle schema",
            schema=descriptor.get("schema"))
    fixed_paths = {
        "model": "model/report.json", "summary": "summary.json",
        "catalog": "catalog.json", "replay": "replay.json",
        "integrity": "integrity.json",
    }
    invalid_paths = {name: descriptor.get(name) for name, expected in fixed_paths.items()
                     if descriptor.get(name) != expected}
    if invalid_paths:
        raise errors.AutonomError(
            errors.REPORT_INTEGRITY_FAILED,
            "bundle descriptor contains non-canonical core paths",
            paths=invalid_paths)
    integrity_path = bundle / fixed_paths["integrity"]
    finalized_path = bundle / "finalized.json"
    if not integrity_path.is_file() or not finalized_path.is_file():
        raise errors.AutonomError(
            errors.REPORT_INTEGRITY_FAILED,
            "bundle is missing its integrity or finalization record")
    finalized_record = json.loads(finalized_path.read_text(encoding="utf-8"))
    if finalized_record.get("integrity_sha256") != sha256_file(integrity_path):
        raise errors.AutonomError(
            errors.REPORT_INTEGRITY_FAILED,
            "bundle integrity catalog digest does not match finalization record")
    model_path = bundle / fixed_paths["model"]
    if (not model_path.is_file()
            or finalized_record.get("model_sha256")
            != sha256_bytes(canonical_json(json.loads(
                model_path.read_text(encoding="utf-8"))))):
        raise errors.AutonomError(
            errors.REPORT_INTEGRITY_FAILED,
            "report model digest does not match finalization record")
    integrity = json.loads(integrity_path.read_text(encoding="utf-8"))
    failures = []
    for item in integrity.get("files") or []:
        relative = _safe_relative(item["path"])
        path = bundle / relative
        if not path.is_file():
            failures.append({"path": item["path"], "reason": "missing"})
        elif sha256_file(path) != item["sha256"]:
            failures.append({"path": item["path"], "reason": "digest-mismatch"})
    if failures:
        raise errors.AutonomError(
            errors.REPORT_INTEGRITY_FAILED,
            "report bundle integrity verification failed", failures=failures)
    return {"ok": True, "bundle": str(bundle),
            "files": len(integrity.get("files") or []),
            "run_id": descriptor.get("run_id")}


def annotate(bundle: Path, text: str, *, author: str,
             step_id: str | None = None) -> dict[str, Any]:
    """Write a mutable annotation beside, never into, finalized raw evidence."""
    verify(bundle)
    model = report_model.load(bundle / "model/report.json")
    if step_id and not any(item.get("step_id") == step_id
                           for item in model.get("steps") or []):
        raise errors.AutonomError(errors.FLOW_REPLAY_STEP_NOT_REACHED,
                                  f"bundle has no step {step_id!r}")
    payload = {
        "schema": "autonom.annotation/v1", "annotation_id": fresh_id("annotation"),
        "created_at": utc_now(), "author": author, "text": text,
        "attempt_id": model["attempt"]["attempt_id"], "step_id": step_id,
    }
    path = bundle / "annotations" / f"{payload['annotation_id']}.json"
    _write_json(path, payload)
    return {**payload, "path": str(path)}
