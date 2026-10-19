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
import math
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from .. import errors

BODY_INLINE_LIMIT = 1_048_576  # 1 MiB — an inline --json body beyond this is a file
# A delayed or held response never waits longer than this; the addon applies
# the same cap to `hang` and then kills the flow (recorded as incomplete).
MAX_DELAY_S = 120.0
DELAY_HINT = "delay is seconds in (0, 120], e.g. 2.5"

# `--body-file` without an explicit Content-Type header: the type follows the
# file extension, so a JSON fixture is not served as an untyped blob.
CONTENT_TYPES_BY_EXTENSION = {
    ".json": "application/json",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
DEFAULT_CONTENT_TYPE = "application/octet-stream"


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


def persistent_mocks_warning(state: dict[str, Any] | None = None, *,
                             registry: Path | None = None) -> dict[str, str] | None:
    """The `persistent_mocks_active` warning, or None when no rule is enabled.

    `network start` and `doctor` raised it; `network status` — the verb an
    agent re-runs while judging evidence — did not, although the skill says
    it does. One definition, so the three cannot drift apart again. `state`
    is a `summary()` the caller already has.
    """
    state = state if state is not None else summary(registry)
    if not state.get("active"):
        return None
    return {
        "code": "persistent_mocks_active",
        "error": f"{state['active']} mock rule(s) loaded from the persistent "
                 f"registry — matching responses WILL be faked",
        "hint": "Review with 'autonom network mock list', switch off with "
                "'autonom network mock disable --all'.",
    }


def hit_counts(flows: Iterable[dict[str, Any]]) -> dict[str, int]:
    """How often each rule actually fired, from recorded flows' `mock_id`."""
    counts: dict[str, int] = {}
    for flow in flows:
        identifier = flow.get("mock_id") if isinstance(flow, dict) else None
        if identifier:
            counts[identifier] = counts.get(identifier, 0) + 1
    return counts


def annotate_hits(rules: list[dict[str, Any]],
                  counts: dict[str, int]) -> list[dict[str, str]]:
    """Set each rule's `hits` from `counts`; return the `mock_never_matched`
    warning (or nothing) for enabled rules that never fired.

    The warning is only worth giving once some rule HAS fired: otherwise the
    proxy simply has not seen traffic yet, which is a different problem.
    """
    for rule in rules:
        rule["hits"] = counts.get(rule.get("id"), 0)
    idle = [r["id"] for r in rules if r.get("enabled", True) and not r["hits"]]
    if idle and counts:
        return [{
            "code": "mock_never_matched",
            "error": "enabled rule(s) that have not matched a single request: "
                     + ", ".join(idle),
            "hint": "Check the glob against a real URL — 'network requests list' "
                    "shows the full URL, and a query string defeats a glob that "
                    "ends at the path.",
        }]
    return []


def session_hit_counts(record: dict[str, Any] | None = None) -> dict[str, int]:
    """`hit_counts` over a session's flow store — the current session's when
    `record` is None; empty when there is no session or no store."""
    from .. import session as session_mod
    from . import store

    try:
        record = record if record is not None else session_mod.load_current()
        if not record:
            return {}
        flows, _warnings = store.read_all(record)
    except (errors.AutonomError, OSError, ValueError, KeyError):
        return {}
    return hit_counts(flows)


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


def parse_header_args(values: Iterable[str] | None) -> dict[str, str] | None:
    """`--header 'Name: value'` (repeatable) as a mapping; None when unused.

    A value without a colon, or with an empty name, is refused rather than
    dropped: a rule that silently lost the header its author asked for would
    serve a response nobody intended.
    """
    if not values:
        return None
    headers: dict[str, str] = {}
    for raw in values:
        name, colon, value = str(raw).partition(":")
        if not colon or not name.strip():
            raise errors.AutonomError(
                errors.INVALID_VALUE,
                f"malformed --header {raw!r}: expected 'Name: value'",
                "Pass each header as --header 'Name: value'.",
            )
        headers[name.strip()] = value.strip()
    return headers


def content_type_for(path: Path | str) -> str:
    """The Content-Type a `--body-file` is served with when none is given."""
    return CONTENT_TYPES_BY_EXTENSION.get(Path(str(path)).suffix.lower(), DEFAULT_CONTENT_TYPE)


def has_content_type(headers: dict[str, str] | None) -> bool:
    return any(str(name).strip().lower() == "content-type" for name in (headers or {}))


def with_body_file_content_type(headers: dict[str, str] | None,
                                body_file: Path | str | None) -> dict[str, str]:
    """`headers` plus a Content-Type from `body_file`'s extension.

    An explicit Content-Type header (any case) always wins; without a body
    file the headers are returned unchanged.
    """
    result = dict(headers or {})
    if body_file is not None and not has_content_type(result):
        result["Content-Type"] = content_type_for(body_file)
    return result


def implicit_content_type(body_file: Path | str | None = None,
                          body_text: str | None = None) -> str | None:
    """The type a body gets when its author named none, or None.

    A `--body-file` follows its extension; an inline `--json` body that looks
    like JSON (starts with `{` or `[`) is application/json; other inline text
    gets no type, as before.
    """
    if body_file is not None:
        return content_type_for(body_file)
    if body_text is not None and body_text.lstrip()[:1] in ("{", "["):
        return "application/json"
    return None


def _without_content_type(headers: dict[str, str] | None) -> dict[str, str]:
    return {name: value for name, value in (headers or {}).items()
            if str(name).strip().lower() != "content-type"}


def _content_type_value(headers: dict[str, str] | None) -> str | None:
    for name, value in (headers or {}).items():
        if str(name).strip().lower() == "content-type":
            return value
    return None


# A delay given as text is a plain decimal: `3`, `0.5`, `.5`, `2.` — never
# `2_5`, `1e1`, `+3`, `inf` or `nan`, which float() would read.
_PLAIN_DECIMAL = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")


def validate_delay(delay_s: Any = None, hang: Any = None) -> float | None:
    """The checked `delay_s` (or None). `delay_s` and `hang` exclude each other.

    A delay must be a finite number of seconds in (0, 120]; a bool, a string
    that is not a plain decimal (``_PLAIN_DECIMAL``: no underscores, signs or
    exponents), NaN or infinity is refused like an out-of-range one.
    """
    if delay_s is not None and hang:
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            "--delay and --hang both given; a mock rule takes one of them",
            DELAY_HINT,
        )
    if delay_s is None:
        return None
    try:
        if isinstance(delay_s, bool):
            raise ValueError("bool")
        if isinstance(delay_s, str) and not _PLAIN_DECIMAL.fullmatch(delay_s.strip()):
            raise ValueError("not a plain decimal")
        seconds = float(delay_s)
    except (TypeError, ValueError):
        seconds = math.nan
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_DELAY_S:
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            f"--delay {delay_s} is not a number of seconds in (0, 120]",
            DELAY_HINT,
        )
    return seconds


