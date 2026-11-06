"""Shared pieces of the Autonom MCP server: ids, envelope, outcomes, schemas.

Contract sections 2.4 (envelope), 2.5 (outcomes) and 2.7 (schemas). Every
tool module builds on these helpers, so one rule set covers every tool:

- ``Tool`` is the catalog entry each module exports in its ``TOOLS`` tuple;
  its handler is ``handler(ctx, args) -> dict | Reply`` and raises
  ``ToolError`` for a failure.
- ``CallContext`` is what a handler gets: the shared ``Services``, the call's
  ``operation_id``, a cancellation event, warnings and the resolved session.
- ``output_schema`` merges the envelope properties with a tool's own fields;
  ``validate`` is the stdlib validator used for inputs and for the server's
  own outputs.

Python 3.9 compatible and standard library only.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

ADAPTER_VERSION = "0.1.0"
SCHEMA_VERSION = "autonom.mcp/v1"

HOST_ID_RE = r"^h_[a-z2-7]{26}$"
SESSION_ID_RE = r"^s_[A-Za-z0-9]{1,64}$"
TARGET_ID_RE = r"^[A-Za-z0-9._:-]{1,128}$"
OPERATION_ID_RE = r"^op_[0-9a-f]{16}$"
STREAM_ID_RE = r"^st_[0-9a-f]{16}$"
RUN_ID_RE = r"^run_[0-9a-f]{16}$"
ARTIFACT_ID_RE = r"^art_[0-9a-f]{24}$"
FLOW_ID_RE = r"^fl_[0-9a-f]{16}$"
DIAGNOSTICS_RUN_ID_RE = r"^diag-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$"

AGENT_HOLDER_PREFIX = "chatgpt-agent:"
AGENT_HOLDER_LABEL = "ChatGPT agent"

OUTCOMES = ("completed", "rejected", "failed", "outcome_unknown")
MAX_WARNINGS = 20
ALLOWED_META_KEYS = ("autonom/frames", "autonom/artifact")

HINTS = {
    "invalid_arguments": "Fix the named argument and call again.",
    "schema_violation": "The server built an invalid result; check the server log and report it.",
    "host_unavailable": ("Point autonom_cli in <state>/chatgpt-mcp/config.json (or AUTONOM_CLI) at an "
                         "Autonom CLI that has `autonom host info`."),
    "host_not_found": "Only the local host is supported yet; call list_hosts for its host_id.",
    "session_not_found": "Call list_sessions for the session ids of this host.",
    "session_stopped": "The session is stopped; start a new one with start_session.",
    "target_not_allowed": "Add the target to allowed_targets in the adapter config, or use another one.",
    "session_busy": "Another agent action runs on this session; wait for it to finish and call again.",
    "cli_unavailable": "Install the Autonom CLI or set autonom_cli and cli_python in the adapter config.",
    "cli_timeout": "Check the device with list_sessions and the screen before acting again.",
    "cli_output_invalid": "Check the screen and list_sessions before acting again.",
    "cli_failed": "Check the screen and list_sessions before acting again.",
    "diagnostics_invalid": "Send the results of the diagnostics page (schema_version 1 and a diag- run id).",
    "diagnostics_too_large": "Save a smaller results object (at most 256 KiB of compact JSON).",
    "diagnostics_contain_secret": "Remove tokens, bearer values and home paths from the results first.",
    "write_failed": "Check the permissions of <state>/chatgpt-mcp.",
    "internal_error": "Check the server log; the call may be retried when it is read-only.",
}

# ---------------------------------------------------------------- secrets

_SECRETS_LOCK = threading.Lock()
_SECRETS: List[str] = []
_TOKEN_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[^\s\"',;]+"),
    re.compile(r"(?i)((?:#|\?|&|\b)token=)[^\s\"'&#]+"),
)


def register_secret(value: Optional[str]) -> None:
    """Remember a value (the Canvas token) that must never leave the server."""
    if not isinstance(value, str) or len(value) < 4:
        return
    with _SECRETS_LOCK:
        if value not in _SECRETS:
            _SECRETS.append(value)


def known_secrets() -> List[str]:
    with _SECRETS_LOCK:
        return list(_SECRETS)


def redact(text: str, extra: Sequence[str] = ()) -> str:
    """``text`` with registered secrets, ``extra`` values and token shapes removed."""
    out = str(text)
    for value in sorted(set(known_secrets()) | {item for item in extra if isinstance(item, str)},
                        key=len, reverse=True):
        if value and len(value) >= 4:
            out = out.replace(value, "<redacted>")
    for pattern in _TOKEN_PATTERNS:
        out = pattern.sub(lambda match: match.group(1) + "<redacted>", out)
    return out


# A local prefix ends where a path name ends: "/Users/al" does not match "/Users/alice".
_PATH_END = r"(?![\w.~+@-])"


class PathScrubber:
    """Replaces local path prefixes (the home, the state base) with short labels.

    ``labels`` is a sequence of ``(prefix, label)`` pairs; the longest prefix
    wins and prefixes shorter than 2 characters (``/``) are ignored. ``text``
    also runs ``redact`` (with ``extra`` values), so one call cleans CLI text
    for a tool result or a log line (contract 2.4 and p3-020: no absolute
    local paths, no secrets).
    """

    def __init__(self, labels: Sequence[Tuple[str, str]] = (), extra: Sequence[str] = ()) -> None:
        self.extra = [value for value in extra if isinstance(value, str)]
        pairs: Dict[str, str] = {}
        for prefix, label in labels:
            if not isinstance(prefix, str):
                continue
            prefix = prefix.rstrip("/")
            if len(prefix) >= 2 and prefix not in pairs:
                pairs[prefix] = label
        self.labels = sorted(pairs.items(), key=lambda item: len(item[0]), reverse=True)
        self._patterns = [(re.compile(re.escape(prefix) + _PATH_END), label)
                          for prefix, label in self.labels]

    def text(self, value: Any) -> str:
        out = str(value)
        for pattern, label in self._patterns:
            out = pattern.sub(lambda _match, _label=label: _label, out)
        return redact(out, self.extra)

    def value(self, item: Any) -> Any:
        """``item`` with every string inside it (dict values, list and tuple items) scrubbed."""
        if isinstance(item, str):
            return self.text(item)
        if isinstance(item, dict):
            return {key: self.value(value) for key, value in item.items()}
        if isinstance(item, list):
            return [self.value(value) for value in item]
        if isinstance(item, tuple):
            return tuple(self.value(value) for value in item)
        return item


# ---------------------------------------------------------------- ids

def new_id(prefix: str, nbytes: int) -> str:
    return prefix + secrets.token_hex(nbytes)


def new_operation_id() -> str:
    return new_id("op_", 8)


def agent_holder_id(host_id: str) -> str:
    return AGENT_HOLDER_PREFIX + host_id


# ---------------------------------------------------------------- errors and tools

class ToolError(Exception):
    """A tool failure. ``rejected`` means nothing reached a device or session.

    ``extras`` become top-level envelope fields; every key must be declared
    in the envelope (``ERROR_FIELDS``) or in the tool's ``output_fields``,
    else the server's self-validation turns the result into
    ``schema_violation``.
    """

    def __init__(self, error_code: str, error: str, hint: Optional[str] = None, *,
                 rejected: bool = False, **extras: Any) -> None:
        super().__init__(error)
        self.error_code = error_code
        self.error = error
        self.hint = hint if hint is not None else HINTS.get(error_code)
        self.rejected = rejected
        self.extras = extras


def rejected(error_code: str, error: str, hint: Optional[str] = None, **extras: Any) -> ToolError:
    return ToolError(error_code, error, hint, rejected=True, **extras)


def bad_argument(field: str, problem: str) -> ToolError:
    return rejected("invalid_arguments", "Invalid argument %s: %s" % (field, problem),
                    field=field[:200], problem=problem[:200])


class Tool(NamedTuple):
    """One catalog entry (contract 2.9)."""

    name: str
    title: str
    description: str
    input_schema: Dict[str, Any]
    output_fields: Dict[str, Any]
    annotations: Dict[str, Any]
    meta: Dict[str, Any]
    mutating: bool
    handler: Callable[..., Any]


def read_only(tool: Tool) -> bool:
    return bool(tool.annotations.get("readOnlyHint"))


def session_scoped(tool: Tool) -> bool:
    return "session_id" in (tool.input_schema.get("properties") or {})


class Reply:
    """A handler result with extra content blocks or ``_meta`` entries."""

    def __init__(self, fields: Dict[str, Any], content: Optional[List[Dict[str, Any]]] = None,
                 meta: Optional[Dict[str, Any]] = None) -> None:
        self.fields = fields
        self.content = list(content or [])
        self.meta = dict(meta or {})


class Services:
    """The shared objects every handler reaches through ``ctx.services``.

    Built once by the server: ``env``, ``home``, ``config`` (``mcp_env.Config``),
    ``state_dir``, ``resolver``, ``runner`` (``mcp_cli.Runner``), ``refs``
    (``mcp_refs.Refs``), ``guard``, ``hooks``, ``views``, ``keeper``
    (``mcp_control.LeaseKeeper``), ``log`` and ``scrub`` (``PathScrubber`` for any
    local text a tool shows). Tool modules may keep their own
    state in ``extensions`` under their module name, and register shutdown work
    with ``on_shutdown`` (lower ``order`` first: runs 10, streams 20, leases 30,
    Canvas 40).
    """

    def __init__(self, **items: Any) -> None:
        self.extensions: Dict[str, Any] = {}
        self._shutdown: List[Tuple[int, str, Callable[[], Any]]] = []
        self._lock = threading.Lock()
        for key, value in items.items():
            setattr(self, key, value)

    def on_shutdown(self, name: str, fn: Callable[[], Any], order: int = 50) -> None:
        with self._lock:
            self._shutdown.append((order, name, fn))

    def shutdown_hooks(self) -> List[Tuple[str, Callable[[], Any]]]:
        with self._lock:
            ordered = sorted(enumerate(self._shutdown), key=lambda item: (item[1][0], item[0]))
        return [(entry[1], entry[2]) for _index, entry in ordered]


class CallContext:
    """Per-call state handed to a tool handler."""

    def __init__(self, services: Services, tool: Tool, operation_id: str,
                 cancelled: Optional[threading.Event] = None) -> None:
        self.services = services
        self.tool = tool
        self.operation_id = operation_id
        self.cancelled = cancelled if cancelled is not None else threading.Event()
        self.warnings: List[Dict[str, str]] = []
        self.session: Optional[Dict[str, str]] = None
        self.host_id: Optional[str] = None

    def warn(self, code: str, message: str) -> None:
        if len(self.warnings) < MAX_WARNINGS:
            self.warnings.append({"code": str(code)[:80], "message": str(message)[:500]})

    def resolve(self, host_id: str, session_id: str, *, live: bool = True) -> Dict[str, Any]:
        """``refs.resolve`` that also records the reference for the envelope."""
        ref = self.services.refs.resolve(host_id, session_id, live=live)
        self.host_id = ref["host_id"]
        self.session = {"host_id": ref["host_id"], "session_id": ref["session_id"]}
        return ref


# ---------------------------------------------------------------- schema helpers

def string(pattern: Optional[str] = None, min_length: Optional[int] = None,
           max_length: Optional[int] = None, description: Optional[str] = None,
           enum: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "string"}
    if pattern is not None:
        schema["pattern"] = pattern
    if min_length is not None:
        schema["minLength"] = min_length
    if max_length is not None:
        schema["maxLength"] = max_length
    if enum is not None:
        schema["enum"] = list(enum)
    if description:
        schema["description"] = description
    return schema


def integer(minimum: Optional[int] = None, maximum: Optional[int] = None,
            description: Optional[str] = None, default: Optional[int] = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "integer"}
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum
    if default is not None:
        schema["default"] = default
    if description:
        schema["description"] = description
    return schema


def obj(properties: Dict[str, Any], required: Sequence[str] = (), *,
        description: Optional[str] = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "object", "properties": properties,
                              "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    if description:
        schema["description"] = description
    return schema


def nullable(schema: Dict[str, Any]) -> Dict[str, Any]:
    """``schema`` or null: a type union where possible, else ``oneOf``."""
    out = dict(schema)
    kind = out.get("type")
    if isinstance(kind, str) and "enum" not in out and "const" not in out:
        out["type"] = [kind, "null"]
        return out
    if isinstance(kind, list) and "enum" not in out and "const" not in out:
        out["type"] = list(kind) + (["null"] if "null" not in kind else [])
        return out
    return {"oneOf": [schema, {"type": "null"}]}


HOST_ID = string(HOST_ID_RE, description="Host id from list_hosts.")
SESSION_ID = string(SESSION_ID_RE, description="Session id from list_sessions or start_session.")
TARGET_ID = string(TARGET_ID_RE)
PLATFORM = string(enum=("android", "ios"))
SESSION_REF = obj({"host_id": HOST_ID, "session_id": SESSION_ID}, ("host_id", "session_id"))
HOLDER = obj({"kind": string(max_length=40), "id": string(max_length=200),
              "label": nullable(string(max_length=200))}, ("kind", "id", "label"))
CONTROL_VIEW = obj({
    "state": string(enum=("none", "free", "held", "expired", "unknown")),
    "holder": nullable(HOLDER),
    "generation": nullable(integer(minimum=0)),
    "expires_at": nullable(string(max_length=64)),
    "mine": {"type": "boolean"},
    "role": {"oneOf": [string(enum=("agent", "human")), {"type": "null"}]},
    "took_over_from": nullable(HOLDER),
}, ("state", "holder", "generation", "expires_at", "mine", "role", "took_over_from"))
WARNING = obj({"code": string(max_length=80), "message": string(max_length=500)}, ("code", "message"))
STARTED_BY = obj({"kind": string(max_length=40), "port": nullable(integer(minimum=0)),
                  "pid": nullable(integer(minimum=0))}, ("kind", "port", "pid"))
SESSION_ENTRY = obj({
    "host_id": HOST_ID,
    "session_id": SESSION_ID,
    "platform": PLATFORM,
    "target_id": TARGET_ID,
    "label": nullable(string(max_length=64)),
    "live": {"type": "boolean"},
    "started_at": nullable(string(max_length=64)),
    "stopped_at": nullable(string(max_length=64)),
    "started_by": nullable(STARTED_BY),
}, ("host_id", "session_id", "platform", "target_id", "label", "live", "started_at",
    "stopped_at", "started_by"))

ENVELOPE_REQUIRED = ("schema_version", "operation_id", "tool", "ok", "outcome", "host_id", "warnings")
ERROR_FIELDS: Dict[str, Any] = {
    "error_code": string(max_length=80),
    "error": string(max_length=2000),
    "hint": nullable(string(max_length=2000)),
    "retry_safe": {"type": "boolean"},
    "cli_error_code": string(max_length=80),
    "cli_error": string(max_length=500),
    "cli_hint": string(max_length=500),
    "capability": string(max_length=80),
    "control": nullable(CONTROL_VIEW),
    "field": string(max_length=200),
    "problem": string(max_length=200),
    "busy_with": obj({"tool": string(max_length=80), "run_id": string(RUN_ID_RE)}),
}


def output_schema(tool_name: str, output_fields: Dict[str, Any], with_session: bool) -> Dict[str, Any]:
    """The envelope of 2.4 plus the tool's own (declared, not required) fields."""
    properties: Dict[str, Any] = {
        "schema_version": {"const": SCHEMA_VERSION},
        "operation_id": string(OPERATION_ID_RE),
        "tool": {"const": tool_name},
        "ok": {"type": "boolean"},
        "outcome": string(enum=OUTCOMES),
        "host_id": nullable(HOST_ID),
        "warnings": {"type": "array", "maxItems": MAX_WARNINGS, "items": WARNING},
    }
    if with_session:
        properties["session"] = nullable(SESSION_REF)
    for key, value in ERROR_FIELDS.items():
        properties[key] = value
    for key, value in output_fields.items():
        properties[key] = value
    return {"type": "object", "properties": properties, "required": list(ENVELOPE_REQUIRED),
            "additionalProperties": False}


