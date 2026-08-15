"""Flow run event stream: the §13.3 envelope, NDJSON on disk, journal bridge.

Every run writes ``flows/<run_id>/events.ndjson`` under the session's
artifact dir — one compact JSON object per line, file chmod 600 (events can
name selectors and screens; they are evidence, not chatter). The envelope
versions itself (``schema_version``) because journal entries never did.

Secrets never reach this module: the executor redacts values *before*
building payloads, so a bug here cannot leak what it never saw.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from . import EVENT_SCHEMA_VERSION
from .. import journal as journal_mod
from .. import session as session_mod


def _timestamp() -> str:
    now = time.time()
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
    return f"{base}.{int((now % 1) * 1000):03d}Z"


class EventWriter:
    def __init__(self, session_record: dict, run_id: str, flow_id: str | None,
                 platform: str, target_id: str, serial: str | None = None,
                 stdout_stream: Any | None = None) -> None:
        self.session = session_record
        self.run_id = run_id
        self.flow_id = flow_id
        self.platform = platform
        self.target_id = target_id
        self.serial = serial
        self.stdout_stream = stdout_stream
        self.path = session_mod.artifact_path(session_record, "flows", run_id,
                                              "events.ndjson")
        self.path.touch()
        os.chmod(self.path, 0o600)

    def run_dir(self) -> Path:
        return self.path.parent

    def emit(self, kind: str, payload: dict[str, Any],
             sensitive: bool = False) -> dict[str, Any]:
        event: dict[str, Any] = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_id": f"evt_{uuid.uuid4().hex[:12]}",
            "run_id": self.run_id,
            "session_id": self.session.get("session_id"),
            "flow_id": self.flow_id,
            "timestamp": _timestamp(),
            "kind": kind,
            "platform": self.platform,
            "target_id": self.target_id,
            "sensitive": sensitive,
            "payload": payload,
        }
        if self.serial:
            event["serial"] = self.serial  # DEC-004: permanent on Android
        line = json.dumps(event, ensure_ascii=False)
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass  # best-effort, like the journal: evidence loss never kills a run
        if self.stdout_stream is not None:
            print(line, file=self.stdout_stream, flush=True)
        return event

    def journal_step(self, step_event: dict[str, Any]) -> None:
        """One slim, scrubbed journal line per step, cross-referenced by id."""
        payload = step_event["payload"]
        entry = {
            "kind": "flow_step",
            "event_id": step_event["event_id"],
            "run_id": self.run_id,
            "flow_id": self.flow_id,
            "verb": "flow run",
            "step_index": payload.get("step_index"),
            "command": payload.get("command"),
            "label": payload.get("label"),
            "ok": payload.get("status") == "passed",
            "status": payload.get("status"),
            "error_code": payload.get("error_code"),
        }
        journal_mod.append(self.session,
                           {k: v for k, v in entry.items() if v is not None})
