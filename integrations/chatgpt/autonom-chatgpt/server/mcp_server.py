#!/usr/bin/env python3
"""Autonom MCP server: newline-delimited JSON-RPC 2.0 over stdio (contract 2.2).

stdout carries only MCP messages, one compact JSON object per line, written
through one locked function. Diagnostics go to stderr as single token-free
lines. A daemon thread reads stdin; the main thread answers ``initialize``,
``ping`` and notifications itself; every other request runs on a pool of 16
workers, so a slow call never delays another one, and answers leave in
completion order. Runs on Python
>= 3.9 with the standard library and its sibling modules only.
"""
from __future__ import annotations

import json
import os
import queue
import signal
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Mapping, Optional, TextIO, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp_catalog import CatalogError, build_catalog, definition, setup_modules  # noqa: E402
from mcp_cli import Runner  # noqa: E402
from mcp_common import (ADAPTER_VERSION, CallContext, Reply, Services, Tool, ToolError,  # noqa: E402
                        base_fields, call_result, check_arguments, failure_fields, new_operation_id,
                        output_schema, session_scoped, validate_detail)
from mcp_control import LeaseKeeper  # noqa: E402
from mcp_core_tools import ResourceBroken, ResourceNotFound, read_resource, resource_entries  # noqa: E402
from mcp_env import Resolver, load_config, mcp_state_dir, path_scrubber  # noqa: E402
from mcp_refs import Refs, SessionGuard, SessionHooks, SessionViews  # noqa: E402

SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
MAX_LINE = 4 * 1024 * 1024
MAX_WORKERS = 16
UI_EXTENSION = "io.modelcontextprotocol/ui"
UI_MIME = "text/html;profile=mcp-app"
NEEDS_INIT = ("tools/list", "tools/call", "resources/list", "resources/read", "resources/templates/list")
SHUTDOWN_HOOKS_BUDGET_S = 10.0
INFLIGHT_WAIT_S = 5.0
# How often the main thread wakes from its wait for the next input line, so a
# stop signal the kernel handed to another thread still reaches its handler.
SIGNAL_POLL_S = 0.2
INSTRUCTIONS = (
    "Autonom drives Android emulators and iOS Simulators on this computer. Every device tool needs "
    "host_id and session_id from list_sessions or start_session. Mutations report outcome "
    "(completed, rejected, failed or outcome_unknown). Never retry an outcome_unknown mutation "
    "without checking get_operation and the screen first.")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
RESOURCE_NOT_FOUND = -32002


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class Shutdown(Exception):
    """Raised from the signal handler to leave the blocking read."""


_OVERSIZED = object()


def _id_key(msg_id: Any) -> Tuple[str, Any]:
    return (type(msg_id).__name__, msg_id)


