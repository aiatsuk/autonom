"""ChatGPT probe MCP server: protocol, envelope, tools, media child and resources.

Contract sections 1, 2 and 8 (server). The server runs as a real subprocess
over stdio with fakes for everything outside it:

- ``AUTONOM_CLI`` = ``tests/fakes/fake_probe_cli.py`` (``FAKE_PROBE_CLI_MODE``);
- ``AUTONOM_PROBE_NODE`` = this interpreter and ``AUTONOM_PROBE_MEDIA_SCRIPT`` =
  ``tests/fakes/fake_probe_media.py`` (``FAKE_PROBE_MEDIA_MODE``);
- ``AUTONOM_PROBE_UI_DIR`` = a temp dir with a minimal page and core;
- ``AUTONOM_HOME`` and ``HOME`` are temp dirs, and ``media_port`` is 0.

No test touches a device or the real ``~/.autonom`` / ``~/.local/state/autonom``.
"""
from __future__ import annotations

import ast
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

try:
    from env_isolation import EnvSandboxMixin  # discover -s tests / run_parallel
except ImportError:  # python3 -m unittest tests.test_chatgpt_probe_server
    from tests.env_isolation import EnvSandboxMixin

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "integrations" / "chatgpt" / "autonom-chatgpt-probe"
SERVER_DIR = PLUGIN / "server"
SERVER = SERVER_DIR / "probe_server.py"
LAUNCHER = PLUGIN / "bin" / "launch.sh"
FAKE_CLI = ROOT / "tests" / "fakes" / "fake_probe_cli.py"
FAKE_MEDIA = ROOT / "tests" / "fakes" / "fake_probe_media.py"
# The fake media child gets this long to print its ready line, so a slow CI runner
# cannot fail a test that is not about the limit (the product default is 5 s).
MEDIA_READY_TIMEOUT = "60"

SCHEMA = "autonom.chatgpt-probe/v1"
UI_URI = "ui://autonom-probe/probe.html"
UI_MIME = "text/html;profile=mcp-app"
SANDBOX_ORIGIN = "https://web-sandbox.oaiusercontent.com"
TOOL_ORDER = ["open_workspace", "probe_read", "probe_write", "probe_fail", "probe_select",
              "probe_media_frames", "probe_save_results"]
APP_ONLY = ("probe_fail", "probe_select", "probe_media_frames", "probe_save_results")
OP_ID = re.compile(r"^op_[0-9a-f]{16}$")
NONCE = re.compile(r"^probe-[0-9a-f]{4}-(lime|plum|pear|kiwi|fig|sage|mint|teal|rust|jade|onyx|ruby"
                   r"|iris|fern|moss|opal)$")
MISSING = object()

MINIMAL_PAGE = (
    "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
    "<script type=\"application/json\" id=\"probe-config\">__PROBE_CONFIG_JSON__</script>\n"
    "</head><body><main id=\"probe\"></main>\n"
    "<!-- PROBE_CORE -->\n"
    "<script type=\"module\">document.title = \"probe\";</script>\n</body></html>\n"
)
MINIMAL_CORE = "export const CORE_MARK = \"probe-core-test\";\nglobalThis.AutonomProbeCore = { CORE_MARK };\n"


def import_server_modules():
    """Import the server modules in process without writing bytecode into the plugin."""
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    if str(SERVER_DIR) not in sys.path:
        sys.path.insert(0, str(SERVER_DIR))
    try:
        import probe_env
        import probe_server
        import probe_state
        import probe_tools
    finally:
        sys.dont_write_bytecode = previous
    return probe_env, probe_state, probe_tools, probe_server


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_dead(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)


def command_of(pid: int) -> str:
    out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, check=False)
    return out.stdout.strip()


class ProbeProcess:
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

    def request(self, method: str, params: Any = MISSING, timeout: float = 15.0) -> Dict[str, Any]:
        msg_id = self.next_id
        self.next_id += 1
        message: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not MISSING:
            message["params"] = params
        self.send(message)
        deadline = time.monotonic() + timeout
        while True:
            reply = self.messages.get(timeout=max(0.1, deadline - time.monotonic()))
            if reply is None:
                raise AssertionError("server closed stdout while waiting for %s" % method)
            if isinstance(reply, dict) and reply.get("id") == msg_id:
                return reply

    def initialize(self, version: Any = "2025-11-25", capabilities: Optional[Dict[str, Any]] = None,
                   client: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"capabilities": capabilities or {},
                                  "clientInfo": client or {"name": "probe-test", "version": "1.0"}}
        if version is not MISSING:
            params["protocolVersion"] = version
        reply = self.request("initialize", params)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return reply

    def call(self, name: str, arguments: Any = MISSING, timeout: float = 20.0) -> Dict[str, Any]:
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

    def close(self, timeout: float = 5.0) -> Optional[int]:
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
        self.close(timeout=5.0)
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        self.reader.join(timeout=2)
        self.stderr_file.close()


