#!/usr/bin/env python3
"""Autonom probe MCP server: newline-delimited JSON-RPC 2.0 over stdio.

Contract sections 2.1, 2.2 and 2.8. stdout carries only MCP messages, one
compact JSON object per line, written through one locked function.
Diagnostics go to stderr as single token-free lines. Runs on Python >= 3.9
with the standard library and its sibling modules only.
"""
from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Mapping, Optional, TextIO

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from probe_env import PROBE_VERSION, Resolver, load_config, state_dir  # noqa: E402
from probe_media_child import MediaChild  # noqa: E402
from probe_state import StateStore  # noqa: E402
from probe_tools import UI_URI, ProbeTools, UnknownTool, tool_definitions  # noqa: E402

SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
MAX_LINE = 4 * 1024 * 1024
UI_EXTENSION = "io.modelcontextprotocol/ui"
UI_MIME = "text/html;profile=mcp-app"
CORE_MARKER = "<!-- PROBE_CORE -->"
CONFIG_MARKER = "__PROBE_CONFIG_JSON__"
NEEDS_INIT = ("tools/list", "tools/call", "resources/list", "resources/read", "resources/templates/list")
MAX_METHODS = 50

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


def _short(value: Any, limit: int = 80) -> Optional[str]:
    return value[:limit] if isinstance(value, str) else None


def _ws_twin(origin: str) -> str:
    if origin.startswith("https://"):
        return "wss://" + origin[len("https://"):]
    if origin.startswith("http://"):
        return "ws://" + origin[len("http://"):]
    return origin


