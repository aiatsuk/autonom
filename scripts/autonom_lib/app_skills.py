"""Portable application knowledge packs under .autonom/apps/<app-id>."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from . import errors
from .contracts import canonical_json, utc_now
from .flow import validator

APP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,199}$")


def root(workspace: Path, app_id: str) -> Path:
    if not APP_ID_RE.fullmatch(app_id):
        raise errors.AutonomError(errors.APP_SKILL_INVALID,
                                  "app id is not safe for an App Skill path",
                                  app_id=app_id)
    return workspace / ".autonom" / "apps" / app_id


def validate(workspace: Path, app_id: str) -> dict[str, Any]:
    app_root = root(workspace, app_id)
    required = ["SKILL.md", "selectors.yaml", "fixtures.yaml", "compatibility.yaml"]
    missing = [name for name in required if not (app_root / name).is_file()]
    failures = []
    if missing:
        failures.append({"missing": missing})
    skill = app_root / "SKILL.md"
    if skill.is_file():
        text = skill.read_text(encoding="utf-8")
        if len(text.strip()) < 20 or "#" not in text:
            failures.append({"file": "SKILL.md", "reason": "needs a heading and guidance"})
        if re.search(r"(?i)(api[_-]?key|password|secret)\s*[:=]\s*\S+", text):
            failures.append({"file": "SKILL.md", "reason": "credential-shaped value"})
    flows = []
    subflows = app_root / "subflows"
    if subflows.is_dir():
        for path in sorted(subflows.glob("*.yaml")):
            try:
                flow = validator.validate_tree(path)
                flows.append({"path": str(path), "flow_id": flow.flow_id,
                              "name": flow.name})
            except errors.AutonomError as exc:
                failures.append({"file": str(path), "reason": exc.message,
                                 "error_code": exc.code})
    if failures:
        raise errors.AutonomError(errors.APP_SKILL_INVALID,
                                  "App Skill validation failed", failures=failures)
    return {"ok": True, "app_id": app_id, "root": str(app_root),
            "subflows": flows}


def promote(workspace: Path, app_id: str, flow_path: Path,
            *, approval: Path | None = None) -> dict[str, Any]:
    flow = validator.validate_tree(flow_path)
    if flow.app_id and flow.app_id != app_id:
        raise errors.AutonomError(errors.APP_SKILL_INVALID,
                                  "flow appId does not match the App Skill",
                                  flow_app_id=flow.app_id, app_id=app_id)
    if approval is None:
        approval = flow_path.with_suffix(flow_path.suffix + ".approved.json")
    if not approval.is_file():
        raise errors.AutonomError(errors.TEACH_APPROVAL_BLOCKED,
                                  "flow has no Teach approval receipt",
                                  hint="Run 'autonom teach approve <flow>'.")
    app_root = root(workspace, app_id)
    (app_root / "subflows").mkdir(parents=True, exist_ok=True)
    defaults = {
        "SKILL.md": f"# {app_id}\n\nVerified Autonom application knowledge.\n",
        "selectors.yaml": "schema: autonom.selectors/v1\nselectors: []\n",
        "fixtures.yaml": "schema: autonom.fixtures/v1\nfixtures: []\n",
        "compatibility.yaml": "schema: autonom.compatibility/v1\nbuilds: []\n",
    }
    for name, content in defaults.items():
        path = app_root / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    destination = app_root / "subflows" / flow_path.name
    shutil.copyfile(flow_path, destination)
    receipt = json.loads(approval.read_text(encoding="utf-8"))
    catalog_path = app_root / "catalog.json"
    catalog = (json.loads(catalog_path.read_text(encoding="utf-8"))
               if catalog_path.is_file() else
               {"schema": "autonom.app-skill/v1", "app_id": app_id, "flows": []})
    catalog["flows"] = [item for item in catalog["flows"]
                        if item.get("flow_id") != flow.flow_id]
    catalog["flows"].append({"flow_id": flow.flow_id, "name": flow.name,
                             "path": f"subflows/{flow_path.name}",
                             "approval": receipt, "promoted_at": utc_now()})
    catalog_path.write_bytes(canonical_json(catalog) + b"\n")
    result = validate(workspace, app_id)
    return {**result, "promoted": str(destination), "flow_id": flow.flow_id}