class ProbeServerCase(EnvSandboxMixin, unittest.TestCase):
    """Sandboxed state, fakes and helpers shared by the server tests."""

    maxDiff = None

    def setUp(self) -> None:
        self.state_root = self.sandbox_home()
        tmp = Path(tempfile.mkdtemp(prefix="probe-server-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.tmp = tmp
        self.home = tmp / "home"
        self.home.mkdir()
        self.ui_dir = tmp / "ui"
        self.ui_dir.mkdir()
        (self.ui_dir / "probe-page.html").write_text(MINIMAL_PAGE, encoding="utf-8")
        (self.ui_dir / "probe-core.mjs").write_text(MINIMAL_CORE, encoding="utf-8")
        self.cli_log = tmp / "cli.log"
        self.media_log = tmp / "media.log"
        self.write_config({"media_port": 0})
        self.addCleanup(self._kill_leftover_media)

    # -- environment

    @property
    def probe_dir(self) -> Path:
        return self.state_root / "chatgpt-probe"

    def write_config(self, config: Any, raw: Optional[str] = None) -> None:
        self.probe_dir.mkdir(parents=True, exist_ok=True)
        text = raw if raw is not None else json.dumps(config)
        (self.probe_dir / "config.json").write_text(text, encoding="utf-8")

    def server_env(self, **overrides: Optional[str]) -> Dict[str, str]:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AUTONOM_", "FAKE_PROBE_", "PYTHON")) and key != "XDG_STATE_HOME"}
        real_home = os.path.expanduser("~")
        env["PATH"] = os.pathsep.join(
            part for part in os.environ.get("PATH", "").split(os.pathsep)
            if part and not part.startswith(os.path.join(real_home, ".local")))
        env.update({
            "HOME": str(self.home),
            "AUTONOM_HOME": str(self.state_root),
            "AUTONOM_CLI": str(FAKE_CLI),
            "AUTONOM_PROBE_NODE": sys.executable,
            "AUTONOM_PROBE_MEDIA_SCRIPT": str(FAKE_MEDIA),
            "AUTONOM_PROBE_MEDIA_READY_TIMEOUT": MEDIA_READY_TIMEOUT,
            "AUTONOM_PROBE_UI_DIR": str(self.ui_dir),
            "AUTONOM_ROOT": str(ROOT),
            "FAKE_PROBE_CLI_LOG": str(self.cli_log),
            "FAKE_PROBE_MEDIA_LOG": str(self.media_log),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def start(self, argv: Optional[List[str]] = None, **overrides: Optional[str]) -> ProbeProcess:
        probe = ProbeProcess(argv or [sys.executable, str(SERVER)], self.server_env(**overrides))
        self.addCleanup(self._finish, probe)
        return probe

    def started(self, **overrides: Optional[str]) -> ProbeProcess:
        probe = self.start(**overrides)
        reply = probe.initialize()
        self.assertIn("result", reply)
        return probe

    def _finish(self, probe: ProbeProcess) -> None:
        probe.kill()
        for line in probe.raw_lines:
            message = json.loads(line.decode("utf-8"))
            self.assertEqual(message.get("jsonrpc"), "2.0", line)

    def media_starts(self) -> List[Dict[str, Any]]:
        if not self.media_log.exists():
            return []
        return [json.loads(line) for line in self.media_log.read_text().splitlines() if line.strip()]

    def cli_calls(self) -> List[Dict[str, Any]]:
        if not self.cli_log.exists():
            return []
        return [json.loads(line) for line in self.cli_log.read_text().splitlines() if line.strip()]

    def _kill_leftover_media(self) -> None:
        for start in self.media_starts():
            pid = start["pid"]
            if pid_alive(pid) and "fake_probe_media" in command_of(pid):
                os.kill(pid, signal.SIGKILL)

    # -- assertions

    def check_envelope(self, result: Dict[str, Any]) -> Dict[str, Any]:
        structured = result["structuredContent"]
        self.assertEqual(structured["schema_version"], SCHEMA)
        self.assertRegex(structured["operation_id"], OP_ID)
        self.assertIsInstance(structured["ok"], bool)
        self.assertIs(result["isError"], not structured["ok"])
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(result["content"][0]["text"],
                         json.dumps(structured, ensure_ascii=False, sort_keys=True))
        if not structured["ok"]:
            for key in ("error_code", "error", "hint"):
                self.assertIsInstance(structured[key], str, key)
                self.assertTrue(structured[key], key)
            self.assertNotIn("_meta", result)
        return structured

    def call_ok(self, probe: ProbeProcess, name: str, arguments: Any = MISSING) -> Dict[str, Any]:
        result = probe.call(name, arguments)
        structured = self.check_envelope(result)
        self.assertTrue(structured["ok"], structured)
        return result

    def call_error(self, probe: ProbeProcess, name: str, arguments: Any, code: str) -> Dict[str, Any]:
        result = probe.call(name, arguments)
        structured = self.check_envelope(result)
        self.assertFalse(structured["ok"], structured)
        self.assertEqual(structured["error_code"], code, structured)
        return structured

    def assert_mode(self, path: Path, mode: int) -> None:
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode, str(path))


# ====================================================================== protocol

class ProtocolTests(ProbeServerCase):
    def test_initialize_echoes_each_supported_version_and_defaults_otherwise(self) -> None:
        cases = [("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"),
                 ("2025-03-26", "2025-03-26"), ("2024-11-05", "2024-11-05"),
                 ("1999-01-01", "2025-11-25"), (MISSING, "2025-11-25"), (7, "2025-11-25")]
        for requested, answered in cases:
            with self.subTest(requested=requested):
                probe = self.start()
                result = probe.initialize(version=requested)["result"]
                self.assertEqual(result["protocolVersion"], answered)
                self.assertEqual(result["serverInfo"], {"name": "autonom-chatgpt-probe",
                                                        "title": "Autonom probe", "version": "0.1.0"})
                caps = result["capabilities"]
                self.assertEqual(caps["tools"], {"listChanged": False})
                self.assertEqual(caps["resources"], {"listChanged": False, "subscribe": False})
                ui = {"io.modelcontextprotocol/ui": {"mimeTypes": [UI_MIME]}}
                self.assertEqual(caps["extensions"], ui)
                self.assertEqual(caps["experimental"], ui)
                self.assertIn("probe_read", result["instructions"])
                self.assertEqual(probe.close(), 0)

    def test_negotiation_and_faults_in_order_and_methods_seen(self) -> None:
        # CHP-HOST-002-S01
        probe = self.start()
        self.assertEqual(probe.initialize(version="2025-06-18")["result"]["protocolVersion"], "2025-06-18")
        probe.send([{"jsonrpc": "2.0", "id": 50, "method": "ping"}])
        probe.send_raw(b"{not json\n")
        probe.send({"jsonrpc": "2.0", "id": 51, "method": "server/discover"})
        probe.send({"jsonrpc": "2.0", "id": 52, "method": "tools/call", "params": {"name": "nope"}})
        replies = [probe.recv() for _ in range(4)]
        self.assertEqual([r["error"]["code"] for r in replies], [-32600, -32700, -32601, -32602])
        self.assertEqual([r["id"] for r in replies], [None, None, 51, 52])
        self.assertEqual(replies[1]["error"]["message"], "Parse error")
        self.assertEqual(replies[2]["error"]["data"], {"method": "server/discover"})
        self.assertEqual(replies[3]["error"]["message"], "Unknown tool: nope")
        server = self.call_ok(probe, "probe_read")["structuredContent"]["server"]
        self.assertEqual(server["methods_seen"][:4],
                         ["initialize", "notifications/initialized", "server/discover", "tools/call"])
        self.assertEqual(server["protocol_version"], "2025-06-18")
        self.assertEqual(server["requested_protocol_version"], "2025-06-18")

    def test_methods_seen_is_capped_and_truncated(self) -> None:
        probe = self.started()
        probe.request("x" * 200)
        for index in range(60):
            probe.request("unknown/%d" % index)
        seen = self.call_ok(probe, "probe_read")["structuredContent"]["server"]["methods_seen"]
        self.assertEqual(len(seen), 50)
        self.assertIn("x" * 80, seen)
        self.assertTrue(all(len(name) <= 80 for name in seen))

    def test_client_ui_extension_is_recorded_from_extensions_or_legacy_experimental(self) -> None:
        mimes = {"mimeTypes": [UI_MIME]}
        for location in ("extensions", "experimental"):
            with self.subTest(location=location):
                probe = self.start()
                probe.initialize(capabilities={location: {"io.modelcontextprotocol/ui": mimes}},
                                 client={"name": "n" * 100, "version": "v1"})
                server = self.call_ok(probe, "probe_read")["structuredContent"]["server"]
                self.assertIs(server["client_ui_extension"], True)
                self.assertEqual(server["client_ui_extension_location"], location)
                self.assertEqual(server["client_ui_mime_types"], [UI_MIME])
                self.assertEqual(server["client"], {"name": "n" * 80, "version": "v1"})
        probe = self.started()
        server = self.call_ok(probe, "probe_read")["structuredContent"]["server"]
        self.assertIs(server["client_ui_extension"], False)
        self.assertIsNone(server["client_ui_extension_location"])
        self.assertEqual(server["client_ui_mime_types"], [])

    def test_requests_before_initialize_are_refused_but_ping_works(self) -> None:
        probe = self.start()
        self.assertEqual(probe.request("ping"), {"jsonrpc": "2.0", "id": 1, "result": {}})
        for method, params in (("tools/list", {}), ("tools/call", {"name": "probe_read"}),
                               ("resources/list", {}), ("resources/read", {"uri": UI_URI}),
                               ("resources/templates/list", {})):
            with self.subTest(method=method):
                error = probe.request(method, params)["error"]
                self.assertEqual(error, {"code": -32600, "message": "Server not initialized"})
        probe.initialize()
        self.assertEqual(probe.request("resources/templates/list")["result"], {"resourceTemplates": []})
        self.assertEqual(probe.request("ping")["result"], {})

    def test_notifications_and_client_responses_are_never_answered(self) -> None:
        probe = self.started()
        probe.send({"jsonrpc": "2.0", "method": "notifications/unknown", "params": {"x": 1}})
        probe.send({"jsonrpc": "2.0", "method": "tools/list"})
        probe.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}})
        probe.send({"jsonrpc": "2.0", "id": 900, "result": {}})
        probe.send({"jsonrpc": "2.0", "id": 901, "error": {"code": 1, "message": "x"}})
        probe.send({"jsonrpc": "2.0", "id": 77, "method": "ping"})
        self.assertEqual(probe.recv(), {"jsonrpc": "2.0", "id": 77, "result": {}})
        self.assertTrue(probe.messages.empty())

    def test_parse_errors_bad_json_bad_utf8_and_oversized_line(self) -> None:
        probe = self.started()
        for raw in (b"{\"jsonrpc\": \"2.0\", \"id\": 1,\n", b"\xff\xfe\xfd\n"):
            probe.send_raw(raw)
            self.assertEqual(probe.recv(), {"jsonrpc": "2.0", "id": None,
                                            "error": {"code": -32700, "message": "Parse error"}})
        big = b"{\"jsonrpc\":\"2.0\",\"id\":5,\"method\":\"ping\",\"pad\":\"" + b"a" * (4 * 1024 * 1024) + b"\"}\n"
        writer = threading.Thread(target=probe.send_raw, args=(big,))
        writer.start()
        self.assertEqual(probe.recv(timeout=30), {"jsonrpc": "2.0", "id": None,
                                                  "error": {"code": -32700, "message": "Parse error"}})
        writer.join(timeout=30)
        self.assertEqual(probe.request("ping")["result"], {})

    def test_invalid_requests(self) -> None:
        probe = self.started()
        cases = [
            ([{"jsonrpc": "2.0", "id": 1, "method": "ping"}], None),
            ({"id": 7, "method": "ping"}, 7),
            ({"jsonrpc": "1.0", "id": "s1", "method": "ping"}, "s1"),
            ({"jsonrpc": "2.0", "id": "s2", "method": 5}, "s2"),
            ({"jsonrpc": "2.0", "id": 1.5, "method": "ping"}, None),
            ({"jsonrpc": "2.0", "id": None, "method": "ping"}, None),
            ({"jsonrpc": "2.0", "id": True, "method": "ping"}, None),
            ("just a string", None),
            (42, None),
        ]
        for message, expected_id in cases:
            with self.subTest(message=message):
                probe.send(message)
                reply = probe.recv()
                self.assertEqual(reply, {"jsonrpc": "2.0", "id": expected_id,
                                         "error": {"code": -32600, "message": "Invalid Request"}})

    def test_unknown_method_tool_resource_and_invalid_params(self) -> None:
        probe = self.started()
        self.assertEqual(probe.request("frobnicate")["error"],
                         {"code": -32601, "message": "Method not found", "data": {"method": "frobnicate"}})
        self.assertEqual(probe.request("tools/call", {"name": "nope", "arguments": {}})["error"],
                         {"code": -32602, "message": "Unknown tool: nope"})
        for method, params in (("tools/call", MISSING), ("tools/call", []), ("tools/call", {"name": 3}),
                               ("tools/call", {"arguments": {}}), ("resources/read", MISSING),
                               ("resources/read", {"uri": 5}), ("resources/read", "x")):
            with self.subTest(method=method, params=params):
                self.assertEqual(probe.request(method, params)["error"],
                                 {"code": -32602, "message": "Invalid params"})
        self.assertEqual(probe.request("resources/read", {"uri": "ui://other/x.html"})["error"],
                         {"code": -32002, "message": "Resource not found", "data": {"uri": "ui://other/x.html"}})

    def test_non_ascii_round_trip_under_the_c_locale(self) -> None:
        probe = self.start(LC_ALL="C", LANG=None, LC_CTYPE=None, PYTHONIOENCODING=None, PYTHONUTF8=None)
        probe.initialize()
        text = "caf\u00e9 \u2713 \u65e5\u672c \U0001f600"
        written = self.call_ok(probe, "probe_write", {"text": text, "expected_revision": 0})
        self.assertEqual(written["structuredContent"]["note"]["text"], text)
        read = self.call_ok(probe, "probe_read")["structuredContent"]
        self.assertEqual(read["note"]["text"], text)
        self.assertTrue(any(text.encode("utf-8") in line for line in probe.raw_lines))
        self.assertEqual(json.loads((self.probe_dir / "note.json").read_text("utf-8"))["text"], text)

    def test_stdout_carries_only_single_line_json(self) -> None:
        probe = self.started()
        self.call_ok(probe, "open_workspace")
        self.call_ok(probe, "probe_read")
        probe.request("resources/read", {"uri": UI_URI})
        self.assertEqual(probe.close(), 0)
        probe.reader.join(timeout=5)
        self.assertTrue(probe.raw_lines)
        for line in probe.raw_lines:
            self.assertTrue(line.endswith(b"\n"))
            self.assertNotIn(b"\n", line[:-1])
            self.assertEqual(json.loads(line.decode("utf-8"))["jsonrpc"], "2.0")


class InProcessServerTests(EnvSandboxMixin, unittest.TestCase):
    """Paths that are hard to provoke over stdio, driven in process."""

    def setUp(self) -> None:
        self.state_root = self.sandbox_home()
        _env, _state, _tools, self.server_mod = import_server_modules()

    def make(self, lines: List[bytes]):
        import io
        stdout = io.BytesIO()
        stderr = io.StringIO()
        server = self.server_mod.ProbeServer({"AUTONOM_HOME": str(self.state_root), "PATH": "/usr/bin:/bin"},
                                             self.state_root, io.BytesIO(b"".join(lines)), stdout, stderr)
        return server, stdout, stderr

    def test_uncaught_handler_exception_is_internal_error_with_traceback_on_stderr(self) -> None:
        init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode() + b"\n"
        call = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "probe_fail"}}).encode() + b"\n"
        server, stdout, stderr = self.make([init, call])

        def boom(name: str, arguments: Any) -> Dict[str, Any]:
            raise RuntimeError("multi\nline failure")

        server.tools.call = boom
        server.serve()
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(replies[1], {"jsonrpc": "2.0", "id": 2,
                                      "error": {"code": -32603, "message": "Internal error: RuntimeError"}})
        self.assertIn("Traceback", stderr.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue().decode("utf-8"))

    def test_line_of_exactly_the_limit_is_read_and_one_byte_more_is_discarded(self) -> None:
        limit = self.server_mod.MAX_LINE
        server, _out, _err = self.make([b"a" * limit + b"\n", b"b" * (limit + 1) + b"\n", b"tail"])
        self.assertEqual(len(server.read_line()), limit)
        self.assertIs(server.read_line(), self.server_mod._OVERSIZED)
        self.assertEqual(server.read_line(), b"tail")
        self.assertIsNone(server.read_line())


# ====================================================================== tools/list

