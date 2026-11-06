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
from . import redact

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


# --- request conditions and rewrite edits (MR-01..MR-11) ----------------------

MODES = ("respond", "rewrite")
# Per kind: at most this many conditions (or edits), each value at most this long.
MAX_CONDITIONS = 20
MAX_CONDITION_VALUE = 2048
_HEADER_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
CONDITION_KEYS = ("query", "request_headers", "body_contains", "body_json")
EDIT_KEYS = ("remove_headers", "json_set", "json_remove", "replace")
BODY_EDIT_KEYS = ("json_set", "json_remove", "replace")
# The registry spelling of each list.
_EDIT_FLAGS = {"remove_headers": "--remove-header", "json_set": "--json-set",
               "json_remove": "--json-remove", "replace": "--replace"}


def _invalid(message: str, hint: str) -> errors.AutonomError:
    return errors.AutonomError(errors.INVALID_VALUE, message, hint)


def rule_mode(rule: dict[str, Any] | None) -> str:
    """`rewrite` for a rewrite rule; `respond` otherwise (absent included)."""
    return "rewrite" if (rule or {}).get("mode") == "rewrite" else "respond"


def check_mode(mode: Any) -> str | None:
    """None (not given), `respond` or `rewrite`; anything else is refused."""
    if mode is None:
        return None
    if mode not in MODES:
        raise _invalid(f"mode {mode!r} is neither 'respond' nor 'rewrite'",
                       "Pass --rewrite to edit the real response, --no-rewrite to fake it.")
    return mode


def _check_count(items: list[Any], flag: str) -> None:
    if len(items) > MAX_CONDITIONS:
        raise _invalid(f"{flag} given {len(items)} times; at most {MAX_CONDITIONS} are allowed",
                       f"Keep {flag} to {MAX_CONDITIONS} entries or fewer.")


def _check_length(text: str, flag: str, what: str = "value") -> None:
    if len(text) > MAX_CONDITION_VALUE:
        raise _invalid(f"{flag} {what} is longer than {MAX_CONDITION_VALUE} characters",
                       f"Shorten the {flag} {what}, or use a glob such as 'prefix*'.")


