"""Offline-first CI campaign, shard pack, merge, finalize, and publication."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from . import errors
from .contracts import canonical_json, fresh_id, sha256_file, utc_now
from . import gates, report_model


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")


def create_campaign(out: Path, *, expected_shards: int = 1,
                    campaign_id: str | None = None) -> dict[str, Any]:
    payload = {
        "schema": "autonom.campaign/v1",
        "campaign_id": campaign_id or fresh_id("campaign"),
        "created_at": utc_now(), "expected_shards": expected_shards,
        "shards": [], "status": "collecting", "publication": None,
    }
    _write_json(out, payload)
    return payload


def pack(bundle: Path, out: Path, *, shard_id: str = "shard-1") -> dict[str, Any]:
    from . import report_bundle
    verified = report_bundle.verify(bundle)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, Path("bundle") / path.relative_to(bundle))
        archive.writestr("shard.json", canonical_json({
            "schema": "autonom.shard/v1", "shard_id": shard_id,
            "run_id": verified["run_id"], "packed_at": utc_now(),
        }) + b"\n")
    return {"ok": True, "out": str(out), "shard_id": shard_id,
            "sha256": sha256_file(out), "run_id": verified["run_id"]}


def merge(packs: list[Path], out: Path, *, expected_shards: int | None = None) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    models: list[dict[str, Any]] = []
    shards: list[dict[str, Any]] = []
    seen_attempts: set[str] = set()
    for pack_path in packs:
        with zipfile.ZipFile(pack_path) as archive:
            shard = json.loads(archive.read("shard.json"))
            model = json.loads(archive.read("bundle/model/report.json"))
            report_model.validate(model)
            attempt_id = model["attempt"]["attempt_id"]
            if attempt_id in seen_attempts:
                continue  # idempotent shard retries
            seen_attempts.add(attempt_id)
            models.append(model)
            shards.append({**shard, "pack": str(pack_path),
                           "sha256": sha256_file(pack_path)})
    expected = expected_shards if expected_shards is not None else len(shards)
    missing = max(0, expected - len(shards))
    for model in models:
        _write_json(out / "models" / f"{model['attempt']['attempt_id']}.json", model)
    history = gates.history(models)
    campaign = {
        "schema": "autonom.campaign-result/v1", "created_at": utc_now(),
        "expected_shards": expected, "received_shards": len(shards),
        "missing_shards": missing, "status": "incomplete" if missing else "complete",
        "shards": shards, "history": history,
        "summary": {
            "attempts": len(models),
            "passed": sum(1 for item in models
                          if item["attempt"]["status"] == "passed"),
            "failed": sum(1 for item in models
                          if item["attempt"]["status"] in ("failed", "broken")),
        },
    }
    _write_json(out / "campaign.json", campaign)
    return campaign


def finalize(campaign_dir: Path, *, rules_path: Path | None = None) -> dict[str, Any]:
    campaign = json.loads((campaign_dir / "campaign.json").read_text(encoding="utf-8"))
    models = [report_model.load(path) for path in sorted(
        (campaign_dir / "models").glob("*.json"))]
    selected_rules = gates.load_rules(rules_path)
    results = [gates.evaluate(model, selected_rules) for model in models]
    missing = campaign.get("missing_shards", 0)
    passed = not missing and all(item["passed"] for item in results)
    payload = {
        "schema": "autonom.campaign-final/v1", "finalized_at": utc_now(),
        "status": "passed" if passed else "failed",
        "execution_status": campaign.get("status"),
        "publication_status": "not_requested",
        "missing_shards": missing, "gates": results,
    }
    _write_json(campaign_dir / "final.json", payload)
    return payload


def publish(campaign_dir: Path, destination: str) -> dict[str, Any]:
    """Publish to a filesystem path or generic HTTP PUT endpoint.

    The execution outcome is never rewritten by publication failure; callers
    receive a separate publication status as required by the blueprint.
    """
    final = campaign_dir / "final.json"
    if not final.is_file():
        raise errors.AutonomError(errors.CI_DESTINATION_FAILED,
                                  "campaign must be finalized before publication")
    if destination.startswith(("http://", "https://")):
        archive_path = Path(tempfile.mkstemp(suffix=".zip")[1])
        try:
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(campaign_dir.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(campaign_dir))
            request = urllib.request.Request(
                destination, data=archive_path.read_bytes(), method="PUT",
                headers={"Content-Type": "application/zip",
                         "Idempotency-Key": sha256_file(archive_path)},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                status = response.status
            return {"publication_status": "published", "destination": destination,
                    "http_status": status}
        except Exception as exc:
            raise errors.AutonomError(
                errors.CI_DESTINATION_FAILED, "campaign publication failed",
                destination=destination, reason=str(exc)) from exc
        finally:
            archive_path.unlink(missing_ok=True)
    target = Path(destination).resolve()
    if target.exists():
        if not target.is_dir():
            raise errors.AutonomError(errors.CI_DESTINATION_FAILED,
                                      "filesystem destination is not a directory",
                                      destination=str(target))
        if any(target.iterdir()):
            raise errors.AutonomError(errors.CI_DESTINATION_FAILED,
                                      "filesystem destination is not empty",
                                      destination=str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.rmdir()
    shutil.copytree(campaign_dir, target)
    return {"publication_status": "published", "destination": str(target)}