class ToolsListTests(ProbeServerCase):
    def test_seven_tools_in_order_with_schemas_and_meta(self) -> None:
        probe = self.started()
        tools = probe.request("tools/list")["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], TOOL_ORDER)
        output_schema = {
            "type": "object", "required": ["schema_version", "operation_id", "ok"],
            "properties": {"schema_version": {"const": SCHEMA},
                           "operation_id": {"type": "string", "pattern": "^op_[0-9a-f]{16}$"},
                           "ok": {"type": "boolean"}, "error_code": {"type": "string"},
                           "error": {"type": "string"}, "hint": {"type": "string"}},
            "additionalProperties": True}
        by_name = {tool["name"]: tool for tool in tools}
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                for key in ("name", "title", "description", "inputSchema", "outputSchema", "annotations", "_meta"):
                    self.assertIn(key, tool)
                self.assertEqual(tool["inputSchema"]["type"], "object")
                self.assertIs(tool["inputSchema"]["additionalProperties"], False)
                self.assertEqual(tool["outputSchema"], output_schema)
                self.assertIs(tool["_meta"]["openai/widgetAccessible"], True)
                if tool["name"] in APP_ONLY:
                    self.assertEqual(tool["_meta"]["ui"]["visibility"], ["app"])
                    self.assertEqual(tool["_meta"]["openai/visibility"], "private")
                else:
                    self.assertNotIn("openai/visibility", tool["_meta"])
                if tool["name"] != "open_workspace":
                    self.assertNotIn("resourceUri", tool["_meta"].get("ui", {}))
        opener = by_name["open_workspace"]
        self.assertEqual(opener["title"], "Open Autonom probe")
        self.assertEqual(opener["inputSchema"], {"type": "object", "properties": {}, "additionalProperties": False})
        self.assertEqual(opener["_meta"]["ui"], {"resourceUri": UI_URI})
        self.assertEqual(opener["_meta"]["openai/outputTemplate"], UI_URI)
        self.assertEqual(opener["_meta"]["openai/ui"], {"entrypoints": [{"type": "global"}, {"type": "thread"}]})
        self.assertEqual(opener["annotations"], {"readOnlyHint": False, "idempotentHint": True,
                                                 "openWorldHint": False})
        self.assertEqual(by_name["probe_read"]["title"], "Read Autonom probe status")
        self.assertEqual(by_name["probe_read"]["annotations"], {"readOnlyHint": True, "openWorldHint": False})
        self.assertEqual(by_name["probe_write"]["title"], "Write the Autonom probe note")
        self.assertEqual(by_name["probe_write"]["annotations"],
                         {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                          "openWorldHint": False})
        self.assertEqual(by_name["probe_write"]["inputSchema"]["required"], ["text", "expected_revision"])
        self.assertEqual(by_name["probe_select"]["inputSchema"]["required"], ["target_ref", "expected_revision"])
        frames = by_name["probe_media_frames"]["inputSchema"]["properties"]
        self.assertEqual((frames["count"]["minimum"], frames["count"]["maximum"], frames["count"]["default"]),
                         (1, 30, 10))
        self.assertEqual((frames["after_seq"]["minimum"], frames["after_seq"]["default"]), (-1, -1))


# ====================================================================== arguments

class ArgumentTests(ProbeServerCase):
    def test_tools_without_required_arguments_accept_empty_and_missing_arguments(self) -> None:
        probe = self.started()
        for name in ("probe_read", "probe_fail", "probe_media_frames", "open_workspace"):
            for arguments in ({}, MISSING, None):
                with self.subTest(name=name, arguments=arguments):
                    structured = self.check_envelope(probe.call(name, arguments))
                    self.assertNotEqual(structured.get("error_code"), "invalid_arguments", structured)

    def test_invalid_arguments_are_tool_errors_naming_the_field(self) -> None:
        probe = self.started()
        cases = [
            ("open_workspace", {"x": 1}, "x", "unknown argument"),
            ("probe_read", {"verbose": True}, "verbose", "unknown argument"),
            ("probe_fail", {"a": 1}, "a", "unknown argument"),
            ("probe_read", [], "arguments", "must be an object"),
            ("probe_write", {}, "text", "required"),
            ("probe_write", MISSING, "text", "required"),
            ("probe_write", {"text": "x"}, "expected_revision", "required"),
            ("probe_write", {"text": 5, "expected_revision": 0}, "text", "must be a string"),
            ("probe_write", {"text": "", "expected_revision": 0}, "text", "must be 1-500 characters"),
            ("probe_write", {"text": "x" * 501, "expected_revision": 0}, "text", "must be 1-500 characters"),
            ("probe_write", {"text": "a\nb", "expected_revision": 0}, "text",
             "must not contain control characters"),
            ("probe_write", {"text": "a\u007fb", "expected_revision": 0}, "text",
             "must not contain control characters"),
            ("probe_write", {"text": "x", "expected_revision": True}, "expected_revision", "must be an integer"),
            ("probe_write", {"text": "x", "expected_revision": "1"}, "expected_revision", "must be an integer"),
            ("probe_write", {"text": "x", "expected_revision": 1.0}, "expected_revision", "must be an integer"),
            ("probe_write", {"text": "x", "expected_revision": -1}, "expected_revision", "must be >= 0"),
            ("probe_write", {"text": "x", "expected_revision": 0, "selection_revision": -1},
             "selection_revision", "must be >= 0"),
            ("probe_write", {"text": "x", "expected_revision": 0, "extra": 1}, "extra", "unknown argument"),
            ("probe_select", {}, "target_ref", "required"),
            ("probe_select", {"target_ref": "bad ref", "expected_revision": 0}, "target_ref", None),
            ("probe_select", {"target_ref": "desktop~x", "expected_revision": 0}, "target_ref", None),
            ("probe_select", {"target_ref": "android~", "expected_revision": 0}, "target_ref", None),
            ("probe_select", {"target_ref": "android~emulator-5584"}, "expected_revision", "required"),
            ("probe_media_frames", {"count": 0}, "count", "must be 1-30"),
            ("probe_media_frames", {"count": 31}, "count", "must be 1-30"),
            ("probe_media_frames", {"count": "5"}, "count", "must be an integer"),
            ("probe_media_frames", {"after_seq": -2}, "after_seq", "must be >= -1"),
            ("probe_save_results", {}, "results", "required"),
            ("probe_save_results", {"results": []}, "results", "must be an object"),
        ]
        for name, arguments, field, problem in cases:
            with self.subTest(name=name, arguments=arguments):
                structured = self.call_error(probe, name, arguments, "invalid_arguments")
                self.assertEqual(structured["field"], field)
                if problem is not None:
                    self.assertEqual(structured["problem"], problem)
                self.assertEqual(structured["hint"], "Fix the named argument and call again.")
        self.assertFalse((self.probe_dir / "note.json").exists())


# ====================================================================== probe_read

class ProbeReadTests(ProbeServerCase):
    def test_success_fields(self) -> None:
        probe = self.started()
        structured = self.call_ok(probe, "probe_read")["structuredContent"]
        self.assertEqual(set(structured["host"]), {"hostname", "platform", "os_release", "machine"})
        self.assertEqual(structured["host"]["platform"], sys.platform)
        server = structured["server"]
        self.assertEqual(server["probe_version"], "0.1.0")
        self.assertEqual(server["pid"], probe.proc.pid)
        self.assertRegex(server["python"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(server["protocol_version"], "2025-11-25")
        self.assertEqual(server["client"], {"name": "probe-test", "version": "1.0"})
        self.assertIsNone(server["config_error"])
        self.assertEqual(structured["autonom"], {"version": "0.35.1", "cli": "autonom",
                                                 "cli_python": "%d.%d" % sys.version_info[:2]})
        self.assertEqual(structured["devices"], [
            {"platform": "android", "target_id": "emulator-5584", "state": "device",
             "name": "sdk_gphone64_arm64", "avd": "Autonom_Split_API36"},
            {"platform": "ios", "target_id": "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", "state": "booted",
             "name": "Autonom-Split-Test"}])
        self.assertEqual(structured["devices_warnings"], 1)
        self.assertEqual(structured["media"], {"state": "stopped", "port": 0, "clients": None,
                                               "refused_origins": [], "error_code": None})
        self.assertEqual(structured["note"], {"revision": 0, "text": "", "updated_at": None})
        self.assertEqual(structured["selection"], {"target_ref": None, "revision": 0})
        self.assertNotIn(str(FAKE_CLI), json.dumps(structured))
        calls = self.cli_calls()
        self.assertEqual([call["argv"] for call in calls], [["version"], ["devices", "list"]])

    def test_media_status_and_refused_origins_come_from_the_child(self) -> None:
        probe = self.started()
        port = self.call_ok(probe, "open_workspace")["structuredContent"]["media"]["port"]
        media = self.call_ok(probe, "probe_read")["structuredContent"]["media"]
        self.assertEqual(media, {"state": "running", "port": port, "clients": 1, "error_code": None,
                                 "refused_origins": [{"origin": "https://x.example", "count": 2,
                                                      "last_at": "2026-10-10T14:22:33Z"}]})

    def test_explicit_missing_cli_does_not_fall_through_to_the_home_launcher(self) -> None:
        # CHP-HOST-003-S01, with a valid launcher at <home>/.local/bin/autonom.
        self.make_copy_launcher()
        probe = self.started(AUTONOM_CLI=str(self.tmp / "missing" / "autonom"))
        structured = self.call_error(probe, "probe_read", {}, "autonom_cli_not_found")
        self.assertEqual(structured["source"], "env")
        self.assertIn("install_cli.sh", structured["hint"])
        self.assertEqual(structured["host"]["platform"], sys.platform)
        self.assertEqual(structured["server"]["probe_version"], "0.1.0")
        self.assertEqual(self.cli_calls(), [])
        self.assertEqual(probe.request("ping")["result"], {})
        self.call_error(probe, "probe_read", {}, "autonom_cli_not_found")

    def test_config_cli_that_is_not_autonom_is_refused_with_source_config(self) -> None:
        stray = self.tmp / "autonom"
        stray.write_text("#!/bin/sh\necho hi\n")
        self.write_config({"media_port": 0, "autonom_cli": str(stray)})
        probe = self.started(AUTONOM_CLI=None)
        structured = self.call_error(probe, "probe_read", {}, "autonom_cli_not_found")
        self.assertEqual(structured["source"], "config")
        self.assertEqual(structured["found"], "autonom")

    def make_copy_launcher(self) -> Path:
        launcher = self.home / ".local" / "bin" / "autonom"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/usr/bin/env bash\n# autonom launcher (copy mode) -> %s\n"
                            "exec python3 \"%s\" \"$@\"\n" % (FAKE_CLI, FAKE_CLI))
        launcher.chmod(0o755)
        return launcher

    def test_copy_mode_launcher_resolves_to_its_python_target(self) -> None:
        self.make_copy_launcher()
        probe = self.started(AUTONOM_CLI=None)
        structured = self.call_ok(probe, "probe_read")["structuredContent"]
        self.assertEqual(structured["autonom"]["version"], "0.35.1")
        self.assertEqual(len(self.cli_calls()), 2)

    def test_timeout_kills_the_cli_and_reports_cli_timeout(self) -> None:
        # CHP-HOST-004-S01
        probe = self.started(FAKE_PROBE_CLI_MODE="slow", AUTONOM_PROBE_CLI_TIMEOUT="1")
        started = time.monotonic()
        structured = self.call_error(probe, "probe_read", {}, "cli_timeout")
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(structured["timeout_s"], 1)
        self.assertEqual(structured["command"], ["autonom", "version"])
        self.assertIn("autonom doctor", structured["hint"])
        self.assertIn("host", structured)
        self.assertIn("server", structured)
        calls = self.cli_calls()
        self.assertEqual(len(calls), 1)
        self.assertTrue(wait_dead(calls[0]["pid"], 2.0), "the timed-out fake CLI is still running")

    def test_garbage_output_is_cli_failed(self) -> None:
        probe = self.started(FAKE_PROBE_CLI_MODE="garbage")
        structured = self.call_error(probe, "probe_read", {}, "cli_failed")
        self.assertEqual(structured["command"], ["autonom", "version"])
        self.assertEqual(structured["exit_code"], 0)
        self.assertNotIn("cli_error_code", structured)

    def test_cli_error_passes_its_code_and_hint_through(self) -> None:
        probe = self.started(FAKE_PROBE_CLI_MODE="error")
        structured = self.call_error(probe, "probe_read", {}, "cli_failed")
        self.assertEqual(structured["cli_error_code"], "adb_not_found")
        self.assertEqual(structured["cli_error"], "adb was not found")
        self.assertEqual(structured["cli_hint"], "Install Android platform-tools.")
        self.assertEqual(structured["hint"], "Install Android platform-tools.")
        self.assertEqual(structured["exit_code"], 1)

    def test_config_errors_surface_as_config_error_and_never_crash(self) -> None:
        self.write_config(None, raw="{not json")
        probe = self.started()
        server = self.call_ok(probe, "probe_read")["structuredContent"]["server"]
        self.assertIn("not valid JSON", server["config_error"])
        meta = probe.request("resources/list")["result"]["resources"][0]["_meta"]
        self.assertEqual(meta["ui"]["csp"]["connectDomains"][0], "http://127.0.0.1:47811")
        probe.close()

        self.write_config({"media_port": "x", "cli_timeout_s": 0,
                           "remote_routes": [{"label": "ok", "origin": "http://127.0.0.1:47901"},
                                             {"label": "bad", "origin": "ftp://x"}, "nope"],
                           "allow_origins": ["https://Upper.example", "https://*.a.example"],
                           "allowed_devices": ["android:emulator-5554", "android:emulator-5584", "pc:1"],
                           "node": "relative/node"})
        probe = self.started()
        error = self.call_ok(probe, "probe_read")["structuredContent"]["server"]["config_error"]
        for fragment in ("media_port", "cli_timeout_s", "remote_routes[1]", "remote_routes[2]",
                         "allow_origins[0]", "allowed_devices[0]", "allowed_devices[2]", "node"):
            self.assertIn(fragment, error)
        domains = probe.request("resources/list")["result"]["resources"][0]["_meta"]["ui"]["csp"]["connectDomains"]
        self.assertIn("http://127.0.0.1:47901", domains)
        self.assertNotIn("ftp://x", domains)


class CliPythonTests(ProbeServerCase):
    def fake_interpreter(self, version: str) -> Path:
        fake = self.tmp / "fakepython"
        fake.write_text("#!/bin/sh\necho %s\n" % version)
        fake.chmod(0o755)
        return fake

    def test_resolver_refuses_an_explicit_interpreter_older_than_3_11(self) -> None:
        probe_env, _state, _tools, _server = import_server_modules()
        fake = self.fake_interpreter("3.9")
        resolver = probe_env.Resolver({"AUTONOM_PROBE_CLI_PYTHON": str(fake), "PATH": "/usr/bin:/bin"},
                                      self.home, probe_env.default_config(), version_info=(3, 9, 6))
        with self.assertRaises(probe_env.ProbeError) as caught:
            resolver.resolve_cli_python()
        self.assertEqual(caught.exception.error_code, "python_too_old")
        self.assertEqual(caught.exception.extras, {"found": ["3.9"], "source": "env"})
        self.assertIn("3.11", caught.exception.hint)
        good = probe_env.Resolver({"AUTONOM_PROBE_CLI_PYTHON": sys.executable, "PATH": "/usr/bin:/bin"},
                                  self.home, probe_env.default_config(), version_info=(3, 9, 6))
        self.assertEqual(good.resolve_cli_python(), (sys.executable, "%d.%d" % sys.version_info[:2]))
        current = probe_env.Resolver({}, self.home, probe_env.default_config(), version_info=(3, 12, 0),
                                     executable="/x/python3.12")
        self.assertEqual(current.resolve_cli_python(), ("/x/python3.12", "3.12"))

    def test_resolver_explicit_sources_do_not_fall_through(self) -> None:
        probe_env, _state, _tools, _server = import_server_modules()
        config = dict(probe_env.default_config(), node="/nonexistent/node", autonom_root="/nonexistent")
        resolver = probe_env.Resolver({"PATH": "/usr/bin:/bin"}, self.home, config,
                                      which=lambda name, path: "/usr/bin/true")
        for method, code in ((resolver.resolve_node, "node_not_found"),
                             (resolver.resolve_autonom_root, "autonom_root_not_found")):
            with self.assertRaises(probe_env.ProbeError) as caught:
                method()
            self.assertEqual(caught.exception.error_code, code)
            self.assertEqual(caught.exception.extras.get("source"), "config")
        missing = probe_env.Resolver({"PATH": "/usr/bin:/bin"}, self.home, probe_env.default_config(),
                                     which=lambda name, path: None)
        with self.assertRaises(probe_env.ProbeError) as caught:
            missing.resolve_node()
        self.assertEqual(caught.exception.error_code, "node_not_found")
        with self.assertRaises(probe_env.ProbeError) as caught:
            missing.resolve_cli()
        self.assertEqual(caught.exception.error_code, "autonom_cli_not_found")
        self.assertIsNone(missing.resolve_adb())

    def test_state_and_path_helpers(self) -> None:
        probe_env, _state, _tools, _server = import_server_modules()
        home = Path("/h")
        self.assertEqual(probe_env.state_dir({"AUTONOM_HOME": "/a"}, home), Path("/a/chatgpt-probe"))
        self.assertEqual(probe_env.state_dir({"XDG_STATE_HOME": "/x"}, home), Path("/x/autonom/chatgpt-probe"))
        self.assertEqual(probe_env.state_dir({}, home), Path("/h/.local/state/autonom/chatgpt-probe"))
        self.assertEqual(probe_env.augmented_path({"PATH": "/usr/bin:/opt/x:/usr/bin"}, home),
                         "/usr/bin:/opt/x:/opt/homebrew/bin:/usr/local/bin:/h/.local/bin:/bin")

    def system_python(self) -> Optional[str]:
        candidate = "/usr/bin/python3"
        if not os.path.exists(candidate):
            return None
        out = subprocess.run([candidate, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
        if out.returncode != 0 or out.stdout.strip() not in ("3.9", "3.10"):
            return None
        return candidate

    def test_python_too_old_end_to_end_under_the_system_python(self) -> None:
        old = self.system_python()
        if old is None:
            self.skipTest("/usr/bin/python3 is absent or not 3.9/3.10")
        fake = self.fake_interpreter("3.9")
        probe = self.start(argv=[old, str(SERVER)], AUTONOM_PROBE_CLI_PYTHON=str(fake))
        probe.initialize()
        structured = self.call_error(probe, "probe_read", {}, "python_too_old")
        self.assertEqual(structured["found"], ["3.9"])
        self.assertEqual(structured["source"], "env")
        self.assertRegex(structured["server"]["python"], r"^3\.(9|10)\.")

    def test_every_tool_runs_under_the_system_python(self) -> None:
        old = self.system_python()
        if old is None:
            self.skipTest("/usr/bin/python3 is absent or not 3.9/3.10")
        probe = self.start(argv=[old, str(SERVER)], AUTONOM_PROBE_CLI_PYTHON=sys.executable)
        probe.initialize()
        self.assertEqual(len(probe.request("tools/list")["result"]["tools"]), 7)
        self.assertIn("result", probe.request("resources/read", {"uri": UI_URI}))
        opened = self.call_ok(probe, "open_workspace")
        token = opened["_meta"]["autonom/probe"]["media_token"]
        read = self.call_ok(probe, "probe_read")["structuredContent"]
        self.assertEqual(read["autonom"]["cli_python"], "%d.%d" % sys.version_info[:2])
        self.call_ok(probe, "probe_write", {"text": "old python", "expected_revision": 0})
        self.call_ok(probe, "probe_select", {"target_ref": "fixture~local", "expected_revision": 0})
        self.call_error(probe, "probe_fail", {}, "probe_deliberate_failure")
        self.assertEqual(self.call_ok(probe, "probe_media_frames", {"count": 2})["structuredContent"]["frames"], 2)
        self.call_ok(probe, "probe_save_results", {"results": valid_results()})
        self.assertNotIn(token, probe.stderr_text())
        self.assertEqual(probe.close(), 0)


# ====================================================================== write / select / fail

class StateToolTests(ProbeServerCase):
    def note_on_disk(self) -> Dict[str, Any]:
        return json.loads((self.probe_dir / "note.json").read_text("utf-8"))

    def test_write_success_bumps_the_revision_and_stores_the_note(self) -> None:
        probe = self.started()
        result = self.call_ok(probe, "probe_write", {"text": "hello", "expected_revision": 0})
        structured = result["structuredContent"]
        self.assertEqual(structured["note"]["revision"], 1)
        self.assertEqual(structured["note"]["text"], "hello")
        self.assertRegex(structured["note"]["updated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(structured["selection"], {"target_ref": None, "revision": 0})
        disk = self.note_on_disk()
        self.assertEqual(disk, {"schema": 1, "revision": 1, "text": "hello",
                                "updated_at": structured["note"]["updated_at"],
                                "operation_id": structured["operation_id"]})
        self.assert_mode(self.probe_dir / "note.json", 0o600)
        self.assert_mode(self.probe_dir, 0o700)
        self.assertEqual([p.name for p in self.probe_dir.iterdir() if p.name.startswith(".tmp-")], [])
        read = self.call_ok(probe, "probe_read")["structuredContent"]["note"]
        self.assertEqual((read["revision"], read["text"]), (1, "hello"))
        again = self.call_ok(probe, "probe_write", {"text": "x" * 500, "expected_revision": 1,
                                                   "selection_revision": 0})
        self.assertEqual(again["structuredContent"]["note"]["revision"], 2)

    def prepare(self, probe: ProbeProcess, note_revision: int, selection_revision: int) -> None:
        for revision in range(note_revision):
            self.call_ok(probe, "probe_write", {"text": "note %d" % (revision + 1), "expected_revision": revision})
        for revision in range(selection_revision):
            self.call_ok(probe, "probe_select", {"target_ref": "android~emulator-5584",
                                                 "expected_revision": revision})

    def test_stale_selection_leaves_the_note_unchanged(self) -> None:
        # CHP-HOST-005-S01
        probe = self.started()
        self.prepare(probe, 2, 5)
        before = self.note_on_disk()
        structured = self.call_error(probe, "probe_write",
                                     {"text": "x", "expected_revision": 2, "selection_revision": 4},
                                     "stale_selection")
        self.assertEqual(structured["current_selection_revision"], 5)
        self.assertIn("selection", structured["hint"])
        self.assertEqual(self.note_on_disk(), before)
        self.assertEqual((before["revision"], before["text"]), (2, "note 2"))
        # A stale selection wins over a stale note revision.
        self.call_error(probe, "probe_write", {"text": "x", "expected_revision": 0, "selection_revision": 4},
                        "stale_selection")

    def test_stale_revision_leaves_the_note_unchanged(self) -> None:
        # CHP-HOST-005-S02
        probe = self.started()
        self.prepare(probe, 2, 0)
        before = self.note_on_disk()
        structured = self.call_error(probe, "probe_write", {"text": "x", "expected_revision": 1}, "stale_revision")
        self.assertEqual(structured["current_revision"], 2)
        self.assertIn("probe_read", structured["hint"])
        self.assertEqual(self.note_on_disk(), before)

    def test_select_success_and_stale(self) -> None:
        probe = self.started()
        first = self.call_ok(probe, "probe_select", {"target_ref": "fixture~local", "expected_revision": 0})
        self.assertEqual(first["structuredContent"]["selection"], {"target_ref": "fixture~local", "revision": 1})
        second = self.call_ok(probe, "probe_select", {"target_ref": "ios~45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5",
                                                      "expected_revision": 1})
        self.assertEqual(second["structuredContent"]["selection"]["revision"], 2)
        stale = self.call_error(probe, "probe_select", {"target_ref": "fixture~x", "expected_revision": 1},
                                "stale_selection")
        self.assertEqual(stale["current_selection_revision"], 2)
        disk = json.loads((self.probe_dir / "selection.json").read_text())
        self.assertEqual((disk["schema"], disk["target_ref"], disk["revision"]),
                         (1, "ios~45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", 2))
        self.assert_mode(self.probe_dir / "selection.json", 0o600)
        opened = self.call_ok(probe, "open_workspace")["structuredContent"]
        self.assertEqual(opened["selection"], {"target_ref": "ios~45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5",
                                               "revision": 2})

    def test_corrupt_state_files_read_as_empty_and_are_replaced(self) -> None:
        (self.probe_dir / "note.json").write_text("{broken")
        (self.probe_dir / "selection.json").write_text("[1, 2]")
        probe = self.started()
        read = self.call_ok(probe, "probe_read")["structuredContent"]
        self.assertEqual(read["note"], {"revision": 0, "text": "", "updated_at": None})
        self.assertEqual(read["selection"], {"target_ref": None, "revision": 0})
        self.call_ok(probe, "probe_write", {"text": "fixed", "expected_revision": 0})
        self.assertEqual(self.note_on_disk()["text"], "fixed")

    def test_unwritable_state_is_state_unwritable(self) -> None:
        blocker = self.tmp / "not-a-dir"
        blocker.write_text("x")
        probe = self.started(AUTONOM_HOME=str(blocker))
        structured = self.call_error(probe, "probe_write", {"text": "x", "expected_revision": 0}, "state_unwritable")
        self.assertIn("chatgpt-probe", structured["hint"])
        self.call_error(probe, "probe_select", {"target_ref": "fixture~a", "expected_revision": 0},
                        "state_unwritable")
        self.call_error(probe, "probe_save_results", {"results": valid_results()}, "state_unwritable")
        media = self.call_ok(probe, "open_workspace")["structuredContent"]["media"]
        self.assertEqual((media["state"], media["error_code"]), ("failed", "state_unwritable"))
        self.assertEqual(self.media_starts(), [])

    def test_probe_fail_is_always_the_deliberate_failure(self) -> None:
        probe = self.started()
        for arguments in ({}, MISSING):
            structured = self.call_error(probe, "probe_fail", arguments, "probe_deliberate_failure")
            self.assertEqual(structured["error"], "Deliberate failure for the bridge.error check")
            self.assertEqual(structured["hint"],
                             "This failure is intentional; the page checks how errors render.")
            self.assertIs(structured["probe"], True)
            self.assertEqual(structured["detail"], {"nested": [1, "two", None]})


# ====================================================================== open_workspace and media

class OpenWorkspaceTests(ProbeServerCase):
    def test_token_only_in_meta_and_media_running(self) -> None:
        # CHP-HOST-006-S01
        self.write_config({"media_port": 0, "allowed_devices": ["android:emulator-5584"],
                           "remote_routes": [{"label": "forwarded", "origin": "http://127.0.0.1:47812"}]})
        probe = self.started()
        result = self.call_ok(probe, "open_workspace", {})
        meta = result["_meta"]["autonom/probe"]
        token, nonce = meta["media_token"], meta["context_nonce"]
        self.assertIsInstance(token, str)
        self.assertGreaterEqual(len(token), 32)
        self.assertRegex(nonce, NONCE)
        self.assertNotEqual(nonce, token)
        structured = result["structuredContent"]
        visible = json.dumps(structured) + result["content"][0]["text"]
        self.assertNotIn(token, visible)
        self.assertNotIn(nonce, visible)
        media = structured["media"]
        port = media["port"]
        self.assertGreater(port, 0)
        self.assertEqual(media, {"state": "running", "local_origin": "http://127.0.0.1:%d" % port, "port": port,
                                 "remote_routes": [{"label": "forwarded", "origin": "http://127.0.0.1:47812"}],
                                 "allowed_devices": ["android:emulator-5584"],
                                 "error_code": None, "error": None, "hint": None})
        self.assertEqual(structured["probe_version"], "0.1.0")
        self.assertEqual(structured["selection"], {"target_ref": None, "revision": 0})
        self.assertEqual(structured["note"], {"revision": 0})
        html = probe.request("resources/read", {"uri": UI_URI})["result"]["contents"][0]["text"]
        self.assertNotIn(token, html)
        self.assertNotIn(nonce, html)
        for name in TOOL_ORDER[1:]:
            other = probe.call(name, {})
            self.assertNotIn(token, json.dumps(other.get("structuredContent")))
            self.assertNotIn(nonce, json.dumps(other.get("structuredContent")))
        again = self.call_ok(probe, "open_workspace")
        self.assertEqual(again["structuredContent"]["media"]["port"], port)
        self.assertEqual(again["_meta"]["autonom/probe"], {"media_token": token, "context_nonce": nonce})
        starts = self.media_starts()
        self.assertEqual(len(starts), 1)
        argv = starts[0]["argv"]
        self.assertEqual(argv[:4], ["--port", "0", "--autonom-root", os.path.realpath(str(ROOT))])
        self.assertIn(["--allow-origin", SANDBOX_ORIGIN], [argv[i:i + 2] for i in range(len(argv))])
        self.assertIn(["--allow-host", "127.0.0.1:47812"], [argv[i:i + 2] for i in range(len(argv))])
        self.assertIn(["--allow-device", "android:emulator-5584"], [argv[i:i + 2] for i in range(len(argv))])
        self.assertNotIn(token, json.dumps(argv))
        self.assertGreaterEqual(starts[0]["token_len"], 32)
        self.assertEqual(probe.close(), 0)
        self.assertNotIn(token, probe.stderr_text())
        record_gone = not (self.probe_dir / "media.json").exists()
        self.assertTrue(record_gone)

    def test_media_failures_are_reported_inside_a_successful_result(self) -> None:
        # CHP-HOST-006-S02 and the other start failures
        cases = [("port_in_use", "media_port_in_use"), ("crash", "media_start_failed")]
        for mode, code in cases:
            with self.subTest(mode=mode):
                probe = self.started(FAKE_PROBE_MEDIA_MODE=mode)
                result = self.call_ok(probe, "open_workspace")
                media = result["structuredContent"]["media"]
                self.assertEqual((media["state"], media["error_code"]), ("failed", code))
                self.assertTrue(media["error"])
                self.assertTrue(media["hint"])
                self.assertIsNone(result["_meta"]["autonom/probe"]["media_token"])
                self.assertRegex(result["_meta"]["autonom/probe"]["context_nonce"], NONCE)
                if mode == "crash":
                    self.assertIn("fake media crashed", media["reason"])
                    self.assertIn("[redacted]", media["reason"])
                    self.assertLessEqual(len(media["reason"]), 400)
                if mode == "port_in_use":
                    self.assertIn("media_port", media["hint"])
                read = self.call_ok(probe, "probe_read")["structuredContent"]["media"]
                self.assertEqual((read["state"], read["error_code"]), ("failed", code))
                self.assertFalse((self.probe_dir / "media.json").exists())
                self.assertEqual(probe.close(), 0)

    def test_silent_child_times_out_and_is_killed(self) -> None:
        probe = self.started(FAKE_PROBE_MEDIA_MODE="silent", AUTONOM_PROBE_MEDIA_READY_TIMEOUT="2")
        started = time.monotonic()
        media = self.call_ok(probe, "open_workspace")["structuredContent"]["media"]
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1.8)
        self.assertLess(elapsed, 10.0)
        self.assertEqual((media["state"], media["error_code"]), ("failed", "media_start_failed"))
        self.assertRegex(media["reason"], r"^no ready line within 2 s \(waited [0-9.]+ s; "
                                          r"AUTONOM_PROBE_MEDIA_READY_TIMEOUT raises the limit\)")
        pid = self.media_starts()[0]["pid"]
        self.assertTrue(wait_dead(pid, 3.0), "the silent media child is still running")

    def test_a_long_stderr_tail_never_cuts_the_timeout_head(self) -> None:
        probe = self.started(FAKE_PROBE_MEDIA_MODE="noisy", AUTONOM_PROBE_MEDIA_READY_TIMEOUT="1")
        media = self.call_ok(probe, "open_workspace")["structuredContent"]["media"]
        reason = media["reason"]
        self.assertEqual(media["error_code"], "media_start_failed")
        self.assertLessEqual(len(reason), 400)
        self.assertRegex(reason, r"^no ready line within 1 s \(waited [0-9.]+ s; "
                                 r"AUTONOM_PROBE_MEDIA_READY_TIMEOUT raises the limit\): ")
        # The newest stderr is kept, the oldest is cut.
        self.assertIn("warn: line 49 ", reason)
        self.assertNotIn("warn: line 0 ", reason)

    def test_missing_node_and_root_are_reported_in_media(self) -> None:
        for overrides, code in (({"AUTONOM_PROBE_NODE": str(self.tmp / "no-node")}, "node_not_found"),
                                ({"AUTONOM_ROOT": str(self.tmp)}, "autonom_root_not_found")):
            with self.subTest(code=code):
                probe = self.started(**overrides)
                media = self.call_ok(probe, "open_workspace")["structuredContent"]["media"]
                self.assertEqual((media["state"], media["error_code"]), ("failed", code))
                self.assertTrue(media["hint"])
                probe.close()
        self.assertEqual(self.media_starts(), [])

    def test_a_dead_child_is_restarted_by_the_next_open(self) -> None:
        probe = self.started()
        self.call_ok(probe, "open_workspace")
        first = self.media_starts()[0]["pid"]
        os.kill(first, signal.SIGKILL)
        deadline = time.monotonic() + 3.0
        state = "running"
        while state == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
            state = self.call_ok(probe, "probe_read")["structuredContent"]["media"]["state"]
        self.assertEqual(state, "stopped")
        media = self.call_ok(probe, "open_workspace")["structuredContent"]["media"]
        self.assertEqual(media["state"], "running")
        starts = self.media_starts()
        self.assertEqual(len(starts), 2)
        record = json.loads((self.probe_dir / "media.json").read_text())
        self.assertEqual(record["pid"], starts[1]["pid"])

    def test_media_frames_without_and_with_the_child(self) -> None:
        probe = self.started()
        structured = self.call_error(probe, "probe_media_frames", {}, "media_not_running")
        self.assertIn("open_workspace", structured["hint"])
        self.call_ok(probe, "open_workspace")
        result = self.call_ok(probe, "probe_media_frames", {"count": 3, "after_seq": 5})
        structured = result["structuredContent"]
        self.assertEqual({k: structured[k] for k in ("frames", "bytes", "last_seq", "codec", "width", "height")},
                         {"frames": 3, "bytes": 3 * 32, "last_seq": 8, "codec": "avc1.42C01E",
                          "width": 96, "height": 208})
        self.assertNotIn("data_b64", result["content"][0]["text"])
        meta = result["_meta"]["autonom/probe"]
        self.assertEqual([frame["seq"] for frame in meta["frames"]], [6, 7, 8])
        self.assertTrue(all(frame["key"] is True and frame["data_b64"] for frame in meta["frames"]))
        self.assertTrue(all(isinstance(frame["t_server_ms"], float) for frame in meta["frames"]))
        self.assertEqual(meta["config_b64"], "AAAAAWdCwB4AAAABaM48gA==")
        self.assertNotIn("media_token", meta)
        default = self.call_ok(probe, "probe_media_frames")["structuredContent"]
        self.assertEqual((default["frames"], default["last_seq"]), (10, 9))

    def test_media_frames_after_a_failed_start_is_media_start_failed(self) -> None:
        probe = self.started(FAKE_PROBE_MEDIA_MODE="port_in_use")
        self.call_ok(probe, "open_workspace")
        structured = self.call_error(probe, "probe_media_frames", {}, "media_start_failed")
        self.assertEqual(structured["media_error_code"], "media_port_in_use")


class MediaSharingTests(ProbeServerCase):
    def test_second_server_reuses_the_child_and_only_the_owner_stops_it(self) -> None:
        owner = self.started()
        first = self.call_ok(owner, "open_workspace")
        port = first["structuredContent"]["media"]["port"]
        token = first["_meta"]["autonom/probe"]["media_token"]
        record_path = self.probe_dir / "media.json"
        record = json.loads(record_path.read_text())
        self.assert_mode(record_path, 0o600)
        child = self.media_starts()[0]["pid"]
        self.assertEqual((record["schema"], record["pid"], record["port"], record["token"], record["owner_pid"]),
                         (1, child, port, token, owner.proc.pid))
        self.assertNotIn("shared", first["structuredContent"]["media"])

        guest = self.started()
        second = self.call_ok(guest, "open_workspace")
        media = second["structuredContent"]["media"]
        self.assertEqual((media["state"], media["port"], media["shared"]), ("running", port, True))
        self.assertEqual(second["_meta"]["autonom/probe"]["media_token"], token)
        self.assertNotIn(token, json.dumps(second["structuredContent"]))
        read = self.call_ok(guest, "probe_read")["structuredContent"]["media"]
        self.assertIs(read["shared"], True)
        self.assertEqual(read["clients"], 1)
        frames = self.call_ok(guest, "probe_media_frames", {"count": 1})["structuredContent"]
        self.assertEqual(frames["frames"], 1)
        self.assertEqual(len(self.media_starts()), 1)

        self.assertEqual(guest.close(), 0)
        time.sleep(0.3)
        self.assertTrue(pid_alive(child), "closing the non-owner stopped the shared child")
        self.assertTrue(record_path.exists())

        started = time.monotonic()
        self.assertEqual(owner.close(), 0)
        self.assertLess(time.monotonic() - started, 3.5)
        self.assertTrue(wait_dead(child, 3.0), "closing the owner left the child running")
        self.assertFalse(record_path.exists())

    def test_a_stale_record_with_a_dead_pid_is_replaced(self) -> None:
        dead = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL)
        dead.wait()
        self.probe_dir.mkdir(parents=True, exist_ok=True)
        (self.probe_dir / "media.json").write_text(json.dumps(
            {"schema": 1, "pid": dead.pid, "port": 9, "token": "t" * 43, "owner_pid": dead.pid,
             "started_at": "2026-10-10T00:00:00Z"}))
        probe = self.started()
        media = self.call_ok(probe, "open_workspace")["structuredContent"]["media"]
        self.assertEqual(media["state"], "running")
        self.assertNotIn("shared", media)
        record = json.loads((self.probe_dir / "media.json").read_text())
        self.assertEqual(record["pid"], self.media_starts()[0]["pid"])
        self.assertNotEqual(record["token"], "t" * 43)
        self.assert_mode(self.probe_dir / "media.json", 0o600)

    def test_a_live_pid_whose_status_fails_is_not_reused(self) -> None:
        self.probe_dir.mkdir(parents=True, exist_ok=True)
        (self.probe_dir / "media.json").write_text(json.dumps(
            {"schema": 1, "pid": os.getpid(), "port": 9, "token": "t" * 43, "owner_pid": os.getpid(),
             "started_at": "2026-10-10T00:00:00Z"}))
        probe = self.started()
        media = self.call_ok(probe, "open_workspace")["structuredContent"]["media"]
        self.assertEqual(media["state"], "running")
        self.assertNotIn("shared", media)
        self.assertTrue(pid_alive(os.getpid()))

    def test_stdin_eof_and_sigterm_stop_the_child_and_exit_zero(self) -> None:
        # CHP-HOST-002-S02
        for how in ("eof", "sigterm", "sigint"):
            with self.subTest(how=how):
                probe = self.started()
                self.call_ok(probe, "open_workspace")
                child = self.media_starts()[-1]["pid"]
                started = time.monotonic()
                if how == "eof":
                    code = probe.close(timeout=5)
                else:
                    probe.proc.send_signal(signal.SIGTERM if how == "sigterm" else signal.SIGINT)
                    code = probe.proc.wait(timeout=5)
                self.assertEqual(code, 0)
                self.assertLess(time.monotonic() - started, 3.0)
                self.assertTrue(wait_dead(child, 2.0), "media child survived the server")
                self.assertFalse((self.probe_dir / "media.json").exists())


# ====================================================================== results

def valid_results(run_id: str = "probe-20261010-142233-7f3a", **extra: Any) -> Dict[str, Any]:
    results: Dict[str, Any] = {"schema_version": 1, "probe_version": "0.1.0", "run_id": run_id,
                               "checks": [{"id": "host.init", "status": "pass"}],
                               "summary": {"pass": 1, "fail": 0, "not_exercised": 0}}
    results.update(extra)
    return results


class SaveResultsTests(ProbeServerCase):
    def test_success_writes_a_private_file(self) -> None:
        probe = self.started()
        results = valid_results(note="caf\u00e9")
        structured = self.call_ok(probe, "probe_save_results", {"results": results})["structuredContent"]
        path = self.probe_dir / "results" / "probe-20261010-142233-7f3a.json"
        self.assertEqual(structured["saved"], {"run_id": "probe-20261010-142233-7f3a",
                                               "path_hint": "chatgpt-probe/results/probe-20261010-142233-7f3a.json",
                                               "bytes": path.stat().st_size})
        self.assertEqual(json.loads(path.read_text("utf-8")), results)
        self.assert_mode(path, 0o600)
        self.assert_mode(path.parent, 0o700)
        self.assert_mode(self.probe_dir, 0o700)
        results["summary"]["pass"] = 0
        self.call_ok(probe, "probe_save_results", {"results": results})
        self.assertEqual(json.loads(path.read_text("utf-8"))["summary"]["pass"], 0)
        self.assertEqual(len(list(path.parent.iterdir())), 1)

    def test_too_large_invalid_and_secret_results_are_refused_without_a_write(self) -> None:
        probe = self.started()
        token = self.call_ok(probe, "open_workspace")["_meta"]["autonom/probe"]["media_token"]
        big = valid_results(padding="x" * (512 * 1024))
        structured = self.call_error(probe, "probe_save_results", {"results": big}, "results_too_large")
        self.assertGreater(structured["bytes"], 512 * 1024)
        self.assertEqual(structured["limit_bytes"], 512 * 1024)
        invalid = [
            (valid_results(schema_version=2), "results.schema_version"),
            (valid_results(schema_version="1"), "results.schema_version"),
            (valid_results(run_id="probe-1"), "results.run_id"),
            (valid_results(run_id="../../etc/passwd"), "results.run_id"),
            (valid_results(checks={"a": 1}), "results.checks"),
        ]
        for results, field in invalid:
            with self.subTest(field=field, results=results.get(field.split(".")[1])):
                structured = self.call_error(probe, "probe_save_results", {"results": results}, "results_invalid")
                self.assertEqual(structured["field"], field)
        for secret in (token, "bearer." + "x" * 10, "Bearer.abc"):
            with self.subTest(secret=secret[:7]):
                results = valid_results(checks=[{"id": "net.ws.local", "error": {"message": "p " + secret}}])
                structured = self.call_error(probe, "probe_save_results", {"results": results},
                                             "results_contain_secret")
                self.assertNotIn(token, json.dumps(structured))
        self.assertFalse((self.probe_dir / "results").exists())


# ====================================================================== resources

class ResourceTests(ProbeServerCase):
    def test_csp_from_config_with_a_remote_route(self) -> None:
        # CHP-HOST-007-S01
        self.write_config({"media_port": 47900,
                           "remote_routes": [{"label": "forwarded", "origin": "http://127.0.0.1:47901"}]})
        probe = self.started()
        expected = ["http://127.0.0.1:47900", "ws://127.0.0.1:47900", "http://localhost:47900",
                    "ws://localhost:47900", "http://127.0.0.1:47901", "ws://127.0.0.1:47901"]
        listed = probe.request("resources/list")["result"]["resources"]
        self.assertEqual(len(listed), 1)
        entry = listed[0]
        self.assertEqual({k: entry[k] for k in ("uri", "name", "title", "mimeType")},
                         {"uri": UI_URI, "name": "autonom-probe", "title": "Autonom probe", "mimeType": UI_MIME})
        content = probe.request("resources/read", {"uri": UI_URI})["result"]["contents"]
        self.assertEqual(len(content), 1)
        self.assertEqual((content[0]["uri"], content[0]["mimeType"]), (UI_URI, UI_MIME))
        for meta in (entry["_meta"], content[0]["_meta"]):
            self.assertEqual(meta["ui"]["csp"], {"connectDomains": expected, "resourceDomains": [],
                                                 "frameDomains": []})
            self.assertIs(meta["ui"]["prefersBorder"], False)
            self.assertNotIn("domain", meta["ui"])
            self.assertEqual(meta["openai/widgetCSP"], {"connect_domains": expected, "resource_domains": []})
            self.assertEqual(meta["openai/ui"], {"availableDisplayModes": ["inline", "fullscreen"]})
            self.assertTrue(meta["openai/widgetDescription"])
        html = content[0]["text"]
        self.assertNotIn("<!-- PROBE_CORE -->", html)
        self.assertNotIn("__PROBE_CONFIG_JSON__", html)
        self.assertIn('<script type="module">\n' + MINIMAL_CORE + "\n</script>", html)
        config_text = re.search(r'id="probe-config">(.*?)</script>', html, re.S).group(1)
        self.assertEqual(json.loads(config_text), {
            "probe_version": "0.1.0", "local_origin": "http://127.0.0.1:47900",
            "remote_routes": [{"label": "forwarded", "origin": "http://127.0.0.1:47901"}],
            "allowed_devices": [], "media_port": 47900})

    def test_https_route_gets_a_wss_twin_and_config_json_is_escaped(self) -> None:
        self.write_config({"media_port": 47900, "allowed_devices": ["ios:45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5"],
                           "remote_routes": [{"label": "</script><b>", "origin": "https://relay.example.com:8443"}]})
        probe = self.started()
        content = probe.request("resources/read", {"uri": UI_URI})["result"]["contents"][0]
        domains = content["_meta"]["ui"]["csp"]["connectDomains"]
        self.assertEqual(domains[4:], ["https://relay.example.com:8443", "wss://relay.example.com:8443"])
        html = content["text"]
        self.assertNotIn("</script><b>", html)
        self.assertIn("<\\/script><b>", html)
        self.assertIn("ios:45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", html)

    def test_missing_or_unsafe_ui_files_are_internal_errors(self) -> None:
        probe = self.started()
        cases = [
            (lambda: (self.ui_dir / "probe-core.mjs").unlink(), "Probe UI files are missing"),
            (lambda: (self.ui_dir / "probe-page.html").write_text("<html>no markers</html>"),
             "Probe UI files are missing"),
            (lambda: (self.ui_dir / "probe-page.html").write_text(MINIMAL_PAGE + "<!-- PROBE_CORE -->\n"),
             "Probe UI files are missing"),
            (lambda: (self.ui_dir / "probe-core.mjs").write_text("const s = '</SCRIPT>';\n"),
             "Probe UI core cannot be inlined"),
        ]
        for prepare, message in cases:
            with self.subTest(message=message):
                (self.ui_dir / "probe-page.html").write_text(MINIMAL_PAGE)
                (self.ui_dir / "probe-core.mjs").write_text(MINIMAL_CORE)
                prepare()
                self.assertEqual(probe.request("resources/read", {"uri": UI_URI})["error"],
                                 {"code": -32603, "message": message})
                self.assertEqual(probe.request("ping")["result"], {})
        empty = self.tmp / "empty-ui"
        empty.mkdir()
        other = self.started(AUTONOM_PROBE_UI_DIR=str(empty))
        self.assertEqual(other.request("resources/read", {"uri": UI_URI})["error"]["message"],
                         "Probe UI files are missing")
        self.assertEqual(len(other.request("resources/list")["result"]["resources"]), 1)


# ====================================================================== remaining refusal branches

class RefusalBranchTests(ProbeServerCase):
    """Each remaining error path, checked against its payload."""

    def test_cli_json_without_version_or_devices_is_cli_failed(self) -> None:
        for mode, command in (("no_version", ["autonom", "version"]),
                              ("no_devices", ["autonom", "devices", "list"])):
            with self.subTest(mode=mode):
                probe = self.started(FAKE_PROBE_CLI_MODE=mode)
                structured = self.call_error(probe, "probe_read", {}, "cli_failed")
                self.assertEqual(structured["command"], command)
                self.assertEqual(structured["exit_code"], 0)
                self.assertIn("host", structured)
                probe.close()

    def test_media_ready_timeout_defaults_to_5_s_and_takes_an_override(self) -> None:
        probe_env = import_server_modules()[0]
        cases = [(None, 5.0, None), ("0.5", 0.5, None), ("600", 600.0, None),
                 ("0", 5.0, "AUTONOM_PROBE_MEDIA_READY_TIMEOUT"),
                 ("601", 5.0, "AUTONOM_PROBE_MEDIA_READY_TIMEOUT"),
                 ("soon", 5.0, "AUTONOM_PROBE_MEDIA_READY_TIMEOUT")]
        for value, limit, problem in cases:
            with self.subTest(value=value):
                env = {} if value is None else {"AUTONOM_PROBE_MEDIA_READY_TIMEOUT": value}
                config = probe_env.load_config(env, self.home)
                self.assertEqual(config["media_ready_timeout_s"], limit)
                if problem:
                    self.assertIn(problem, config["config_error"])
                else:
                    self.assertIsNone(config["config_error"])

    def test_bad_media_ready_timeout_override_is_a_config_error(self) -> None:
        probe = self.started(AUTONOM_PROBE_MEDIA_READY_TIMEOUT="soon")
        server = self.call_ok(probe, "probe_read")["structuredContent"]["server"]
        self.assertIn("AUTONOM_PROBE_MEDIA_READY_TIMEOUT", server["config_error"])

    def test_bad_cli_timeout_override_is_a_config_error(self) -> None:
        probe = self.started(AUTONOM_PROBE_CLI_TIMEOUT="soon")
        server = self.call_ok(probe, "probe_read")["structuredContent"]["server"]
        self.assertIn("AUTONOM_PROBE_CLI_TIMEOUT", server["config_error"])

    def test_media_start_failures_name_their_reason(self) -> None:
        not_binary = self.tmp / "node"
        not_binary.write_text("this is not a program\n")
        not_binary.chmod(0o755)
        cases = [
            ({"AUTONOM_PROBE_MEDIA_SCRIPT": str(self.tmp / "missing.mjs")}, "exited with code"),
            ({"AUTONOM_PROBE_NODE": str(not_binary)}, "OSError"),
            ({"FAKE_PROBE_MEDIA_MODE": "junk"}, "unexpected first line: starting up..."),
        ]
        for overrides, fragment in cases:
            with self.subTest(fragment=fragment):
                probe = self.started(**overrides)
                media = self.call_ok(probe, "open_workspace")["structuredContent"]["media"]
                self.assertEqual((media["state"], media["error_code"]), ("failed", "media_start_failed"))
                self.assertIn(fragment, media["reason"])
                self.assertEqual(media["hint"], "See reason; run the probe media endpoint by hand to debug.")
                self.assertFalse((self.probe_dir / "media.json").exists())
                probe.close()
        junk = [start["pid"] for start in self.media_starts() if start["mode"] == "junk"]
        self.assertEqual(len(junk), 1)
        self.assertTrue(wait_dead(junk[0], 3.0))

    def test_a_token_cut_by_a_line_limit_is_still_redacted(self) -> None:
        # The stderr tail keeps 2000 characters per line and the reason quotes 120 characters
        # of an unexpected first line; the token must be removed before either cut.
        cases = [("crash", 1958, ""), ("crash", 1990, ""), ("crash", 1995, ""),
                 ("junk", 100, "unexpected first line: "), ("junk", 115, "unexpected first line: ")]
        for mode, pad, prefix in cases:
            with self.subTest(mode=mode, pad=pad):
                probe = self.started(FAKE_PROBE_MEDIA_MODE=mode, FAKE_PROBE_MEDIA_PAD=str(pad))
                media = self.call_ok(probe, "open_workspace")["structuredContent"]["media"]
                self.assertEqual((media["state"], media["error_code"]), ("failed", "media_start_failed"))
                reason = media["reason"]
                self.assertLessEqual(len(reason), 400)
                if prefix:
                    self.assertIn(prefix + "y" * 50, reason)
                match = re.search(r"y+([^y]*)$", reason)
                self.assertIsNotNone(match, reason)
                rest = match.group(1)
                self.assertTrue(rest, reason)
                self.assertTrue("[redacted]".startswith(rest), reason)
                text = json.dumps(self.call_ok(probe, "probe_read")["structuredContent"])
                self.assertNotRegex(text, r"y{50}[A-Za-z0-9_-]")
                self.assertEqual(probe.close(), 0)
                self.assertNotRegex(probe.stderr_text(), r"y{50}[A-Za-z0-9_-]")

    def test_values_with_a_trailing_newline_are_refused(self) -> None:
        # Validators must match the whole string: "$" alone also matches before a final newline.
        probe_env, _state, probe_tools, _server = import_server_modules()
        for good, check in (("android:emulator-5584", probe_env.valid_device_spec),
                            ("ios:45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", probe_env.valid_device_spec),
                            ("http://127.0.0.1:47812", probe_env.valid_route_origin),
                            ("https://a.example:8443", probe_env.valid_route_origin),
                            ("https://a.example", probe_env.valid_allow_origin),
                            ("https://*.a.example", probe_env.valid_allow_origin),
                            ("http://[::1]:9", probe_env.valid_allow_origin)):
            with self.subTest(value=good):
                self.assertTrue(check(good))
                self.assertFalse(check(good + "\n"))
        self.assertFalse(probe_env.device_allowed_spec("android:emulator-5554\n"))
        self.assertIsNone(probe_env.TARGET_REF.fullmatch("fixture~local\n"))
        self.assertIsNone(probe_tools.RUN_ID.fullmatch("probe-20261010-142233-7f3a\n"))

        self.write_config({"media_port": 0,
                           "allowed_devices": ["android:emulator-5554\n", "android:emulator-5584\n",
                                               "android:emulator-5586"],
                           "remote_routes": [{"label": "nl", "origin": "http://127.0.0.1:47812\n"}],
                           "allow_origins": ["https://a.example\n", "https://*.a.example\n"]})
        probe = self.started()
        error = self.call_ok(probe, "probe_read")["structuredContent"]["server"]["config_error"]
        for fragment in ("allowed_devices[0]", "allowed_devices[1]", "remote_routes[0].origin",
                         "allow_origins[0]", "allow_origins[1]"):
            self.assertIn(fragment, error)
        domains = probe.request("resources/list")["result"]["resources"][0]["_meta"]["ui"]["csp"]["connectDomains"]
        self.assertFalse([domain for domain in domains if "\n" in domain], domains)
        self.assertNotIn("http://127.0.0.1:47812", domains)
        opened = self.call_ok(probe, "open_workspace")["structuredContent"]
        self.assertEqual(opened["media"]["state"], "running")
        argv = self.media_starts()[0]["argv"]
        self.assertFalse([part for part in argv if "\n" in part], argv)
        pairs = [argv[i:i + 2] for i in range(len(argv))]
        self.assertIn(["--allow-device", "android:emulator-5586"], pairs)
        self.assertEqual(len([pair for pair in pairs if pair[0] == "--allow-device"]), 1)
        self.assertNotIn("--allow-host", argv)

        structured = self.call_error(probe, "probe_select",
                                     {"target_ref": "android~emulator-5584\n", "expected_revision": 0},
                                     "invalid_arguments")
        self.assertEqual(structured["field"], "target_ref")
        self.assertFalse((self.probe_dir / "selection.json").exists())
        structured = self.call_error(probe, "probe_save_results",
                                     {"results": valid_results(run_id="probe-20261010-142233-7f3a\n")},
                                     "results_invalid")
        self.assertEqual(structured["field"], "results.run_id")
        self.assertFalse((self.probe_dir / "results").exists())
        self.assertEqual(probe.close(), 0)

    def test_frames_refused_by_the_child_is_media_not_running_with_details(self) -> None:
        probe = self.started(FAKE_PROBE_MEDIA_FRAMES="error")
        self.call_ok(probe, "open_workspace")
        structured = self.call_error(probe, "probe_media_frames", {"count": 1}, "media_not_running")
        self.assertEqual(structured["http_status"], 503)
        self.assertEqual(structured["media_error_code"], "busy")
        self.assertEqual(structured["media_state"], "running")

    def test_non_explicit_home_cli_that_is_not_autonom_is_refused(self) -> None:
        launcher = self.home / ".local" / "bin" / "autonom"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\necho not autonom\n")
        probe = self.started(AUTONOM_CLI=None)
        structured = self.call_error(probe, "probe_read", {}, "autonom_cli_not_found")
        self.assertEqual(structured["found"], "autonom")
        self.assertNotIn("source", structured)

    def test_resolver_fallthrough_and_root_derivation_failures(self) -> None:
        probe_env, _state, _tools, _server = import_server_modules()
        old = probe_env.Resolver({"PATH": "/usr/bin:/bin"}, self.home, probe_env.default_config(),
                                 which=lambda name, path: sys.executable if name.startswith("python") else None,
                                 run=lambda argv, timeout, env: (0, "3.9\n"), version_info=(3, 9, 6))
        with self.assertRaises(probe_env.ProbeError) as caught:
            old.resolve_cli_python()
        self.assertEqual(caught.exception.error_code, "python_too_old")
        self.assertEqual(caught.exception.extras, {"found": ["3.9"]})
        missing_explicit = probe_env.Resolver({"AUTONOM_PROBE_CLI_PYTHON": "/nonexistent/python", "PATH": "/bin"},
                                              self.home, probe_env.default_config(), version_info=(3, 9, 6))
        with self.assertRaises(probe_env.ProbeError) as caught:
            missing_explicit.resolve_cli_python()
        self.assertEqual(caught.exception.extras, {"found": [], "source": "env"})
        no_cli = probe_env.Resolver({"PATH": "/bin"}, self.home, probe_env.default_config(),
                                    which=lambda name, path: None)
        with self.assertRaises(probe_env.ProbeError) as caught:
            no_cli.resolve_autonom_root()
        self.assertEqual(caught.exception.error_code, "autonom_root_not_found")
        self.assertIn("could not be derived", caught.exception.error)
        outside = probe_env.Resolver({"AUTONOM_CLI": str(FAKE_CLI), "PATH": "/bin"}, self.home,
                                     probe_env.default_config())
        with self.assertRaises(probe_env.ProbeError) as caught:
            outside.resolve_autonom_root()
        self.assertEqual(caught.exception.error_code, "autonom_root_not_found")
        inside = probe_env.Resolver({"AUTONOM_CLI": str(ROOT / "scripts" / "autonom.py"), "PATH": "/bin"},
                                    self.home, probe_env.default_config())
        self.assertEqual(inside.resolve_autonom_root(), os.path.realpath(str(ROOT)))

    def test_cli_that_cannot_be_spawned_is_cli_failed(self) -> None:
        probe_env, probe_state, probe_tools, _server = import_server_modules()
        config = probe_env.default_config()
        resolver = probe_env.Resolver({"PATH": "/bin"}, self.home, config)
        tools = probe_tools.ProbeTools(resolver, config, probe_state.StateStore(self.probe_dir),
                                       media=None, server_info=dict, log=lambda line: None)
        with self.assertRaises(probe_env.ProbeError) as caught:
            tools._run_cli(str(self.tmp / "no-python"), str(FAKE_CLI), ["version"])
        self.assertEqual(caught.exception.error_code, "cli_failed")
        self.assertEqual(caught.exception.extras, {"command": ["autonom", "version"], "exit_code": None})
        self.assertTrue(caught.exception.hint)


class HostileInputTests(ProbeServerCase):
    """Input that once killed the server: lone surrogates and very deep nesting."""

    def test_lone_surrogates_that_are_echoed_never_kill_the_server(self) -> None:
        probe = self.started()
        reply = probe.request("\ud800/x")
        self.assertEqual(reply["error"]["code"], -32601)
        self.assertEqual(reply["error"]["data"], {"method": "\ud800/x"})
        probe.send({"jsonrpc": "2.0", "id": "\udc00", "method": "ping"})
        self.assertEqual(probe.recv(), {"jsonrpc": "2.0", "id": "\udc00", "result": {}})
        reply = probe.request("tools/call", {"name": "\ud800tool"})
        self.assertEqual(reply["error"]["code"], -32602)
        self.assertEqual(reply["error"]["message"], "Unknown tool: \ud800tool")
        reply = probe.request("resources/read", {"uri": "ui://\ud800"})
        self.assertEqual(reply["error"]["code"], -32002)
        structured = self.call_error(probe, "probe_save_results",
                                     {"results": valid_results(note="bad \ud800 text")}, "results_invalid")
        self.assertEqual(structured["field"], "results")
        self.assertFalse((self.probe_dir / "results").exists())
        reply = probe.request("tools/call", {"name": "probe_write",
                                             "arguments": {"text": "\ud800", "expected_revision": 0}})
        self.assertTrue("result" in reply or reply["error"]["code"] == -32603, reply)
        for line in probe.raw_lines:
            line.decode("utf-8")
        self.assertEqual(probe.request("ping")["result"], {})
        self.assertEqual(probe.close(), 0)

    def test_an_echoed_surrogate_inside_a_tool_envelope_is_escaped(self) -> None:
        _env, _state, probe_tools, probe_server = import_server_modules()
        result = probe_tools.envelope("op_0123456789abcdef", {"ok": False, "error": "\ud800"})
        text = result["content"][0]["text"]
        text.encode("utf-8")
        self.assertEqual(json.loads(text)["error"], "\ud800")
        line = probe_server.ProbeServer.encode({"jsonrpc": "2.0", "id": 1, "result": result})
        self.assertEqual(json.loads(line.decode("utf-8"))["result"], result)

    def test_an_unserialisable_reply_becomes_an_internal_error(self) -> None:
        _env, _state, _tools, probe_server = import_server_modules()
        import io
        out = io.BytesIO()
        server = probe_server.ProbeServer.__new__(probe_server.ProbeServer)
        server.stdout, server.stderr = out, io.StringIO()
        import threading as _threading
        server._write_lock = _threading.Lock()
        server.send({"jsonrpc": "2.0", "id": 4, "result": {"x": object()}})
        self.assertEqual(json.loads(out.getvalue().decode("utf-8")),
                         {"jsonrpc": "2.0", "id": 4, "error": {"code": -32603, "message": "Internal error: TypeError"}})

    def test_deeply_nested_json_is_a_parse_error(self) -> None:
        probe = self.started()
        for raw in (b"[" * 200000 + b"\n",
                    b"{\"jsonrpc\":\"2.0\",\"id\":9,\"method\":\"ping\",\"params\":" + b"[" * 200000 + b"\n"):
            with self.subTest(size=len(raw)):
                probe.send_raw(raw)
                self.assertEqual(probe.recv(timeout=30), {"jsonrpc": "2.0", "id": None,
                                                          "error": {"code": -32700, "message": "Parse error"}})
        self.assertEqual(probe.request("ping")["result"], {})
        self.assertEqual(probe.close(), 0)

    def test_deeply_nested_config_is_a_config_error(self) -> None:
        self.write_config(None, raw="[" * 200000)
        probe = self.started()
        server = self.call_ok(probe, "probe_read")["structuredContent"]["server"]
        self.assertIn("config file is not valid JSON", server["config_error"])
        self.assertEqual(probe.close(), 0)


# ====================================================================== package

class PackageTests(unittest.TestCase):
    def test_manifest_and_mcp_config_match_the_contract(self) -> None:
        manifest = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text("utf-8"))
        self.assertEqual(manifest["name"], "autonom-chatgpt-probe")
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        self.assertEqual(manifest["license"], "MIT")
        self.assertEqual(manifest["interface"]["displayName"], "Autonom probe")
        self.assertEqual(manifest["interface"]["capabilities"], ["Interactive", "Read", "Write"])
        self.assertEqual(manifest["interface"]["defaultPrompt"], ["Open the Autonom probe and run all checks."])
        self.assertNotIn("skills", manifest)
        self.assertFalse((PLUGIN / "skills").exists())
        mcp = json.loads((PLUGIN / ".mcp.json").read_text("utf-8"))
        self.assertEqual(mcp, {"mcpServers": {"autonom-chatgpt-probe": {
            "command": "sh", "args": ["./bin/launch.sh"], "cwd": "."}}})

    def test_server_sources_parse_as_python_3_9_and_import_only_stdlib_and_siblings(self) -> None:
        files = sorted(SERVER_DIR.glob("*.py"))
        self.assertEqual([path.name for path in files],
                         ["probe_env.py", "probe_media_child.py", "probe_server.py", "probe_state.py",
                          "probe_tools.py"])
        siblings = {path.stem for path in files}
        stdlib = set(getattr(sys, "stdlib_module_names", ())) or None
        for path in files:
            with self.subTest(file=path.name):
                source = path.read_text("utf-8")
                tree = ast.parse(source, filename=str(path), feature_version=(3, 9))
                self.assertIn("from __future__ import annotations", source)
                for node in ast.walk(tree):
                    names: List[str] = []
                    if isinstance(node, ast.Import):
                        names = [alias.name.split(".")[0] for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        self.assertEqual(node.level, 0)
                        names = [(node.module or "").split(".")[0]]
                    for name in names:
                        if stdlib is not None:
                            self.assertTrue(name in siblings or name in stdlib, "%s imports %s" % (path.name, name))
                        else:
                            self.assertNotIn(name, ("autonom_lib", "scripts"))
                for banned in ("datetime.UTC", "tomllib"):
                    self.assertNotIn(banned, source)
        self.assertTrue(SERVER.read_text().startswith("#!/usr/bin/env python3\n"))


class LauncherTests(ProbeServerCase):
    def test_launcher_parses_under_sh_and_bash_and_is_shellcheck_clean(self) -> None:
        for shell in ("sh", "bash"):
            with self.subTest(shell=shell):
                out = subprocess.run([shell, "-n", str(LAUNCHER)], stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True, check=False)
                self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(LAUNCHER.read_text().startswith("#!/bin/sh\n"))
        checker = shutil.which("shellcheck")
        if checker is None:
            self.skipTest("shellcheck is not installed")
        out = subprocess.run([checker, str(LAUNCHER)], stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_launcher_execs_the_server_and_writes_nothing_else_to_stdout(self) -> None:
        env = self.server_env(AUTONOM_PROBE_PYTHON=sys.executable)
        launched = ProbeProcess(["sh", str(LAUNCHER)], env)
        self.addCleanup(self._finish, launched)
        reply = launched.initialize()
        self.assertEqual(reply["result"]["serverInfo"]["name"], "autonom-chatgpt-probe")
        server = self.call_ok(launched, "probe_read")["structuredContent"]["server"]
        self.assertEqual(server["pid"], launched.proc.pid, "launch.sh must exec the server")
        self.assertEqual(launched.close(), 0)

    def test_launcher_skips_an_unusable_override(self) -> None:
        env = self.server_env(AUTONOM_PROBE_PYTHON=str(self.tmp / "missing-python"))
        launched = ProbeProcess(["sh", str(LAUNCHER)], env)
        self.addCleanup(self._finish, launched)
        self.assertIn("result", launched.initialize())
        self.assertEqual(launched.close(), 0)
        self.assertEqual(launched.stderr_text(), "")


if __name__ == "__main__":
    unittest.main()
