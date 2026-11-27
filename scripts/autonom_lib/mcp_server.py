"""A persistent MCP server for the agent loop (`autonom mcp serve`).

One long-lived process speaks MCP over stdio (newline-delimited JSON-RPC 2.0)
and runs every tool call as an Autonom command *in this process*: no
interpreter start, no import of the CLI and no shell per call. A tool builds
the same argv an agent would type and hands it to ``run`` (the CLI's own
``main``, with its stdout and stderr captured), so a tool does exactly what
the verb does: the same checks, session, control lease, journal and exit
codes. Nothing here reaches a device by another way.

The tools are the loop of the `autonom` skill, worded for an agent:

- ``devices``, ``session_start``, ``session_stop``;
- ``screen`` (the outline), ``tap``, ``type_text``, ``fill``, ``swipe``,
  ``key`` (each may wait with ``until`` and take a ``shot``), ``wait``,
  ``steps`` (`ui do`), ``screenshot``;
- ``autonom``: any other verb by its arguments (`network mock add ...`),
  at the same in-process speed.

Replies are the short text of `autonom_lib.brief`, whatever the session's
profile: the action tools ask the CLI for its JSON line (and to observe) and
word it here. An expected failure (exit 2) is ``isError`` with the code, the
message and the hint; an unmet wait or a failed step run (exit 1) is a
normal reply that says so.

The server answers one request at a time, in order. Messages it does not
know are refused with a JSON-RPC error, never by exiting: only the end of
stdin stops it. stdout carries MCP messages and nothing else.
"""
from __future__ import annotations

import json
from typing import Any, BinaryIO, Callable, Mapping, NamedTuple

from . import __version__, brief

SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "autonom"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    "Drive an Android emulator or an iOS Simulator with Autonom. Loop: devices, session_start, "
    "screen once, then tap / type_text / key / swipe; each reply says what changed, so decide the "
    "next step from it. Never sleep between calls: pass `until` on the action (text=V, desc=V, "
    "id=V, gone:text=V, request=GLOB) or call wait. Pass `shot` for an evidence screenshot. A "
    "reply line that starts with `!` needs attention; after `! until_not_met` the input was "
    "already sent: read the screen, do not repeat it. On iOS a visible label is in desc. Use the "
    "autonom tool for any other verb (network mocks, logs, journal)."
)


class Call(NamedTuple):
    """One CLI invocation a tool makes: its argv, what it reads on stdin, and
    how its JSON reply is worded (None: printed as it is)."""

    argv: list[str]
    stdin: str | None = None
    words: Callable[[Mapping[str, Any]], str] | None = None


class BadArguments(ValueError):
    """Tool arguments the schema allows but the tool cannot use."""


# --- argument helpers -----------------------------------------------------------------------


def _text(params: Mapping[str, Any], name: str, *, required: bool = False) -> str | None:
    value = params.get(name)
    if value is None:
        if required:
            raise BadArguments(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise BadArguments(f"{name} must be a string")
    return value


def _number(params: Mapping[str, Any], name: str) -> str | None:
    value = params.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadArguments(f"{name} must be a number")
    return str(int(value)) if float(value).is_integer() else str(value)


def _point(params: Mapping[str, Any], name: str) -> str:
    value = params.get(name)
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(isinstance(part, bool) or not isinstance(part, (int, float)) for part in value)):
        raise BadArguments(f"{name} must be [x, y]")
    return ",".join(str(int(part)) for part in value)


def _option(flag: str, value: str | None) -> list[str]:
    """``--flag=value``: a value that starts with a dash is still a value."""
    return [] if value is None else [f"{flag}={value}"]


def _target(params: Mapping[str, Any]) -> list[str]:
    return [*_option("--session-id", _text(params, "session_id")),
            *_option("--serial", _text(params, "serial")),
            *_option("--udid", _text(params, "udid"))]