EMPTY_INPUT = obj({})


# ---------------------------------------------------------------- validator

_PATTERNS: Dict[str, Any] = {}
_PATTERN_LOCK = threading.Lock()


def ecma_pattern(pattern: str) -> str:
    """``pattern`` with ECMA-262 end anchoring for Python's ``re``.

    JSON Schema patterns follow ECMA-262, where ``$`` (no multiline flag)
    matches only at the very end. Python's ``$`` also matches before a final
    newline, so every unescaped ``$`` outside a character class becomes ``\\Z``:
    ``^s_[a-z]+$`` must not accept ``"s_abc\\n"``.
    """
    out: List[str] = []
    index = 0
    in_class = False
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            out.append(pattern[index:index + 2])
            index += 2
            continue
        if in_class:
            if char == "]":
                in_class = False
        elif char == "[":
            in_class = True
            # A leading "]" (or "^]") is a literal member, not the end of the class.
            if pattern[index + 1:index + 2] == "^":
                out.append("[^")
                index += 2
            else:
                out.append("[")
                index += 1
            if pattern[index:index + 1] == "]":
                out.append("]")
                index += 1
            continue
        elif char == "$":
            out.append("\\Z")
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _compiled(pattern: str) -> Any:
    with _PATTERN_LOCK:
        compiled = _PATTERNS.get(pattern)
        if compiled is None:
            # re.ASCII: ECMA-262 \\d and \\w are ASCII-only, as the declared schemas mean.
            compiled = re.compile(ecma_pattern(pattern), re.ASCII)
            _PATTERNS[pattern] = compiled
        return compiled