def _apply_timing(rule: dict[str, Any], delay_s: float | None, hang: bool | None) -> None:
    """Store `delay_s` / `hang` on a rule so it never carries both."""
    if delay_s is not None:
        rule["delay_s"] = delay_s
        rule.pop("hang", None)
    if hang:
        rule["hang"] = True
        rule.pop("delay_s", None)
    elif hang is not None:
        rule.pop("hang", None)


def validate_response(*, status: int | None = None, body_text: str | None = None) -> None:
    """Refuse a response the proxy could not serve as the author meant it.

    `status` must be a real HTTP status (100..599). An inline body that looks
    like JSON (starts with `{` or `[`) must parse: `--json '{bad'` would
    otherwise be served verbatim under `Content-Type: application/json`.
    Plain text bodies are still allowed.
    """
    if status is not None:
        try:
            code = int(status)
        except (TypeError, ValueError):
            code = -1
        if not 100 <= code <= 599:
            raise errors.AutonomError(
                errors.INVALID_VALUE,
                f"--status {status} is not an HTTP status (100..599)",
                "Pass a status such as 200, 404 or 503.",
            )
    if body_text is not None and body_text.lstrip()[:1] in ("{", "["):
        try:
            json.loads(body_text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise errors.AutonomError(
                errors.INVALID_VALUE,
                f"--json is not valid JSON: {exc}",
                "Fix the JSON, or pass the body with --body-file.",
            ) from None


def require_target(url_glob: str | None) -> None:
    """A rule without a URL target would match nothing (or, as `*`, everything)."""
    if not url_glob:
        raise errors.AutonomError(
            errors.SELECTOR_REQUIRED,
            "no target given",
            "Pass --url <full URL> or --match <glob>.",
        )


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
    delay_s: float | None = None,
    hang: bool = False,
    registry: Path | None = None,
) -> dict[str, Any]:
    require_target(url_glob)
    validate_response(status=status, body_text=body_text)
    delay_s = validate_delay(delay_s, hang)
    # A type the author did not name is marked implicit, so a later body
    # change can replace it while an explicit `--header` type always stays.
    implicit = None if has_content_type(headers) else implicit_content_type(body_file, body_text)
    headers = dict(headers or {})
    if implicit:
        headers["Content-Type"] = implicit
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
    if implicit:
        rule["response"]["content_type_implicit"] = True
    _apply_timing(rule, delay_s, hang or None)
    payload["mocks"].append(rule)
    _write_payload(payload, registry)
    return rule