def _selector(params: Mapping[str, Any]) -> list[str]:
    out = [*_option("--text", _text(params, "text")), *_option("--desc", _text(params, "desc")),
           *_option("--resource-id", _text(params, "id"))]
    if out:
        out += _option("--index", _number(params, "index"))
        if params.get("exact"):
            out += ["--mode", "exact"]
    return out


def _after(params: Mapping[str, Any]) -> list[str]:
    """What an action does after its input: ``until``, ``shot``; and it
    always observes and answers with its JSON line, worded here."""
    out = ["--observe", "--json"]
    until = params.get("until")
    if until is not None:
        if isinstance(until, str):
            until = [until]
        if not isinstance(until, list) or not all(isinstance(item, str) for item in until):
            raise BadArguments("until must be a condition or a list of conditions")
        for item in until:
            out += _option("--until", item)
    out += _option("--until-timeout-ms", _number(params, "until_timeout_ms"))
    out += _option("--shot", _text(params, "shot"))
    return out


def _action(verb: str, params: Mapping[str, Any]) -> Callable[[Mapping[str, Any]], str]:
    whole = bool(params.get("whole_screen"))
    return lambda data: brief.action(verb, data, screen=whole)


# --- the tools ------------------------------------------------------------------------------


def _devices(params: Mapping[str, Any]) -> Call:
    return Call(["devices"], words=brief.devices)


def _session_start(params: Mapping[str, Any]) -> Call:
    argv = ["session", "start", "--profile", "agent", *_target(params),
            *_option("--app-id", _text(params, "app_id")),
            *_option("--install", _text(params, "install")),
            *_option("--label", _text(params, "label"))]
    if params.get("launch"):
        argv.append("--launch")
    if params.get("alongside"):
        argv.append("--alongside")
    return Call(argv, words=brief.session)


def _session_stop(params: Mapping[str, Any]) -> Call:
    return Call(["session", "stop", *_target(params)], words=brief.session)


def _screen(params: Mapping[str, Any]) -> Call:
    argv = ["ui", "tree", "--format", "outline", *_target(params),
            *_option("--max-nodes", _number(params, "max_nodes"))]
    if params.get("interactable"):
        argv.append("--interactable")
    return Call(argv, words=brief.tree)


def _tap(params: Mapping[str, Any]) -> Call:
    selector = _selector(params)
    point = [*_option("--x", _number(params, "x")), *_option("--y", _number(params, "y"))]
    if bool(selector) == bool(point) or len(point) == 1:
        raise BadArguments("tap needs text, desc or id, or both x and y")
    return Call(["ui", "tap", *_target(params), *selector, *point,
                 *_option("--duration", _number(params, "hold_ms")), *_after(params)],
                words=_action("ui tap", params))


def _type_text(params: Mapping[str, Any]) -> Call:
    argv = ["ui", "type", *_target(params), *_after(params)]
    if params.get("sensitive"):
        argv.append("--sensitive")
    return Call([*argv, "--", _text(params, "value", required=True) or ""],
                words=_action("ui type", params))


def _fill(params: Mapping[str, Any]) -> Call:
    argv = ["ui", "fill", *_target(params), *_selector(params), *_after(params)]
    if params.get("sensitive"):
        argv.append("--sensitive")
    return Call([*argv, "--", _text(params, "value", required=True) or ""],
                words=_action("ui fill", params))


def _swipe(params: Mapping[str, Any]) -> Call:
    return Call(["ui", "swipe", *_target(params), *_option("--from", _point(params, "from")),
                 *_option("--to", _point(params, "to")),
                 *_option("--duration", _number(params, "seconds")), *_after(params)],
                words=_action("ui swipe", params))


def _key(params: Mapping[str, Any]) -> Call:
    return Call(["ui", "key", *_target(params), *_after(params), "--",
                 _text(params, "key", required=True) or ""], words=_action("ui key", params))


