"""Session journal: an append-only timeline of everything a session did.

`<artifacts_dir>/journal.ndjson` gets one JSON line per event — every CLI verb
(what ran, its scrubbed arguments, whether it succeeded, and the artifact it
produced), plus freeform notes an agent writes with `autonom note`. Read it
back with `autonom journal` for a full account of a run: which taps happened,
which screens were captured, which mocks were in force, what the agent
concluded.

Two hard rules:

- **Best-effort.** A journal write must never fail the command it records. Every
  entry point swallows its own errors — losing a line is acceptable, breaking a
  tap is not.
- **No secrets.** The journal lands on disk, so it gets the same treatment as
  captured traffic: the text typed into a field (often a password) is reduced to
  its length, and the values behind body/header/env flags are masked. What the
  agent chooses to write in a note is its own responsibility.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import errors
from .network import redact

JOURNAL_FILE = "journal.ndjson"

# Flags whose *value* (the next argv token) can carry a credential.
_SENSITIVE_VALUE_FLAGS = {
    "--json", "--header", "--setenv", "--data", "--body", "--raw",
}
# Fields worth lifting from a command's result into the timeline summary.
# Deliberately excludes anything free-form or body-shaped ("typed", previews).
_SUMMARY_KEYS = (
    "target_id", "platform", "saved", "path", "count", "matched", "gesture",
    "booted", "stopped", "via", "mocks_active", "mocks", "hits", "port",
    "har", "url", "app_id", "note", "status", "run_id",
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def journal_path(session: dict[str, Any]) -> Path:
    return Path(session["artifacts_dir"]) / JOURNAL_FILE


def _next_seq(path: Path) -> int:
    if not path.exists():
        return 1
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle) + 1
    except OSError:
        return 1


def scrub_argv(argv: list[str]) -> list[str]:
    """Mask credential-shaped arguments before they reach disk."""
    out: list[str] = []
    skip_next = False
    for index, token in enumerate(argv):
        if skip_next:
            out.append("***")
            skip_next = False
            continue
        if token in _SENSITIVE_VALUE_FLAGS:
            out.append(token)
            skip_next = True
            continue
        # `ui type <text>`: the positional after `type` is whatever was entered
        # into the focused field, which is exactly where a password shows up.
        if index >= 2 and argv[index - 1] == "type" and argv[index - 2] == "ui" \
                and not token.startswith("-"):
            out.append(f"<{len(token)} chars>")
            continue
        out.append(redact.scrub_body(token) if "=" in token or "{" in token else token)
    return out


def _summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {key: payload[key] for key in _SUMMARY_KEYS if key in payload}


def append(session: dict[str, Any] | None, entry: dict[str, Any]) -> None:
    """Append one entry. Never raises — journaling is best-effort."""
    if not session:
        return
    try:
        path = journal_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        full = {"seq": _next_seq(path), "ts": _now(), **entry}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(full, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — a broken journal must not break a command
        pass


def record_action(
    session: dict[str, Any] | None,
    *,
    verb: str,
    argv: list[str],
    payload: dict[str, Any] | None,
    ok: bool,
    error_code: str | None = None,
) -> None:
    entry: dict[str, Any] = {
        "kind": "action",
        "verb": verb,
        "argv": scrub_argv(argv),
        "ok": ok,
    }
    summary = _summary(payload)
    if summary:
        entry["result"] = summary
    if error_code:
        entry["error_code"] = error_code
    append(session, entry)


def note(
    session: dict[str, Any],
    text: str,
    *,
    task: str | None = None,
    tags: list[str] | None = None,
    author: str = "agent",
) -> dict[str, Any]:
    entry: dict[str, Any] = {"kind": "note", "author": author, "text": text}
    if task:
        entry["task"] = task
    if tags:
        entry["tags"] = tags
    append(session, entry)
    return entry


def read(
    session: dict[str, Any],
    *,
    kind: str | None = None,
    verb: str | None = None,
    task: str | None = None,
    grep: str | None = None,
    max_entries: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return (entries, total_matched). Newest entries are kept when truncating."""
    path = journal_path(session)
    if not path.exists():
        return [], 0
    import re

    pattern = re.compile(grep, re.IGNORECASE) if grep else None
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if kind and item.get("kind") != kind:
            continue
        if verb and item.get("verb") != verb:
            continue
        if task and item.get("task") != task:
            continue
        if pattern and not pattern.search(json.dumps(item, ensure_ascii=False)):
            continue
        entries.append(item)
    total = len(entries)
    if max_entries is not None and total > max_entries:
        entries = entries[-max_entries:]
    return entries, total