def get(identifier: str, registry: Path | None = None, *,
        record: dict[str, Any] | None = None,
        counts: dict[str, int] | None = None) -> dict[str, Any]:
    """One rule, with `hits` — how often it fired in the session's flows —
    exactly as `mock list` shows it (`mock show` used to omit it).

    Hits come from `counts`, else from `record`'s flow store, else — for the
    machine registry only — the current session's. An explicit `registry`
    (a test seam) never reaches for the current session.
    """
    rule = dict(_find(load(registry), identifier))
    if counts is None:
        counts = (session_hit_counts(record) if record is not None or registry is None
                  else {})
    rule["hits"] = counts.get(identifier, 0)
    return rule


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
    delay_s: float | None = None,
    hang: bool | None = None,
    registry: Path | None = None,
) -> dict[str, Any]:
    """Partial update. Only the fields actually supplied are touched.

    A new `delay_s` replaces a stored `hang` and `hang=True` a stored delay;
    `hang=False` switches a held rule back to answering at once.

    Content-Type: `headers` replaces the header set, but an implicit type
    (from the body file's extension or a JSON-looking inline body) survives a
    header-only update that names no Content-Type. A new body (`body_file` or
    `body_text`) replaces an implicit type with the new body's own; a
    Content-Type the author gave explicitly is never replaced by a body change.
    """
    validate_response(status=status, body_text=body_text)
    delay_s = validate_delay(delay_s, hang)
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
    implicit = response.get("content_type_implicit") is True
    current = dict(response.get("headers") or {})
    if headers is not None:
        kept = _content_type_value(current) if implicit else None
        current = dict(headers)
        if has_content_type(current):
            implicit = False  # the author named the type: it is explicit now
        elif kept is not None:
            current["Content-Type"] = kept  # header-only update keeps it
        else:
            implicit = False
    if body_file is not None or body_text is not None:
        response["body_path"] = _store_body(
            identifier, body_file=body_file, body_text=body_text, registry=registry
        )
        if implicit or not has_content_type(current):
            # No type of the author's own: follow the new body.
            current = _without_content_type(current)
            new_type = implicit_content_type(body_file, body_text)
            implicit = new_type is not None
            if new_type:
                current["Content-Type"] = new_type
    if headers is not None or body_file is not None or body_text is not None:
        response["headers"] = current
        if implicit:
            response["content_type_implicit"] = True
        else:
            response.pop("content_type_implicit", None)
    if note is not None:
        rule["note"] = note or None
    _apply_timing(rule, delay_s, hang)

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
