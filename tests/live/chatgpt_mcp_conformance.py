#!/usr/bin/env python3
"""Stdlib MCP conformance client for the Autonom MCP adapter (contract run ws-p3-mcp-adapter, section 8).

A stand-in for MCP Inspector (which needs npm): it starts the adapter server from a plugin
folder exactly as the folder's ``.mcp.json`` says (``sh ./bin/launch.sh`` with ``cwd`` the
plugin folder), speaks newline-delimited JSON-RPC 2.0 over stdio and checks:

- the protocol fault table: ping before ``initialize``, the five gated methods refused before
  it, version negotiation, server info and instructions, parse errors (bad JSON, bad UTF-8,
  an oversized line), batch and non-object messages, a message without ``jsonrpc``, an
  unanswered notification and an ignored client response, unknown method and tool, the UI
  resource, a duplicate in-flight id, compact output lines and exit 0 at end of input;
- every tool's schema: the 28 tools in table order, input and output schemas in the
  contract's JSON Schema subset, the envelope fields, annotations and app-only metadata, and
  a call with an unknown argument refused ``invalid_arguments`` with the field named, its
  result valid against the tool's own output schema;
- the error-path table (fake CLI only): a CLI timeout, malformed output, partial collection,
  a cancelled request, a lost response (stdin closed during a slow mutation: the server exits
  0 and the fake journal holds exactly one entry for that operation id), a rejected and an
  unknown CLI error code.

Usage:

    python3 tests/live/chatgpt_mcp_conformance.py [--plugin DIR] [--real] [--report FILE]
    python3 tests/live/chatgpt_mcp_conformance.py --dry-run [--plugin DIR] [--real]
    python3 tests/live/chatgpt_mcp_conformance.py --self-test

By default the server runs against the fake CLI (``tests/fakes/fake_mcp_cli.py``) with
temporary ``HOME``, ``AUTONOM_HOME`` and ``XDG_STATE_HOME`` and failing ``adb``, ``emulator``,
``xcrun``, ``simctl`` and ``idb`` stubs first on ``PATH``; no device is touched. ``--real``
points the server at the CLI of this checkout (``scripts/autonom.py``, Python >= 3.11) with a
temporary ``AUTONOM_HOME`` and calls the read-only tools only (``list_hosts``,
``open_workspace``, ``list_devices``, ``list_sessions``, ``list_flows``); the error-path
table then reports ``not_exercised``. ``--report FILE`` writes
``{"ok", "mode", "checks": [{"id", "status", "detail"}]}``. Exit 0 when no check failed, 1
otherwise, 2 on bad arguments (a JSON error with ``error_code`` and ``hint`` on stderr).
``--dry-run`` prints the plan and starts nothing. ``--self-test`` checks the pure helpers and
then runs the whole fake suite.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DEFAULT_PLUGIN = ROOT / "integrations" / "chatgpt" / "autonom-chatgpt"
FAKES = ROOT / "tests" / "fakes"
FAKE_CLI = FAKES / "fake_mcp_cli.py"
FAKE_CANVAS = FAKES / "fake_mcp_canvas.mjs"
REPO_CLI = ROOT / "scripts" / "autonom.py"

SERVER_NAME = "autonom-chatgpt"
ADAPTER_VERSION = "0.1.0"
SCHEMA_VERSION = "autonom.mcp/v1"
UI_URI = "ui://autonom/workspace-dev.html"
UI_MIME = "text/html;profile=mcp-app"
LATEST_PROTOCOL = "2025-11-25"
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
GATED_METHODS = ("tools/list", "tools/call", "resources/list", "resources/read", "resources/templates/list")

TOOL_ORDER = (
    "list_hosts", "open_workspace", "save_diagnostics",
    "list_devices", "list_sessions", "get_session", "start_session", "stop_session",
    "get_screen", "get_ui_tree", "tap_element", "type_text", "press_key",
    "acquire_control", "release_control", "get_operation",
    "list_flows", "start_flow_run", "get_run", "cancel_run", "list_artifacts", "get_artifact",
    "open_stream", "get_frames", "close_stream", "send_input", "pause_session", "resume_session",
)
APP_ONLY = frozenset({"save_diagnostics", "open_stream", "get_frames", "close_stream", "send_input",
                      "pause_session", "resume_session"})
MUTATING = frozenset({"start_session", "stop_session", "tap_element", "type_text", "press_key",
                      "acquire_control", "release_control", "pause_session", "resume_session",
                      "start_flow_run", "cancel_run", "send_input"})
# Not mutations in the outcome sense (2.5), but they change server or file state.
STATEFUL = frozenset({"save_diagnostics", "open_stream", "close_stream"})
DESTRUCTIVE = frozenset({"stop_session", "cancel_run"})
READ_ONLY_REAL = ("list_hosts", "open_workspace", "list_devices", "list_sessions", "list_flows")
ENVELOPE_REQUIRED = ("schema_version", "operation_id", "tool", "ok", "outcome", "host_id", "warnings")
FAILURE_FIELDS = ("error_code", "error", "hint", "retry_safe")
OUTCOMES = ("completed", "rejected", "failed", "outcome_unknown")
SCHEMA_KEYWORDS = frozenset({"type", "properties", "required", "additionalProperties", "enum", "const",
                             "pattern", "minimum", "maximum", "minLength", "maxLength", "minItems",
                             "maxItems", "items", "oneOf", "default", "description"})
SCHEMA_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
DEVICE_TOOLS = ("adb", "emulator", "xcrun", "simctl", "idb")
STATUSES = ("pass", "fail", "not_exercised")
UNKNOWN_ARGUMENT = "__conformance_unknown__"
MISSING = object()

PROTOCOL_IDS = (
    "protocol.ping_before_initialize", "protocol.uninitialized_refused", "protocol.initialize",
    "protocol.version_negotiation", "protocol.parse_error", "protocol.bad_utf8", "protocol.oversized_line",
    "protocol.batch_refused", "protocol.non_object_refused", "protocol.missing_jsonrpc",
    "protocol.notification_and_client_response_unanswered", "protocol.unknown_method",
    "protocol.unknown_tool", "protocol.resources", "protocol.duplicate_id", "protocol.compact_lines",
    "protocol.eof_exit_zero",
)
CATALOG_IDS = ("catalog.order",) + tuple("schema.%s" % name for name in TOOL_ORDER)
REAL_READ_IDS = tuple("real.%s" % name for name in READ_ONLY_REAL)
ERROR_IDS = ("error.timeout", "error.malformed_output", "error.partial_collection", "error.cancellation",
             "error.lost_response", "error.rejected_code", "error.unknown_code")


# ====================================================================== small helpers

class CheckFailed(AssertionError):
    pass


class NotExercised(Exception):
    pass


class UsageError(Exception):
    def __init__(self, code: str, message: str, hint: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.extra = extra

    def payload(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ok": False, "error_code": self.code, "error": self.message, "hint": self.hint}
        out.update(self.extra)
        return out


def expect(condition: Any, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def wait_until(predicate: Callable[[], Any], timeout: float, step: float = 0.05) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(step)


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, check=False).stdout.strip()
    return bool(out) and not out.startswith("Z")


def get_path(value: Any, path: str) -> Any:
    """A dotted path into ``value``; ``name[]`` walks every item of a list (MISSING when absent or empty)."""
    current = [value]
    for part in path.split("."):
        spread = part.endswith("[]")
        key = part[:-2] if spread else part
        found = []
        for item in current:
            if not isinstance(item, dict) or key not in item:
                return MISSING
            inner = item[key]
            if spread:
                if not isinstance(inner, list) or not inner:
                    return MISSING
                found.extend(inner)
            else:
                found.append(inner)
        current = found
    return current if len(current) != 1 else current[0]


# ====================================================================== JSON Schema subset

def _is_type(value: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "null":
        return value is None
    return False


def _same(left: Any, right: Any) -> bool:
    """JSON equality: ``True`` is not ``1``."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    return left == right


