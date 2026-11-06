"""Autonom MCP adapter core: protocol, envelope, schemas, CLI runner, refs, lease keeper.

Contract section 2 (cases 1-10 of 2.10). The server runs as a real subprocess
over stdio with ``AUTONOM_MCP_PARTIAL_CATALOG=1`` and the fake CLI
(``tests/fakes/fake_mcp_cli.py`` through ``AUTONOM_CLI``); runner, refs and
lease keeper cases build the same objects in process with an explicit
environment. ``HOME``, ``AUTONOM_HOME`` and ``XDG_STATE_HOME`` are temp dirs
for the server and every child; no test touches a device or a host service.
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "integrations" / "chatgpt" / "autonom-chatgpt"
SERVER_DIR = PLUGIN / "server"
SERVER = SERVER_DIR / "mcp_server.py"
LAUNCHER = PLUGIN / "bin" / "launch.sh"
FAKES = ROOT / "tests" / "fakes"
FAKE_CLI = FAKES / "fake_mcp_cli.py"
ERRORS_PY = ROOT / "scripts" / "autonom_lib" / "errors.py"

SCHEMA = "autonom.mcp/v1"
OP_ID = re.compile(r"^op_[0-9a-f]{16}$")
HOST_ID = re.compile(r"^h_[a-z2-7]{26}$")
UI_URI = "ui://autonom/workspace-dev.html"
UI_MIME = "text/html;profile=mcp-app"
CORE_TOOLS = ["list_hosts", "open_workspace", "save_diagnostics"]
TEST_TOOLS = ["test_bad_output", "test_bad_mutation", "test_slow_mutation", "test_hold_run", "test_hooks"]
# The 28 tools of contract 2.9 in table order (an oracle independent of mcp_catalog.TOOL_ORDER).
TABLE_TOOLS = [
    "list_hosts", "open_workspace", "save_diagnostics",
    "list_devices", "list_sessions", "get_session", "start_session", "stop_session",
    "get_screen", "get_ui_tree", "tap_element", "type_text", "press_key",
    "acquire_control", "release_control", "get_operation",
    "list_flows", "start_flow_run", "get_run", "cancel_run", "list_artifacts", "get_artifact",
    "open_stream", "get_frames", "close_stream", "send_input", "pause_session", "resume_session",
]
IOS_UDID = "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5"
OTHER_HOST = "h_" + "a" * 26
MISSING = object()

MINIMAL_PAGE = (
    "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
    "<script type=\"application/json\" id=\"workspace-config\">__WORKSPACE_CONFIG_JSON__</script>\n"
    "</head><body><main id=\"workspace\"></main>\n<!-- WORKSPACE_CORE -->\n</body></html>\n")
MINIMAL_CORE = "export const CORE_MARK = \"workspace-core-test\";\n"


def import_server_modules() -> Dict[str, Any]:
    """Import the plugin modules in process without writing bytecode into the plugin."""
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    if str(SERVER_DIR) not in sys.path:
        sys.path.insert(0, str(SERVER_DIR))
    try:
        import mcp_catalog
        import mcp_cli
        import mcp_common
        import mcp_control
        import mcp_core_tools
        import mcp_env
        import mcp_refs
        import mcp_server
    finally:
        sys.dont_write_bytecode = previous
    return {"common": mcp_common, "env": mcp_env, "cli": mcp_cli, "refs": mcp_refs,
            "control": mcp_control, "catalog": mcp_catalog, "core": mcp_core_tools, "server": mcp_server}


def import_fake() -> Any:
    spec = importlib.util.spec_from_file_location("fake_mcp_cli_helpers", FAKE_CLI)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    finally:
        sys.dont_write_bytecode = previous
    return module


FAKE = import_fake()


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, check=False).stdout.strip()
    except OSError:
        return True
    return bool(out) and not out.startswith("Z")


def catalog_order_problems(names: List[str]) -> List[str]:
    """Problems with a partial-mode ``tools/list`` order (contract 2.10 item 2a, DEC-024).

    The core tools come first in table order and the test-only tools last; every other name is
    one of the 28 table tools, listed once and in table order. Later tasks fill their modules, so
    the list may hold any subset of tools 4-28.
    """
    problems = []
    if names[:len(CORE_TOOLS)] != CORE_TOOLS:
        problems.append("does not start with the core tools: %r" % names[:len(CORE_TOOLS)])
    if len(names) < len(CORE_TOOLS) + len(TEST_TOOLS) or names[-len(TEST_TOOLS):] != TEST_TOOLS:
        problems.append("does not end with the test-only tools: %r" % names[-len(TEST_TOOLS):])
    middle = names[len(CORE_TOOLS):len(names) - len(TEST_TOOLS)]
    last = len(CORE_TOOLS) - 1
    for name in middle:
        if name not in TABLE_TOOLS[len(CORE_TOOLS):]:
            problems.append("not a table tool after the core tools: %r" % name)
            continue
        index = TABLE_TOOLS.index(name)
        if index <= last:
            problems.append("out of table order or listed twice: %r" % name)
        last = max(last, index)
    return problems


def wait_dead(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)


class ServerProcess:
    """One server subprocess speaking newline-delimited JSON-RPC over stdio."""

    def __init__(self, argv: List[str], env: Dict[str, str]) -> None:
        self.stderr_file = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.stderr_file, env=env)
        self.raw_lines: List[bytes] = []
        self.messages: "queue.Queue[Any]" = queue.Queue()
        self.next_id = 1
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in iter(self.proc.stdout.readline, b""):
            self.raw_lines.append(line)
            try:
                self.messages.put(json.loads(line.decode("utf-8")))
            except ValueError:
                self.messages.put({"__not_json__": line})
        self.messages.put(None)

    def send_raw(self, data: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def send(self, message: Any) -> None:
        self.send_raw(json.dumps(message).encode("utf-8") + b"\n")

    def recv(self, timeout: float = 15.0) -> Any:
        return self.messages.get(timeout=timeout)

    def new_id(self) -> int:
        msg_id = self.next_id
        self.next_id += 1
        return msg_id

    def wait_for(self, msg_id: Any, timeout: float = 20.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            reply = self.messages.get(timeout=max(0.1, deadline - time.monotonic()))
            if reply is None:
                raise AssertionError("server closed stdout while waiting for %r" % (msg_id,))
            if isinstance(reply, dict) and reply.get("id") == msg_id and "method" not in reply:
                return reply

    def request(self, method: str, params: Any = MISSING, timeout: float = 20.0) -> Dict[str, Any]:
        msg_id = self.new_id()
        message: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not MISSING:
            message["params"] = params
        self.send(message)
        return self.wait_for(msg_id, timeout)

    def initialize(self, version: Any = "2025-11-25") -> Dict[str, Any]:
        params: Dict[str, Any] = {"capabilities": {}, "clientInfo": {"name": "core-test", "version": "1"}}
        if version is not MISSING:
            params["protocolVersion"] = version
        reply = self.request("initialize", params)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return reply

    def call(self, name: str, arguments: Any = MISSING, timeout: float = 30.0) -> Dict[str, Any]:
        params: Dict[str, Any] = {"name": name}
        if arguments is not MISSING:
            params["arguments"] = arguments
        reply = self.request("tools/call", params, timeout=timeout)
        if "error" in reply:
            raise AssertionError("tools/call %s answered a JSON-RPC error: %r" % (name, reply["error"]))
        return reply["result"]

    def stderr_text(self) -> str:
        self.stderr_file.seek(0)
        return self.stderr_file.read().decode("utf-8", "replace")

    def close(self, timeout: float = 15.0) -> Optional[int]:
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
        self.close(timeout=15.0)
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        self.reader.join(timeout=2)
        self.stderr_file.close()


class CoreCase(unittest.TestCase):
    """Temp HOME, state base, fake CLI state and helpers shared by the cases."""

    maxDiff = None

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="mcp-core-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.tmp = tmp
        self.home = tmp / "home"
        self.home.mkdir()
        self.state_base = tmp / "state"
        self.xdg = tmp / "xdg"
        self.fake_state = tmp / "fake"
        self.fake_state.mkdir()
        self.ui_dir = tmp / "ui"
        self.ui_dir.mkdir()
        (self.ui_dir / "workspace-dev.html").write_text(MINIMAL_PAGE, encoding="utf-8")
        (self.ui_dir / "workspace-dev-core.mjs").write_text(MINIMAL_CORE, encoding="utf-8")
        self.cli_log = tmp / "cli.log"
        self.addCleanup(self._kill_leftovers)

    @property
    def mcp_dir(self) -> Path:
        return self.state_base / "chatgpt-mcp"

    def write_config(self, config: Any, raw: Optional[str] = None) -> None:
        self.mcp_dir.mkdir(parents=True, exist_ok=True)
        (self.mcp_dir / "config.json").write_text(raw if raw is not None else json.dumps(config),
                                                  encoding="utf-8")

    def server_env(self, **overrides: Optional[str]) -> Dict[str, str]:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AUTONOM_", "FAKE_MCP_", "PYTHON")) and key != "XDG_STATE_HOME"}
        real_home = os.path.expanduser("~")
        env["PATH"] = os.pathsep.join(
            part for part in os.environ.get("PATH", "").split(os.pathsep)
            if part and not part.startswith(os.path.join(real_home, ".local")))
        env.update({
            "HOME": str(self.home),
            "AUTONOM_HOME": str(self.state_base),
            "XDG_STATE_HOME": str(self.xdg),
            "AUTONOM_CLI": str(FAKE_CLI),
            "AUTONOM_MCP_CLI_PYTHON": sys.executable,
            "AUTONOM_MCP_UI_DIR": str(self.ui_dir),
            "AUTONOM_MCP_PARTIAL_CATALOG": "1",
            "AUTONOM_MCP_TEST_BAD_OUTPUT": "1",
            "FAKE_MCP_STATE": str(self.fake_state),
            "FAKE_MCP_CLI_LOG": str(self.cli_log),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def start(self, argv: Optional[List[str]] = None, **overrides: Optional[str]) -> ServerProcess:
        server = ServerProcess(argv or [sys.executable, str(SERVER)], self.server_env(**overrides))
        self.addCleanup(self._finish, server)
        return server

    def started(self, **overrides: Optional[str]) -> ServerProcess:
        server = self.start(**overrides)
        reply = server.initialize()
        self.assertIn("result", reply)
        return server

    def _finish(self, server: ServerProcess) -> None:
        server.kill()
        for line in server.raw_lines:
            message = json.loads(line.decode("utf-8"))
            self.assertEqual(message.get("jsonrpc"), "2.0", line)

    def cli_calls(self, key: Optional[str] = None) -> List[Dict[str, Any]]:
        calls = FAKE.read_log(self.cli_log)
        return [call for call in calls if key is None or call["key"] == key]

    def _kill_leftovers(self) -> None:
        for call in FAKE.read_log(self.cli_log):
            for pid in (call.get("pid"), call.get("child_pid")):
                if pid and pid_alive(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass

    # -- assertions

    def check_envelope(self, result: Dict[str, Any], tool: str) -> Dict[str, Any]:
        structured = result["structuredContent"]
        self.assertEqual(structured["schema_version"], SCHEMA)
        self.assertRegex(structured["operation_id"], OP_ID)
        self.assertEqual(structured["tool"], tool)
        self.assertIsInstance(structured["ok"], bool)
        self.assertIn(structured["outcome"], ("completed", "rejected", "failed", "outcome_unknown"))
        self.assertIn("host_id", structured)
        self.assertIsInstance(structured["warnings"], list)
        self.assertLessEqual(len(structured["warnings"]), 20)
        self.assertIs(result["isError"], not structured["ok"])
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(result["content"][0]["text"], json.dumps(structured, ensure_ascii=False, sort_keys=True))
        if structured["ok"]:
            self.assertEqual(structured["outcome"], "completed")
        else:
            self.assertNotEqual(structured["outcome"], "completed")
            for key in ("error_code", "error"):
                self.assertIsInstance(structured[key], str, key)
                self.assertTrue(structured[key], key)
            self.assertIn("hint", structured)
            self.assertIsInstance(structured["retry_safe"], bool)
        return structured

    def schemas(self, server: ServerProcess) -> Dict[str, Dict[str, Any]]:
        tools = server.request("tools/list")["result"]["tools"]
        return {tool["name"]: tool for tool in tools}

    def call_checked(self, server: ServerProcess, name: str, arguments: Any = MISSING) -> Dict[str, Any]:
        result = server.call(name, arguments)
        structured = self.check_envelope(result, name)
        common = import_server_modules()["common"]
        if not hasattr(self, "_tool_defs"):
            self._tool_defs = self.schemas(server)
        problems = common.validate(self._tool_defs[name]["outputSchema"], structured)
        self.assertEqual(problems, [], structured)
        return structured

    def call_ok(self, server: ServerProcess, name: str, arguments: Any = MISSING) -> Dict[str, Any]:
        structured = self.call_checked(server, name, arguments)
        self.assertTrue(structured["ok"], structured)
        return structured

    def call_error(self, server: ServerProcess, name: str, arguments: Any, code: str,
                   outcome: str = "rejected") -> Dict[str, Any]:
        structured = self.call_checked(server, name, arguments)
        self.assertFalse(structured["ok"], structured)
        self.assertEqual(structured["error_code"], code, structured)
        self.assertEqual(structured["outcome"], outcome, structured)
        return structured

    def host_id(self, server: ServerProcess) -> str:
        hosts = self.call_ok(server, "list_hosts")["hosts"]
        return hosts[0]["host_id"]

    def assert_mode(self, path: Path, mode: int) -> None:
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode, str(path))

    def in_process(self, config: Optional[Dict[str, Any]] = None, **env_overrides: Optional[str]):
        """(modules, env, runner, refs, logs) built with an explicit environment."""
        modules = import_server_modules()
        env = self.server_env(**env_overrides)
        if config is not None:
            self.write_config(config)
        cfg = modules["env"].load_config(env, self.home)
        state_dir = modules["env"].mcp_state_dir(env, self.home)
        resolver = modules["env"].Resolver(env, self.home, cfg)
        logs: List[str] = []
        runner = modules["cli"].Runner(env, self.home, cfg, resolver, state_dir, logs.append)
        refs = modules["refs"].Refs(runner, cfg, env, self.home, state_dir, logs.append)
        return modules, env, runner, refs, logs


# ====================================================================== 1. protocol

class ProtocolTests(CoreCase):
    def test_protocol_version_negotiation(self) -> None:
        for requested, expected in (("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"),
                                    ("2025-03-26", "2025-03-26"), ("2024-11-05", "2024-11-05"),
                                    ("1999-01-01", "2025-11-25"), (MISSING, "2025-11-25"), (7, "2025-11-25")):
            server = self.start()
            result = server.initialize(requested)["result"]
            self.assertEqual(result["protocolVersion"], expected, requested)
            self.assertEqual(result["serverInfo"], {"name": "autonom-chatgpt", "title": "Autonom",
                                                    "version": "0.1.0"})
            self.assertIn("host_id", result["instructions"])
            self.assertIn("session_id", result["instructions"])
            self.assertIn("outcome_unknown", result["instructions"])
            self.assertIn("get_operation", result["instructions"])
            self.assertEqual(result["capabilities"]["tools"], {"listChanged": False})
            self.assertEqual(server.close(), 0)

    def test_fault_table(self) -> None:
        server = self.start()
        # Before initialize: ping works, the five gated methods are refused.
        self.assertEqual(server.request("ping")["result"], {})
        for method in ("tools/list", "tools/call", "resources/list", "resources/read",
                       "resources/templates/list"):
            reply = server.request(method, {})
            self.assertEqual(reply["error"]["code"], -32600, method)
            self.assertEqual(reply["error"]["message"], "Server not initialized")
        server.initialize()
        # Parse errors: bad JSON, bad UTF-8, an oversized line.
        server.send_raw(b"{not json\n")
        self.assertEqual(server.recv(), {"jsonrpc": "2.0", "id": None,
                                         "error": {"code": -32700, "message": "Parse error"}})
        server.send_raw(b"\xff\xfe\n")
        self.assertEqual(server.recv()["error"]["code"], -32700)
        server.send_raw(b"[" + b" " * (4 * 1024 * 1024 + 10) + b"]\n")
        reply = server.recv()
        self.assertEqual((reply["id"], reply["error"]["code"]), (None, -32700))
        # A batch array and a non-object are invalid requests.
        server.send([{"jsonrpc": "2.0", "id": 900, "method": "ping"}])
        reply = server.recv()
        self.assertEqual((reply["id"], reply["error"]["code"]), (None, -32600))
        server.send(5)
        self.assertEqual(server.recv()["error"]["code"], -32600)
        server.send({"id": 901, "method": "ping"})
        self.assertEqual(server.recv(), {"jsonrpc": "2.0", "id": 901,
                                         "error": {"code": -32600, "message": "Invalid Request"}})
        server.send({"jsonrpc": "2.0", "id": True, "method": "ping"})
        self.assertEqual(server.recv()["id"], None)
        # A notification and a response from the client are never answered.
        server.send({"jsonrpc": "2.0", "method": "notifications/something"})
        server.send({"jsonrpc": "2.0", "id": 77, "result": {"x": 1}})
        server.send({"jsonrpc": "2.0", "id": 78, "error": {"code": 1, "message": "x"}})
        reply = server.request("ping")
        self.assertEqual(reply["result"], {})
        # Unknown method, unknown tool, invalid params, unknown resource.
        reply = server.request("does/not/exist")
        self.assertEqual(reply["error"]["code"], -32601)
        self.assertEqual(server.request("tools/call", {"name": "no_such_tool"})["error"]["code"], -32602)
        self.assertEqual(server.request("tools/call", {"arguments": {}})["error"]["code"], -32602)
        self.assertEqual(server.request("tools/call", [])["error"]["code"], -32602)
        self.assertEqual(server.request("resources/read", {})["error"]["code"], -32602)
        self.assertEqual(server.request("resources/read", {"uri": "ui://nope"})["error"]["code"], -32002)
        self.assertEqual(server.request("resources/templates/list")["result"], {"resourceTemplates": []})
        # Every line on stdout is one compact JSON-RPC message.
        self.assertEqual(server.close(), 0)
        for line in server.raw_lines:
            self.assertTrue(line.endswith(b"\n"))
            self.assertNotIn(b"\n", line[:-1])

    def test_handler_exception_is_internal_error_with_class_name_only(self) -> None:
        modules = import_server_modules()
        env = self.server_env()
        requests = b"".join(json.dumps(item).encode() + b"\n" for item in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ))
        stdout = io.BytesIO()
        stderr = io.StringIO()
        server = modules["server"].McpServer(env, self.home, io.BytesIO(requests), stdout, stderr)

        def broken(method: str, params: Any, entry: Dict[str, Any]) -> Dict[str, Any]:
            raise LookupError("secret detail that must not leak")

        server.dispatch = broken  # type: ignore[assignment]
        server.serve()
        self.assertTrue(server.shutdown())
        replies = {item["id"]: item for item in
                   (json.loads(line) for line in stdout.getvalue().splitlines())}
        self.assertEqual(replies[2]["error"], {"code": -32603, "message": "Internal error: LookupError"})
        self.assertEqual(replies[3]["result"], {})
        self.assertIn("Traceback", stderr.getvalue())
        self.assertNotIn("secret detail", json.dumps(replies))

    def test_core_build_refuses_to_start_without_the_partial_catalog_flag(self) -> None:
        catalog = import_server_modules()["catalog"]
        try:
            catalog.build_catalog({})
        except catalog.CatalogError:
            complete = False
        else:
            complete = True
        server = self.start(AUTONOM_MCP_PARTIAL_CATALOG=None)
        if complete:
            # Every later module is filled in (contract 2.9): the full catalog starts without the flag.
            self.assertIn("result", server.initialize())
            names = [tool["name"] for tool in server.request("tools/list")["result"]["tools"]]
            self.assertEqual(names, TABLE_TOOLS + TEST_TOOLS)
            return
        self.assertEqual(server.proc.wait(timeout=20), 1)
        self.assertIn("catalog", server.stderr_text())


# ====================================================================== 2. concurrency

class ConcurrencyTests(CoreCase):
    def test_slow_call_does_not_block_ping(self) -> None:
        server = self.started(FAKE_MCP_CLI_MODE="slow:host_info:1500")
        server.send({"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "list_hosts"}})
        server.send({"jsonrpc": "2.0", "id": 11, "method": "ping"})
        first = server.recv(timeout=20)
        second = server.recv(timeout=20)
        self.assertEqual(first, {"jsonrpc": "2.0", "id": 11, "result": {}})
        self.assertEqual(second["id"], 10)
        self.assertTrue(second["result"]["structuredContent"]["ok"])

    def test_duplicate_in_flight_id_is_refused_and_not_run(self) -> None:
        server = self.started(FAKE_MCP_CLI_MODE="slow:host_info:1500")
        call = {"jsonrpc": "2.0", "id": "dup", "method": "tools/call", "params": {"name": "list_hosts"}}
        server.send(call)
        time.sleep(0.3)
        server.send(call)
        reply = server.recv(timeout=10)
        self.assertEqual(reply, {"jsonrpc": "2.0", "id": "dup",
                                 "error": {"code": -32600, "message": "Duplicate request id"}})
        result = server.recv(timeout=20)
        self.assertEqual(result["id"], "dup")
        self.assertIn("result", result)
        self.assertEqual(len(self.cli_calls("host_info")), 1)
        # Once answered, the id may be used again.
        server.send(call)
        self.assertIn("result", server.wait_for("dup"))

    def test_cancelled_request_gets_no_response(self) -> None:
        server = self.started(FAKE_MCP_CLI_MODE="slow:host_info:1500")
        server.send({"jsonrpc": "2.0", "id": 30, "method": "tools/call", "params": {"name": "list_hosts"}})
        time.sleep(0.2)
        server.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                     "params": {"requestId": 30, "reason": "test"}})
        started = time.monotonic()
        self.assertEqual(server.request("ping")["result"], {})
        self.assertLess(time.monotonic() - started, 1.0)
        time.sleep(2.5)
        seen = []
        while True:
            try:
                seen.append(server.messages.get_nowait())
            except queue.Empty:
                break
        self.assertFalse([item for item in seen if isinstance(item, dict) and item.get("id") == 30], seen)
        # The call itself still ran to the end (a cancellation interrupts nothing).
        self.assertEqual(len(self.cli_calls("host_info")), 1)
        self.assertIsNotNone(self.cli_calls("host_info")[0]["end"])
        self.assertTrue(self.call_ok(server, "list_hosts")["hosts"])

    def test_eof_during_a_slow_mutation_waits_for_it_and_exits_zero(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started()
        host = self.host_id(server)
        server.send({"jsonrpc": "2.0", "id": 50, "method": "tools/call",
                     "params": {"name": "test_slow_mutation",
                                "arguments": {"host_id": host, "session_id": session_id, "ms": 1500}}})
        time.sleep(0.4)
        started = time.monotonic()
        self.assertEqual(server.close(timeout=15), 0)
        self.assertLess(time.monotonic() - started, 15)
        replies = [json.loads(line) for line in server.raw_lines]
        answered = [item for item in replies if item.get("id") == 50]
        self.assertEqual(len(answered), 1)
        self.assertTrue(answered[0]["result"]["structuredContent"]["ok"])

    def test_sigterm_exits_zero(self) -> None:
        server = self.started()
        self.call_ok(server, "list_hosts")
        server.proc.send_signal(signal.SIGTERM)
        self.assertEqual(server.proc.wait(timeout=15), 0)

    def test_a_stop_signal_handed_to_another_thread_still_ends_the_read(self) -> None:
        # The kernel may deliver SIGTERM to any thread, but Python runs the
        # handler only on the main thread: the main thread must not sit in a
        # blocking read that the signal never interrupts.
        modules = import_server_modules()
        read_end, write_end = os.pipe()
        stdin = os.fdopen(read_end, "rb")
        self.addCleanup(stdin.close)
        server = modules["server"].McpServer(self.server_env(), self.home, stdin, io.BytesIO(),
                                             io.StringIO())
        self.addCleanup(server.pool.shutdown, False)
        shutdown_cls = modules["server"].Shutdown
        fired = threading.Event()

        def stop(_signum: int, _frame: Any) -> None:
            if not fired.is_set():
                fired.set()
                raise shutdown_cls()

        previous = signal.signal(signal.SIGUSR1, stop)
        self.addCleanup(signal.signal, signal.SIGUSR1, previous)
        closed = threading.Lock()

        def close_writer() -> None:
            if closed.acquire(blocking=False):
                os.close(write_end)

        # Safety net: EOF after 10 s, so a regression fails instead of hanging.
        safety = threading.Timer(10.0, close_writer)
        safety.start()
        self.addCleanup(safety.cancel)
        sender = threading.Thread(target=lambda: (time.sleep(0.5),
                                                  signal.pthread_kill(threading.get_ident(), signal.SIGUSR1)))
        started = time.monotonic()
        sender.start()
        with self.assertRaises(shutdown_cls):
            server.serve()
        self.assertLess(time.monotonic() - started, 5.0)
        sender.join(5)
        safety.cancel()
        close_writer()

    def test_a_stop_signal_during_an_eof_shutdown_does_not_cut_it_short(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started()
        host = self.host_id(server)
        server.send({"jsonrpc": "2.0", "id": 52, "method": "tools/call",
                     "params": {"name": "test_slow_mutation",
                                "arguments": {"host_id": host, "session_id": session_id, "ms": 3000}}})
        time.sleep(0.5)
        assert server.proc.stdin is not None
        server.proc.stdin.close()  # EOF starts the shutdown
        time.sleep(0.5)
        server.proc.send_signal(signal.SIGTERM)  # the host asks again before the server is done
        self.assertEqual(server.proc.wait(timeout=20), 0)
        server.reader.join(timeout=5)
        answered = [item for item in (json.loads(line) for line in server.raw_lines) if item.get("id") == 52]
        self.assertEqual(len(answered), 1, server.stderr_text())
        self.assertTrue(answered[0]["result"]["structuredContent"]["ok"])

    def test_a_broken_stdout_shuts_down_with_exit_zero(self) -> None:
        read_end, write_end = os.pipe()
        os.close(read_end)  # nobody will ever read the answers
        stderr_file = tempfile.TemporaryFile()
        self.addCleanup(stderr_file.close)
        try:
            proc = subprocess.Popen([sys.executable, str(SERVER)], stdin=subprocess.PIPE, stdout=write_end,
                                    stderr=stderr_file, env=self.server_env())
        finally:
            os.close(write_end)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        assert proc.stdin is not None
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"capabilities": {}}},
                 {"jsonrpc": "2.0", "id": 2, "method": "ping"}]
        try:
            for message in lines:
                proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
            proc.stdin.flush()
        except BrokenPipeError:
            pass
        code = proc.wait(timeout=20)
        stderr_file.seek(0)
        log = stderr_file.read().decode("utf-8", "replace")
        self.assertEqual(code, 0, log)
        self.assertIn("stdout closed; shutting down", log)
        self.assertNotIn("Exception ignored", log)
        try:
            proc.stdin.close()
        except OSError:
            pass

    def test_a_request_cancelled_while_queued_never_runs(self) -> None:
        modules = import_server_modules()
        stdout = io.BytesIO()
        server = modules["server"].McpServer(self.server_env(), self.home, io.BytesIO(b""), stdout,
                                             io.StringIO())
        server.initialized = True
        from concurrent.futures import ThreadPoolExecutor
        server.pool.shutdown(wait=True)
        server.pool = ThreadPoolExecutor(max_workers=1)  # one busy worker stands for sixteen
        release = threading.Event()
        server.pool.submit(release.wait, 10)
        ran: List[str] = []

        def recording(method: str, params: Any, entry: Dict[str, Any]) -> Dict[str, Any]:
            ran.append(method)
            return {}

        server.dispatch = recording  # type: ignore[assignment]
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "id": 40, "method": "tools/call",
                                         "params": {"name": "list_hosts"}}))
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                         "params": {"requestId": 40}}))
        release.set()
        server.pool.shutdown(wait=True)
        self.assertEqual(ran, [])
        self.assertEqual(stdout.getvalue(), b"")
        self.assertEqual(server.inflight_count(), 0)
        self.assertEqual(self.cli_calls(), [])

    def test_an_id_stays_reserved_until_its_response_is_written(self) -> None:
        # The handler is done but its answer waits on a blocked stdout (a slow
        # client, or another worker holding the write lock): a duplicate id must
        # still be refused and must not run the call a second time.
        modules = import_server_modules()

        class BlockedWriter(io.BytesIO):
            def __init__(self) -> None:
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()

            def write(self, data: Any) -> int:
                self.entered.set()
                if not self.release.wait(10):
                    raise AssertionError("the writer was never released")
                return super().write(data)

        stdout = BlockedWriter()
        server = modules["server"].McpServer(self.server_env(), self.home, io.BytesIO(b""), stdout,
                                             io.StringIO())
        server.initialized = True
        ran: List[Any] = []

        def recording(method: str, params: Any, entry: Dict[str, Any]) -> Dict[str, Any]:
            ran.append(entry["id"])
            return {}

        server.dispatch = recording  # type: ignore[assignment]
        call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "list_hosts"}}
        try:
            self.assertIsNone(server.handle(call))
            self.assertTrue(stdout.entered.wait(10), "the first response never reached stdout")
            self.assertEqual(ran, [7])
            self.assertEqual(server.inflight_count(), 1)
            self.assertEqual(server.handle(call), {"jsonrpc": "2.0", "id": 7,
                                                   "error": {"code": -32600, "message": "Duplicate request id"}})
        finally:
            stdout.release.set()
        deadline = time.monotonic() + 10
        while server.inflight_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(server.inflight_count(), 0)
        self.assertEqual(ran, [7])
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(replies, [{"jsonrpc": "2.0", "id": 7, "result": {}}])
        # Once the answer is written, the id may be used again.
        self.assertIsNone(server.handle(call))
        server.pool.shutdown(wait=True)
        self.assertEqual(ran, [7, 7])
        self.assertEqual(server.inflight_count(), 0)
        self.assertEqual(self.cli_calls(), [])


# ====================================================================== 2a. catalog order oracle

class CatalogOrderTests(unittest.TestCase):
    def test_the_order_oracle_accepts_any_later_subset_in_table_order(self) -> None:
        for names in (CORE_TOOLS + TEST_TOOLS,
                      CORE_TOOLS + ["list_devices", "get_operation"] + TEST_TOOLS,
                      CORE_TOOLS + ["list_flows", "get_artifact", "open_stream"] + TEST_TOOLS,
                      TABLE_TOOLS + TEST_TOOLS):
            self.assertEqual(catalog_order_problems(names), [], names)

    def test_the_order_oracle_refuses_bad_lists(self) -> None:
        bad = [
            ["open_workspace", "list_hosts", "save_diagnostics"] + TEST_TOOLS,
            CORE_TOOLS[:2] + TEST_TOOLS,
            CORE_TOOLS + ["list_devices"],
            CORE_TOOLS + TEST_TOOLS[:1] + ["list_devices"] + TEST_TOOLS[1:],
            CORE_TOOLS + ["get_operation", "list_devices"] + TEST_TOOLS,
            CORE_TOOLS + ["list_devices", "list_devices"] + TEST_TOOLS,
            CORE_TOOLS + ["list_hosts"] + TEST_TOOLS,
            CORE_TOOLS + ["not_a_tool"] + TEST_TOOLS,
            [],
        ]
        for names in bad:
            self.assertNotEqual(catalog_order_problems(names), [], names)

    def test_later_module_tools_keep_the_partial_catalog_in_order(self) -> None:
        mods = import_server_modules()
        catalog, core = mods["catalog"], mods["core"]
        template = core.TOOLS[0]
        fills = {"mcp_sessions": ("list_devices", "get_operation"), "mcp_runs": (),
                 "mcp_media": ("open_stream",)}
        saved = {name: catalog.OWNERS[name].TOOLS for name in fills}
        try:
            for module_name, tool_names in fills.items():
                catalog.OWNERS[module_name].TOOLS = tuple(template._replace(name=name) for name in tool_names)
            env = {"AUTONOM_MCP_PARTIAL_CATALOG": "1", "AUTONOM_MCP_TEST_BAD_OUTPUT": "1"}
            names = [tool.name for tool in catalog.build_catalog(env)]
        finally:
            for module_name, tools in saved.items():
                catalog.OWNERS[module_name].TOOLS = tools
        self.assertEqual(names, CORE_TOOLS + ["list_devices", "get_operation", "open_stream"] + TEST_TOOLS)
        self.assertEqual(catalog_order_problems(names), [])


# ====================================================================== 3. envelope and 4. self-validation

class EnvelopeTests(CoreCase):
    def test_tools_list_order_schemas_and_meta(self) -> None:
        server = self.started()
        tools = server.request("tools/list")["result"]["tools"]
        names = [tool["name"] for tool in tools]
        self.assertEqual(catalog_order_problems(names), [], names)
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object", tool["name"])
            self.assertIs(tool["inputSchema"]["additionalProperties"], False, tool["name"])
            out = tool["outputSchema"]
            self.assertIs(out["additionalProperties"], False, tool["name"])
            self.assertEqual(out["required"], ["schema_version", "operation_id", "tool", "ok", "outcome",
                                               "host_id", "warnings"])
            self.assertEqual(out["properties"]["tool"], {"const": tool["name"]})
            self.assertIs(tool["_meta"]["openai/widgetAccessible"], True)
            self.assertIs(tool["annotations"]["openWorldHint"], False)
        by_name = {tool["name"]: tool for tool in tools}
        self.assertIs(by_name["list_hosts"]["annotations"]["readOnlyHint"], True)
        self.assertIs(by_name["open_workspace"]["annotations"]["readOnlyHint"], True)
        self.assertEqual(by_name["open_workspace"]["_meta"], {
            "ui": {"resourceUri": UI_URI}, "openai/outputTemplate": UI_URI,
            "openai/ui": {"entrypoints": [{"type": "global"}, {"type": "thread"}]},
            "openai/widgetAccessible": True})
        self.assertEqual(by_name["save_diagnostics"]["_meta"], {
            "ui": {"visibility": ["app"]}, "openai/visibility": "private", "openai/widgetAccessible": True})
        self.assertNotIn("ui", by_name["list_hosts"]["_meta"])
        self.assertNotIn("session", by_name["list_hosts"]["outputSchema"]["properties"])
        self.assertIn("session", by_name["test_slow_mutation"]["outputSchema"]["properties"])

    def test_every_core_result_has_the_envelope_and_validates(self) -> None:
        server = self.started()
        for name, arguments in (("list_hosts", MISSING), ("list_hosts", {}), ("open_workspace", MISSING),
                                ("open_workspace", {}),
                                ("save_diagnostics", {"results": {"schema_version": 1,
                                                                  "run_id": "diag-20261020-101010-abcd"}})):
            structured = self.call_ok(server, name, arguments)
            self.assertRegex(structured["host_id"] or "", HOST_ID)
            self.assertEqual(structured["warnings"], [])
        ids = [self.call_ok(server, "list_hosts")["operation_id"] for _ in range(3)]
        self.assertEqual(len(set(ids)), 3)

    def test_invalid_arguments_name_the_field_and_run_nothing(self) -> None:
        server = self.started()
        cases = [
            ("list_hosts", {"x": 1}, "x", "unknown property"),
            ("list_hosts", {"host_id": "h_bad"}, "host_id", None),
            ("list_hosts", [], "arguments", "must be an object"),
            ("open_workspace", {"extra": True}, "extra", "unknown property"),
            ("save_diagnostics", {}, "results", "required"),
            ("save_diagnostics", {"results": []}, "results", "must be object"),
            ("test_slow_mutation", {"host_id": OTHER_HOST, "session_id": "s_abc", "ms": 999999}, "ms", None),
        ]
        for name, arguments, field, problem in cases:
            structured = self.call_error(server, name, arguments, "invalid_arguments")
            self.assertEqual(structured["field"], field, (name, arguments))
            if problem is not None:
                self.assertEqual(structured["problem"], problem)
            self.assertIs(structured["retry_safe"], True)
        self.assertEqual(self.cli_calls(), [])

    def test_schema_violation_replaces_an_invalid_result(self) -> None:
        server = self.started()
        read = self.call_error(server, "test_bad_output", {}, "schema_violation", outcome="failed")
        self.assertIs(read["retry_safe"], True)
        self.assertNotIn("undeclared_field", read)
        mutation = self.call_error(server, "test_bad_mutation", {}, "schema_violation",
                                   outcome="outcome_unknown")
        self.assertIs(mutation["retry_safe"], False)
        log = server.stderr_text()
        self.assertIn("test_bad_output output violates its schema at undeclared_field", log)

    def test_validator_subset(self) -> None:
        common = import_server_modules()["common"]
        schema = common.obj({
            "a": common.string(r"^x+$", 1, 3),
            "b": common.nullable(common.integer(0, 5)),
            "c": {"type": "array", "minItems": 1, "maxItems": 2, "items": {"enum": ["p", "q"]}},
            "d": {"oneOf": [common.string(enum=("agent", "human")), {"type": "null"}]},
            "e": {"const": True},
            "f": {"type": "number", "minimum": 0.5},
        }, ("a",))
        self.assertEqual(common.validate(schema, {"a": "xx", "b": None, "c": ["p"], "d": None, "e": True,
                                                  "f": 1}), [])
        self.assertEqual(common.validate(schema, {}), ["a: required"])
        self.assertEqual(common.validate(schema, {"a": "xxxx"}), ["a: must be at most 3 characters"])
        self.assertEqual(common.validate(schema, {"a": "y"})[0], "a: does not match ^x+$")
        self.assertEqual(common.validate(schema, {"a": "x", "b": True}), ["b: must be integer or null"])
        self.assertEqual(common.validate(schema, {"a": "x", "b": 6}), ["b: must be <= 5"])
        self.assertEqual(common.validate(schema, {"a": "x", "c": []}), ["c: must have at least 1 items"])
        self.assertEqual(common.validate(schema, {"a": "x", "c": ["z"]})[0][:4], "c[0]")
        self.assertEqual(common.validate(schema, {"a": "x", "d": "robot"}),
                         ["d: matches 0 of the allowed shapes"])
        self.assertEqual(common.validate(schema, {"a": "x", "e": 1}), ["e: must be true"])
        self.assertEqual(common.validate(schema, {"a": "x", "f": False}), ["f: must be number"])
        self.assertEqual(common.validate(schema, {"a": "x", "zz": 1}), ["zz: unknown property"])
        self.assertEqual(common.validate(schema, 3), ["$: must be object"])

    def test_patterns_anchor_at_the_true_end_like_json_schema(self) -> None:
        common = import_server_modules()["common"]
        for pattern, value in ((common.HOST_ID_RE, OTHER_HOST), (common.SESSION_ID_RE, "s_abc"),
                               (common.TARGET_ID_RE, "emulator-5584"), (r"^[A-Z][A-Z0-9_]{0,47}$", "HOME")):
            schema = common.string(pattern)
            self.assertEqual(common.validate(schema, value), [], pattern)
            for bad in (value + "\n", value + "\r\n", value + "\nX"):
                self.assertTrue(common.validate(schema, bad), (pattern, bad))
        # Unanchored and escaped dollars keep their meaning; \\d stays ASCII as in ECMA-262.
        self.assertEqual(common.validate(common.string(r"a\$b"), "xa$by"), [])
        self.assertEqual(common.validate(common.string(r"^[$]+$"), "$$"), [])
        self.assertTrue(common.validate(common.string(r"^[0-9]$"), "1\n"))
        self.assertTrue(common.validate(common.string(r"^\\d$"), "\u0661"))
        server = self.started()
        structured = self.call_error(server, "list_hosts", {"host_id": OTHER_HOST + "\n"}, "invalid_arguments")
        self.assertEqual(structured["field"], "host_id")
        self.assertEqual(self.cli_calls(), [])


# ====================================================================== 5. runner

class RunnerTests(CoreCase):
    def test_timeout_kills_the_process_group_and_reports_cli_timeout(self) -> None:
        _m, _env, runner, _refs, logs = self.in_process({"cli_timeout_scale": 0.2},
                                                        FAKE_MCP_CLI_MODE="timeout:host_info")
        started = time.monotonic()
        result = runner.run("host_info")
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual((result.status, result.error_code), ("timeout", "cli_timeout"))
        self.assertFalse(result.rejected)
        calls = self.cli_calls("host_info")
        self.assertEqual(len(calls), 1)
        self.assertTrue(wait_dead(calls[0]["pid"], 5), "the CLI process is still alive")
        self.assertTrue(wait_dead(calls[0]["child_pid"], 5), "a child of the CLI is still alive")
        self.assertIsNone(calls[0]["exit"])
        error = result.to_error()
        self.assertEqual(error.error_code, "cli_timeout")
        self.assertTrue(any("cli host_info" in line and "timeout" in line for line in logs))

    def test_a_reaped_cli_pid_is_never_signalled_alone(self) -> None:
        cli = import_server_modules()["cli"]
        killed: List[int] = []
        real_killpg, real_kill = cli.os.killpg, cli.os.kill

        def gone_group(pid: int, sig: int) -> None:
            raise ProcessLookupError(pid)

        def record_kill(pid: int, sig: int) -> None:
            killed.append(pid)

        cli.os.killpg, cli.os.kill = gone_group, record_kill
        try:
            cli.kill_group(424242, group_only=True)
            self.assertEqual(killed, [])  # a reaped pid may belong to anyone now
            cli.kill_group(424242)
            self.assertEqual(killed, [424242])  # a live, unreaped CLI still gets the fallback
        finally:
            cli.os.killpg, cli.os.kill = real_killpg, real_kill
        _m, _env, runner, _refs, _logs = self.in_process({"cli_timeout_scale": 0.05},
                                                         FAKE_MCP_CLI_MODE="timeout:host_info")
        calls: List[Any] = []
        real_kill_group = cli.kill_group

        def recording_kill_group(pid: int, group_only: bool = False) -> None:
            calls.append((pid, group_only))
            real_kill_group(pid, group_only)

        cli.kill_group = recording_kill_group
        try:
            result = runner.run("host_info")
        finally:
            cli.kill_group = real_kill_group
        self.assertEqual(result.status, "timeout")
        pid = self.cli_calls("host_info")[0]["pid"]
        self.assertEqual(calls, [(pid, False), (pid, True)])
        self.assertTrue(wait_dead(pid, 5))

    def test_a_helper_holding_the_pipe_open_does_not_hold_the_call(self) -> None:
        pid_file = self.tmp / "helper.pid"
        script = self.tmp / "leaky_cli.py"
        script.write_text(
            "import json, os, subprocess, sys\n"
            "helper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'],\n"
            "                          start_new_session=True)\n"
            "open(%r, 'w').write(str(helper.pid))\n"
            "mode = os.environ.get('LEAKY_MODE', 'ok')\n"
            "if mode == 'hang':\n"
            "    import time; time.sleep(30)\n"
            "print(json.dumps({'ok': True, 'host_id': 'h_' + 'a' * 26}))\n" % str(pid_file),
            encoding="utf-8")

        def kill_helper() -> None:
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass

        self.addCleanup(kill_helper)
        _m, _env, runner, _refs, _logs = self.in_process(AUTONOM_CLI=str(script))
        started = time.monotonic()
        result = runner.run("host_info")
        elapsed = time.monotonic() - started
        self.assertEqual(result.status, "ok", result)
        self.assertEqual(result.data["host_id"], "h_" + "a" * 26)
        self.assertLess(elapsed, 6.0)  # the helper lives 8 s; the grace is 2 s
        kill_helper()
        _m2, _env2, slow_runner, _r2, _l2 = self.in_process({"cli_timeout_scale": 0.2},
                                                            AUTONOM_CLI=str(script), LEAKY_MODE="hang")
        started = time.monotonic()
        timed_out = slow_runner.run("host_info")
        self.assertEqual(timed_out.status, "timeout")
        self.assertLess(time.monotonic() - started, slow_runner.timeout_for("host_info") + 6.0)

    def test_malformed_crash_and_cli_errors_are_classified(self) -> None:
        modules, _env, runner, _refs, _logs = self.in_process(
            FAKE_MCP_CLI_MODE="malformed:devices,crash:sessions,fail:ui_tap:no_matching_node,"
                              "fail:ui_key:ui_action_uncertain,fail:ui_type:brand_new_code,"
                              "fail:control_status:pretest_damaged")
        common, cli = modules["common"], modules["cli"]
        self.assertEqual(runner.run("devices").error_code, "cli_output_invalid")
        self.assertEqual(runner.run("sessions", {"all": True}).error_code, "cli_output_invalid")
        session_id = "s_0123456789"
        tap = runner.run("ui_tap", {"session_id": session_id, "selector": {"text": "OK"}},
                         operation_id=common.new_operation_id())
        key = runner.run("ui_key", {"session_id": session_id, "key": "HOME"},
                         operation_id=common.new_operation_id())
        typed = runner.run("ui_type", {"session_id": session_id, "text": "abc"},
                           operation_id=common.new_operation_id())
        status = runner.run("control_status", {"session_id": session_id})
        self.assertEqual((tap.status, tap.cli_error_code, tap.rejected), ("cli_error", "no_matching_node", True))
        self.assertEqual((key.cli_error_code, key.rejected), ("ui_action_uncertain", False))
        self.assertEqual((typed.cli_error_code, typed.rejected), ("brand_new_code", False))
        self.assertTrue(status.rejected)
        mutating = common.Tool("t", "t", "t", common.EMPTY_INPUT, {}, {"readOnlyHint": False}, {}, True, None)
        reading = common.Tool("r", "r", "r", common.EMPTY_INPUT, {}, {"readOnlyHint": True}, {}, False, None)
        self.assertEqual(common.failure_outcome(mutating, tap.to_error()), "rejected")
        self.assertEqual(common.failure_outcome(mutating, key.to_error()), "outcome_unknown")
        self.assertEqual(common.failure_outcome(mutating, typed.to_error()), "outcome_unknown")
        self.assertEqual(common.failure_outcome(reading, runner.run("devices").to_error()), "failed")
        error = tap.to_error()
        self.assertEqual(error.extras["cli_error_code"], "no_matching_node")
        self.assertEqual(error.extras["cli_error"], "fake failure no_matching_node")
        self.assertEqual(error.extras["cli_hint"], "fake hint")
        for key_name in ("ui_tap", "ui_key", "ui_type"):
            self.assertEqual(len(self.cli_calls(key_name)), 1, key_name)
        self.assertIn(cli.classify("never_seen_code"), ("unknown",))

    def test_parse_rules(self) -> None:
        cli = import_server_modules()["cli"]
        classify = cli.Runner._classify
        indented = json.dumps({"ok": True, "x": 1}, indent=2)
        self.assertEqual(classify("devices", 0, indented, "", False, 0.1, 1).data, {"ok": True, "x": 1})
        self.assertEqual(classify("devices", 0, "noise\n{\"ok\": true, \"y\": 2}\n", "", False, 0.1, 1).data,
                         {"ok": True, "y": 2})
        self.assertEqual(classify("devices", 0, indented, "", True, 0.1, 1).status, "malformed")
        self.assertEqual(classify("devices", 0, "[1, 2]", "", False, 0.1, 1).status, "malformed")
        self.assertEqual(classify("devices", 0, '{"ok": false}', "", False, 0.1, 1).status, "malformed")
        refused = classify("devices", 0, '{"ok": false, "error_code": "no_target", "error": "x"}', "",
                           False, 0.1, 1)
        self.assertEqual((refused.status, refused.cli_error_code), ("cli_error", "no_target"))
        on_stderr = classify("devices", 2, "", '{"ok": false, "error_code": "usage_error", "error": "e"}',
                             False, 0.1, 1)
        self.assertEqual(on_stderr.cli_error_code, "usage_error")
        on_stdout = classify("devices", 1, '{"ok": false, "error_code": "adb_not_found", "error": "e"}', "",
                             False, 0.1, 1)
        self.assertEqual(on_stdout.cli_error_code, "adb_not_found")
        self.assertEqual(classify("devices", 1, "", "Traceback ...", False, 0.1, 1).status, "malformed")
        interrupted = classify("flow_run", 130, "", '{"ok": false, "error": "interrupted"}', False, 0.1, 1)
        self.assertEqual((interrupted.status, interrupted.error_code), ("interrupted", "cli_interrupted"))
        long_error = classify("devices", 2, "", json.dumps({"ok": False, "error_code": "x" * 100,
                                                            "error": "e" * 900}), False, 0.1, 1)
        self.assertEqual(len(long_error.to_error().extras["cli_error"]), 500)
        self.assertEqual(len(long_error.cli_error_code), 80)

    def test_the_fake_cli_models_agent_profile_sessions(self) -> None:
        _m, _env, runner, _refs, _logs = self.in_process()
        common = _m["common"]
        agent = FAKE.add_session(self.fake_state, profile="agent")
        plain = FAKE.add_session(self.fake_state, "ios", IOS_UDID)
        tree = runner.run("ui_tree", {"session_id": agent, "max_nodes": 200})
        self.assertTrue(tree.ok)
        self.assertIn("nodes", tree.data)
        self.assertNotIn("outline", tree.data)
        tap = runner.run("ui_tap", {"session_id": agent, "selector": {"text": "Settings"}},
                         operation_id=common.new_operation_id())
        self.assertTrue(tap.ok)
        self.assertNotIn("screen", tap.data)

        def fake(*argv: str) -> Dict[str, Any]:
            env = self.server_env()
            done = subprocess.run([sys.executable, str(FAKE_CLI), *argv], env=env, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)
            return json.loads(done.stdout)

        outline = fake("ui", "tree", "--session-id", agent)
        self.assertEqual(outline.get("format"), "outline")
        self.assertNotIn("nodes", outline)
        self.assertIn("n1 text \"Settings\"", outline["outline"])
        self.assertIn("nodes", fake("ui", "tree", "--session-id", agent, "--format", "json"))
        self.assertIn("nodes", fake("ui", "tree", "--session-id", plain))
        self.assertEqual(fake("ui", "tree", "--session-id", plain, "--format", "outline")["format"], "outline")
        observed = fake("ui", "key", "--session-id", agent, "HOME")
        self.assertIn("screen", observed)
        self.assertEqual(sorted(k for k in ("screen", "changes", "foreground") if k in observed),
                         ["changes", "foreground", "screen"])
        self.assertNotIn("screen", fake("ui", "key", "--session-id", agent, "--no-observe", "HOME"))
        self.assertNotIn("screen", fake("ui", "type", "--session-id", agent, "--no-observe", "--", "x"))
        self.assertIn("screen", fake("ui", "type", "--session-id", plain, "--observe", "--", "x"))
        self.assertNotIn("screen", fake("ui", "tap", "--session-id", plain, "--text", "Settings"))
        fake("session", "stop", "--session-id", plain)
        started = fake("session", "start", "--platform", "ios", "--target", IOS_UDID,
                       "--alongside", "--profile", "agent")
        self.assertEqual(started["session"]["profile"], "agent")
        self.assertEqual(fake("ui", "tree", "--session-id", started["session"]["session_id"])["format"],
                         "outline")

    def test_child_environment_cwd_claim_and_unavailable_cli(self) -> None:
        modules, _env, runner, _refs, _logs = self.in_process(
            AUTONOM_CONTROL_HOLDER="leaked-holder", AUTONOM_CONTROL_GENERATION="99")
        self.assertTrue(runner.run("host_info").ok)
        call = self.cli_calls("host_info")[0]
        self.assertEqual(call["claim"], {"AUTONOM_CONTROL_HOLDER": None, "AUTONOM_CONTROL_GENERATION": None})
        self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(str(self.mcp_dir)))
        self.assertEqual(call["argv"], ["host", "info"])
        session_id = FAKE.add_session(self.fake_state)
        result = runner.run("ui_key", {"session_id": session_id, "key": "HOME"},
                            operation_id="op_0000000000000001", claim={"holder": "h1", "generation": 3})
        self.assertFalse(result.ok)  # no lease: the fake refuses the claim
        call = self.cli_calls("ui_key")[0]
        self.assertEqual(call["claim"], {"AUTONOM_CONTROL_HOLDER": "h1", "AUTONOM_CONTROL_GENERATION": "3"})
        self.assertEqual(call["argv"][:4], ["ui", "key", "--operation-id", "op_0000000000000001"])
        missing, _env2, missing_runner, _r, _l = self.in_process(AUTONOM_CLI=str(self.tmp / "nope.py"))
        unavailable = missing_runner.run("host_info")
        self.assertEqual((unavailable.status, unavailable.error_code, unavailable.rejected),
                         ("unavailable", "cli_unavailable", True))
        _m3, _e3, old_runner, _r3, _l3 = self.in_process(AUTONOM_MCP_CLI_PYTHON=str(self.tmp / "nopython"))
        self.assertEqual(old_runner.run("host_info").error_code, "cli_unavailable")

    def test_programming_errors_and_sensitive_argv(self) -> None:
        cli = import_server_modules()["cli"]
        with self.assertRaises(KeyError):
            cli.build_argv("rm_rf", {})
        with self.assertRaises(ValueError):
            cli.build_argv("ui_tap", {"session_id": "s_1", "selector": {"text": "x"}})
        with self.assertRaises(ValueError):
            cli.build_argv("host_info", {}, "op_0000000000000001")
        with self.assertRaises(ValueError):
            cli.build_argv("ui_tap", {"session_id": "s_1", "selector": {}}, "op_0000000000000001")
        with self.assertRaises(ValueError):
            cli.build_argv("session_show", {})
        _m, _env, runner, _refs, _logs = self.in_process()
        with self.assertRaises(ValueError):
            runner.run("flow_run", {"path": "/x.yaml", "session_id": "s_1"}, operation_id="op_0000000000000001")
        argv = cli.build_argv("ui_type", {"session_id": "s_1", "text": "-n secret", "sensitive": True},
                              "op_0000000000000001")
        self.assertEqual(argv, ["ui", "type", "--operation-id", "op_0000000000000001", "--session-id", "s_1",
                                "--no-observe", "--sensitive", "--", "-n secret"])
        self.assertEqual(cli.build_argv("ui_tree", {"session_id": "s_1", "max_nodes": 50}),
                         ["ui", "tree", "--session-id", "s_1", "--format", "json", "--max-nodes", "50",
                          "--no-accessibility-recovery"])
        self.assertEqual(cli.build_argv("ui_key", {"session_id": "s_1", "key": "HOME"}, "op_0000000000000001"),
                         ["ui", "key", "--operation-id", "op_0000000000000001", "--session-id", "s_1",
                          "--no-observe", "HOME"])
        self.assertEqual(cli.build_argv("screenshot", {"session_id": "s_1", "label": "-x"}),
                         ["screenshot", "--session-id", "s_1", "--label=-x"])
        for key in cli.ALLOWLIST:
            self.assertNotIn("takeover", " ".join(cli.ALLOWLIST[key].path))

    def test_redaction_of_logged_stderr(self) -> None:
        modules, _env, runner, _refs, logs = self.in_process(FAKE_MCP_CLI_MODE="crash:devices")
        common = modules["common"]
        common.register_secret("canvas-token-value-123")
        self.assertEqual(common.redact("x canvas-token-value-123 Bearer abc #token=zz"),
                         "x <redacted> Bearer <redacted> #token=<redacted>")
        runner.run("devices")
        self.assertTrue(logs)
        for line in logs:
            self.assertNotIn(str(self.tmp), line)
            self.assertNotIn(str(self.home), line)


# ====================================================================== 6. code sets

class CodeSetTests(unittest.TestCase):
    def test_rejected_and_unknown_cover_errors_py_exactly_once(self) -> None:
        cli = import_server_modules()["cli"]
        text = ERRORS_PY.read_text(encoding="utf-8")
        codes = set(re.findall(r'^[A-Z][A-Z0-9_]* = "([a-z][a-z0-9_]*)"', text, re.M))
        self.assertGreater(len(codes), 140)
        self.assertFalse(cli.REJECTED_CLI_CODES & cli.UNKNOWN_CLI_CODES)
        self.assertEqual(set(), codes - (cli.REJECTED_CLI_CODES | cli.UNKNOWN_CLI_CODES),
                         "codes of errors.py missing from both sets")
        self.assertEqual(set(), (cli.REJECTED_CLI_CODES | cli.UNKNOWN_CLI_CODES) - codes,
                         "codes in the sets that errors.py does not define")
        for code in ("budget_exhausted", "forbidden_action", "app_not_allowed", "pretest_damaged",
                     "usage_error", "control_held", "control_generation_stale", "host_identity_invalid",
                     "session_already_active", "selector_required", "canvas_not_found"):
            self.assertIn(code, cli.REJECTED_CLI_CODES, code)
        for code in ("ui_action_uncertain", "backend_failed", "invalid_value", "ui_not_idle", "interrupted"):
            self.assertIn(code, cli.UNKNOWN_CLI_CODES, code)
        self.assertTrue(cli.CONTROL_CODES <= cli.REJECTED_CLI_CODES)


# ====================================================================== 7. refs and guard

class RefsTests(CoreCase):
    def test_reference_refusals_through_the_server(self) -> None:
        live = FAKE.add_session(self.fake_state)
        stopped = FAKE.add_session(self.fake_state, "ios", IOS_UDID, live=False)
        server = self.started()
        host = self.host_id(server)
        args = {"host_id": OTHER_HOST, "session_id": live, "ms": 0}
        refused = self.call_error(server, "test_slow_mutation", args, "host_not_found")
        self.assertIs(refused["retry_safe"], True)
        self.assertIsNone(refused["session"])
        self.assertIn("not supported", refused["hint"])
        self.call_error(server, "test_slow_mutation", dict(args, host_id=host, session_id="s_nosuch"),
                        "session_not_found")
        self.call_error(server, "test_slow_mutation", dict(args, host_id=host, session_id=stopped),
                        "session_stopped")
        done = self.call_ok(server, "test_slow_mutation", dict(args, host_id=host))
        self.assertEqual(done["session"], {"host_id": host, "session_id": live})
        self.assertEqual(done["host_id"], host)
        self.assertEqual(self.cli_calls("session_start"), [])

    def test_target_not_allowed(self) -> None:
        live = FAKE.add_session(self.fake_state)
        ios = FAKE.add_session(self.fake_state, "ios", IOS_UDID)
        self.write_config({"allowed_targets": ["ios:%s" % IOS_UDID]})
        server = self.started()
        host = self.host_id(server)
        self.call_error(server, "test_slow_mutation", {"host_id": host, "session_id": live},
                        "target_not_allowed")
        self.call_ok(server, "test_slow_mutation", {"host_id": host, "session_id": ios})

    def test_host_unavailable_when_the_cli_has_no_host_info(self) -> None:
        live = FAKE.add_session(self.fake_state)
        server = self.started(FAKE_MCP_CLI_MODE="fail:host_info:usage_error")
        refused = self.call_error(server, "test_slow_mutation", {"host_id": OTHER_HOST, "session_id": live},
                                  "host_unavailable")
        self.assertEqual(refused["cli_error_code"], "usage_error")
        self.assertIs(refused["retry_safe"], True)
        self.assertIn("host info", refused["hint"])
        self.assertIsNone(refused["host_id"])
        hosts = self.call_ok(server, "list_hosts")["hosts"]
        self.assertEqual([(item["state"], item["error_code"]) for item in hosts], [("error", "host_unavailable")])
        self.assertEqual(len(self.cli_calls("host_info")), 2, "a failed identity read is not cached")

    def test_session_busy_while_a_mutation_or_run_holds_the_guard(self) -> None:
        first = FAKE.add_session(self.fake_state)
        second = FAKE.add_session(self.fake_state, "ios", IOS_UDID)
        server = self.started()
        host = self.host_id(server)
        server.send({"jsonrpc": "2.0", "id": "slow", "method": "tools/call",
                     "params": {"name": "test_slow_mutation",
                                "arguments": {"host_id": host, "session_id": first, "ms": 2000}}})
        time.sleep(0.6)
        busy = self.call_error(server, "test_slow_mutation", {"host_id": host, "session_id": first},
                               "session_busy")
        self.assertEqual(busy["busy_with"], {"tool": "test_slow_mutation"})
        self.assertEqual(busy["session"], {"host_id": host, "session_id": first})
        self.call_ok(server, "test_slow_mutation", {"host_id": host, "session_id": second})
        self.assertTrue(server.wait_for("slow")["result"]["structuredContent"]["ok"])
        run = self.call_ok(server, "test_hold_run", {"host_id": host, "session_id": first, "ms": 20000})
        busy = self.call_error(server, "test_slow_mutation", {"host_id": host, "session_id": first},
                               "session_busy")
        self.assertEqual(busy["busy_with"], {"run_id": run["run_id"]})

    def test_guard_hooks_views_in_process(self) -> None:
        refs_mod = import_server_modules()["refs"]
        common = import_server_modules()["common"]
        guard = refs_mod.SessionGuard()
        with guard.mutation("s_a", "tap_element"):
            self.assertEqual(guard.busy("s_a"), {"tool": "tap_element"})
            with self.assertRaises(common.ToolError) as caught:
                with guard.mutation("s_a", "type_text"):
                    pass
            self.assertEqual(caught.exception.error_code, "session_busy")
            self.assertTrue(caught.exception.rejected)
            with guard.mutation("s_b", {"run_id": "run_0123456789abcdef"}):
                self.assertEqual(guard.busy("s_b"), {"run_id": "run_0123456789abcdef"})
        self.assertIsNone(guard.busy("s_a"))
        guard.hold("s_c", "x")
        threading.Timer(0.3, guard.release, args=("s_c",)).start()
        self.assertTrue(guard.try_hold("s_c", "y", wait_s=5))
        self.assertFalse(guard.try_hold("s_c", "z", wait_s=0.1))
        guard.release("s_c")

        order: List[str] = []
        hooks = refs_mod.SessionHooks()
        hooks.register("cancel_runs", lambda sid: order.append("runs:" + sid) or "cancelled")
        hooks.register("close_streams", lambda sid: (_ for _ in ()).throw(RuntimeError("boom")))
        hooks.register("release_lease", lambda sid: order.append("lease:" + sid) or {"released": True})
        results = hooks.stopping("s_a")
        self.assertEqual(order, ["runs:s_a", "lease:s_a"])
        self.assertEqual(results, [{"name": "cancel_runs", "ok": True, "detail": "cancelled"},
                                   {"name": "close_streams", "ok": False, "detail": "RuntimeError"},
                                   {"name": "release_lease", "ok": True, "detail": '{"released": true}'}])
        views = refs_mod.SessionViews()
        self.assertEqual(views.get("streams", "s_a"), [])
        views.register("streams", lambda sid: [{"stream_id": "st_0123456789abcdef", "profile": "focus"}])
        self.assertEqual(views.get("streams", "s_a")[0]["profile"], "focus")
        views.register("broken", lambda sid: 1 / 0)
        self.assertEqual(views.get("broken", "s_a"), [])

    def test_artifact_identity_and_session_dirs(self) -> None:
        modules, env, runner, refs, _logs = self.in_process()
        refs_mod = modules["refs"]
        state_dir = modules["env"].mcp_state_dir(env, self.home)
        keys: List[bytes] = []
        threads = [threading.Thread(target=lambda: keys.append(refs_mod.artifact_key(state_dir)))
                   for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(keys)), 1)
        self.assertEqual(len(keys[0]), 32)
        self.assert_mode(state_dir / "artifact-key", 0o600)
        key = keys[0]
        one = refs_mod.artifact_id(key, "h_x", "s_1", "screenshots/a.png")
        self.assertRegex(one, r"^art_[0-9a-f]{24}$")
        self.assertEqual(one, refs.artifact_id("h_x", "s_1", "screenshots/a.png"))
        self.assertNotEqual(one, refs_mod.artifact_id(key, "h_x", "s_2", "screenshots/a.png"))
        self.assertNotEqual(one, refs_mod.artifact_id(key, "h_y", "s_1", "screenshots/a.png"))
        session_id = FAKE.add_session(self.fake_state)
        directory = refs.session_artifacts_dir({"session_id": session_id})
        self.assertEqual(directory, self.fake_state / "sessions" / session_id)
        # The CLI refuses: the documented store layout is read instead.
        store = self.state_base / "sessions" / "s_stored1234"
        store.mkdir(parents=True)
        (store / "session.json").write_text(json.dumps({"session_id": "s_stored1234"}), encoding="utf-8")
        _m2, _e2, _r2, refusing, _l2 = self.in_process(FAKE_MCP_CLI_MODE="fail:session_show:session_stopped")
        self.assertEqual(refusing.session_artifacts_dir({"session_id": "s_stored1234"}), store)
        with self.assertRaises(modules["common"].ToolError) as caught:
            refusing.session_artifacts_dir({"session_id": "s_unknown123"})
        self.assertEqual(caught.exception.error_code, "artifacts_unavailable")

    def test_the_artifact_key_is_never_published_half_written(self) -> None:
        modules, env, _runner, _refs, _logs = self.in_process()
        refs_mod = modules["refs"]
        state_dir = modules["env"].mcp_state_dir(env, self.home)
        real_write = os.write

        def dying_write(fd: int, data: bytes) -> int:
            raise KeyboardInterrupt("killed between create and write")

        refs_mod.os.write = dying_write
        try:
            with self.assertRaises(KeyboardInterrupt):
                refs_mod.artifact_key(state_dir)
        finally:
            refs_mod.os.write = real_write
        self.assertFalse((state_dir / "artifact-key").exists())
        self.assertEqual(list(state_dir.iterdir()), [])  # the temporary file is gone too
        key = refs_mod.artifact_key(state_dir)
        self.assertEqual(len(key), 32)
        self.assertEqual(refs_mod.artifact_key(state_dir), key)
        self.assert_mode(state_dir / "artifact-key", 0o600)
        self.assertEqual([item.name for item in state_dir.iterdir()], ["artifact-key"])

    def test_resolve_caches_the_session_list_for_one_second(self) -> None:
        _modules, _env, _runner, refs, _logs = self.in_process()
        session_id = FAKE.add_session(self.fake_state, label="checkout")
        host = refs.local_host()["host_id"]
        entry = refs.resolve(host, session_id)
        self.assertEqual(set(entry), {"host_id", "session_id", "platform", "target_id", "label", "live",
                                      "started_at", "stopped_at", "started_by"})
        self.assertEqual((entry["platform"], entry["target_id"], entry["label"], entry["live"]),
                         ("android", "emulator-5584", "checkout", True))
        refs.resolve(host, session_id)
        self.assertEqual(len(self.cli_calls("sessions")), 1)
        self.assertEqual(len(self.cli_calls("host_info")), 1)
        time.sleep(1.1)
        refs.resolve(host, session_id)
        self.assertEqual(len(self.cli_calls("sessions")), 2)
        self.assertEqual(self.cli_calls("sessions")[0]["argv"], ["session", "list", "--all"])


# ====================================================================== 8. lease keeper

class LeaseKeeperTests(CoreCase):
    def keeper(self, **env: Optional[str]):
        modules, _env, runner, refs, logs = self.in_process(**env)
        session_id = FAKE.add_session(self.fake_state)
        host = refs.local_host()["host_id"]
        ref = refs.resolve(host, session_id)
        keeper = modules["control"].LeaseKeeper(runner, refs, refs.config, logs.append)
        return modules, keeper, ref, host

    def test_auto_acquire_reuse_renew_window_and_drop(self) -> None:
        modules, keeper, ref, host = self.keeper()
        claim = keeper.ensure(ref)
        self.assertEqual(claim, {"holder": "chatgpt-agent:" + host, "generation": 1})
        acquires = self.cli_calls("control_acquire")
        self.assertEqual(len(acquires), 1)
        argv = acquires[0]["argv"]
        self.assertEqual(argv[:3], ["control", "acquire", "--operation-id"])
        self.assertRegex(argv[3], OP_ID)
        self.assertEqual(argv[4:], ["--session-id", ref["session_id"], "--holder", "chatgpt-agent:" + host,
                                    "--kind", "agent", "--label", "ChatGPT agent", "--ttl", "120"])
        self.assertEqual(keeper.ensure(ref), claim)
        self.assertEqual(len(self.cli_calls("control_acquire")), 1, "a fresh lease is reused")
        # Within 10 s of expiry the lease is acquired again (renewed in place, same generation).
        keeper.clock = lambda: time.time() + 115
        self.assertEqual(keeper.ensure(ref, operation_id="op_00000000000000aa"), claim)
        self.assertEqual(len(self.cli_calls("control_acquire")), 2)
        self.assertEqual(self.cli_calls("control_acquire")[1]["argv"][3], "op_00000000000000aa")
        keeper.clock = time.time
        keeper.drop(ref["platform"], ref["target_id"])
        self.assertIsNone(keeper.cached(ref["platform"], ref["target_id"]))
        keeper.ensure(ref)
        self.assertEqual(len(self.cli_calls("control_acquire")), 3)
        for call in self.cli_calls():
            self.assertNotIn("--takeover", call["argv"])

    def test_refusal_drops_the_cache_and_reports_the_lease(self) -> None:
        modules, keeper, ref, host = self.keeper()
        keeper.ensure(ref)
        human = {"kind": "human", "id": "canvas:4000:123", "label": "Canvas 4000"}
        FAKE.set_lease(self.fake_state, ref["platform"], ref["target_id"], holder=human, generation=5)
        keeper.drop(ref["platform"], ref["target_id"])
        with self.assertRaises(modules["common"].ToolError) as caught:
            keeper.ensure(ref)
        error = caught.exception
        self.assertEqual((error.error_code, error.rejected), ("control_held", True))
        self.assertEqual(error.extras["control"], {
            "state": "held", "holder": human, "generation": 5, "expires_at": error.extras["control"]["expires_at"],
            "mine": False, "role": "human", "took_over_from": None})
        self.assertEqual(error.extras["capability"], "control.lease")
        self.assertIsNone(keeper.cached(ref["platform"], ref["target_id"]))
        keeper.human_holder_ids.add("canvas:4000:123")
        self.assertTrue(keeper.status(ref)["mine"])
        # A mutation answered with a control code drops the entry without repeating anything.
        FAKE.set_lease(self.fake_state, ref["platform"], ref["target_id"], holder=None, generation=5)
        keeper.ensure(ref)
        self.assertEqual(keeper.cached(ref["platform"], ref["target_id"])["generation"], 6)
        cli = modules["cli"]
        refused = cli.CliResult("ui_tap", "cli_error", exit_code=2,
                                error={"ok": False, "error_code": "control_generation_stale", "error": "x",
                                       "generation": 7, "holder": human})
        view = keeper.note(ref, refused)
        self.assertEqual((view["state"], view["generation"]), ("held", 7))
        self.assertIsNone(keeper.cached(ref["platform"], ref["target_id"]))
        self.assertIsNone(keeper.note(ref, cli.CliResult("ui_tap", "timeout")))

    def test_release_renew_status_and_release_all(self) -> None:
        modules, keeper, ref, host = self.keeper()
        common = modules["common"]
        with self.assertRaises(common.ToolError) as caught:
            keeper.release(ref)
        self.assertEqual((caught.exception.error_code, caught.exception.rejected), ("control_not_held", True))
        self.assertEqual(self.cli_calls("control_release"), [])
        keeper.ensure(ref)
        status = keeper.status(ref)
        self.assertEqual((status["state"], status["mine"], status["role"], status["generation"]),
                         ("held", True, "agent", 1))
        renewed = keeper.renew(ref)
        self.assertEqual(renewed["generation"], 1)
        renew_argv = self.cli_calls("control_renew")[0]["argv"]
        self.assertEqual(renew_argv, ["control", "renew", "--session-id", ref["session_id"], "--holder",
                                      "chatgpt-agent:" + host, "--generation", "1", "--ttl", "120"])
        after = keeper.release(ref)
        self.assertEqual((after["state"], after["holder"]), ("free", None))
        release_argv = self.cli_calls("control_release")[0]["argv"]
        self.assertEqual(release_argv[:3], ["control", "release", "--operation-id"])
        self.assertEqual(release_argv[4:], ["--session-id", ref["session_id"], "--holder",
                                            "chatgpt-agent:" + host, "--generation", "1"])
        self.assertIsNone(keeper.cached(ref["platform"], ref["target_id"]))
        with self.assertRaises(common.ToolError):
            keeper.renew(ref)
        keeper.ensure(ref)
        self.assertEqual(keeper.release_all(), [{"target": "android:emulator-5584", "ok": True}])
        self.assertEqual(len(self.cli_calls("control_release")), 2)
        self.assertEqual(FAKE.get_lease(self.fake_state, "android", "emulator-5584")["holder"], None)
        for call in self.cli_calls():
            self.assertNotIn("--takeover", call["argv"])

    def test_lease_views(self) -> None:
        control = import_server_modules()["control"]
        self.assertEqual(control.lease_view(None, set())["state"], "none")
        self.assertEqual(control.lease_view({"state": "unreadable"}, set())["state"], "unknown")
        view = control.lease_view({"state": "held", "generation": 3, "expires_at": "2026-10-20T10:00:00.000Z",
                                   "holder": {"kind": "agent", "id": "a", "label": "A"},
                                   "took_over_from": {"kind": "human", "id": "c", "label": None}}, {"a"})
        self.assertEqual(view, {"state": "held", "holder": {"kind": "agent", "id": "a", "label": "A"},
                                "generation": 3, "expires_at": "2026-10-20T10:00:00.000Z", "mine": True,
                                "role": "agent", "took_over_from": {"kind": "human", "id": "c", "label": None}})
        self.assertEqual(control.parse_iso("1970-01-01T00:00:10.500Z"), 10.5)
        self.assertIsNone(control.parse_iso("yesterday"))
        common = import_server_modules()["common"]
        self.assertEqual(common.validate(common.CONTROL_VIEW, view), [])

    def test_a_refusal_naming_an_expired_holder_reports_expired(self) -> None:
        control = import_server_modules()["control"]
        human = {"kind": "human", "id": "canvas:4000:123", "label": "Canvas 4000"}
        now = control.parse_iso("2026-10-20T10:00:00.000Z")
        for code in ("control_generation_stale", "control_not_held", "control_held"):
            past = control.refusal_view({"error_code": code, "holder": human, "generation": 4,
                                         "expires_at": "2026-10-20T09:59:59.000Z"}, set(), now)
            ahead = control.refusal_view({"error_code": code, "holder": human, "generation": 4,
                                          "expires_at": "2026-10-20T10:00:30.000Z"}, set(), now)
            self.assertEqual((past["state"], past["role"]), ("expired", "human"), code)
            self.assertEqual(ahead["state"], "held", code)
        self.assertEqual(control.refusal_view({"error_code": "control_lease_expired", "holder": human,
                                               "generation": 4, "expires_at": "2026-10-20T10:00:30.000Z"},
                                              set(), now)["state"], "expired")
        self.assertEqual(control.refusal_view({"error_code": "control_not_held", "holder": None,
                                               "generation": 0}, set(), now)["state"], "free")


# ====================================================================== 9. argv conformance

class ArgvConformanceTests(unittest.TestCase):
    OP = "op_0123456789abcdef"
    SID = "s_0123456789"
    HANDLERS = {
        "host_info": "cmd_host_info", "devices": "cmd_devices", "sessions": "cmd_session_list",
        "session_show": "cmd_session_show", "session_start": "cmd_session_start",
        "session_stop": "cmd_session_stop", "screenshot": "cmd_screenshot", "ui_tree": "cmd_ui_tree",
        "ui_tap": "cmd_ui_tap", "ui_type": "cmd_ui_type", "ui_key": "cmd_ui_key",
        "control_status": "cmd_control_status", "control_acquire": "cmd_control_acquire",
        "control_release": "cmd_control_release", "control_renew": "cmd_control_renew",
        "journal_find": "cmd_journal", "flow_run": "cmd_flow_run", "canvas_serve": "cmd_canvas_serve",
        "canvas_attach": "cmd_canvas_attach", "canvas_detach": "cmd_canvas_detach",
        "canvas_stop": "cmd_canvas_stop",
    }

    @classmethod
    def setUpClass(cls) -> None:
        scripts = str(ROOT / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec = importlib.util.spec_from_file_location("autonom_cli_for_mcp_conformance",
                                                          ROOT / "scripts" / "autonom.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        finally:
            sys.dont_write_bytecode = previous
        cls.parser = module.build_parser()
        cls.cli = import_server_modules()["cli"]

    def samples(self) -> Dict[str, Dict[str, Any]]:
        sid = self.SID
        return {
            "host_info": {}, "devices": {}, "sessions": {"all": True}, "session_show": {"session_id": sid},
            "session_start": {"platform": "android", "target_id": "emulator-5584", "label": "checkout bug"},
            "session_stop": {"session_id": sid}, "screenshot": {"session_id": sid, "label": "before"},
            "ui_tree": {"session_id": sid, "max_nodes": 200, "interactable": True},
            "ui_tap": {"session_id": sid, "selector": {"text": "OK", "desc": "d", "resource_id": "r",
                                                       "class_name": "c", "role": "button"},
                       "mode": "exact", "index": 2},
            "ui_type": {"session_id": sid, "text": "hello", "sensitive": True},
            "ui_key": {"session_id": sid, "key": "HOME"},
            "control_status": {"session_id": sid},
            "control_acquire": {"session_id": sid, "holder": "chatgpt-agent:h_x", "label": "ChatGPT agent",
                                "ttl": 120},
            "control_release": {"session_id": sid, "holder": "chatgpt-agent:h_x", "generation": 4},
            "control_renew": {"session_id": sid, "holder": "chatgpt-agent:h_x", "generation": 4, "ttl": 60},
            "journal_find": {"session_id": sid, "grep": "op_0123456789abcdef"},
            "flow_run": {"path": "/tmp/flows/settings.yaml", "session_id": sid, "env": {"USER_NAME": "a b"}},
            "canvas_serve": {"workspace": "chatgpt", "control_idle": 120},
            "canvas_attach": {"workspace": "chatgpt", "platform": "ios", "target_id": IOS_UDID},
            "canvas_detach": {"workspace": "chatgpt", "platform": "android", "target_id": "emulator-5584"},
            "canvas_stop": {"workspace": "chatgpt"},
        }

    def parse(self, argv: List[str]) -> Any:
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                return self.parser.parse_args(argv)
        except SystemExit:
            self.fail("the real parser refused %r: %s" % (argv, stderr.getvalue()))

    def test_every_template_parses_with_the_real_parser(self) -> None:
        samples = self.samples()
        self.assertEqual(set(samples), set(self.cli.ALLOWLIST))
        self.assertEqual(set(self.HANDLERS), set(self.cli.ALLOWLIST))
        for key, params in samples.items():
            verb = self.cli.ALLOWLIST[key]
            argv = self.cli.build_argv(key, params, self.OP if verb.mutating else None)
            namespace = self.parse(argv)
            self.assertEqual(namespace.func.__name__, self.HANDLERS[key], key)
            if verb.mutating:
                self.assertEqual(namespace.operation_id, self.OP, key)
                self.assertEqual(argv[len(verb.path):len(verb.path) + 2], ["--operation-id", self.OP], key)
            else:
                self.assertIsNone(getattr(namespace, "operation_id", None), key)
            if "session_id" in params:
                self.assertEqual(namespace.session_id, self.SID, key)
            self.assertNotIn("--takeover", argv)
        tap = self.parse(self.cli.build_argv("ui_tap", samples["ui_tap"], self.OP))
        self.assertEqual((tap.text, tap.desc, tap.resource_id, tap.class_name, tap.role, tap.mode, tap.index),
                         ("OK", "d", "r", "c", "button", "exact", 2))
        start = self.parse(self.cli.build_argv("session_start", samples["session_start"], self.OP))
        self.assertEqual((start.platform, start.target, start.alongside, start.label),
                         ("android", "emulator-5584", True, "checkout bug"))
        tree = self.parse(self.cli.build_argv("ui_tree", samples["ui_tree"]))
        self.assertEqual((tree.max_nodes, tree.no_accessibility_recovery, tree.interactable, tree.format),
                         (200, True, True, "json"))
        # An agent-profile session observes by default; the adapter always asks for the plain reply.
        self.assertIs(tap.observe, False)
        typed = self.parse(self.cli.build_argv("ui_type", samples["ui_type"], self.OP))
        self.assertEqual((typed.observe, typed.sensitive, typed.text), (False, True, "hello"))
        key = self.parse(self.cli.build_argv("ui_key", samples["ui_key"], self.OP))
        self.assertEqual((key.observe, key.keycode), (False, "HOME"))
        type_argv = self.cli.build_argv("ui_type", samples["ui_type"], self.OP)
        self.assertLess(type_argv.index("--no-observe"), type_argv.index("--"))
        key_argv = self.cli.build_argv("ui_key", samples["ui_key"], self.OP)
        self.assertLess(key_argv.index("--no-observe"), key_argv.index("HOME"))
        flow = self.parse(self.cli.build_argv("flow_run", samples["flow_run"], self.OP))
        self.assertEqual((flow.path, flow.events, flow.env), ("/tmp/flows/settings.yaml", True, ["USER_NAME=a b"]))
        serve_argv = self.cli.build_argv("canvas_serve", samples["canvas_serve"])
        self.assertEqual(serve_argv, ["canvas", "serve", "--workspace", "chatgpt", "--ephemeral", "--bootable-only",
                                      "--port", "0", "--control-idle", "120"])
        serve = self.parse(serve_argv)
        self.assertEqual((serve.workspace, serve.ephemeral, serve.bootable_only, str(serve.port),
                          str(serve.control_idle)), ("chatgpt", True, True, "0", "120"))
        self.assertFalse(getattr(serve, "single", False))
        self.assertFalse(serve.bootable)
        acquire = self.parse(self.cli.build_argv("control_acquire", samples["control_acquire"], self.OP))
        self.assertEqual((acquire.holder, acquire.kind, acquire.label, acquire.ttl, acquire.takeover),
                         ("chatgpt-agent:h_x", "agent", "ChatGPT agent", 120, False))
        journal = self.parse(self.cli.build_argv("journal_find", samples["journal_find"]))
        self.assertEqual((journal.grep, journal.max), ("op_0123456789abcdef", 5))

    def test_typed_text_that_looks_like_flags_stays_text(self) -> None:
        text = "--operation-id op_x -n"
        namespace = self.parse(self.cli.build_argv("ui_type", {"session_id": self.SID, "text": text}, self.OP))
        self.assertEqual(namespace.text, text)
        self.assertEqual(namespace.operation_id, self.OP)
        label = self.parse(self.cli.build_argv("session_start", {"platform": "android", "target_id": "e-1",
                                                                 "label": "--operation-id"}, self.OP))
        self.assertEqual((label.label, label.operation_id), ("--operation-id", self.OP))
        tap = self.parse(self.cli.build_argv("ui_tap", {"session_id": self.SID, "selector": {"text": "-1 day"}},
                                             self.OP))
        self.assertEqual(tap.text, "-1 day")


# ====================================================================== 10. package, Python 3.9 and core tools

class PackageTests(CoreCase):
    def test_server_files_parse_on_3_9_and_import_only_stdlib_and_siblings(self) -> None:
        files = sorted(SERVER_DIR.glob("*.py"))
        names = {path.stem for path in files}
        self.assertTrue({"mcp_server", "mcp_common", "mcp_env", "mcp_cli", "mcp_refs", "mcp_control",
                         "mcp_catalog", "mcp_core_tools", "mcp_sessions", "mcp_runs", "mcp_media"} <= names)
        stdlib = set(sys.stdlib_module_names)
        for path in files:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path), feature_version=(3, 9))
            body = list(tree.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                body = body[1:]
            first = body[0]
            self.assertIsInstance(first, ast.ImportFrom, path.name)
            self.assertEqual((first.module, [alias.name for alias in first.names]),
                             ("__future__", ["annotations"]), path.name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    self.assertEqual(node.level, 0, path.name)
                    modules = [(node.module or "").split(".")[0]]
                else:
                    continue
                for module in modules:
                    self.assertTrue(module in stdlib or module in names or module == "__future__",
                                    "%s imports %s" % (path.name, module))
            for banned in ("strict=", "datetime.UTC", "tomllib", "kw_only=", "slots="):
                self.assertNotIn(banned, source, "%s uses %s" % (path.name, banned))

    def test_plugin_files_launcher_and_manifest(self) -> None:
        for path in PLUGIN.rglob("*"):
            if path.is_file():
                self.assertLess(path.stat().st_size, 256 * 1024, str(path))
        manifest = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["name"], manifest["version"], manifest["mcpServers"]),
                         ("autonom-chatgpt", "0.1.0", "./.mcp.json"))
        probe = json.loads((ROOT / "integrations" / "chatgpt" / "autonom-chatgpt-probe" / ".codex-plugin" /
                            "plugin.json").read_text(encoding="utf-8"))
        for key in ("author", "homepage", "repository", "license"):
            self.assertEqual(manifest[key], probe[key], key)
        interface = manifest["interface"]
        self.assertEqual((interface["displayName"], interface["category"], interface["capabilities"],
                          interface["defaultPrompt"]),
                         ("Autonom (dev)", "Developer Tools", ["Interactive", "Read", "Write"],
                          ["Open Autonom and list my devices."]))
        for key in ("shortDescription", "longDescription", "developerName"):
            self.assertTrue(interface[key], key)
        self.assertEqual(json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8")),
                         {"mcpServers": {"autonom-chatgpt": {"command": "sh", "args": ["./bin/launch.sh"],
                                                             "cwd": "."}}})
        common = import_server_modules()["common"]
        self.assertEqual((common.ADAPTER_VERSION, common.SCHEMA_VERSION), ("0.1.0", "autonom.mcp/v1"))
        self.assertEqual(subprocess.run(["sh", "-n", str(LAUNCHER)], stdin=subprocess.DEVNULL,
                                        capture_output=True, check=False).returncode, 0)
        launcher = LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("AUTONOM_MCP_PYTHON", launcher)
        self.assertNotIn("PROBE", launcher)
        self.assertIn('exec "$py" "$PLUGIN_DIR/server/mcp_server.py"', launcher)
        self.assertIn("python_not_found", launcher)
        self.assertIn("Autonom MCP adapter", launcher)
        for line in launcher.splitlines():
            if line.strip().startswith(("printf", "echo")):
                self.assertTrue(line.rstrip().endswith(">&2"), line)

    def test_launcher_starts_the_server_and_says_nothing_on_stdout(self) -> None:
        server = self.start(argv=["sh", str(LAUNCHER)], AUTONOM_MCP_PYTHON=sys.executable)
        self.assertEqual(server.initialize()["result"]["serverInfo"]["name"], "autonom-chatgpt")
        self.assertEqual(server.close(), 0)
        empty = Path(tempfile.mkdtemp(dir=str(self.tmp)))
        failed = subprocess.run(["/bin/sh", str(LAUNCHER)], stdin=subprocess.DEVNULL, capture_output=True,
                                env={"PATH": str(empty), "HOME": str(self.home), "AUTONOM_MCP_PYTHON": "/nope"},
                                check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(failed.stdout, b"")

    def system_python(self) -> Optional[str]:
        candidate = "/usr/bin/python3"
        if not os.path.exists(candidate):
            return None
        out = subprocess.run([candidate, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
        if out.returncode != 0 or out.stdout.strip() not in ("3.9", "3.10"):
            return None
        return candidate

    def test_the_server_runs_under_the_system_python(self) -> None:
        old = self.system_python()
        if old is None:
            self.skipTest("/usr/bin/python3 is absent or not 3.9/3.10")
        server = self.start(argv=["sh", str(LAUNCHER)], AUTONOM_MCP_PYTHON=old)
        server.initialize()
        names = [tool["name"] for tool in server.request("tools/list")["result"]["tools"]]
        self.assertEqual(names[:3], CORE_TOOLS)
        hosts = self.call_ok(server, "list_hosts")["hosts"]
        self.assertEqual(hosts[0]["state"], "online")
        self.assertIn("result", server.request("resources/read", {"uri": UI_URI}))
        self.call_ok(server, "open_workspace")
        self.call_error(server, "test_bad_output", {}, "schema_violation", outcome="failed")
        self.assertEqual(server.close(), 0)


class CoreToolTests(CoreCase):
    def test_list_hosts_online_filter_flows_and_config_warnings(self) -> None:
        flows = self.tmp / "flows"
        flows.mkdir()
        self.write_config({"flow_roots": [str(flows)], "max_streams": 99, "canvas_workspace": "bad name!",
                           "allowed_targets": "android:x", "unknown_key": {"kept": True},
                           "autonom_cli": "relative/path.py"})
        server = self.started()
        structured = self.call_ok(server, "list_hosts")
        host = structured["hosts"][0]
        self.assertRegex(host["host_id"], HOST_ID)
        self.assertEqual(structured["host_id"], host["host_id"])
        self.assertEqual((host["name"], host["kind"], host["version"], host["state"]),
                         ("fake-host", "local", "0.36.2", "online"))
        self.assertEqual(host["capabilities"], {
            "sessions": True, "control": True, "flows": True, "artifacts": True,
            "media": {"transport": "bridge", "codec": "h264"}, "input": ["tap", "swipe", "key", "text", "system"]})
        codes = [item["code"] for item in structured["warnings"]]
        self.assertEqual(set(codes), {"config_error"})
        messages = " ".join(item["message"] for item in structured["warnings"])
        for key in ("max_streams", "canvas_workspace", "allowed_targets", "autonom_cli"):
            self.assertIn(key, messages)
        self.assertNotIn("relative/path.py", messages)
        self.assertNotIn("bad name!", messages)
        self.assertEqual(self.call_ok(server, "list_hosts", {"host_id": OTHER_HOST})["hosts"], [])
        self.assertEqual(len(self.call_ok(server, "list_hosts", {"host_id": host["host_id"]})["hosts"]), 1)
        self.assertEqual(len(self.cli_calls("host_info")), 1, "the identity is cached after a success")

    def test_list_hosts_error_state_is_ok_and_retried(self) -> None:
        server = self.started(FAKE_MCP_CLI_MODE="crash:host_info")
        for attempt in (1, 2):
            structured = self.call_ok(server, "list_hosts")
            self.assertIsNone(structured["host_id"])
            host = structured["hosts"][0]
            self.assertEqual((host["state"], host["error_code"], host["host_id"]),
                             ("error", "host_unavailable", None))
            self.assertIn("cli_output_invalid", host["error"])
            self.assertEqual(len(self.cli_calls("host_info")), attempt)

    def test_open_workspace_lists_live_sessions_and_survives_a_failing_cli(self) -> None:
        live = FAKE.add_session(self.fake_state, label="one")
        FAKE.add_session(self.fake_state, "ios", IOS_UDID, live=False)
        server = self.started()
        workspace = self.call_ok(server, "open_workspace")["workspace"]
        self.assertEqual(workspace["adapter_version"], "0.1.0")
        self.assertRegex(workspace["host_id"], HOST_ID)
        self.assertEqual(workspace["hosts"][0]["state"], "online")
        self.assertEqual([item["session_id"] for item in workspace["sessions"]], [live])
        entry = workspace["sessions"][0]
        self.assertEqual(entry, {"host_id": workspace["host_id"], "session_id": live, "platform": "android",
                                 "target_id": "emulator-5584", "label": "one", "live": True,
                                 "started_at": entry["started_at"], "stopped_at": None, "started_by": None})
        failing = self.started(FAKE_MCP_CLI_MODE="fail:sessions:backend_failed")
        structured = self.call_ok(failing, "open_workspace")
        self.assertEqual(structured["workspace"]["sessions"], [])
        self.assertEqual([item["code"] for item in structured["warnings"]], ["sessions_unavailable"])
        # A host that cannot be read still opens the page (after the host_info timeout).
        self.write_config({"cli_timeout_scale": 0.05})
        broken = self.started(FAKE_MCP_CLI_MODE="timeout:host_info")
        structured = self.call_ok(broken, "open_workspace")
        self.assertIsNone(structured["workspace"]["host_id"])
        self.assertEqual(structured["workspace"]["hosts"][0]["state"], "error")
        self.assertIn("sessions_unavailable", [item["code"] for item in structured["warnings"]])

    def test_save_diagnostics_writes_a_private_file_and_refuses_secrets(self) -> None:
        server = self.started()
        results = {"schema_version": 1, "run_id": "diag-20261020-101010-abcd",
                   "checks": {"bridge.list_sessions": "pass", "media.focus": "not_exercised"},
                   "note": "caf\u00e9"}
        saved = self.call_ok(server, "save_diagnostics", {"results": results})["saved"]
        path = self.mcp_dir / "diagnostics" / "diag-20261020-101010-abcd.json"
        self.assertEqual(saved, {"run_id": "diag-20261020-101010-abcd", "bytes": path.stat().st_size})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), results)
        self.assert_mode(path, 0o600)
        self.assert_mode(path.parent, 0o700)
        self.assert_mode(self.mcp_dir, 0o700)  # the first call created the MCP state dir itself
        self.assert_mode(self.state_base, 0o700)
        refusals = [
            ({"schema_version": 2, "run_id": "diag-20261020-101010-abcd"}, "diagnostics_invalid"),
            ({"schema_version": True, "run_id": "diag-20261020-101010-abcd"}, "diagnostics_invalid"),
            ({"schema_version": 1, "run_id": "../../etc/passwd"}, "diagnostics_invalid"),
            ({"schema_version": 1, "run_id": "diag-20261020-101010-beef", "h": "Authorization: Bearer abc"},
             "diagnostics_contain_secret"),
            ({"schema_version": 1, "run_id": "diag-20261020-101010-beef", "u": "http://x/#token=abc"},
             "diagnostics_contain_secret"),
            ({"schema_version": 1, "run_id": "diag-20261020-101010-beef", "p": str(self.home) + "/x.png"},
             "diagnostics_contain_secret"),
            ({"schema_version": 1, "run_id": "diag-20261020-101010-beef", "pad": "x" * (256 * 1024)},
             "diagnostics_too_large"),
        ]
        for payload, code in refusals:
            structured = self.call_error(server, "save_diagnostics", {"results": payload}, code)
            self.assertIs(structured["retry_safe"], True)
        self.assertEqual(sorted(item.name for item in path.parent.iterdir()), [path.name])
        core = import_server_modules()["core"]
        common = import_server_modules()["common"]
        common.register_secret("tok-0123456789")
        self.assertTrue(core._contains_secret('{"x":"tok-0123456789"}', "/nohome"))
        self.assertFalse(core._contains_secret('{"x":"fine"}', "/nohome"))
        for item in self.call_ok(server, "list_hosts")["hosts"]:
            self.assertNotIn(str(self.tmp), json.dumps(item))

    def test_private_dirs_are_private_at_every_level(self) -> None:
        env = import_server_modules()["env"]
        old_umask = os.umask(0o022)
        try:
            deep = self.tmp / "p1" / "p2" / "leaf"
            env.ensure_private_dir(deep)
            for item in (self.tmp / "p1", self.tmp / "p1" / "p2", deep):
                self.assert_mode(item, 0o700)
            loose = self.tmp / "loose"
            loose.mkdir(mode=0o755)
            os.chmod(str(loose), 0o755)
            env.ensure_private_dir(loose / "child")
            self.assert_mode(loose, 0o755)  # an existing parent is left alone
            self.assert_mode(loose / "child", 0o700)
            env.ensure_private_dir(loose)
            self.assert_mode(loose, 0o700)  # an existing leaf is tightened, never loosened
        finally:
            os.umask(old_umask)

    def test_resources_list_and_read(self) -> None:
        server = self.started()
        listed = server.request("resources/list")["result"]["resources"]
        self.assertEqual(len(listed), 1)
        meta = {
            "ui": {"csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": []}, "prefersBorder": False},
            "openai/ui": {"availableDisplayModes": ["inline", "fullscreen"]},
            "openai/widgetCSP": {"connect_domains": [], "resource_domains": []},
        }
        entry = listed[0]
        self.assertEqual((entry["uri"], entry["mimeType"]), (UI_URI, UI_MIME))
        for key, value in meta.items():
            self.assertEqual(entry["_meta"][key], value, key)
        self.assertTrue(entry["_meta"]["openai/widgetDescription"])
        contents = server.request("resources/read", {"uri": UI_URI})["result"]["contents"]
        self.assertEqual((contents[0]["uri"], contents[0]["mimeType"]), (UI_URI, UI_MIME))
        self.assertEqual(contents[0]["_meta"], entry["_meta"])
        html = contents[0]["text"]
        self.assertIn('{"adapter_version": "0.1.0", "schema_version": "autonom.mcp/v1"}', html)
        self.assertIn('<script type="module">\n' + MINIMAL_CORE, html)
        self.assertNotIn("__WORKSPACE_CONFIG_JSON__", html)
        self.assertNotIn("<!-- WORKSPACE_CORE -->", html)
        for name, page, core in (("missing", None, MINIMAL_CORE),
                                 ("two markers", MINIMAL_PAGE + "<!-- WORKSPACE_CORE -->", MINIMAL_CORE),
                                 ("no config element", MINIMAL_PAGE.replace('id="workspace-config"', ""),
                                  MINIMAL_CORE),
                                 ("closing tag in core", MINIMAL_PAGE, "const x = '</SCRIPT>';\n")):
            ui = self.tmp / ("ui-" + name.replace(" ", "-"))
            ui.mkdir()
            if page is not None:
                (ui / "workspace-dev.html").write_text(page, encoding="utf-8")
            (ui / "workspace-dev-core.mjs").write_text(core, encoding="utf-8")
            broken = self.started(AUTONOM_MCP_UI_DIR=str(ui))
            self.assertEqual(broken.request("resources/read", {"uri": UI_URI})["error"]["code"], -32603, name)

    def test_the_shipped_ui_files_keep_the_markers(self) -> None:
        core = import_server_modules()["core"]
        html = core.assemble_html(PLUGIN / "ui")
        self.assertIn('"schema_version": "autonom.mcp/v1"', html)
        self.assertNotRegex((PLUGIN / "ui" / "workspace-dev-core.mjs").read_text(encoding="utf-8"),
                            r"(?i)</script")

    def test_logs_carry_no_temp_paths(self) -> None:
        server = self.started(FAKE_MCP_CLI_MODE="crash:host_info")
        self.call_ok(server, "list_hosts")
        self.call_error(server, "test_bad_output", {}, "schema_violation", outcome="failed")
        self.assertEqual(server.close(), 0)
        log = server.stderr_text()
        self.assertTrue(log)
        self.assertNotIn(str(self.tmp), log)
        self.assertNotIn(str(self.home), log)



# ====================================================================== 10b. no local paths in results

class LocalPathTests(CoreCase):
    """CLI error text names local files; tool results show labels instead (p3-020)."""

    def corrupt_host_env(self) -> Dict[str, str]:
        state = str(self.state_base)
        return {"FAKE_MCP_CLI_MODE": "fail:host_info:host_identity_invalid",
                "FAKE_MCP_FAIL_ERROR": "host identity file %s/host.json is invalid: not valid JSON" % state,
                "FAKE_MCP_FAIL_HINT": "Move %s/host.json aside (see %s/notes.txt and %s/x)"
                                      % (os.path.realpath(state), self.home, self.xdg)}

    def assert_no_local_paths(self, value: Any) -> None:
        text = json.dumps(value, ensure_ascii=False)
        for path in {str(self.tmp), os.path.realpath(str(self.tmp)), str(self.home), str(self.state_base)}:
            self.assertNotIn(path, text)

    def test_the_scrubber_labels_local_prefixes_only(self) -> None:
        modules = import_server_modules()
        common, env_mod = modules["common"], modules["env"]
        env = {"AUTONOM_HOME": "/srv/st", "XDG_STATE_HOME": "/srv/xdg"}
        scrub = env_mod.path_scrubber(env, Path("/Users/alice"))
        self.assertEqual(scrub.text("/srv/st/host.json bad; /srv/xdg/autonom and /Users/alice/a.txt"),
                         "<state>/host.json bad; <xdg-state>/autonom and ~/a.txt")
        self.assertEqual(scrub.text("/Users/alicebob/x /srv/stx /Users/alice"),
                         "/Users/alicebob/x /srv/stx ~")
        default = env_mod.path_scrubber({}, Path("/Users/alice"))
        self.assertEqual(default.text("/Users/alice/.local/state/autonom/host.json, /Users/alice/.autonom/s"),
                         "<state>/host.json, ~/.autonom/s")
        self.assertEqual(common.PathScrubber([("/", "root")]).text("/etc/x"), "/etc/x")
        nested = scrub.value({"error": "/srv/st/a", "holder": {"label": "/Users/alice/b"},
                              "list": ["/srv/st", 3, None], "n": 7})
        self.assertEqual(nested, {"error": "<state>/a", "holder": {"label": "~/b"},
                                  "list": ["<state>", 3, None], "n": 7})
        common.register_secret("canvas-secret-0987")
        self.assertEqual(scrub.text("x canvas-secret-0987 Bearer abc"), "x <redacted> Bearer <redacted>")
        self.assertEqual(common.PathScrubber([], ["typed-secret"]).text("got typed-secret"), "got <redacted>")
        if os.path.realpath(str(self.tmp)) != str(self.tmp):
            resolved = env_mod.path_scrubber({"AUTONOM_HOME": str(self.state_base)}, self.home)
            self.assertEqual(resolved.text(os.path.realpath(str(self.state_base)) + "/h"), "<state>/h")

    def test_runner_and_refs_clean_cli_error_text(self) -> None:
        modules, _env, runner, refs, _logs = self.in_process(**self.corrupt_host_env())
        result = runner.run("host_info")
        self.assertEqual(result.status, "cli_error")
        self.assertEqual(result.error["error"], "host identity file <state>/host.json is invalid: not valid JSON")
        self.assertEqual(result.error["hint"], "Move <state>/host.json aside (see ~/notes.txt and <xdg-state>/x)")
        error = result.to_error()
        self.assert_no_local_paths([error.error, error.hint, error.extras])
        with self.assertRaises(modules["common"].ToolError) as caught:
            refs.local_host()
        self.assertEqual(caught.exception.error_code, "host_unavailable")
        self.assertTrue(caught.exception.rejected)
        self.assertEqual(caught.exception.extras["cli_error_code"], "host_identity_invalid")
        self.assertIn("<state>/host.json", caught.exception.extras["cli_error"])
        self.assertIn("<state>/host.json", caught.exception.extras["cli_hint"])
        self.assert_no_local_paths([caught.exception.error, caught.exception.hint, caught.exception.extras])
        sensitive = runner.run("host_info", redact_values=["not valid JSON"])
        self.assertEqual(sensitive.error["error"], "host identity file <state>/host.json is invalid: <redacted>")
        unavailable = modules["cli"].CliResult("devices", "unavailable",
                                               reason="no CLI at %s/bin/autonom" % self.home)
        self.assertEqual(runner.scrubbed(unavailable).reason, "no CLI at ~/bin/autonom")

    def test_tool_results_carry_no_local_paths_from_cli_errors(self) -> None:
        server = self.started(**self.corrupt_host_env())
        hosts = self.call_ok(server, "list_hosts")["hosts"]
        self.assertEqual((hosts[0]["state"], hosts[0]["error_code"]), ("error", "host_unavailable"))
        self.assertIn("host_identity_invalid", hosts[0]["error"])
        failed = self.call_error(server, "test_slow_mutation", {"host_id": OTHER_HOST, "session_id": "s_1"},
                                 "host_unavailable")
        self.assertEqual(failed["cli_error_code"], "host_identity_invalid")
        self.assertEqual(failed["cli_error"], "host identity file <state>/host.json is invalid: not valid JSON")
        self.assertEqual(failed["cli_hint"], "Move <state>/host.json aside (see ~/notes.txt and <xdg-state>/x)")
        workspace = self.call_ok(server, "open_workspace")
        for result in (hosts, failed, workspace):
            self.assert_no_local_paths(result)
        raw = b"".join(server.raw_lines).decode("utf-8")
        self.assertIn("host_identity_invalid", raw)
        for path in (str(self.tmp), os.path.realpath(str(self.tmp)), str(self.home)):
            self.assertNotIn(path, raw)
        self.assertEqual(server.close(), 0)
        self.assertNotIn(str(self.home), server.stderr_text())

    def test_the_server_cleans_handler_text_of_failures_and_warnings(self) -> None:
        modules = import_server_modules()
        common = modules["common"]
        server = modules["server"].McpServer(self.server_env(), self.home, io.BytesIO(b""), io.BytesIO(),
                                             io.StringIO())
        self.addCleanup(server.pool.shutdown, True)
        home = str(self.home)

        def failing(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
            ctx.warn("note", "read %s/a.txt" % home)
            raise common.ToolError("write_failed", "could not write %s/out.json" % home,
                                   "Check %s/state" % self.state_base)

        def warning(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
            ctx.warn("note", "read %s/a.txt" % home)
            return {"calls": [{"name": "kept %s" % home, "session_id": "s_1"}]}

        tool = server.tools["test_hooks"]
        result = server.call_tool(tool._replace(handler=failing), {"action": "calls"}, threading.Event())
        structured = result["structuredContent"]
        self.assertEqual(structured["error_code"], "write_failed", structured)
        self.assertEqual(structured["error"], "could not write ~/out.json")
        self.assertEqual(structured["hint"], "Check <state>/state")
        self.assertEqual(structured["warnings"], [{"code": "note", "message": "read ~/a.txt"}])
        self.assertNotIn(home, result["content"][0]["text"])
        result = server.call_tool(tool._replace(handler=warning), {"action": "calls"}, threading.Event())
        structured = result["structuredContent"]
        self.assertTrue(structured["ok"], structured)
        self.assertEqual(structured["warnings"], [{"code": "note", "message": "read ~/a.txt"}])
        # Successful data is the tool's own business; only warnings are cleaned.
        self.assertEqual(structured["calls"][0]["name"], "kept %s" % home)
        self.assertIs(server.services.scrub, server.scrub)


if __name__ == "__main__":
    unittest.main()
