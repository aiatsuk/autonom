"""Per-action detail records — the Session→Flow compiler's raw material.

The journal stays scrubbed and summary-shaped by design, which is exactly
why it cannot feed a compiler: a tap's matched node, the selector that found
it, and the surrounding tree were computed and thrown away. Instrumented
handlers write one JSON file per action under ``<session>/actions/`` (owner
-only, like every other artifact that can carry screen content) and put the
relative path into their response payload, which the journal choke point
lifts into the timeline via the ``detail`` summary key — so each journal
entry links to its rich record without the journal itself carrying it.

Best-effort like the journal: recording detail never fails the command.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from . import session as session_mod

_SLUG = re.compile(r"[^a-z0-9]+")


def record_detail(session: dict[str, Any] | None, hint: str,
                  payload: dict[str, Any]) -> str | None:
    """Write ``actions/NNNN_<hint>.json``; return the artifact-relative path."""
    if not session:
        return None
    try:
        directory = session_mod.artifact_path(session, "actions")
        directory.mkdir(parents=True, exist_ok=True)
        index = sum(1 for entry in directory.iterdir()
                    if entry.suffix == ".json") + 1
        slug = _SLUG.sub("-", hint.lower()).strip("-") or "action"
        path = directory / f"{index:04d}_{slug}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        os.chmod(path, 0o600)
        return str(path.relative_to(session["artifacts_dir"]))
    except Exception:  # noqa: BLE001 — evidence loss never fails a command
        return None


def read_details(session: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """All detail records keyed by their artifact-relative path."""
    out: dict[str, dict[str, Any]] = {}
    try:
        directory = session_mod.artifact_path(session, "actions")
        for path in sorted(directory.glob("*.json")):
            try:
                out[f"actions/{path.name}"] = json.loads(
                    path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    except Exception:  # noqa: BLE001
        pass
    return out