def _pattern_matches(pattern: str, text: str) -> bool:
    # JSON Schema patterns are ECMA regexes searched anywhere; a trailing "$" anchors at the true
    # end (Python's "$" also matches before a final newline).
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + r"\Z"
    try:
        return re.search(pattern, text) is not None
    except re.error:
        return False


def validate(schema: Any, value: Any, path: str = "$") -> List[str]:
    """Problems of ``value`` against the contract's schema subset (2.7), as ``path: problem``."""
    problems: List[str] = []
    if not isinstance(schema, dict):
        return problems
    if "oneOf" in schema:
        branches = [branch for branch in schema["oneOf"] if not validate(branch, value, path)]
        if len(branches) != 1:
            problems.append("%s: matches %d oneOf branches" % (path, len(branches)))
    kind = schema.get("type")
    if kind is not None:
        names = kind if isinstance(kind, list) else [kind]
        if not any(_is_type(value, name) for name in names):
            return problems + ["%s: expected %s" % (path, "|".join(str(name) for name in names))]
    if "const" in schema and not _same(value, schema["const"]):
        problems.append("%s: expected the constant %s" % (path, short(schema["const"], 60)))
    if "enum" in schema and not any(_same(value, item) for item in schema["enum"]):
        problems.append("%s: not one of %s" % (path, short(schema["enum"], 80)))
    if isinstance(value, str):
        if "pattern" in schema and not _pattern_matches(schema["pattern"], value):
            problems.append("%s: does not match %s" % (path, schema["pattern"]))
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append("%s: shorter than %d" % (path, schema["minLength"]))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append("%s: longer than %d" % (path, schema["maxLength"]))
    if _is_type(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append("%s: below %s" % (path, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            problems.append("%s: above %s" % (path, schema["maximum"]))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append("%s: fewer than %d items" % (path, schema["minItems"]))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append("%s: more than %d items" % (path, schema["maxItems"]))
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                problems.extend(validate(schema["items"], item, "%s[%d]" % (path, index)))
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for name in schema.get("required") or ():
            if name not in value:
                problems.append("%s: missing required property %s" % (path, name))
        for name, inner in value.items():
            if name in properties:
                problems.extend(validate(properties[name], inner, "%s.%s" % (path, name)))
            elif schema.get("additionalProperties") is False:
                problems.append("%s: unexpected property %s" % (path, name))
            elif isinstance(schema.get("additionalProperties"), dict):
                problems.extend(validate(schema["additionalProperties"], inner, "%s.%s" % (path, name)))
    return problems


def schema_problems(schema: Any, path: str = "$") -> List[str]:
    """Problems of a schema itself: keywords outside the subset, malformed keyword values."""
    if not isinstance(schema, dict):
        return ["%s: a schema must be an object" % path]
    problems = ["%s: keyword %s is outside the contract subset" % (path, key)
                for key in sorted(schema) if key not in SCHEMA_KEYWORDS]
    kind = schema.get("type")
    if kind is not None:
        names = kind if isinstance(kind, list) else [kind]
        for name in names:
            if name not in SCHEMA_TYPES:
                problems.append("%s: unknown type %r" % (path, name))
    if "properties" in schema:
        if not isinstance(schema["properties"], dict):
            problems.append("%s: properties must be an object" % path)
        else:
            for name, inner in schema["properties"].items():
                problems.extend(schema_problems(inner, "%s.%s" % (path, name)))
    if "required" in schema and not (isinstance(schema["required"], list)
                                     and all(isinstance(item, str) for item in schema["required"])):
        problems.append("%s: required must be a list of names" % path)
    if "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if isinstance(extra, dict):
            problems.extend(schema_problems(extra, path + ".additionalProperties"))
        elif not isinstance(extra, bool):
            problems.append("%s: additionalProperties must be a bool or a schema" % path)
    if "items" in schema:
        problems.extend(schema_problems(schema["items"], path + "[]"))
    if "oneOf" in schema:
        if not isinstance(schema["oneOf"], list) or not schema["oneOf"]:
            problems.append("%s: oneOf must be a non-empty list" % path)
        else:
            for index, branch in enumerate(schema["oneOf"]):
                problems.extend(schema_problems(branch, "%s.oneOf[%d]" % (path, index)))
    if "enum" in schema and not (isinstance(schema["enum"], list) and schema["enum"]):
        problems.append("%s: enum must be a non-empty list" % path)
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except (re.error, TypeError):
            problems.append("%s: pattern does not compile" % path)
    return problems


def tool_definition_problems(tool: Dict[str, Any]) -> List[str]:
    """Contract 2.7 and 2.9 rules for one ``tools/list`` entry."""
    name = tool.get("name")
    problems: List[str] = []
    for key in ("name", "title", "description", "inputSchema", "outputSchema", "annotations"):
        if key not in tool:
            problems.append("missing %s" % key)
    input_schema = tool.get("inputSchema") or {}
    output_schema = tool.get("outputSchema") or {}
    problems += ["inputSchema " + item for item in schema_problems(input_schema)]
    problems += ["outputSchema " + item for item in schema_problems(output_schema)]
    if input_schema.get("type") != "object" or input_schema.get("additionalProperties") is not False:
        problems.append("inputSchema must be an object with additionalProperties false")
    if output_schema.get("type") != "object" or output_schema.get("additionalProperties") is not False:
        problems.append("outputSchema must be an object with additionalProperties false")
    required = output_schema.get("required") or []
    properties = output_schema.get("properties") or {}
    for key in ENVELOPE_REQUIRED:
        if key not in required:
            problems.append("outputSchema does not require %s" % key)
    for key in ENVELOPE_REQUIRED + FAILURE_FIELDS:
        if key not in properties:
            problems.append("outputSchema lacks the envelope property %s" % key)
    if (properties.get("tool") or {}).get("const") != name:
        problems.append("outputSchema tool is not the constant %s" % name)
    if (properties.get("schema_version") or {}).get("const") != SCHEMA_VERSION:
        problems.append("outputSchema schema_version is not %s" % SCHEMA_VERSION)
    annotations = tool.get("annotations") or {}
    writes = name in MUTATING or name in STATEFUL
    if annotations.get("readOnlyHint") is not (not writes):
        problems.append("readOnlyHint must be %s" % (not writes))
    if bool(annotations.get("destructiveHint", False)) is not (name in DESTRUCTIVE):
        problems.append("destructiveHint must be %s" % (name in DESTRUCTIVE))
    if annotations.get("openWorldHint") is not False:
        problems.append("openWorldHint must be false")
    meta = tool.get("_meta") or {}
    if meta.get("openai/widgetAccessible") is not True:
        problems.append("_meta openai/widgetAccessible must be true")
    app_only = (meta.get("ui") or {}).get("visibility") == ["app"] and meta.get("openai/visibility") == "private"
    if app_only is not (name in APP_ONLY):
        problems.append("app-only metadata must be %s" % ("present" if name in APP_ONLY else "absent"))
    if name == "open_workspace":
        if (meta.get("ui") or {}).get("resourceUri") != UI_URI or meta.get("openai/outputTemplate") != UI_URI:
            problems.append("open_workspace must name the UI resource")
        entrypoints = [item.get("type") for item in ((meta.get("openai/ui") or {}).get("entrypoints") or [])]
        if sorted(entrypoints) != ["global", "thread"]:
            problems.append("open_workspace entrypoints must be global and thread")
    return problems


def envelope_problems(result: Any, tool: Dict[str, Any]) -> List[str]:
    """Contract 2.4 rules for one CallToolResult of ``tool``."""
    if not isinstance(result, dict) or not isinstance(result.get("structuredContent"), dict):
        return ["the result has no structuredContent"]
    structured = result["structuredContent"]
    problems = validate(tool.get("outputSchema") or {}, structured)
    if structured.get("tool") != tool.get("name"):
        problems.append("tool is %r" % structured.get("tool"))
    if result.get("isError") is not (structured.get("ok") is not True):
        problems.append("isError does not equal not ok")
    if structured.get("outcome") not in OUTCOMES:
        problems.append("unknown outcome %r" % structured.get("outcome"))
    if structured.get("ok") is not True:
        for key in FAILURE_FIELDS:
            if key not in structured:
                problems.append("a failure lacks %s" % key)
        if structured.get("retry_safe") is True and structured.get("outcome") not in ("rejected", "failed"):
            problems.append("retry_safe is true for outcome %s" % structured.get("outcome"))
    content = result.get("content")
    if not isinstance(content, list) or not content or content[0].get("type") != "text":
        problems.append("content[0] is not a text block")
    else:
        try:
            if json.loads(content[0]["text"]) != structured:
                problems.append("content text is not the structured content")
        except (TypeError, ValueError):
            problems.append("content text is not JSON")
    for key in (result.get("_meta") or {}):
        if key not in ("autonom/frames", "autonom/artifact"):
            problems.append("unexpected _meta key %s" % key)
    return problems


# ====================================================================== the client

class ServerClosed(AssertionError):
    pass


class McpClient:
    """One server process speaking newline-delimited JSON-RPC 2.0 over stdio."""

    def __init__(self, argv: Sequence[str], env: Dict[str, str], cwd: Optional[Path] = None) -> None:
        self.stderr_file = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(list(argv), cwd=str(cwd) if cwd else None, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self.stderr_file, env=env)
        self.raw_lines: List[bytes] = []
        self.seen: List[Any] = []
        self.messages: "queue.Queue[Any]" = queue.Queue()
        self.next_id = 1
        self.lock = threading.Lock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    @classmethod
    def from_plugin(cls, plugin: Path, env: Dict[str, str]) -> "McpClient":
        """Start the server as the plugin's ``.mcp.json`` says."""
        manifest = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))
        entry = manifest["mcpServers"][SERVER_NAME]
        return cls([entry["command"], *entry.get("args", [])], env, plugin / entry.get("cwd", "."))

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in iter(self.proc.stdout.readline, b""):
            self.raw_lines.append(line)
            try:
                message = json.loads(line.decode("utf-8"))
            except ValueError:
                message = {"__not_json__": line[:200].decode("utf-8", "replace")}
            self.seen.append(message)
            self.messages.put(message)
        self.messages.put(None)

    def new_id(self) -> int:
        with self.lock:
            msg_id = self.next_id
            self.next_id += 1
        return msg_id

    def send(self, message: Any) -> None:
        self.send_raw(json.dumps(message).encode("utf-8") + b"\n")

    def send_raw(self, data: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def recv(self, timeout: float = 30.0) -> Any:
        try:
            message = self.messages.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no message from the server within %.1f s" % timeout)
        if message is None:
            self.messages.put(None)
            raise ServerClosed("the server closed its output")
        return message

    def wait_for(self, msg_id: Any, timeout: float = 30.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            reply = self.recv(timeout=max(0.05, deadline - time.monotonic()))
            if isinstance(reply, dict) and reply.get("id") == msg_id and "method" not in reply:
                return reply

    def request(self, method: str, params: Any = MISSING, timeout: float = 30.0) -> Dict[str, Any]:
        msg_id = self.new_id()
        message: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not MISSING:
            message["params"] = params
        self.send(message)
        return self.wait_for(msg_id, timeout)

    def notify(self, method: str, params: Any = MISSING) -> None:
        message: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not MISSING:
            message["params"] = params
        self.send(message)

    def initialize(self, version: Any = LATEST_PROTOCOL, name: str = "autonom-conformance") -> Dict[str, Any]:
        params: Dict[str, Any] = {"capabilities": {"extensions": {"io.modelcontextprotocol/ui": {
            "mimeTypes": [UI_MIME]}}}, "clientInfo": {"name": name, "version": ADAPTER_VERSION}}
        if version is not MISSING:
            params["protocolVersion"] = version
        reply = self.request("initialize", params)
        self.notify("notifications/initialized")
        return reply

    def call(self, name: str, arguments: Any = MISSING, timeout: float = 60.0) -> Dict[str, Any]:
        params: Dict[str, Any] = {"name": name}
        if arguments is not MISSING:
            params["arguments"] = arguments
        reply = self.request("tools/call", params, timeout=timeout)
        if "error" in reply:
            raise CheckFailed("tools/call %s answered a JSON-RPC error: %s" % (name, short(reply["error"])))
        return reply["result"]

    def tools(self) -> List[Dict[str, Any]]:
        return self.request("tools/list")["result"]["tools"]

    def stderr_text(self) -> str:
        try:
            self.stderr_file.seek(0)
            return self.stderr_file.read().decode("utf-8", "replace")
        except (OSError, ValueError):
            return ""

    def close(self, timeout: float = 20.0) -> Optional[int]:
        """Close stdin (end of input) and wait for the exit code (None when it did not exit)."""
        if self.proc.stdin is not None and not self.proc.stdin.closed:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill(self) -> None:
        self.close(timeout=10)
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        self.reader.join(timeout=2)
        try:
            self.stderr_file.close()
        except OSError:
            pass


# ====================================================================== the fake world

def load_fake() -> Any:
    """``tests/fakes/fake_mcp_cli.py`` as a module (its state helpers), without writing bytecode."""
    spec = importlib.util.spec_from_file_location("fake_mcp_cli_conformance", FAKE_CLI)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def make_device_stubs(directory: Path, log: Path) -> None:
    """Executable ``adb``, ``emulator``, ``xcrun``, ``simctl`` and ``idb`` that log their call and fail."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in DEVICE_TOOLS:
        path = directory / name
        path.write_text("#!/bin/sh\nprintf '%%s %%s\\n' %s \"$*\" >> %s\nexit 1\n"
                        % (shlex.quote(name), shlex.quote(str(log))), encoding="utf-8")
        path.chmod(0o755)


def clean_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The environment without Autonom, fake, Python or state overrides of the caller."""
    source = os.environ if base is None else base
    return {key: value for key, value in source.items()
            if not key.startswith(("AUTONOM_", "FAKE_MCP_", "PYTHON")) and key != "XDG_STATE_HOME"}


class FakeWorld:
    """Temporary HOME, state base, fake CLI and fake Canvas state, and failing device-tool stubs."""

    def __init__(self, prefix: str = "mcp-conformance-") -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.xdg = self.tmp / "xdg"
        self.fake_state = self.tmp / "fake"
        self.fake_state.mkdir()
        self.cli_log = self.tmp / "cli.log"
        self.canvas_log = self.tmp / "canvas.log"
        self.canvas_config = self.tmp / "canvas.json"
        self.stub_dir = self.tmp / "stubs"
        self.stub_log = self.tmp / "stubs.log"
        make_device_stubs(self.stub_dir, self.stub_log)
        self.write_canvas({})
        self.node = shutil.which("node")
        self.fake = load_fake()
        self.extra_env: Dict[str, str] = {}

    @property
    def mcp_dir(self) -> Path:
        return self.state / "chatgpt-mcp"

    def write_config(self, config: Dict[str, Any]) -> None:
        self.mcp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.mcp_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

    def write_canvas(self, config: Dict[str, Any]) -> None:
        self.canvas_config.write_text(json.dumps(config), encoding="utf-8")

    def env(self, **overrides: Optional[str]) -> Dict[str, str]:
        env = clean_env()
        real_home = os.path.expanduser("~")
        path = [part for part in env.get("PATH", "").split(os.pathsep)
                if part and not part.startswith(os.path.join(real_home, ".local"))]
        env["PATH"] = os.pathsep.join([str(self.stub_dir)] + path)
        env.update({
            "HOME": str(self.home),
            "AUTONOM_HOME": str(self.state),
            "XDG_STATE_HOME": str(self.xdg),
            "AUTONOM_CLI": str(FAKE_CLI),
            "AUTONOM_MCP_CLI_PYTHON": sys.executable,
            "AUTONOM_MCP_PYTHON": sys.executable,
            "FAKE_MCP_STATE": str(self.fake_state),
            "FAKE_MCP_CLI_LOG": str(self.cli_log),
            "FAKE_MCP_CANVAS_CONFIG": str(self.canvas_config),
            "FAKE_MCP_CANVAS_LOG": str(self.canvas_log),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        if self.node:
            env["FAKE_MCP_CANVAS_CMD"] = "%s %s" % (shlex.quote(self.node), shlex.quote(str(FAKE_CANVAS)))
        env.update(self.extra_env)
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def start(self, plugin: Path = DEFAULT_PLUGIN, **overrides: Optional[str]) -> McpClient:
        return McpClient.from_plugin(plugin, self.env(**overrides))

    def host_id(self) -> str:
        return self.fake.host_id(self.fake_state)

    def add_session(self, platform: str = "android", target_id: str = "emulator-5584") -> str:
        return self.fake.add_session(self.fake_state, platform, target_id)

    def cli_calls(self, key: Optional[str] = None) -> List[Dict[str, Any]]:
        return [call for call in self.fake.read_log(self.cli_log) if key is None or call.get("key") == key]

    def canvas_events(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        try:
            lines = self.canvas_log.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        events = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return [event for event in events if kind is None or event.get("t") == kind]

    def stub_calls(self) -> List[str]:
        try:
            return [line for line in self.stub_log.read_text(encoding="utf-8").splitlines() if line.strip()]
        except OSError:
            return []

    def discovery(self) -> List[Dict[str, Any]]:
        out = []
        for path in sorted((self.state / "canvas").glob("*.json")):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        return out

    def cleanup(self) -> None:
        pids = {event.get("pid") for event in self.canvas_events()}
        pids |= {document.get("pid") for document in self.discovery()}
        for call in self.cli_calls():
            pids.add(call.get("pid"))
            pids.add(call.get("child_pid"))
        for pid in pids:
            if not pid_alive(pid) or pid == os.getpid():
                continue
            command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True, check=False).stdout
            if FAKE_CANVAS.name in command or FAKE_CLI.name in command or "time.sleep(600)" in command:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)


# ====================================================================== the report

class Report:
    def __init__(self) -> None:
        self.checks: List[Dict[str, str]] = []

    def add(self, check_id: str, status: str, detail: str = "") -> None:
        assert status in STATUSES, status
        self.checks.append({"id": check_id, "status": status, "detail": short(detail, 500)})

    def run(self, check_id: str, body: Callable[[], Any]) -> bool:
        try:
            detail = body()
        except NotExercised as exc:
            self.add(check_id, "not_exercised", str(exc))
            return False
        except (CheckFailed, AssertionError, TimeoutError) as exc:
            self.add(check_id, "fail", str(exc) or type(exc).__name__)
            return False
        except Exception as exc:  # noqa: BLE001 - one broken check never stops the table
            self.add(check_id, "fail", "%s: %s" % (type(exc).__name__, exc))
            return False
        self.add(check_id, "pass", detail if isinstance(detail, str) else "")
        return True

    def skip(self, check_ids: Iterable[str], reason: str) -> None:
        for check_id in check_ids:
            self.add(check_id, "not_exercised", reason)

    @property
    def ok(self) -> bool:
        return all(item["status"] != "fail" for item in self.checks)

    def as_json(self, mode: str) -> Dict[str, Any]:
        return {"ok": self.ok, "mode": mode, "checks": list(self.checks)}


# ====================================================================== checks

Starter = Callable[..., McpClient]


def _error(reply: Dict[str, Any]) -> Dict[str, Any]:
    error = reply.get("error")
    expect(isinstance(error, dict), "expected a JSON-RPC error, got %s" % short(reply))
    return error


def _quiet_until_ping(client: McpClient, timeout: float = 10.0) -> List[Any]:
    """Send a ping and return every other message that arrived before its answer."""
    before = len(client.seen)
    reply = client.request("ping", timeout=timeout)
    expect(reply.get("result") == {}, "ping answered %s" % short(reply))
    return [item for item in client.seen[before:] if item is not reply]


def protocol_checks(report: Report, start: Starter, fake: bool) -> None:
    client = start()
    try:
        report.run("protocol.ping_before_initialize",
                   lambda: expect(client.request("ping").get("result") == {}, "ping before initialize failed"))

        def uninitialized() -> None:
            for method in GATED_METHODS:
                error = _error(client.request(method, {}))
                expect(error.get("code") == -32600 and error.get("message") == "Server not initialized",
                       "%s before initialize answered %s" % (method, short(error)))
        report.run("protocol.uninitialized_refused", uninitialized)

        def initialize() -> str:
            result = client.initialize()["result"]
            expect(result.get("protocolVersion") == LATEST_PROTOCOL, "protocolVersion %r" % result.get("protocolVersion"))
            expect(result.get("serverInfo") == {"name": SERVER_NAME, "title": "Autonom", "version": ADAPTER_VERSION},
                   "serverInfo %s" % short(result.get("serverInfo")))
            expect((result.get("capabilities") or {}).get("tools") == {"listChanged": False},
                   "tools capability %s" % short(result.get("capabilities")))
            instructions = result.get("instructions") or ""
            for word in ("host_id", "session_id", "outcome", "outcome_unknown", "get_operation"):
                expect(word in instructions, "the instructions do not mention %s" % word)
            return "serverInfo %s %s" % (SERVER_NAME, ADAPTER_VERSION)
        report.run("protocol.initialize", initialize)

        def parse_error() -> None:
            client.send_raw(b"{not json\n")
            reply = client.recv()
            expect(reply == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                   "bad JSON answered %s" % short(reply))
        report.run("protocol.parse_error", parse_error)

        def bad_utf8() -> None:
            client.send_raw(b"\xff\xfe\n")
            reply = client.recv()
            expect(reply.get("id") is None and _error(reply).get("code") == -32700, "bad UTF-8 answered %s" % short(reply))
        report.run("protocol.bad_utf8", bad_utf8)

        def oversized() -> None:
            client.send_raw(b"[" + b" " * (4 * 1024 * 1024 + 10) + b"]\n")
            reply = client.recv()
            expect(reply.get("id") is None and _error(reply).get("code") == -32700,
                   "an oversized line answered %s" % short(reply))
        report.run("protocol.oversized_line", oversized)

        def batch() -> None:
            client.send([{"jsonrpc": "2.0", "id": 900, "method": "ping"}])
            reply = client.recv()
            expect(reply.get("id") is None and _error(reply).get("code") == -32600, "a batch answered %s" % short(reply))
        report.run("protocol.batch_refused", batch)

        def non_object() -> None:
            client.send(5)
            reply = client.recv()
            expect(_error(reply).get("code") == -32600, "a number answered %s" % short(reply))
        report.run("protocol.non_object_refused", non_object)

        def missing_jsonrpc() -> None:
            client.send({"id": 901, "method": "ping"})
            reply = client.recv()
            expect(reply == {"jsonrpc": "2.0", "id": 901, "error": {"code": -32600, "message": "Invalid Request"}},
                   "a message without jsonrpc answered %s" % short(reply))
        report.run("protocol.missing_jsonrpc", missing_jsonrpc)

        def unanswered() -> None:
            client.notify("notifications/conformance")
            client.send({"jsonrpc": "2.0", "id": 77, "result": {"x": 1}})
            client.send({"jsonrpc": "2.0", "id": 78, "error": {"code": 1, "message": "x"}})
            extra = _quiet_until_ping(client)
            expect(not extra, "the server answered a notification or a client response: %s" % short(extra))
        report.run("protocol.notification_and_client_response_unanswered", unanswered)

        report.run("protocol.unknown_method",
                   lambda: expect(_error(client.request("does/not/exist")).get("code") == -32601,
                                  "an unknown method is not -32601"))

        def unknown_tool() -> None:
            for params in ({"name": "no_such_tool"}, {"arguments": {}}, []):
                error = _error(client.request("tools/call", params))
                expect(error.get("code") == -32602, "tools/call %s answered %s" % (short(params), short(error)))
        report.run("protocol.unknown_tool", unknown_tool)

        def resources() -> str:
            listed = client.request("resources/list")["result"]["resources"]
            entry = next((item for item in listed if item.get("uri") == UI_URI), None)
            expect(entry is not None and entry.get("mimeType") == UI_MIME, "resources/list lacks %s" % UI_URI)
            csp = ((entry.get("_meta") or {}).get("ui") or {}).get("csp") or {}
            expect(csp.get("connectDomains") == [], "the page may connect to %s" % short(csp))
            content = client.request("resources/read", {"uri": UI_URI})["result"]["contents"][0]
            expect(content.get("mimeType") == UI_MIME and "<html" in content.get("text", "").lower(),
                   "resources/read did not return the page")
            for marker in ("<!-- WORKSPACE_CORE -->", "__WORKSPACE_CONFIG_JSON__"):
                expect(marker not in content["text"], "the page still holds %s" % marker)
            expect(_error(client.request("resources/read", {"uri": "ui://nope"})).get("code") == -32002,
                   "an unknown resource is not -32002")
            expect(client.request("resources/templates/list")["result"] == {"resourceTemplates": []},
                   "resources/templates/list is not empty")
            return "%d bytes of page" % len(content["text"])
        report.run("protocol.resources", resources)
    finally:
        def closing() -> None:
            expect(client.close() == 0, "the server did not exit 0 at end of input")
        report.run("protocol.eof_exit_zero", closing)

        def compact() -> str:
            for line in client.raw_lines:
                expect(line.endswith(b"\n") and b"\n" not in line[:-1], "an output line is not one line")
                expect(isinstance(json.loads(line.decode("utf-8")), dict), "an output line is not an object")
            return "%d lines" % len(client.raw_lines)
        report.run("protocol.compact_lines", compact)
        client.kill()

    def negotiation() -> str:
        cases = [(version, version) for version in PROTOCOL_VERSIONS[1:]]
        cases += [("1999-01-01", LATEST_PROTOCOL), (MISSING, LATEST_PROTOCOL), (7, LATEST_PROTOCOL)]
        for requested, expected in cases:
            other = start()
            try:
                got = other.initialize(requested)["result"].get("protocolVersion")
                expect(got == expected, "requested %r, got %r" % (requested, got))
                expect(other.close() == 0, "the server did not exit 0")
            finally:
                other.kill()
        return "%d versions" % len(cases)
    report.run("protocol.version_negotiation", negotiation)

    if not fake:
        report.skip(["protocol.duplicate_id"], "needs a slow CLI call (fake CLI only)")
        return

    def duplicate() -> None:
        slow = start(FAKE_MCP_CLI_MODE="slow:host_info:1500")
        try:
            slow.initialize()
            call = {"jsonrpc": "2.0", "id": "dup", "method": "tools/call", "params": {"name": "list_hosts"}}
            slow.send(call)
            time.sleep(0.3)
            slow.send(call)
            reply = slow.recv(timeout=10)
            expect(reply == {"jsonrpc": "2.0", "id": "dup", "error": {"code": -32600, "message": "Duplicate request id"}},
                   "a duplicate in-flight id answered %s" % short(reply))
            result = slow.recv(timeout=20)
            expect(result.get("id") == "dup" and "result" in result, "the first call answered %s" % short(result))
            expect(slow.close() == 0, "the server did not exit 0")
        finally:
            slow.kill()
    report.run("protocol.duplicate_id", duplicate)


def catalog_checks(report: Report, client: McpClient, fake: bool) -> Dict[str, Dict[str, Any]]:
    tools = client.tools()
    by_name = {tool.get("name"): tool for tool in tools}

    def order() -> str:
        names = [tool.get("name") for tool in tools]
        expect(names == list(TOOL_ORDER), "tools/list is %s" % short(names, 400))
        return "%d tools" % len(names)
    report.run("catalog.order", order)

    for name in TOOL_ORDER:
        def one(name: str = name) -> str:
            tool = by_name.get(name)
            expect(tool is not None, "%s is not listed" % name)
            problems = tool_definition_problems(tool)
            expect(not problems, "; ".join(problems[:5]))
            result = client.call(name, {UNKNOWN_ARGUMENT: 1})
            problems = envelope_problems(result, tool)
            expect(not problems, "the refused call: " + "; ".join(problems[:5]))
            structured = result["structuredContent"]
            expect((structured.get("ok"), structured.get("outcome"), structured.get("error_code")) ==
                   (False, "rejected", "invalid_arguments"), "an unknown argument answered %s" % short(structured))
            expect(isinstance(structured.get("field"), str) and structured.get("retry_safe") is True,
                   "invalid_arguments lacks field or retry_safe")
            return "schema ok, unknown argument refused (field %s)" % structured["field"]
        report.run("schema.%s" % name, one)
    return by_name


def real_read_checks(report: Report, client: McpClient, tools: Dict[str, Dict[str, Any]]) -> None:
    for name in READ_ONLY_REAL:
        def one(name: str = name) -> str:
            tool = tools.get(name)
            expect(tool is not None, "%s is not listed" % name)
            result = client.call(name, {}, timeout=120)
            problems = envelope_problems(result, tool)
            expect(not problems, "; ".join(problems[:5]))
            structured = result["structuredContent"]
            expect(structured.get("ok") is True, "%s answered %s" % (name, short(structured)))
            return "completed with %d warnings" % len(structured.get("warnings") or [])
        report.run("real.%s" % name, one)


def _structured(result: Dict[str, Any]) -> Dict[str, Any]:
    structured = result.get("structuredContent")
    expect(isinstance(structured, dict), "no structured content")
    return structured


def error_path_checks(report: Report, plugin: Path) -> None:
    def case(world_config: Optional[Dict[str, Any]], mode: str,
             body: Callable[[FakeWorld, McpClient, str, str], Any]) -> Callable[[], Any]:
        def run() -> Any:
            world = FakeWorld()
            client = None
            try:
                if world_config:
                    world.write_config(world_config)
                session_id = world.add_session()
                client = world.start(plugin, FAKE_MCP_CLI_MODE=mode or None)
                client.initialize()
                tools = {tool["name"]: tool for tool in client.tools()}
                client.tool_defs = tools  # type: ignore[attr-defined]
                return body(world, client, world.host_id(), session_id)
            finally:
                if client is not None:
                    client.kill()
                stubs = world.stub_calls()
                world.cleanup()
                expect(not stubs, "a device tool was called: %s" % short(stubs))
        return run

    def checked(client: McpClient, name: str, arguments: Dict[str, Any], timeout: float = 60.0) -> Dict[str, Any]:
        result = client.call(name, arguments, timeout=timeout)
        problems = envelope_problems(result, client.tool_defs[name])  # type: ignore[attr-defined]
        expect(not problems, "%s: %s" % (name, "; ".join(problems[:5])))
        return _structured(result)

    def timeout_case(world: FakeWorld, client: McpClient, host: str, session: str) -> str:
        sc = checked(client, "tap_element", {"host_id": host, "session_id": session,
                                             "selector": {"text": "Settings"}}, timeout=90)
        expect((sc["ok"], sc["outcome"], sc["error_code"], sc["retry_safe"]) ==
               (False, "outcome_unknown", "cli_timeout", False), "a CLI timeout answered %s" % short(sc))
        calls = world.cli_calls("ui_tap")
        expect(len(calls) == 1, "%d ui tap invocations" % len(calls))
        expect(wait_until(lambda: not pid_alive(calls[0].get("child_pid")), 10),
               "the timed-out CLI left a child in its process group")
        return "outcome_unknown after one invocation; the process group was killed"

    def malformed_case(world: FakeWorld, client: McpClient, host: str, session: str) -> str:
        ref = {"host_id": host, "session_id": session}
        sc = checked(client, "get_screen", dict(ref))
        expect((sc["outcome"], sc["error_code"]) == ("failed", "cli_output_invalid"), "a read answered %s" % short(sc))
        sc = checked(client, "press_key", dict(ref, key="HOME"))
        expect((sc["outcome"], sc["error_code"]) == ("outcome_unknown", "cli_output_invalid"),
               "a mutation answered %s" % short(sc))
        expect(len(world.cli_calls("ui_key")) == 1, "the key was sent more than once")
        return "read failed, mutation outcome_unknown"

    def partial_case(world: FakeWorld, client: McpClient, host: str, session: str) -> str:
        sc = checked(client, "list_devices", {})
        expect(sc["ok"] is True and sc["outcome"] == "completed", "list_devices answered %s" % short(sc))
        codes = [item.get("code") for item in sc.get("warnings") or []]
        expect("sessions_unavailable" in codes, "no sessions_unavailable warning: %s" % codes)
        devices = sc.get("devices") or []
        expect(devices and all(item.get("session_id") is None for item in devices),
               "devices without session ids expected: %s" % short(devices))
        return "%d devices, warning sessions_unavailable" % len(devices)

    def cancel_case(world: FakeWorld, client: McpClient, host: str, session: str) -> str:
        msg_id = client.new_id()
        client.send({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": "list_devices"}})
        time.sleep(0.3)
        client.notify("notifications/cancelled", {"requestId": msg_id, "reason": "conformance"})
        started = time.monotonic()
        _quiet_until_ping(client)
        expect(time.monotonic() - started < 1.0, "ping waited for the cancelled call")
        expect(wait_until(lambda: [item for item in world.cli_calls("devices") if item.get("end")], 10),
               "the cancelled call never finished")
        time.sleep(0.5)
        _quiet_until_ping(client)
        answered = [item for item in client.seen if isinstance(item, dict) and item.get("id") == msg_id]
        expect(not answered, "the cancelled call was answered: %s" % short(answered))
        return "no response for the cancelled request; the CLI call ran to its end"

    def lost_case(world: FakeWorld, client: McpClient, host: str, session: str) -> str:
        msg_id = client.new_id()
        client.send({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {
            "name": "tap_element", "arguments": {"host_id": host, "session_id": session,
                                                 "selector": {"text": "Settings"}}}})
        expect(wait_until(lambda: world.cli_calls("ui_tap"), 15), "the tap never reached the CLI")
        code = client.close(timeout=30)
        expect(code == 0, "the server exited %r after stdin closed" % code)
        operation_id = world.cli_calls("ui_tap")[0].get("operation_id")
        expect(isinstance(operation_id, str) and operation_id.startswith("op_"), "the tap carried no operation id")
        entries = [entry for entry in world.fake.read_journal(world.fake_state, session)
                   if entry.get("operation_id") == operation_id]
        expect(len(entries) == 1, "%d journal entries for the lost operation" % len(entries))
        expect(len(world.cli_calls("ui_tap")) == 1, "the tap was repeated")
        return "exit 0; one journal entry for the lost operation"

    def rejected_case(world: FakeWorld, client: McpClient, host: str, session: str) -> str:
        sc = checked(client, "tap_element", {"host_id": host, "session_id": session, "selector": {"text": "Nope"}})
        expect((sc["outcome"], sc.get("cli_error_code"), sc["retry_safe"]) == ("rejected", "no_matching_node", True),
               "a rejected CLI code answered %s" % short(sc))
        expect(len(world.cli_calls("ui_tap")) == 1, "the tap was repeated")
        return "rejected, retry_safe"

    def unknown_case(world: FakeWorld, client: McpClient, host: str, session: str) -> str:
        sc = checked(client, "press_key", {"host_id": host, "session_id": session, "key": "HOME"})
        expect((sc["outcome"], sc.get("cli_error_code"), sc["retry_safe"]) ==
               ("outcome_unknown", "ui_action_uncertain", False), "an unknown CLI code answered %s" % short(sc))
        expect(len(world.cli_calls("ui_key")) == 1, "the key was repeated")
        return "outcome_unknown, not retry-safe"

    report.run("error.timeout", case({"cli_timeout_scale": 0.1}, "timeout:ui_tap", timeout_case))
    report.run("error.malformed_output", case(None, "malformed:screenshot,malformed:ui_key", malformed_case))
    report.run("error.partial_collection", case(None, "fail:sessions:backend_failed", partial_case))
    report.run("error.cancellation", case(None, "slow:devices:1500", cancel_case))
    report.run("error.lost_response", case(None, "slow:ui_tap:1500", lost_case))
    report.run("error.rejected_code", case(None, "fail:ui_tap:no_matching_node", rejected_case))
    report.run("error.unknown_code", case(None, "fail:ui_key:ui_action_uncertain", unknown_case))


# ====================================================================== runs

def real_env(state: Path) -> Dict[str, str]:
    env = clean_env()
    env.update({"AUTONOM_HOME": str(state), "AUTONOM_CLI": str(REPO_CLI),
                "AUTONOM_MCP_CLI_PYTHON": sys.executable, "AUTONOM_MCP_PYTHON": sys.executable,
                "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def run_suite(plugin: Path, real: bool) -> Report:
    report = Report()
    if real:
        state = Path(tempfile.mkdtemp(prefix="mcp-conformance-real-")).resolve()
        try:
            def start(**overrides: Optional[str]) -> McpClient:
                env = real_env(state)
                for key, value in overrides.items():
                    if value is None:
                        env.pop(key, None)
                    else:
                        env[key] = value
                return McpClient.from_plugin(plugin, env)
            protocol_checks(report, start, fake=False)
            client = start()
            try:
                client.initialize()
                tools = catalog_checks(report, client, fake=False)
                real_read_checks(report, client, tools)
                report.run("protocol.eof_exit_zero.after_tools",
                           lambda: expect(client.close() == 0, "the server did not exit 0"))
            finally:
                client.kill()
        finally:
            shutil.rmtree(state, ignore_errors=True)
        report.skip(ERROR_IDS, "needs the fake CLI (run without --real)")
        return report
    world = FakeWorld()
    try:
        def start(**overrides: Optional[str]) -> McpClient:
            return world.start(plugin, **overrides)
        protocol_checks(report, start, fake=True)
        client = start()
        try:
            client.initialize()
            catalog_checks(report, client, fake=True)
            report.run("protocol.eof_exit_zero.after_tools",
                       lambda: expect(client.close() == 0, "the server did not exit 0"))
        finally:
            client.kill()
        stubs = world.stub_calls()
    finally:
        world.cleanup()
    error_path_checks(report, plugin)
    report.run("isolation.no_device_tool", lambda: expect(not stubs, "a device tool was called: %s" % short(stubs)))
    return report


def plan(plugin: Path, real: bool, report_path: Optional[str]) -> Dict[str, Any]:
    checks = list(PROTOCOL_IDS) + list(CATALOG_IDS) + ["protocol.eof_exit_zero.after_tools"]
    if real:
        checks += list(REAL_READ_IDS)
    else:
        checks += ["isolation.no_device_tool"]
    checks += list(ERROR_IDS)
    try:
        shown = str(plugin.resolve().relative_to(ROOT))
    except ValueError:
        shown = str(plugin)
    return {"ok": True, "dry_run": True, "mode": "real" if real else "fake", "plugin": shown,
            "cli": "scripts/autonom.py" if real else "tests/fakes/fake_mcp_cli.py",
            "report": report_path, "tools": list(TOOL_ORDER), "checks": checks,
            "error_paths": "not_exercised" if real else "fake CLI"}


# ====================================================================== self-test

def self_test() -> int:
    count = [0]

    def check(condition: Any, message: str) -> None:
        count[0] += 1
        if not condition:
            raise AssertionError(message)

    schema = {"type": "object", "additionalProperties": False, "required": ["a"], "properties": {
        "a": {"type": "string", "pattern": "^op_[0-9a-f]{4}$"},
        "b": {"type": ["integer", "null"], "minimum": 0, "maximum": 3},
        "c": {"type": "array", "maxItems": 2, "items": {"type": "object", "properties": {"x": {"const": True}}}},
        "d": {"oneOf": [{"type": "string", "enum": ["agent", "human"]}, {"type": "null"}]},
    }}
    check(validate(schema, {"a": "op_00ff", "b": None, "c": [{"x": True}], "d": None}) == [], "valid value")
    check(validate(schema, {"a": "op_00ff\n"}) != [], "a trailing newline must not match $")
    check(validate(schema, {}) == ["$: missing required property a"], "required")
    check(validate(schema, {"a": "op_00ff", "z": 1}) == ["$: unexpected property z"], "additionalProperties")
    check(validate(schema, {"a": "op_00ff", "b": True}) != [], "a bool is not an integer")
    check(validate(schema, {"a": "op_00ff", "b": 4}) != [], "maximum")
    check(validate(schema, {"a": "op_00ff", "c": [{"x": 1}]}) != [], "const true is not 1")
    check(validate(schema, {"a": "op_00ff", "c": [{}, {}, {}]}) != [], "maxItems")
    check(validate(schema, {"a": "op_00ff", "d": "robot"}) != [], "oneOf enum")
    check(schema_problems(schema) == [], "the sample schema is in the subset")
    check(schema_problems({"type": "object", "format": "uri"}) != [], "format is outside the subset")
    check(schema_problems({"type": "map"}) != [], "an unknown type")
    check(get_path({"a": [{"b": 1}, {"b": 2}]}, "a[].b") == [1, 2], "spread path")
    check(get_path({"a": []}, "a[].b") is MISSING, "an empty list has no items")
    check(get_path({"a": {"b": None}}, "a.b") is None, "a null leaf is present")
    check(get_path({"a": {}}, "a.b") is MISSING, "a missing leaf")
    good_tool = {"name": "list_hosts", "title": "t", "description": "d", "annotations": {
        "readOnlyHint": True, "openWorldHint": False}, "_meta": {"openai/widgetAccessible": True},
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
        "outputSchema": {"type": "object", "additionalProperties": False, "required": list(ENVELOPE_REQUIRED),
                         "properties": dict({key: {} for key in ENVELOPE_REQUIRED + FAILURE_FIELDS},
                                            tool={"const": "list_hosts"},
                                            schema_version={"const": SCHEMA_VERSION})}}
    check(tool_definition_problems(good_tool) == [], "a good definition")
    bad_tool = json.loads(json.dumps(good_tool))
    bad_tool["annotations"]["readOnlyHint"] = False
    bad_tool["_meta"]["openai/visibility"] = "private"
    bad_tool["_meta"]["ui"] = {"visibility": ["app"]}
    check(len(tool_definition_problems(bad_tool)) == 2, "readOnlyHint and app-only metadata are checked")
    report = Report()
    report.run("a", lambda: None)
    report.run("b", lambda: expect(False, "boom"))
    report.run("c", lambda: (_ for _ in ()).throw(NotExercised("why")))
    check([item["status"] for item in report.checks] == ["pass", "fail", "not_exercised"], "report statuses")
    check(report.ok is False and report.checks[1]["detail"] == "boom", "a failing report")
    stub_root = Path(tempfile.mkdtemp(prefix="mcp-conformance-stubs-"))
    try:
        make_device_stubs(stub_root / "bin", stub_root / "log")
        proc = subprocess.run([str(stub_root / "bin" / "adb"), "devices"], stdin=subprocess.DEVNULL,
                              capture_output=True, check=False)
        check(proc.returncode == 1, "a device stub fails")
        check((stub_root / "log").read_text(encoding="utf-8").strip() == "adb devices", "a device stub logs")
        (stub_root / "log").unlink()
        env = clean_env()
        env.update({"PATH": str(stub_root / "bin") + os.pathsep + os.environ.get("PATH", ""),
                    "HOME": str(stub_root / "home"), "AUTONOM_HOME": str(stub_root / "state"),
                    "XDG_STATE_HOME": str(stub_root / "xdg"), "PYTHONDONTWRITEBYTECODE": "1"})
        for argv, code in ((["--real", "--self-test"], "invalid_arguments"),
                           (["--plugin", "/nonexistent/x"], "plugin_invalid"), (["--bogus"], "invalid_arguments")):
            proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), *argv], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=60, check=False, env=env)
            check(proc.returncode == 2 and proc.stdout == "", "%s exits 2 quietly" % argv)
            check(json.loads(proc.stderr)["error_code"] == code, "%s refuses %s" % (argv, code))
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--dry-run"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60, check=False, env=env)
        check(proc.returncode == 0 and json.loads(proc.stdout)["mode"] == "fake", "the dry run prints the plan")
        check(not (stub_root / "log").exists(), "the refusals and the dry run call no device tool")
    finally:
        shutil.rmtree(stub_root, ignore_errors=True)
    dry = plan(DEFAULT_PLUGIN, False, None)
    check(dry["plugin"] == "integrations/chatgpt/autonom-chatgpt" and len(dry["tools"]) == 28, "the dry-run plan")
    # The whole fake suite.
    started = time.monotonic()
    suite = run_suite(DEFAULT_PLUGIN, real=False)
    failed = [item for item in suite.checks if item["status"] != "pass"]
    check(not failed, "fake suite: %s" % json.dumps(failed, indent=1))
    ids = [item["id"] for item in suite.checks]
    check(sorted(ids) == sorted(dry["checks"]), "the plan names every check: %s" % sorted(set(ids) ^ set(dry["checks"])))
    check(len(ids) == len(set(ids)), "check ids are unique")
    print("self-test ok (%d assertions, %d suite checks in %.1f s)" % (count[0], len(ids), time.monotonic() - started))
    return 0


# ====================================================================== main

class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError("invalid_arguments", message, "See --help.")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="chatgpt_mcp_conformance.py", description=__doc__.split("\n\n")[0],
                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plugin", default=str(DEFAULT_PLUGIN), help="plugin folder holding .mcp.json")
    parser.add_argument("--real", action="store_true", help="the real CLI of this checkout, read-only tools only")
    parser.add_argument("--report", help="write the JSON report to FILE")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and start nothing")
    parser.add_argument("--self-test", action="store_true", help="test the helpers, then run the fake suite")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.self_test and (args.real or args.dry_run or args.report):
            raise UsageError("invalid_arguments", "--self-test takes no other option", "Run --self-test alone.")
        if args.self_test:
            return self_test()
        plugin = Path(args.plugin).expanduser().resolve()
        if not (plugin / ".mcp.json").is_file():
            raise UsageError("plugin_invalid", "The plugin folder has no .mcp.json",
                             "Pass --plugin with the folder that holds .mcp.json.")
        if args.real and sys.version_info < (3, 11):
            raise UsageError("python_too_old", "The Autonom CLI needs Python 3.11 or newer",
                             "Run this script with Python 3.11 or newer.")
        if args.report and not Path(args.report).expanduser().resolve().parent.is_dir():
            raise UsageError("invalid_arguments", "The report folder does not exist", "Create it first.")
        if args.dry_run:
            print(json.dumps(plan(plugin, args.real, args.report), indent=2))
            return 0
    except UsageError as exc:
        sys.stderr.write(json.dumps(exc.payload()) + "\n")
        return 2
    report = run_suite(plugin, args.real)
    payload = report.as_json("real" if args.real else "fake")
    text = json.dumps(payload, indent=2) + "\n"
    if args.report:
        Path(args.report).expanduser().write_text(text, encoding="utf-8")
    failed = [item for item in report.checks if item["status"] == "fail"]
    summary = {"ok": report.ok, "checks": len(report.checks), "failed": [item["id"] for item in failed],
               "not_exercised": [item["id"] for item in report.checks if item["status"] == "not_exercised"]}
    print(json.dumps(summary, indent=2) if args.report else text, end="" if not args.report else "\n")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
