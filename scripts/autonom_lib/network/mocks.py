"""Persistent mock registry + CRUD (CAP-MOCK-001..006).

Rules live in a **machine-level registry**, not inside a session:

    $AUTONOM_HOME (or ~/.local/state/autonom)/mocks/
      registry.json      the rules
      bodies/m_N.body    response bodies, copied in so the source can move

This is a deliberate reversal of the 0.6.0 behaviour, where rules lived in
`<artifacts_dir>/network/mocks.json` and died with the session. A mock is a
*standing decision* about how a backend should behave — "this endpoint returns
this JSON while I work on the feature" — and re-adding it after every restart
made it useless for anything longer than one sitting.

The reversal costs a safety property: a forgotten rule now survives into the next
session and silently fakes a response. The compensating control is **loudness**,
not obscurity — `network start`, `network status` and `doctor` all report the
active rule count and the hosts affected, so a stale mock announces itself before
it can mislead. See `mobile-network` SKILL.md.

The registry is also the only channel to the addon, which re-reads the file when
its mtime changes. That makes the atomic write a correctness requirement rather
than a nicety: a half-written file observed mid-request would drop every rule.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from .. import errors

BODY_INLINE_LIMIT = 1_048_576  # 1 MiB — an inline --json body beyond this is a file


def registry_dir() -> Path:
    """Machine-level registry, beside the CA store and outside any repository.

    Deliberately not inside the project: a rule body is frequently a captured
    response, and a captured response frequently carries a token. Keeping the
    registry out of the working tree means it cannot be committed by accident.
    """
    explicit = os.environ.get("AUTONOM_HOME")
    if explicit:
        root = Path(explicit)
    else:
        state = os.environ.get("XDG_STATE_HOME")
        root = Path(state) / "autonom" if state else Path.home() / ".local/state/autonom"
    path = root / "mocks"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def registry_file(registry: Path | None = None) -> Path:
    return (registry or registry_dir()) / "registry.json"


def bodies_dir(registry: Path | None = None) -> Path:
    path = (registry or registry_dir()) / "bodies"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


# --- persistence --------------------------------------------------------------


def _read_payload(registry: Path | None = None) -> dict[str, Any]:
    path = registry_file(registry)
    if not path.exists():
        return {"version": 1, "next_id": 1, "mocks": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "next_id": 1, "mocks": []}
    if isinstance(payload, list):  # tolerate the 0.6.0 shape
        return {"version": 1, "next_id": len(payload) + 1, "mocks": payload}
    payload.setdefault("mocks", [])
    payload.setdefault("next_id", len(payload["mocks"]) + 1)
    return payload


def _write_payload(payload: dict[str, Any], registry: Path | None = None) -> None:
    """Temp file plus rename: the addon must never observe a partial write."""
    path = registry_file(registry)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=".registry-", suffix=".tmp",
        delete=False,
    )
    try:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.chmod(handle.name, 0o600)
    os.replace(handle.name, path)


def load(registry: Path | None = None) -> list[dict[str, Any]]:
    return list(_read_payload(registry).get("mocks", []))


def save(rules: list[dict[str, Any]], registry: Path | None = None) -> None:
    payload = _read_payload(registry)
    payload["mocks"] = rules
    _write_payload(payload, registry)


def active(registry: Path | None = None) -> list[dict[str, Any]]:
    return [rule for rule in load(registry) if rule.get("enabled", True)]


def summary(registry: Path | None = None) -> dict[str, Any]:
    """What `network start`, `network status` and `doctor` shout about."""
    rules = load(registry)
    enabled = [rule for rule in rules if rule.get("enabled", True)]
    hosts = []
    for rule in enabled:
        match = rule.get("match") or {}
        label = match.get("host") or match.get("url_glob") or "*"
        if label not in hosts:
            hosts.append(label)
    return {
        "total": len(rules),
        "active": len(enabled),
        "targets": hosts,
        "registry": str(registry_file(registry)),
    }


# --- helpers ------------------------------------------------------------------


def _next_id(payload: dict[str, Any]) -> str:
    """Monotonic, never recycled: a reused id would silently re-point a body."""
    index = int(payload.get("next_id") or 1)
    payload["next_id"] = index + 1
    return f"m_{index}"


def _find(rules: Iterable[dict[str, Any]], identifier: str) -> dict[str, Any]:
    for rule in rules:
        if rule.get("id") == identifier:
            return rule
    raise errors.AutonomError(
        errors.MOCK_NOT_FOUND,
        f"no mock rule with id {identifier}",
        "List them with 'autonom network mock list'.",
    )


def _store_body(
    identifier: str,
    *,
    body_file: Path | None,
    body_text: str | None,
    registry: Path | None = None,
) -> str | None:
    if body_file is None and body_text is None:
        return None
    destination = bodies_dir(registry) / f"{identifier}.body"
    if body_file is not None:
        source = Path(body_file).expanduser()
        if not source.exists():
            raise errors.AutonomError(
                errors.BODY_FILE_NOT_FOUND,
                f"body file not found: {source}",
                "Pass an existing file, or use --json for an inline body.",
            )
        shutil.copyfile(source, destination)
    else:
        if len(body_text or "") > BODY_INLINE_LIMIT:
            raise errors.AutonomError(
                errors.BODY_FILE_NOT_FOUND,
                f"inline body exceeds {BODY_INLINE_LIMIT} bytes",
                "Write it to a file and use --body-file instead.",
            )
        destination.write_text(body_text or "", encoding="utf-8")
    os.chmod(destination, 0o600)
    return str(destination)


def url_to_match(url: str) -> dict[str, Any]:
    """`--url <exact URL>` sugar.

    A raw URL is used as a literal glob and the query string is ignored, because
    `…/update/12341?ts=1699` is the same endpoint as `…/update/12341` for the
    purpose of "make this handle return that JSON". Query matching stays
    available through `--match`, where the caller writes the glob themselves.
    """
    base = url.split("?", 1)[0]
    host = None
    if "://" in base:
        remainder = base.split("://", 1)[1]
        host = remainder.split("/", 1)[0].split("@")[-1].split(":")[0] or None
    return {"url_glob": base, "method": None, "host": host, "ignore_query": True}


# --- CRUD ---------------------------------------------------------------------


def add(
    *,
    url_glob: str,
    method: str | None = None,
    host: str | None = None,
    ignore_query: bool = False,
    status: int = 200,
    headers: dict[str, str] | None = None,
    body_file: Path | None = None,
    body_text: str | None = None,
    note: str | None = None,
    registry: Path | None = None,
) -> dict[str, Any]:
    payload = _read_payload(registry)
    identifier = _next_id(payload)
    body_path = _store_body(
        identifier, body_file=body_file, body_text=body_text, registry=registry
    )
    rule = {
        "id": identifier,
        "match": {
            "url_glob": url_glob,
            "method": method.upper() if method else None,
            "host": host,
            "ignore_query": bool(ignore_query),
        },
        "response": {
            "status": int(status),
            "headers": headers or {},
            "body_path": body_path,
        },
        "enabled": True,
        "note": note,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    payload["mocks"].append(rule)
    _write_payload(payload, registry)
    return rule


def get(identifier: str, registry: Path | None = None) -> dict[str, Any]:
    return _find(load(registry), identifier)


def update(
    identifier: str,
    *,
    url_glob: str | None = None,
    method: str | None = None,
    host: str | None = None,
    ignore_query: bool | None = None,
    status: int | None = None,
    headers: dict[str, str] | None = None,
    body_file: Path | None = None,
    body_text: str | None = None,
    note: str | None = None,
    registry: Path | None = None,
) -> dict[str, Any]:
    """Partial update. Only the fields actually supplied are touched."""
    payload = _read_payload(registry)
    rule = _find(payload["mocks"], identifier)

    match = rule.setdefault("match", {})
    if url_glob is not None:
        match["url_glob"] = url_glob
    if method is not None:
        match["method"] = method.upper() or None
    if host is not None:
        match["host"] = host or None
    if ignore_query is not None:
        match["ignore_query"] = bool(ignore_query)

    response = rule.setdefault("response", {})
    if status is not None:
        response["status"] = int(status)
    if headers is not None:
        response["headers"] = headers
    if body_file is not None or body_text is not None:
        response["body_path"] = _store_body(
            identifier, body_file=body_file, body_text=body_text, registry=registry
        )
    if note is not None:
        rule["note"] = note or None

    rule["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_payload(payload, registry)
    return rule


def remove(identifier: str, registry: Path | None = None) -> dict[str, Any]:
    payload = _read_payload(registry)
    rule = _find(payload["mocks"], identifier)
    payload["mocks"] = [item for item in payload["mocks"] if item.get("id") != identifier]
    _write_payload(payload, registry)
    body_path = (rule.get("response") or {}).get("body_path")
    if body_path:
        try:
            Path(body_path).unlink()
        except OSError:
            pass
    return {"removed": identifier, "remaining": len(payload["mocks"])}


def clear(registry: Path | None = None) -> dict[str, Any]:
    payload = _read_payload(registry)
    count = len(payload["mocks"])
    for rule in payload["mocks"]:
        body_path = (rule.get("response") or {}).get("body_path")
        if body_path:
            try:
                Path(body_path).unlink()
            except OSError:
                pass
    payload["mocks"] = []
    _write_payload(payload, registry)
    return {"cleared": count}


def set_enabled(
    identifier: str | None,
    enabled: bool,
    *,
    all_rules: bool = False,
    registry: Path | None = None,
) -> dict[str, Any]:
    payload = _read_payload(registry)
    if all_rules:
        for rule in payload["mocks"]:
            rule["enabled"] = enabled
        _write_payload(payload, registry)
        return {"changed": len(payload["mocks"]), "enabled": enabled}
    if not identifier:
        raise errors.AutonomError(
            errors.MOCK_NOT_FOUND,
            "no mock id given",
            "Pass an id, or --all to change every rule.",
        )
    rule = _find(payload["mocks"], identifier)
    rule["enabled"] = enabled
    _write_payload(payload, registry)
    return {"changed": 1, "enabled": enabled, "mock": rule}


def snapshot(destination: Path, registry: Path | None = None) -> Path:
    """Freeze the active set into session artifacts as evidence.

    Enforcement always reads the live registry; this copy exists so an archived
    run records which rules were in force while it happened.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {"captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "mocks": active(registry)}
    destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    os.chmod(destination, 0o600)
    return destination