def _check_header_name(name: Any, flag: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise _invalid(f"{flag} has an empty header name", f"Pass {flag} 'Name: value'.")
    name = name.strip()
    if not _HEADER_TOKEN.fullmatch(name):
        raise _invalid(f"{flag} header name {name!r} is not a valid HTTP header name",
                       f"A {flag} name uses letters, digits and !#$%&'*+-.^_`|~ only, "
                       "no spaces.")
    _check_length(name, flag, "name")
    return name


def validate_json_path(path: Any, flag: str) -> str:
    """A dotted path such as `user.id` or `items.0.id` (DEC-004)."""
    if not isinstance(path, str) or not path:
        raise _invalid(f"{flag} has an empty JSON path",
                       f"Pass {flag} with a dotted path such as user.id or items.0.id.")
    if any(segment == "" for segment in path.split(".")):
        raise _invalid(f"{flag} path {path!r} has an empty segment",
                       f"Pass {flag} with a dotted path such as user.id or items.0.id.")
    _check_length(path, flag, "path")
    return path


def _reject_constant(name: str) -> Any:
    raise ValueError(name)


def parse_json_value(text: str) -> Any:
    """`2` -> 2, `"pro"` -> "pro", `true` -> True; anything else stays text."""
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return text


def parse_query_arg(raw: str) -> dict[str, Any]:
    """`--query NAME[=GLOB]`: `page=2`, or `debug` for "present with any value"."""
    name, equals, value = str(raw).partition("=")
    if not name.strip():
        raise _invalid(f"malformed --query {raw!r}: the parameter name is empty",
                       "Pass --query NAME or --query NAME=GLOB, e.g. --query page=2.")
    return {"name": name.strip(), "value": value if equals else None}


def parse_header_condition(raw: str) -> dict[str, Any]:
    """`--request-header 'Name[: GLOB]'`; a bare name means "present"."""
    name, colon, value = str(raw).partition(":")
    return {"name": _check_header_name(name, "--request-header"),
            "value": value.strip() if colon else None}


def parse_path_value(raw: str, flag: str = "--body-json") -> dict[str, Any]:
    """`PATH=VALUE`, VALUE read as JSON when it parses, else as text."""
    path, equals, value = str(raw).partition("=")
    if not equals:
        raise _invalid(f"malformed {flag} {raw!r}: expected PATH=VALUE",
                       f"Pass {flag} PATH=VALUE, e.g. {flag} user.role=admin.")
    return {"path": validate_json_path(path.strip(), flag), "value": parse_json_value(value)}


def parse_replace(pair: Any) -> dict[str, str]:
    """`--replace OLD NEW` as `{"find": OLD, "with": NEW}`."""
    if isinstance(pair, dict):
        return {"find": pair.get("find"), "with": pair.get("with")}  # type: ignore[dict-item]
    if isinstance(pair, (list, tuple)) and len(pair) == 2:
        return {"find": pair[0], "with": pair[1]}
    raise _invalid(f"malformed --replace {pair!r}: expected OLD NEW",
                   "Pass --replace OLD NEW, e.g. --replace Free Pro.")


def _named_list(items: Any, flag: str, *, header: bool) -> list[dict[str, Any]] | None:
    if items is None:
        return None
    if not isinstance(items, (list, tuple)):
        raise _invalid(f"{flag} must be a list", f"Pass {flag} once per condition.")
    _check_count(list(items), flag)
    result = []
    for item in items:
        if isinstance(item, str):
            item = parse_header_condition(item) if header else parse_query_arg(item)
        if not isinstance(item, dict):
            raise _invalid(f"{flag} entry {item!r} is not a name/value pair",
                           f"Pass {flag} as NAME or NAME=GLOB.")
        name, value = item.get("name"), item.get("value")
        if header:
            name = _check_header_name(name, flag)
        else:
            if not isinstance(name, str) or not name.strip():
                raise _invalid(f"{flag} has an empty name",
                               f"Pass {flag} NAME or NAME=GLOB, e.g. page=2.")
            name = name.strip()
            _check_length(name, flag, "name")
        if value is not None:
            if not isinstance(value, str):
                raise _invalid(f"{flag} value for {name!r} must be text",
                               f"Pass {flag} with a text value or glob.")
            _check_length(value, flag)
        result.append({"name": name, "value": value})
    return result


def _path_value_list(items: Any, flag: str) -> list[dict[str, Any]] | None:
    if items is None:
        return None
    if not isinstance(items, (list, tuple)):
        raise _invalid(f"{flag} must be a list", f"Pass {flag} once per field.")
    _check_count(list(items), flag)
    result = []
    for item in items:
        if isinstance(item, str):
            item = parse_path_value(item, flag)
        if not isinstance(item, dict) or "value" not in item:
            raise _invalid(f"{flag} entry {item!r} is not PATH=VALUE",
                           f"Pass {flag} PATH=VALUE, e.g. {flag} user.role=admin.")
        path = validate_json_path(item.get("path"), flag)
        try:
            encoded = json.dumps(item["value"], allow_nan=False)
        except (TypeError, ValueError):
            raise _invalid(f"{flag} value for {path!r} is not a JSON value",
                           f"Pass {flag} PATH=VALUE with a JSON value or plain text.") from None
        _check_length(encoded, flag)
        result.append({"path": path, "value": json.loads(encoded)})
    return result


def _text_list(items: Any, flag: str, *, what: str) -> list[str]:
    if not isinstance(items, (list, tuple)):
        raise _invalid(f"{flag} must be a list", f"Pass {flag} once per {what}.")
    _check_count(list(items), flag)
    for item in items:
        if not isinstance(item, str) or not item:
            raise _invalid(f"{flag} has an empty {what}", f"Pass {flag} with a non-empty {what}.")
        _check_length(item, flag, what)
    return list(items)


def validate_conditions(*, query: Any = None, request_headers: Any = None,
                        body_contains: Any = None, body_json: Any = None) -> dict[str, Any]:
    """Checked request conditions (MR-05); a None entry means "not given".

    Entries may be structured (`{"name", "value"}`, `{"path", "value"}`) or
    the flag text (`page=2`, `X-Client: ios*`, `user.role=admin`).
    """
    return {
        "query": _named_list(query, "--query", header=False),
        "request_headers": _named_list(request_headers, "--request-header", header=True),
        "body_contains": (None if body_contains is None
                          else _text_list(body_contains, "--body-contains", what="text")),
        "body_json": _path_value_list(body_json, "--body-json"),
    }


def validate_rewrite(*, remove_headers: Any = None, json_set: Any = None,
                     json_remove: Any = None, replace: Any = None) -> dict[str, Any]:
    """Checked rewrite edits; a None entry means "not given"."""
    removed = None
    if remove_headers is not None:
        if not isinstance(remove_headers, (list, tuple)):
            raise _invalid("--remove-header must be a list",
                           "Pass --remove-header once per header.")
        _check_count(list(remove_headers), "--remove-header")
        removed = [_check_header_name(name, "--remove-header") for name in remove_headers]
    removals = None
    if json_remove is not None:
        removals = [validate_json_path(path, "--json-remove")
                    for path in _text_list(json_remove, "--json-remove", what="path")]
    replacements = None
    if replace is not None:
        if not isinstance(replace, (list, tuple)):
            raise _invalid("--replace must be a list", "Pass --replace OLD NEW per edit.")
        _check_count(list(replace), "--replace")
        replacements = []
        for pair in replace:
            item = parse_replace(pair)
            if not isinstance(item["find"], str) or not item["find"]:
                raise _invalid("--replace has empty text to find",
                               "Pass --replace OLD NEW with a non-empty OLD.")
            if not isinstance(item["with"], str):
                raise _invalid("--replace replacement must be text",
                               "Pass --replace OLD NEW.")
            _check_length(item["find"], "--replace", "text")
            _check_length(item["with"], "--replace", "replacement")
            replacements.append({"find": item["find"], "with": item["with"]})
    return {"remove_headers": removed, "json_set": _path_value_list(json_set, "--json-set"),
            "json_remove": removals, "replace": replacements}


def _check_rule_shape(mode: str, edits: dict[str, list[Any]], *, whole_body: bool,
                      hang: bool, edits_given: bool) -> None:
    """The combinations a rule may not have (MR-07, MR-09, MR-11)."""
    named = [_EDIT_FLAGS[key] for key in EDIT_KEYS if edits.get(key)]
    body_named = [_EDIT_FLAGS[key] for key in BODY_EDIT_KEYS if edits.get(key)]
    if mode == "respond" and named:
        raise _invalid(f"{', '.join(named)} edit(s) a real response and need --rewrite",
                       "Add --rewrite, or drop the edit flags (--clear-edits on update).")
    if mode == "rewrite" and hang:
        raise _invalid("--hang cannot be used with --rewrite",
                       "A rewrite rule waits for the real response; use --delay SECONDS.")
    if mode == "rewrite" and whole_body and body_named and edits_given:
        raise _invalid("a whole body (--json or --body-file) and body edits "
                       f"({', '.join(body_named)}) exclude each other",
                       "Replace the body whole, or edit it with --json-set, "
                       "--json-remove and --replace.")


def _store_conditions(match: dict[str, Any], conditions: dict[str, Any]) -> None:
    """Write given condition lists; an empty list removes that kind."""
    for key, stored in (("query", "query"), ("request_headers", "headers")):
        value = conditions.get(key)
        if value is None:
            continue
        if value:
            match[stored] = value
        else:
            match.pop(stored, None)
    body = dict(match.get("body") or {}) if isinstance(match.get("body"), dict) else {}
    for key, stored in (("body_contains", "contains"), ("body_json", "json")):
        value = conditions.get(key)
        if value is None:
            continue
        if value:
            body[stored] = value
        else:
            body.pop(stored, None)
    if body:
        match["body"] = body
    else:
        match.pop("body", None)


def _stored_edits(rule: dict[str, Any]) -> dict[str, list[Any]]:
    rewrite = rule.get("rewrite") if isinstance(rule.get("rewrite"), dict) else {}
    return {key: list(rewrite.get(key) or []) for key in EDIT_KEYS}


def _store_edits(rule: dict[str, Any], edits: dict[str, list[Any]]) -> None:
    kept = {key: edits[key] for key in EDIT_KEYS if edits.get(key)}
    if kept:
        rule["rewrite"] = kept
    else:
        rule.pop("rewrite", None)


def _restore_masked(given: list[dict[str, Any]] | None, stored: Any, key: str
                    ) -> list[dict[str, Any]] | None:
    """A value shown masked and sent back unchanged keeps the stored secret.

    `mocks.list` masks secret condition values; a form that saves what it was
    shown must not overwrite the real value with the placeholder.
    """
    if not given or not isinstance(stored, list):
        return given
    pool = [item for item in stored if isinstance(item, dict)]
    result = []
    for item in given:
        if item.get("value") == redact.PLACEHOLDER:
            for index, old in enumerate(pool):
                if old.get(key) == item.get(key):
                    item = {**item, "value": old.get("value")}
                    pool.pop(index)
                    break
        result.append(item)
    return result


def _restore_masked_headers(given: dict[str, str] | None, stored: Any
                            ) -> dict[str, str] | None:
    """Response headers shown masked and sent back unchanged keep the stored value.

    Header names compare without case; a placeholder with no stored header of
    that name is kept as given.
    """
    if not given or not isinstance(stored, dict):
        return given
    by_name = {str(name).lower(): value for name, value in stored.items()}
    return {name: (by_name[name.lower()]
                   if value == redact.PLACEHOLDER and name.lower() in by_name else value)
            for name, value in given.items()}


def _sensitive_path(path: Any) -> bool:
    return isinstance(path, str) and path.rsplit(".", 1)[-1].lower() in redact.SENSITIVE_FIELDS


def mask_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """A copy safe to print: secret condition, header and JSON-set values masked (MR-12).

    Masked: request-header conditions on sensitive headers (Authorization,
    Cookie, API keys), response headers with those names (a rewrite rule's
    set headers and a fake response's headers alike), query conditions whose
    names look secret, and JSON values whose last path segment is a
    credential field. Never raises on a hand-edited rule.
    """
    copy = json.loads(json.dumps(rule, default=str))
    match = copy.get("match") if isinstance(copy.get("match"), dict) else {}
    for key, secret in (("query", redact.is_sensitive_query_key),
                        ("headers", lambda name: name.lower() in redact.REDACTED_HEADERS)):
        for item in match.get(key) or [] if isinstance(match.get(key), list) else []:
            if isinstance(item, dict) and isinstance(item.get("name"), str) \
                    and item.get("value") is not None and secret(item["name"]):
                item["value"] = redact.PLACEHOLDER
    body = match.get("body") if isinstance(match.get("body"), dict) else {}
    rewrite = copy.get("rewrite") if isinstance(copy.get("rewrite"), dict) else {}
    for items in (body.get("json"), rewrite.get("json_set")):
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and _sensitive_path(item.get("path")):
                item["value"] = redact.PLACEHOLDER
    response = copy.get("response") if isinstance(copy.get("response"), dict) else {}
    headers = response.get("headers")
    if isinstance(headers, dict):
        for name, value in headers.items():
            if value is not None and str(name).lower() in redact.REDACTED_HEADERS:
                headers[name] = redact.PLACEHOLDER
    return copy


def condition_counts(rule: dict[str, Any]) -> dict[str, int]:
    """How many conditions of each kind a rule has (journal words use counts)."""
    match = rule.get("match") if isinstance(rule.get("match"), dict) else {}
    body = match.get("body") if isinstance(match.get("body"), dict) else {}

    def count(value: Any) -> int:
        return len(value) if isinstance(value, list) else 0

    return {"query": count(match.get("query")), "request_header": count(match.get("headers")),
            "body_contains": count(body.get("contains")), "body_json": count(body.get("json"))}


def edit_counts(rule: dict[str, Any]) -> dict[str, int]:
    rewrite = rule.get("rewrite") if isinstance(rule.get("rewrite"), dict) else {}
    return {key: len(rewrite[key]) if isinstance(rewrite.get(key), list) else 0
            for key in EDIT_KEYS}


def _plural(count: int, word: str) -> str:
    return f"+{count} {word}" + ("" if count == 1 else "s")


def describe(rule: dict[str, Any]) -> dict[str, Any]:
    """Mode and a short masked summary of a rule's conditions and edits.

    `summary` reads like `page=2 · +1 header`; `edits` like
    `status 503 · -1 header · json_set×1`. Secret values never appear.
    """
    masked = mask_rule(rule)
    match = masked.get("match") if isinstance(masked.get("match"), dict) else {}
    counts = condition_counts(masked)
    parts = []
    query = match.get("query") if isinstance(match.get("query"), list) else []
    shown = [item for item in query if isinstance(item, dict)][:2]
    for item in shown:
        name, value = item.get("name"), item.get("value")
        parts.append(f"{name}" if value is None else f"{name}={value}")
    if counts["query"] > len(shown):
        parts.append(_plural(counts["query"] - len(shown), "query param"))
    if counts["request_header"]:
        parts.append(_plural(counts["request_header"], "header"))
    if counts["body_contains"]:
        parts.append(_plural(counts["body_contains"], "body text"))
    if counts["body_json"]:
        parts.append(_plural(counts["body_json"], "body field"))
    mode = rule_mode(rule)
    edits: list[str] = []
    if mode == "rewrite":
        response = masked.get("response") if isinstance(masked.get("response"), dict) else {}
        if response.get("status") is not None:
            edits.append(f"status {response['status']}")
        stored = edit_counts(masked)
        if stored["remove_headers"]:
            edits.append(f"-{stored['remove_headers']} header"
                         + ("" if stored["remove_headers"] == 1 else "s"))
        headers = response.get("headers") if isinstance(response.get("headers"), dict) else {}
        if headers:
            edits.append(_plural(len(headers), "header"))
        if response.get("body_path"):
            edits.append("whole body")
        for key in ("json_set", "json_remove", "replace"):
            if stored[key]:
                edits.append(f"{key}×{stored[key]}")
    return {"mode": mode, "conditions": sum(counts.values()),
            "edits": sum(edit_counts(rule).values()), "summary": " · ".join(parts),
            "edit_summary": " · ".join(edits)}


# --- CRUD ---------------------------------------------------------------------


def add(
    *,
    url_glob: str,
    method: str | None = None,
    host: str | None = None,
    ignore_query: bool = False,
    status: int | None = None,
    headers: dict[str, str] | None = None,
    body_file: Path | None = None,
    body_text: str | None = None,
    note: str | None = None,
    delay_s: float | None = None,
    hang: bool = False,
    mode: str | None = None,
    query: list[Any] | None = None,
    request_headers: list[Any] | None = None,
    body_contains: list[str] | None = None,
    body_json: list[Any] | None = None,
    remove_headers: list[str] | None = None,
    json_set: list[Any] | None = None,
    json_remove: list[str] | None = None,
    replace: list[Any] | None = None,
    registry: Path | None = None,
) -> dict[str, Any]:
    """Add a rule. `status` defaults to 200 for a fake response; a `rewrite`
    rule without one keeps the real server's status.

    Request conditions (`query`, `request_headers`, `body_contains`,
    `body_json`) narrow any rule; `remove_headers`, `json_set`, `json_remove`
    and `replace` edit the real body and need `mode="rewrite"`. In rewrite
    mode `headers` are headers to set and a body replaces the real one whole.
    Everything is checked before anything is written.
    """
    require_target(url_glob)
    mode = check_mode(mode) or "respond"
    validate_response(status=status, body_text=body_text)
    delay_s = validate_delay(delay_s, hang)
    conditions = validate_conditions(query=query, request_headers=request_headers,
                                     body_contains=body_contains, body_json=body_json)
    given = validate_rewrite(remove_headers=remove_headers, json_set=json_set,
                             json_remove=json_remove, replace=replace)
    edits = {key: given[key] or [] for key in EDIT_KEYS}
    _check_rule_shape(mode, edits, hang=bool(hang), edits_given=True,
                      whole_body=body_file is not None or body_text is not None)
    if mode == "rewrite":
        for name in headers or {}:
            _check_header_name(name, "--header")
    if status is None and mode == "respond":
        status = 200
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
    rule: dict[str, Any] = {
        "id": identifier,
        "match": {
            "url_glob": url_glob,
            "method": method.upper() if method else None,
            "host": host,
            "ignore_query": bool(ignore_query),
        },
        "response": {
            "status": int(status) if status is not None else None,
            "headers": headers or {},
            "body_path": body_path,
        },
        "enabled": True,
        "note": note,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if mode == "rewrite":
        rule["mode"] = "rewrite"
    _store_conditions(rule["match"], conditions)
    _store_edits(rule, edits)
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
    mode: str | None = None,
    query: list[Any] | None = None,
    request_headers: list[Any] | None = None,
    body_contains: list[str] | None = None,
    body_json: list[Any] | None = None,
    remove_headers: list[str] | None = None,
    json_set: list[Any] | None = None,
    json_remove: list[str] | None = None,
    replace: list[Any] | None = None,
    clear_conditions: bool = False,
    clear_edits: bool = False,
    clear_status: bool = False,
    clear_body: bool = False,
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

    Conditions and edits: a list that is given replaces that whole list (an
    empty list clears it); `clear_conditions` / `clear_edits` drop them all
    first. A condition value, JSON-set value or response header value equal to
    the masked placeholder keeps the stored value. `mode` switches between `respond` and `rewrite`; `clear_status`
    makes a rewrite rule keep the real status; `clear_body` drops a stored
    body. In rewrite mode a new body replaces stored body edits and new body
    edits replace a stored body; giving both at once is refused.
    """
    validate_response(status=status, body_text=body_text)
    delay_s = validate_delay(delay_s, hang)
    mode = check_mode(mode)
    conditions = validate_conditions(query=query, request_headers=request_headers,
                                     body_contains=body_contains, body_json=body_json)
    given = validate_rewrite(remove_headers=remove_headers, json_set=json_set,
                             json_remove=json_remove, replace=replace)
    if clear_status and status is not None:
        raise _invalid("--status and clearing the status both given",
                       "Pass one of them.")
    new_body = body_file is not None or body_text is not None
    if clear_body and new_body:
        raise _invalid("a new body and clearing the body both given", "Pass one of them.")
    payload = _read_payload(registry)
    rule = _find(payload["mocks"], identifier)

    # The final shape is checked before anything (a body file included) is written.
    final_mode = mode or rule_mode(rule)
    edits = {key: [] if clear_edits else stored
             for key, stored in _stored_edits(rule).items()}
    body_edits_given = any(given[key] is not None for key in BODY_EDIT_KEYS)
    for key in EDIT_KEYS:
        if given[key] is not None:
            edits[key] = given[key]
    final_hang = bool(hang) if hang is not None else (
        rule.get("hang") is True and delay_s is None)
    _check_rule_shape(final_mode, edits, hang=final_hang, edits_given=body_edits_given,
                      whole_body=new_body)
    if final_mode == "respond" and clear_status:
        raise _invalid("a fake response needs a status",
                       "Pass --status, or --rewrite to keep the real status.")
    if final_mode == "rewrite":
        for name in headers or {}:
            _check_header_name(name, "--header")

    match = rule.setdefault("match", {})
    if url_glob is not None:
        match["url_glob"] = url_glob
    if method is not None:
        match["method"] = method.upper() or None
    if host is not None:
        match["host"] = host or None
    if ignore_query is not None:
        match["ignore_query"] = bool(ignore_query)
    if clear_conditions:
        _store_conditions(match, {key: [] for key in CONDITION_KEYS})
    body_stored = match.get("body") if isinstance(match.get("body"), dict) else {}
    conditions["query"] = _restore_masked(conditions["query"], match.get("query"), "name")
    conditions["request_headers"] = _restore_masked(
        conditions["request_headers"], match.get("headers"), "name")
    conditions["body_json"] = _restore_masked(conditions["body_json"], body_stored.get("json"),
                                              "path")
    _store_conditions(match, conditions)
    if given["json_set"] is not None:
        edits["json_set"] = _restore_masked(given["json_set"],
                                            _stored_edits(rule)["json_set"], "path") or []

    response = rule.setdefault("response", {})
    if status is not None:
        response["status"] = int(status)
    elif clear_status:
        response["status"] = None
    implicit = response.get("content_type_implicit") is True
    current = dict(response.get("headers") or {})
    headers = _restore_masked_headers(headers, response.get("headers"))
    if headers is not None:
        kept = _content_type_value(current) if implicit else None
        current = dict(headers)
        if has_content_type(current):
            implicit = False  # the author named the type: it is explicit now
        elif kept is not None:
            current["Content-Type"] = kept  # header-only update keeps it
        else:
            implicit = False
    drop_body = clear_body or (final_mode == "rewrite" and body_edits_given
                               and any(edits[key] for key in BODY_EDIT_KEYS))
    if new_body:
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
        if final_mode == "rewrite" and not body_edits_given:
            # A whole new body replaces stored body edits (one body choice).
            for key in BODY_EDIT_KEYS:
                edits[key] = []
    elif drop_body and response.get("body_path"):
        _unlink_body(response.get("body_path"))
        response["body_path"] = None
        if implicit:
            current = _without_content_type(current)
            implicit = False
    if headers is not None or new_body or drop_body:
        response["headers"] = current
        if implicit:
            response["content_type_implicit"] = True
        else:
            response.pop("content_type_implicit", None)
    if final_mode == "respond" and response.get("status") is None:
        response["status"] = 200
    if final_mode == "rewrite":
        rule["mode"] = "rewrite"
    else:
        rule.pop("mode", None)
    _store_edits(rule, edits)
    if note is not None:
        rule["note"] = note or None
    _apply_timing(rule, delay_s, hang)

    rule["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_payload(payload, registry)
    return rule


def _unlink_body(body_path: Any) -> None:
    if body_path:
        try:
            Path(body_path).unlink()
        except OSError:
            pass


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