def _wait(params: Mapping[str, Any]) -> Call:
    selector = _selector(params)
    request = _text(params, "request")
    kinds = sum((bool(selector), bool(params.get("settled")), request is not None))
    if kinds != 1:
        raise BadArguments("wait needs exactly one of: text, desc or id; settled; request")
    timeout = _option("--timeout-ms", _number(params, "timeout_ms"))
    if request is not None:
        return Call(["network", "requests", "wait", *_target(params), "--path", request,
                     *_option("--method", _text(params, "method")),
                     *_option("--status", _number(params, "status")), *timeout, "--json"],
                    words=brief.waited)
    argv = ["ui", "wait", *_target(params), *timeout, "--json"]
    if selector:
        argv += selector
        if params.get("gone"):
            argv.append("--gone")
    else:
        argv.append("--settled")
    return Call(argv, words=brief.waited)


def _steps(params: Mapping[str, Any]) -> Call:
    listed = params.get("steps")
    if not isinstance(listed, list) or not listed:
        raise BadArguments("steps must be a non-empty list of Flow v1 steps")
    whole = bool(params.get("whole_screen"))
    return Call(["ui", "do", *_target(params), "--json"], stdin=json.dumps(listed),
                words=lambda data: brief.steps(data, screen=whole))


def _screenshot(params: Mapping[str, Any]) -> Call:
    return Call(["screenshot", *_target(params), *_option("--label", _text(params, "label"))],
                words=brief.shot)


def _autonom(params: Mapping[str, Any]) -> Call:
    args = params.get("args")
    if not isinstance(args, list) or not args or not all(isinstance(item, str) for item in args):
        raise BadArguments("args must be a non-empty list of strings, e.g. "
                           '["network", "mock", "list"]')
    return Call(list(args), stdin=_text(params, "stdin"))


_TARGET = {
    "session_id": {"type": "string", "description": "A session other than the current one."},
}
_SELECTOR = {
    "text": {"type": "string", "description": "Visible text of the node (contains, any case)."},
    "desc": {"type": "string", "description": "Its accessibility label; on iOS the visible "
                                              "label is here."},
    "id": {"type": "string", "description": "Its resource id or accessibility identifier."},
    "index": {"type": "integer", "description": "Which of several matches, from 0."},
    "exact": {"type": "boolean", "description": "Match the whole value, not a part of it."},
}
_AFTER = {
    "until": {"type": "array", "items": {"type": "string"},
              "description": "Wait in this call until all hold: text=V, desc=V, id=V (a node on "
                             "screen), gone:text=V and the like (no such node), request=GLOB (a "
                             "request the capture proxy records)."},
    "until_timeout_ms": {"type": "integer", "description": "How long until waits (10000)."},
    "shot": {"type": "string", "description": "Take an evidence screenshot with this label."},
    "whole_screen": {"type": "boolean", "description": "Reply with the whole outline, not only "
                                                       "what changed."},
}