class McpServer:
    def __init__(self, env: Mapping[str, str], home: Path, stdin: BinaryIO, stdout: BinaryIO,
                 stderr: TextIO) -> None:
        self.env = dict(env)
        self.home = Path(home)
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self._write_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self.scrub = path_scrubber(self.env, self.home)
        self.config = load_config(self.env, self.home)
        self.state_dir = mcp_state_dir(self.env, self.home)
        resolver = Resolver(self.env, self.home, self.config)
        runner = Runner(self.env, self.home, self.config, resolver, self.state_dir, self.log)
        refs = Refs(runner, self.config, self.env, self.home, self.state_dir, self.log)
        keeper = LeaseKeeper(runner, refs, self.config, self.log)
        self.services = Services(env=self.env, home=self.home, config=self.config,
                                 state_dir=self.state_dir, resolver=resolver, runner=runner, refs=refs,
                                 guard=SessionGuard(), hooks=SessionHooks(self.log), views=SessionViews(),
                                 keeper=keeper, log=self.log, scrub=self.scrub)
        self.services.on_shutdown("leases", keeper.release_all, 30)
        tools = build_catalog(self.env)
        setup_modules(self.services)
        self.tools: Dict[str, Tool] = {tool.name: tool for tool in tools}
        self.definitions = [definition(tool) for tool in tools]
        self.schemas = {tool.name: output_schema(tool.name, tool.output_fields, session_scoped(tool))
                        for tool in tools}
        self.pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="mcp-worker")
        self._inflight_lock = threading.Condition()
        self._inflight: Dict[Tuple[str, Any], Dict[str, Any]] = {}
        self.stopping = threading.Event()
        self.initialized = False
        self.ready = False
        self.protocol: Optional[str] = None
        for warning in self.config.warnings:
            self.log("config problem: %s" % warning["message"])

    # -- output

    def log(self, message: str) -> None:
        line = "autonom-chatgpt: " + self.scrub.text(" ".join(str(message).split()))
        with self._log_lock:
            try:
                self.stderr.write(line + "\n")
                self.stderr.flush()
            except (OSError, ValueError):
                pass

    @staticmethod
    def encode(message: Dict[str, Any]) -> bytes:
        """One compact JSON line; text that is not valid UTF-8 (a lone surrogate) is escaped."""
        text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        try:
            data = text.encode("utf-8")
        except UnicodeEncodeError:
            data = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        return data + b"\n"

    def send(self, message: Dict[str, Any]) -> None:
        try:
            data = self.encode(message)
        except (ValueError, TypeError, RecursionError) as exc:
            self.log("reply not serialisable: %s" % type(exc).__name__)
            msg_id = message.get("id") if isinstance(message, dict) else None
            if not isinstance(msg_id, (str, int)) or isinstance(msg_id, bool):
                msg_id = None
            data = self.encode(self._error(msg_id, INTERNAL_ERROR, "Internal error: %s" % type(exc).__name__))
        with self._write_lock:
            try:
                self.stdout.write(data)
                self.stdout.flush()
            except (BrokenPipeError, OSError, ValueError):
                self._stdout_broken()

    def _stdout_broken(self) -> None:
        if self.stopping.is_set():
            return
        self.stopping.set()
        self.log("stdout closed; shutting down")
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except OSError:
            pass

    @staticmethod
    def _error(msg_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
        error: Dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": msg_id, "error": error}

    # -- input

    def read_line(self) -> Any:
        """Next line as bytes, ``_OVERSIZED`` for a discarded line, None at EOF."""
        line = self.stdin.readline(MAX_LINE + 2)
        if not line:
            return None
        content = line[:-1] if line.endswith(b"\n") else line
        if content.endswith(b"\r"):
            content = content[:-1]
        if len(content) > MAX_LINE:
            while not line.endswith(b"\n"):
                line = self.stdin.readline(65536)
                if not line:
                    break
            return _OVERSIZED
        return content

    def _pump(self, lines: "queue.Queue[Tuple[bool, Any]]") -> None:
        """Read stdin on its own thread and hand each line over in order."""
        while True:
            try:
                line = self.read_line()
            except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
                lines.put((False, exc))
                return
            lines.put((True, line))
            if line is None:
                return

    def serve(self) -> None:
        # The blocking read runs on a daemon thread and the main thread waits on
        # a queue with a short timeout. Python runs signal handlers only on the
        # main thread, and the kernel may hand SIGTERM to any thread: a read
        # blocked on the main thread would then never return to run the handler.
        lines: "queue.Queue[Tuple[bool, Any]]" = queue.Queue(maxsize=1)
        threading.Thread(target=self._pump, args=(lines,), name="mcp-stdin", daemon=True).start()
        while not self.stopping.is_set():
            try:
                ok, line = lines.get(timeout=SIGNAL_POLL_S)
            except queue.Empty:
                continue
            if not ok:
                raise line
            if line is None:
                return
            if line is _OVERSIZED:
                self.send(self._error(None, PARSE_ERROR, "Parse error"))
                continue
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                self.send(self._error(None, PARSE_ERROR, "Parse error"))
                continue
            try:
                response = self.handle(message)
            except Shutdown:
                raise
            except Exception as exc:  # noqa: BLE001 - the reader never dies on one message
                self.log("internal error in reader: %s" % type(exc).__name__)
                raw_id = message.get("id") if isinstance(message, dict) else None
                msg_id = raw_id if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool) else None
                response = self._error(msg_id, INTERNAL_ERROR, "Internal error: %s" % type(exc).__name__)
            if response is not None:
                self.send(response)

    # -- dispatch

    def handle(self, message: Any) -> Optional[Dict[str, Any]]:
        """Answer inline, schedule on the pool (None), or ignore (None)."""
        if not isinstance(message, dict):
            return self._error(None, INVALID_REQUEST, "Invalid Request")
        has_id = "id" in message
        raw_id = message.get("id")
        id_ok = isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool)
        msg_id = raw_id if id_ok else None
        method = message.get("method")
        if "method" not in message and ("result" in message or "error" in message):
            return None  # a response from the client; this server sends no requests
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return self._error(msg_id, INVALID_REQUEST, "Invalid Request")
        params = message.get("params")
        if not has_id:
            self.notification(method, params)
            return None
        if not id_ok:
            return self._error(None, INVALID_REQUEST, "Invalid Request")
        if method == "initialize":
            return self._inline(msg_id, self.initialize, params)
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if method not in NEEDS_INIT:
            return self._error(msg_id, METHOD_NOT_FOUND, "Method not found", {"method": method[:80]})
        if not self.initialized:
            return self._error(msg_id, INVALID_REQUEST, "Server not initialized")
        key = _id_key(msg_id)
        entry = {"id": msg_id, "cancelled": threading.Event(), "method": method}
        with self._inflight_lock:
            if key in self._inflight:
                return self._error(msg_id, INVALID_REQUEST, "Duplicate request id")
            self._inflight[key] = entry
        try:
            self.pool.submit(self._work, key, entry, method, params)
        except RuntimeError:
            with self._inflight_lock:
                self._inflight.pop(key, None)
                self._inflight_lock.notify_all()
            return self._error(msg_id, INTERNAL_ERROR, "Internal error: server is stopping")
        return None

    def _inline(self, msg_id: Any, fn: Any, params: Any) -> Dict[str, Any]:
        try:
            return {"jsonrpc": "2.0", "id": msg_id, "result": fn(params)}
        except RpcError as exc:
            return self._error(msg_id, exc.code, exc.message, exc.data)

    def notification(self, method: str, params: Any) -> None:
        if method == "notifications/initialized":
            self.ready = True
        elif method == "notifications/cancelled" and isinstance(params, dict):
            request_id = params.get("requestId")
            if isinstance(request_id, (str, int)) and not isinstance(request_id, bool):
                with self._inflight_lock:
                    entry = self._inflight.get(_id_key(request_id))
                if entry is not None:
                    entry["cancelled"].set()

    def _work(self, key: Tuple[str, Any], entry: Dict[str, Any], method: str, params: Any) -> None:
        # The id stays reserved until its response is written (or suppressed), so a
        # duplicate that arrives while the answer waits for stdout is still refused
        # with -32600 and never runs a second time (contract 2.2, DEC-008).
        try:
            if entry["cancelled"].is_set():
                # Cancelled while still queued: the handler never runs, so a queued
                # mutation has no device effect, and no response is sent (MCP).
                return
            response = self._respond(entry, method, params)
            if entry["cancelled"].is_set():
                return  # MCP: no response for a request cancelled before it finished
            self.send(response)
        finally:
            with self._inflight_lock:
                self._inflight.pop(key, None)
                self._inflight_lock.notify_all()

    def _respond(self, entry: Dict[str, Any], method: str, params: Any) -> Dict[str, Any]:
        msg_id = entry["id"]
        try:
            return {"jsonrpc": "2.0", "id": msg_id, "result": self.dispatch(method, params, entry)}
        except RpcError as exc:
            return self._error(msg_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # noqa: BLE001 - every handler fault becomes -32603
            self.log("internal error in %s: %s" % (method[:80], type(exc).__name__))
            for line in traceback.format_exc().splitlines():
                self.log(line)
            return self._error(msg_id, INTERNAL_ERROR, "Internal error: %s" % type(exc).__name__)

    def dispatch(self, method: str, params: Any, entry: Dict[str, Any]) -> Dict[str, Any]:
        if method == "tools/list":
            return {"tools": self.definitions}
        if method == "tools/call":
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                raise RpcError(INVALID_PARAMS, "Invalid params")
            name = params["name"]
            tool = self.tools.get(name)
            if tool is None:
                raise RpcError(INVALID_PARAMS, "Unknown tool: %s" % name[:80])
            return self.call_tool(tool, params.get("arguments"), entry["cancelled"])
        if method == "resources/list":
            return {"resources": resource_entries(self.services)}
        if method == "resources/read":
            if not isinstance(params, dict) or not isinstance(params.get("uri"), str):
                raise RpcError(INVALID_PARAMS, "Invalid params")
            try:
                return read_resource(self.services, params["uri"])
            except ResourceNotFound:
                raise RpcError(RESOURCE_NOT_FOUND, "Resource not found", {"uri": params["uri"][:500]})
            except ResourceBroken as exc:
                raise RpcError(INTERNAL_ERROR, str(exc))
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        raise RpcError(METHOD_NOT_FOUND, "Method not found", {"method": method[:80]})

    # -- tools

    def call_tool(self, tool: Tool, arguments: Any, cancelled: threading.Event) -> Dict[str, Any]:
        ctx = CallContext(self.services, tool, new_operation_id(), cancelled)
        content: List[Dict[str, Any]] = []
        meta: Dict[str, Any] = {}
        try:
            check_arguments(tool.input_schema, arguments)
            value = tool.handler(ctx, dict(arguments or {}))
            if isinstance(value, Reply):
                fields, content, meta = value.fields, value.content, value.meta
            else:
                fields = value if isinstance(value, dict) else {}
            structured = base_fields(tool, ctx, True, "completed", self._host_id(ctx))
            for field, item in fields.items():
                if field in structured:
                    self.log("tool %s tried to set envelope field %s" % (tool.name, field))
                    continue
                structured[field] = item
        except ToolError as exc:
            structured = failure_fields(tool, ctx, exc, self._host_id(ctx))
            content, meta = [], {}
        except Exception as exc:  # noqa: BLE001 - a tool fault still answers with its operation id
            self.log("tool %s failed: %s" % (tool.name, type(exc).__name__))
            for line in traceback.format_exc().splitlines():
                self.log(line)
            error = ToolError("internal_error", "Internal error: %s" % type(exc).__name__)
            structured = failure_fields(tool, ctx, error, self._host_id(ctx))
            content, meta = [], {}
        # No failure text and no warning may carry a local path or a secret (p3-020):
        # the runner already cleans CLI text, this covers what a handler wrote.
        if structured.get("ok") is True:
            structured["warnings"] = self.scrub.value(structured.get("warnings"))
        else:
            structured = self.scrub.value(structured)
        problems = validate_detail(self.schemas[tool.name], structured)
        if problems:
            self.log("tool %s output violates its schema at %s" % (
                tool.name, ", ".join(path or "$" for path, _problem in problems[:10])))
            violation = ToolError("schema_violation", "The server built a result that breaks its schema")
            outcome = "outcome_unknown" if tool.mutating else "failed"
            ctx.warnings = []
            structured = failure_fields(tool, ctx, violation, self._host_id(ctx), outcome=outcome)
            content, meta = [], {}
        return call_result(structured, content, meta)

    def _host_id(self, ctx: CallContext) -> Optional[str]:
        return ctx.host_id or self.services.refs.cached_host_id()

    # -- lifecycle

    def initialize(self, params: Any) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        requested = params.get("protocolVersion")
        self.protocol = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        self.initialized = True
        return {
            "protocolVersion": self.protocol,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False, "subscribe": False},
                "extensions": {UI_EXTENSION: {"mimeTypes": [UI_MIME]}},
                "experimental": {UI_EXTENSION: {"mimeTypes": [UI_MIME]}},
            },
            "serverInfo": {"name": "autonom-chatgpt", "title": "Autonom", "version": ADAPTER_VERSION},
            "instructions": INSTRUCTIONS,
        }

    def inflight_count(self) -> int:
        with self._inflight_lock:
            return len(self._inflight)

    def shutdown(self) -> bool:
        """Run the shutdown hooks (10 s budget) and wait up to 5 s for in-flight
        handlers. True when every handler finished."""
        self.stopping.set()
        hooks = self.services.shutdown_hooks()

        def run_hooks() -> None:
            for name, fn in hooks:
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001 - every hook gets its turn
                    self.log("shutdown hook %s failed: %s" % (name, type(exc).__name__))

        runner = threading.Thread(target=run_hooks, name="mcp-shutdown", daemon=True)
        runner.start()
        runner.join(SHUTDOWN_HOOKS_BUDGET_S)
        if runner.is_alive():
            self.log("shutdown hooks exceeded %.0f s" % SHUTDOWN_HOOKS_BUDGET_S)
        deadline = time.monotonic() + INFLIGHT_WAIT_S
        with self._inflight_lock:
            while self._inflight and time.monotonic() < deadline:
                self._inflight_lock.wait(max(0.01, deadline - time.monotonic()))
            finished = not self._inflight
        if finished and not runner.is_alive():
            # A handler that finished during the wait may have taken a lease again.
            try:
                self.services.keeper.release_all()
            except Exception:  # noqa: BLE001
                pass
        self.pool.shutdown(wait=False)
        return finished and not runner.is_alive()