class ProbeServer:
    def __init__(self, env: Mapping[str, str], home: Path, stdin: BinaryIO, stdout: BinaryIO,
                 stderr: TextIO) -> None:
        self.env = dict(env)
        self.home = Path(home)
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self._write_lock = threading.Lock()
        self.config = load_config(self.env, self.home)
        self.resolver = Resolver(self.env, self.home, self.config)
        self.store = StateStore(state_dir(self.env, self.home))
        self.media = MediaChild(self.resolver, self.config, self.store, self.log)
        self.tools = ProbeTools(self.resolver, self.config, self.store, self.media,
                                self.server_info, self.log)
        self.initialized = False
        self.ready = False
        self.requested_protocol: Optional[str] = None
        self.protocol: Optional[str] = None
        self.client: Dict[str, Optional[str]] = {"name": None, "version": None}
        self.client_ui_extension = False
        self.client_ui_location: Optional[str] = None
        self.client_ui_mime_types: List[str] = []
        self.methods_seen: List[str] = []
        if self.config.get("config_error"):
            self.log("config problem: %s" % self.config["config_error"])

    # -- output

    def log(self, message: str) -> None:
        line = "autonom-chatgpt-probe: " + " ".join(str(message).split())
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
            # A reply that cannot be serialised still answers its request.
            self.log("reply not serialisable: %s" % type(exc).__name__)
            msg_id = message.get("id") if isinstance(message, dict) else None
            if not isinstance(msg_id, (str, int)) or isinstance(msg_id, bool):
                msg_id = None
            data = self.encode(self._error(msg_id, INTERNAL_ERROR, "Internal error: %s" % type(exc).__name__))
        with self._write_lock:
            self.stdout.write(data)
            self.stdout.flush()

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

    def serve(self) -> None:
        while True:
            line = self.read_line()
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
            except RecursionError:
                self.log("internal error: RecursionError")
                raw_id = message.get("id") if isinstance(message, dict) else None
                msg_id = raw_id if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool) else None
                response = self._error(msg_id, INTERNAL_ERROR, "Internal error: RecursionError")
            if response is not None:
                self.send(response)

    # -- dispatch

    def _note_method(self, method: str) -> None:
        name = method[:80]
        if name not in self.methods_seen and len(self.methods_seen) < MAX_METHODS:
            self.methods_seen.append(name)

    def handle(self, message: Any) -> Optional[Dict[str, Any]]:
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
        self._note_method(method)
        if not has_id:
            if method == "notifications/initialized":
                self.ready = True
            return None
        if not id_ok:
            return self._error(None, INVALID_REQUEST, "Invalid Request")
        try:
            result = self.dispatch(method, message.get("params"))
        except RpcError as exc:
            return self._error(msg_id, exc.code, exc.message, exc.data)
        except Shutdown:
            raise
        except Exception as exc:  # noqa: BLE001 - every handler fault becomes -32603
            self.log("internal error in %s: %s" % (method[:80], type(exc).__name__))
            for line in traceback.format_exc().splitlines():
                self.log(line)
            return self._error(msg_id, INTERNAL_ERROR, "Internal error: %s" % type(exc).__name__)
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def dispatch(self, method: str, params: Any) -> Dict[str, Any]:
        if method == "initialize":
            return self.initialize(params)
        if method == "ping":
            return {}
        if method in NEEDS_INIT and not self.initialized:
            raise RpcError(INVALID_REQUEST, "Server not initialized")
        if method == "tools/list":
            return {"tools": tool_definitions()}
        if method == "tools/call":
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                raise RpcError(INVALID_PARAMS, "Invalid params")
            name = params["name"]
            try:
                return self.tools.call(name, params.get("arguments"))
            except UnknownTool:
                raise RpcError(INVALID_PARAMS, "Unknown tool: %s" % name[:80])
        if method == "resources/list":
            return {"resources": [self.resource_entry()]}
        if method == "resources/read":
            if not isinstance(params, dict) or not isinstance(params.get("uri"), str):
                raise RpcError(INVALID_PARAMS, "Invalid params")
            return self.read_resource(params["uri"])
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        raise RpcError(METHOD_NOT_FOUND, "Method not found", {"method": method[:80]})

    # -- lifecycle

    def initialize(self, params: Any) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        requested = params.get("protocolVersion")
        self.requested_protocol = _short(requested)
        self.protocol = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
        self.client = {"name": _short(info.get("name")), "version": _short(info.get("version"))}
        capabilities = params.get("capabilities") if isinstance(params.get("capabilities"), dict) else {}
        self.client_ui_extension, self.client_ui_location, self.client_ui_mime_types = False, None, []
        for location in ("extensions", "experimental"):
            holder = capabilities.get(location)
            if isinstance(holder, dict) and UI_EXTENSION in holder:
                self.client_ui_extension, self.client_ui_location = True, location
                ext = holder[UI_EXTENSION]
                mimes = ext.get("mimeTypes") if isinstance(ext, dict) else None
                if isinstance(mimes, list):
                    self.client_ui_mime_types = [m[:80] for m in mimes if isinstance(m, str)][:10]
                break
        self.initialized = True
        return {
            "protocolVersion": self.protocol,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False, "subscribe": False},
                "extensions": {UI_EXTENSION: {"mimeTypes": [UI_MIME]}},
                "experimental": {UI_EXTENSION: {"mimeTypes": [UI_MIME]}},
            },
            "serverInfo": {"name": "autonom-chatgpt-probe", "title": "Autonom probe", "version": PROBE_VERSION},
            "instructions": ("Development probe. Use probe_read to read host status and probe_write to "
                             "change the probe note."),
        }

    def server_info(self) -> Dict[str, Any]:
        return {
            "probe_version": PROBE_VERSION,
            "python": "%d.%d.%d" % tuple(sys.version_info[:3]),
            "pid": os.getpid(),
            "protocol_version": self.protocol,
            "requested_protocol_version": self.requested_protocol,
            "client": dict(self.client),
            "client_ui_extension": self.client_ui_extension,
            "client_ui_extension_location": self.client_ui_location,
            "client_ui_mime_types": list(self.client_ui_mime_types),
            "methods_seen": list(self.methods_seen),
            "config_error": self.config.get("config_error"),
        }

    def shutdown(self) -> None:
        self.media.stop()

    # -- resources (contract 2.8)

    def connect_domains(self) -> List[str]:
        port = self.media.effective_port()
        domains = ["http://127.0.0.1:%d" % port, "ws://127.0.0.1:%d" % port,
                   "http://localhost:%d" % port, "ws://localhost:%d" % port]
        for route in self.config.get("remote_routes") or []:
            for origin in (route["origin"], _ws_twin(route["origin"])):
                if origin not in domains:
                    domains.append(origin)
        return domains

    def resource_meta(self) -> Dict[str, Any]:
        domains = self.connect_domains()
        return {
            "ui": {"csp": {"connectDomains": list(domains), "resourceDomains": [], "frameDomains": []},
                   "prefersBorder": False},
            "openai/ui": {"availableDisplayModes": ["inline", "fullscreen"]},
            "openai/widgetCSP": {"connect_domains": list(domains), "resource_domains": []},
            "openai/widgetDescription": "Autonom P0 probe: runs host, bridge, network and video checks.",
        }

    def resource_entry(self) -> Dict[str, Any]:
        return {"uri": UI_URI, "name": "autonom-probe", "title": "Autonom probe", "mimeType": UI_MIME,
                "_meta": self.resource_meta()}

    def page_config(self) -> Dict[str, Any]:
        return {
            "probe_version": PROBE_VERSION,
            "local_origin": "http://127.0.0.1:%d" % self.media.effective_port(),
            "remote_routes": [dict(route) for route in self.config.get("remote_routes") or []],
            "allowed_devices": list(self.config.get("allowed_devices") or []),
            "media_port": self.media.effective_port(),
        }

    def assemble_html(self) -> str:
        ui_dir = self.resolver.ui_dir()
        try:
            page = (ui_dir / "probe-page.html").read_text(encoding="utf-8")
            core = (ui_dir / "probe-core.mjs").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise RpcError(INTERNAL_ERROR, "Probe UI files are missing")
        if page.count(CORE_MARKER) != 1 or page.count(CONFIG_MARKER) != 1:
            raise RpcError(INTERNAL_ERROR, "Probe UI files are missing")
        if not re.search(r"<script\b[^>]*probe-config[^>]*>[^<]*" + CONFIG_MARKER, page):
            raise RpcError(INTERNAL_ERROR, "Probe UI files are missing")
        if re.search(r"</script", core, re.IGNORECASE):
            raise RpcError(INTERNAL_ERROR, "Probe UI core cannot be inlined")
        config_json = json.dumps(self.page_config()).replace("</", "<\\/")
        page = page.replace(CONFIG_MARKER, config_json)
        return page.replace(CORE_MARKER, '<script type="module">\n' + core + "\n</script>")

    def read_resource(self, uri: str) -> Dict[str, Any]:
        if uri != UI_URI:
            raise RpcError(RESOURCE_NOT_FOUND, "Resource not found", {"uri": uri[:500]})
        html = self.assemble_html()
        return {"contents": [{"uri": UI_URI, "mimeType": UI_MIME, "text": html,
                              "_meta": self.resource_meta()}]}


def main(argv: Optional[List[str]] = None) -> int:
    server = ProbeServer(os.environ, Path.home(), sys.stdin.buffer, sys.stdout.buffer, sys.stderr)

    def _stop(signum: int, _frame: Any) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise Shutdown()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        server.serve()
    except (Shutdown, KeyboardInterrupt):
        pass
    except BrokenPipeError:
        pass
    finally:
        try:
            server.shutdown()
        except Shutdown:
            server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