def _schema(properties: Mapping[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": dict(properties),
                              "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return schema


class Tool(NamedTuple):
    name: str
    description: str
    schema: dict[str, Any]
    build: Callable[[Mapping[str, Any]], Call]


TOOLS: tuple[Tool, ...] = (
    Tool("devices", "List Android devices and iOS simulators; the running ones first.",
         _schema({}), _devices),
    Tool("session_start",
         "Start the session you drive (replies stay short, actions report the new screen). "
         "Name the device by serial (Android) or udid (iOS) when several are attached.",
         _schema({"serial": {"type": "string"}, "udid": {"type": "string"},
                  "app_id": {"type": "string", "description": "Package or bundle id under test."},
                  "launch": {"type": "boolean", "description": "Launch app_id."},
                  "install": {"type": "string", "description": "Path of an APK or .app to "
                                                               "install first."},
                  "label": {"type": "string"},
                  "alongside": {"type": "boolean", "description": "Keep another device's "
                                                                  "session running."}}),
         _session_start),
    Tool("session_stop", "Stop the session (its proxy, log stream and device helper too).",
         _schema(_TARGET), _session_stop),
    Tool("screen",
         "Read the screen: one line per node (ref, role, label, id, position, state). Call it "
         "once; after that each action's reply says what changed.",
         _schema({**_TARGET,
                  "interactable": {"type": "boolean", "description": "Only nodes that take "
                                                                     "input."},
                  "max_nodes": {"type": "integer"}}), _screen),
    Tool("tap", "Tap a node by text, desc or id (or a point by x and y); hold_ms for a long "
                "press. The reply says what changed on the screen.",
         _schema({**_TARGET, **_SELECTOR, "x": {"type": "integer"}, "y": {"type": "integer"},
                  "hold_ms": {"type": "integer"}, **_AFTER}), _tap),
    Tool("type_text", "Type into the focused field.",
         _schema({**_TARGET, "value": {"type": "string"},
                  "sensitive": {"type": "boolean", "description": "A credential: keep it out "
                                                                  "of replies and artifacts."},
                  **_AFTER}, ("value",)), _type_text),
    Tool("fill", "Android: replace the whole text of a field (by text, desc or id, or the "
                 "focused field).",
         _schema({**_TARGET, "value": {"type": "string"}, **_SELECTOR,
                  "sensitive": {"type": "boolean"}, **_AFTER}, ("value",)), _fill),
    Tool("swipe", "Swipe from one point to another ([x, y] each), e.g. to scroll.",
         _schema({**_TARGET,
                  "from": {"type": "array", "items": {"type": "number"}, "minItems": 2,
                           "maxItems": 2},
                  "to": {"type": "array", "items": {"type": "number"}, "minItems": 2,
                         "maxItems": 2},
                  "seconds": {"type": "number", "description": "Duration (0.3)."}, **_AFTER},
                 ("from", "to")), _swipe),
    Tool("key", "Press a key: Android BACK, HOME, ENTER and other key names; iOS HOME, LOCK.",
         _schema({**_TARGET, "key": {"type": "string"}, **_AFTER}, ("key",)), _key),
    Tool("wait",
         "Wait instead of sleeping, for exactly one thing: a node by text, desc or id (gone: "
         "until it leaves), settled (the screen stops changing), or request (a path glob the "
         "capture proxy records).",
         _schema({**_TARGET, **_SELECTOR, "gone": {"type": "boolean"},
                  "settled": {"type": "boolean"},
                  "request": {"type": "string", "description": "Path or URL glob, e.g. "
                                                               "*/v1/orders*."},
                  "method": {"type": "string"}, "status": {"type": "integer"},
                  "timeout_ms": {"type": "integer"}}), _wait),
    Tool("steps",
         "Run several Flow v1 steps you already know in one call; stops at the first failing "
         'step. Example: [{"tapOn": {"text": "Profile"}}, {"assertVisible": {"text": "Sign '
         'out"}}, "back"].',
         _schema({**_TARGET, "steps": {"type": "array", "items": {}},
                  "whole_screen": _AFTER["whole_screen"]}, ("steps",)), _steps),
    Tool("screenshot", "Take a screenshot on its own (an action's shot is cheaper). Replies "
                       "with the file's path.",
         _schema({**_TARGET, "label": {"type": "string"}}), _screenshot),
    Tool("autonom",
         "Run any other Autonom verb by its arguments, e.g. [\"network\", \"mock\", \"add\", "
         "\"--match\", \"*/v1/orders*\", \"--status\", \"500\"] or [\"journal\", \"--max\", "
         "\"20\"]. Replies with what the verb prints.",
         _schema({"args": {"type": "array", "items": {"type": "string"}},
                  "stdin": {"type": "string"}}, ("args",)), _autonom),
)


# --- the server -----------------------------------------------------------------------------


def _reply_text(call: Call, code: int | None, out: str, err: str) -> tuple[str, bool]:
    """What the tool answers for a finished CLI call, and whether it is an
    error (exit 2 or worse: nothing useful ran)."""
    failed = code not in (0, 1, None)
    if failed:
        try:
            envelope = json.loads(err.strip().splitlines()[-1])
        except (ValueError, IndexError):
            envelope = None
        if isinstance(envelope, dict) and envelope.get("error_code"):
            return brief.failure(envelope), True
        return (err.strip() or out.strip() or f"autonom exited {code}"), True
    text = out.strip()
    if call.words is not None:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            text = call.words(data)
    stray = err.strip()
    if stray:  # consent prose or a warning a verb wrote beside its reply
        text = f"{text}\n{stray}" if text else stray
    return text, False


class Server:
    """The MCP conversation over a pair of byte streams.

    ``run(argv, stdin_text)`` runs one CLI command in this process and
    returns ``(exit code, stdout, stderr)``.
    """

    def __init__(self, run: Callable[[list[str], str | None], tuple[int | None, str, str]],
                 stdin: BinaryIO, stdout: BinaryIO,
                 log: Callable[[str], None] = lambda _line: None) -> None:
        self.run = run
        self.stdin = stdin
        self.stdout = stdout
        self.log = log
        self.tools = {tool.name: tool for tool in TOOLS}

    def serve(self) -> int:
        """Answer requests until stdin ends."""
        while True:
            line = self.stdin.readline()
            if not line:
                return 0
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._send(self._error(None, PARSE_ERROR, "Parse error"))
                continue
            for item in message if isinstance(message, list) else [message]:
                answer = self.handle(item)
                if answer is not None:
                    self._send(answer)

    def _send(self, message: Mapping[str, Any]) -> None:
        self.stdout.write(json.dumps(message, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8") + b"\n")
        self.stdout.flush()

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    def handle(self, message: Any) -> dict[str, Any] | None:
        """The answer to one message; None for a notification."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" \
                or not isinstance(message.get("method"), str):
            if isinstance(message, dict) and "method" not in message:
                return None  # a response to something this server never asked
            return self._error(message.get("id") if isinstance(message, dict) else None,
                               INVALID_REQUEST, "Invalid Request")
        method, params = message["method"], message.get("params")
        if "id" not in message:
            return None  # notifications (initialized, cancelled): nothing to answer
        msg_id = message["id"]
        try:
            return {"jsonrpc": "2.0", "id": msg_id, "result": self._result(method, params)}
        except LookupError:
            return self._error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method[:80]}")
        except BadArguments as exc:
            return self._error(msg_id, INVALID_PARAMS, str(exc))
        except Exception as exc:  # noqa: BLE001 - one bad call must not end the server
            self.log(f"{method} failed: {type(exc).__name__}: {exc}")
            return self._error(msg_id, INTERNAL_ERROR, f"Internal error: {type(exc).__name__}")

    def _result(self, method: str, params: Any) -> dict[str, Any]:
        if method == "initialize":
            asked = params.get("protocolVersion") if isinstance(params, dict) else None
            return {
                "protocolVersion": (asked if asked in SUPPORTED_PROTOCOL_VERSIONS
                                    else SUPPORTED_PROTOCOL_VERSIONS[0]),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "Autonom", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [{"name": tool.name, "description": tool.description,
                               "inputSchema": tool.schema} for tool in TOOLS]}
        if method == "tools/call":
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                raise BadArguments("tools/call needs a tool name")
            tool = self.tools.get(params["name"])
            if tool is None:
                raise BadArguments(f"Unknown tool: {params['name'][:80]}")
            arguments = params.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise BadArguments("arguments must be an object")
            return self.call(tool, arguments)
        raise LookupError(method)

    def call(self, tool: Tool, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Run one tool. Arguments the tool cannot use are a tool error
        (the model reads it and tries again), not a protocol error."""
        unknown = sorted(set(arguments) - set(tool.schema["properties"]))
        try:
            if unknown:
                raise BadArguments(f"{tool.name} takes no {', '.join(unknown)}; it takes "
                                   + ", ".join(tool.schema["properties"]))
            call = tool.build(arguments)
        except BadArguments as exc:
            return {"content": [{"type": "text", "text": f"error invalid_arguments: {exc}"}],
                    "isError": True}
        code, out, err = self.run(call.argv, call.stdin)
        text, failed = _reply_text(call, code, out, err)
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        if failed:
            result["isError"] = True
        return result