def _private_stdin() -> BinaryIO:
    """A reader of its own on a copy of fd 0. The stdin thread may still sit in
    a read when the interpreter exits after a signal; a reader that only that
    thread holds is never closed at exit, so its lock is never contended."""
    try:
        return os.fdopen(os.dup(sys.stdin.fileno()), "rb")
    except (OSError, ValueError, AttributeError):
        return sys.stdin.buffer


def main(argv: Optional[List[str]] = None) -> int:
    try:
        server = McpServer(os.environ, Path.home(), _private_stdin(), sys.stdout.buffer, sys.stderr)
    except CatalogError as exc:
        sys.stderr.write("autonom-chatgpt: catalog error: %s\n" % exc)
        return 1

    def _ignore_stop_signals() -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)

    reading = {"on": True}

    def _stop(signum: int, _frame: Any) -> None:
        # Only a signal that lands while the server still reads ends the read,
        # and at most once: the flag drops before the raise, so a signal that
        # arrives after an EOF (or a broken stdout) ended the read never raises
        # into the shutdown hooks or the in-flight wait.
        _ignore_stop_signals()
        if reading["on"]:
            reading["on"] = False
            raise Shutdown()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        try:
            server.serve()
        finally:
            reading["on"] = False
    except (Shutdown, KeyboardInterrupt, BrokenPipeError):
        pass
    clean = True
    try:
        # However the shutdown began (EOF, a signal or a broken stdout), a
        # further SIGTERM or SIGINT must not cut the hooks or the in-flight wait.
        _ignore_stop_signals()
        clean = server.shutdown()
    except (Shutdown, KeyboardInterrupt):
        clean = False  # a signal landed before it was ignored
    try:
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        # stdout is gone (the broken-pipe shutdown): the bytes that failed to
        # leave must not turn the clean exit into the interpreter's exit 120.
        clean = False
    if not clean:
        os._exit(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