def _is_type(value: Any, kind: str) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "null":
        return value is None
    return False


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    return type(left) is type(right) and left == right


def _join(path: str, key: str) -> str:
    return key if not path else "%s.%s" % (path, key)


def _check(schema: Dict[str, Any], value: Any, path: str, out: List[Tuple[str, str]]) -> None:
    if len(out) >= 50:
        return
    if "oneOf" in schema:
        matches = sum(1 for option in schema["oneOf"] if not validate_detail(option, value))
        if matches != 1:
            out.append((path, "matches %d of the allowed shapes" % matches))
            return
    kind = schema.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(_is_type(value, item) for item in kinds):
            out.append((path, "must be %s" % " or ".join(kinds)))
            return
    if "const" in schema and not _same(schema["const"], value):
        out.append((path, "must be %s" % json.dumps(schema["const"])))
        return
    if "enum" in schema and not any(_same(option, value) for option in schema["enum"]):
        out.append((path, "must be one of %s" % ", ".join(json.dumps(item) for item in schema["enum"])))
        return
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append((path, "must be at least %d characters" % schema["minLength"]))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append((path, "must be at most %d characters" % schema["maxLength"]))
        if "pattern" in schema and not _compiled(schema["pattern"]).search(value):
            out.append((path, "does not match %s" % schema["pattern"]))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append((path, "must be >= %s" % schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            out.append((path, "must be <= %s" % schema["maximum"]))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append((path, "must have at least %d items" % schema["minItems"]))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append((path, "must have at most %d items" % schema["maxItems"]))
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _check(items, item, "%s[%d]" % (path, index), out)
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for key in schema.get("required") or ():
            if key not in value:
                out.append((_join(path, key), "required"))
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                _check(properties[key], item, _join(path, str(key)), out)
            elif additional is False:
                out.append((_join(path, str(key)), "unknown property"))
            elif isinstance(additional, dict):
                _check(additional, item, _join(path, str(key)), out)


def validate_detail(schema: Dict[str, Any], value: Any) -> List[Tuple[str, str]]:
    """Problems as ``(path, problem)`` pairs; the root path is ``""``."""
    out: List[Tuple[str, str]] = []
    try:
        _check(schema, value, "", out)
    except RecursionError:
        out.append(("", "nested too deeply"))
    return out


def validate(schema: Dict[str, Any], value: Any) -> List[str]:
    """Problems as ``"path: problem"`` strings (contract 2.7); empty when valid."""
    return ["%s: %s" % (path or "$", problem) for path, problem in validate_detail(schema, value)]


def check_arguments(schema: Dict[str, Any], arguments: Any) -> None:
    """Raise ``invalid_arguments`` for the first problem of ``arguments``."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise bad_argument("arguments", "must be an object")
    problems = validate_detail(schema, arguments)
    if problems:
        path, problem = problems[0]
        raise bad_argument(path or "arguments", problem)


# ---------------------------------------------------------------- envelope

def text_dump(structured: Dict[str, Any]) -> str:
    """Sorted JSON text of a payload; escaped to ASCII when it holds a lone surrogate."""
    text = json.dumps(structured, ensure_ascii=False, sort_keys=True)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(structured, ensure_ascii=True, sort_keys=True)
    return text


def base_fields(tool: Tool, ctx: CallContext, ok: bool, outcome: str,
                host_id: Optional[str]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "operation_id": ctx.operation_id,
        "tool": tool.name,
        "ok": ok,
        "outcome": outcome,
        "host_id": host_id,
        "warnings": list(ctx.warnings[:MAX_WARNINGS]),
    }
    if session_scoped(tool):
        fields["session"] = dict(ctx.session) if ctx.session else None
    return fields


def failure_outcome(tool: Tool, exc: ToolError) -> str:
    if exc.rejected:
        return "rejected"
    return "outcome_unknown" if tool.mutating else "failed"


def failure_fields(tool: Tool, ctx: CallContext, exc: ToolError, host_id: Optional[str],
                   outcome: Optional[str] = None) -> Dict[str, Any]:
    outcome = outcome or failure_outcome(tool, exc)
    fields = base_fields(tool, ctx, False, outcome, host_id)
    for key, value in exc.extras.items():
        if key not in fields:
            fields[key] = value
    fields["error_code"] = exc.error_code
    fields["error"] = str(exc.error)[:2000]
    fields["hint"] = None if exc.hint is None else str(exc.hint)[:2000]
    fields["retry_safe"] = outcome == "rejected" or read_only(tool)
    return fields


def call_result(structured: Dict[str, Any], content: Sequence[Dict[str, Any]] = (),
                meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "content": [{"type": "text", "text": text_dump(structured)}] + list(content),
        "structuredContent": structured,
        "isError": structured.get("ok") is not True,
    }
    if meta:
        result["_meta"] = {key: value for key, value in meta.items() if key in ALLOWED_META_KEYS}
        if not result["_meta"]:
            del result["_meta"]
    return result
