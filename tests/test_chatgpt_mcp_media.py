"""Autonom MCP adapter media: Canvas link, WebSocket client, streams, input, pause/resume.

Contract section 5 (5.6). The server runs as a real subprocess over stdio (or,
where a case must look inside a stream or the lease keeper, as the same
``McpServer`` object in process) with the fake CLI
(``tests/fakes/fake_mcp_cli.py``), whose canvas verbs run the fake Canvas
``tests/fakes/fake_mcp_canvas.mjs`` (built on the real Canvas ``ws.mjs``).
``HOME``, ``AUTONOM_HOME`` and ``XDG_STATE_HOME`` are temp dirs for the server
and every child; no case touches a device, a device tool or a host service.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import io
import json
import os
import queue
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "integrations" / "chatgpt" / "autonom-chatgpt"
SERVER_DIR = PLUGIN / "server"
SERVER = SERVER_DIR / "mcp_server.py"
FAKES = ROOT / "tests" / "fakes"
FAKE_CLI = FAKES / "fake_mcp_cli.py"
FAKE_CANVAS = FAKES / "fake_mcp_canvas.mjs"
NODE = shutil.which("node")

ANDROID = "emulator-5584"
IOS_UDID = "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5"
ANDROID_DEVICE = "android~" + ANDROID
CONFIG_FIXTURE = bytes.fromhex("000000016764001facd9405005bb011000000300100000030320f1831960"
                               "0000000168ebe3cb22c0")
KEYFRAME = {"t": "system", "op": "keyframe"}
MISSING = object()
MEDIA_TOOLS = ["open_stream", "get_frames", "close_stream", "send_input", "pause_session", "resume_session"]
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
        import mcp_common
        import mcp_media
        import mcp_server
        import mcp_ws
    finally:
        sys.dont_write_bytecode = previous
    return {"common": mcp_common, "media": mcp_media, "server": mcp_server, "ws": mcp_ws}


def import_fake() -> Any:
    spec = importlib.util.spec_from_file_location("fake_mcp_cli_media_helpers", FAKE_CLI)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    finally:
        sys.dont_write_bytecode = previous
    return module


FAKE = import_fake()
MODULES = import_server_modules()


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


def wait_until(predicate: Callable[[], Any], timeout: float, step: float = 0.05) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(step)


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

    def send(self, message: Any) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def new_id(self) -> int:
        msg_id = self.next_id
        self.next_id += 1
        return msg_id

    def wait_for(self, msg_id: Any, timeout: float = 30.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            reply = self.messages.get(timeout=max(0.1, deadline - time.monotonic()))
            if reply is None:
                raise AssertionError("server closed stdout while waiting for %r" % (msg_id,))
            if isinstance(reply, dict) and reply.get("id") == msg_id and "method" not in reply:
                return reply

    def request(self, method: str, params: Any = MISSING, timeout: float = 30.0) -> Dict[str, Any]:
        msg_id = self.new_id()
        message: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not MISSING:
            message["params"] = params
        self.send(message)
        return self.wait_for(msg_id, timeout)

    def initialize(self) -> Dict[str, Any]:
        reply = self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                            "clientInfo": {"name": "media-test", "version": "1"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return reply

    def call(self, name: str, arguments: Any, timeout: float = 30.0) -> Dict[str, Any]:
        reply = self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        if "error" in reply:
            raise AssertionError("tools/call %s answered a JSON-RPC error: %r" % (name, reply["error"]))
        return reply["result"]

    def stderr_text(self) -> str:
        self.stderr_file.seek(0)
        return self.stderr_file.read().decode("utf-8", "replace")

    def close(self, timeout: float = 20.0) -> Optional[int]:
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
        self.close()
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        self.reader.join(timeout=2)
        self.stderr_file.close()


@unittest.skipIf(NODE is None, "node is required for the fake Canvas")
class MediaCase(unittest.TestCase):
    """Temp HOME, state base, fake CLI and fake Canvas state, and shared helpers."""

    maxDiff = None

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="mcp-media-test-"))
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
        self.canvas_log_path = tmp / "canvas.log"
        self.canvas_config_path = tmp / "canvas.json"
        self.write_canvas({})
        self.host_id = FAKE.host_id(self.fake_state)
        self.servers: List[ServerProcess] = []
        self._schemas: Dict[str, Dict[str, Any]] = {}
        self.addCleanup(self._kill_leftovers)

    # -- configuration

    @property
    def mcp_dir(self) -> Path:
        return self.state_base / "chatgpt-mcp"

    def write_config(self, config: Dict[str, Any]) -> None:
        self.mcp_dir.mkdir(parents=True, exist_ok=True)
        (self.mcp_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

    def write_canvas(self, config: Dict[str, Any]) -> None:
        self.canvas_config_path.write_text(json.dumps(config), encoding="utf-8")

    def add_session(self, platform: str = "android", target: str = ANDROID) -> str:
        return FAKE.add_session(self.fake_state, platform, target)

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
            "FAKE_MCP_CANVAS_CMD": "%s %s" % (NODE, FAKE_CANVAS),
            "FAKE_MCP_CANVAS_CONFIG": str(self.canvas_config_path),
            "FAKE_MCP_CANVAS_LOG": str(self.canvas_log_path),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    # -- servers

    def started(self, **overrides: Optional[str]) -> ServerProcess:
        server = ServerProcess([sys.executable, str(SERVER)], self.server_env(**overrides))
        self.servers.append(server)
        self.addCleanup(server.kill)
        self.assertIn("result", server.initialize())
        tools = server.request("tools/list")["result"]["tools"]
        self._schemas = {tool["name"]: tool for tool in tools}
        return server

    def in_process(self) -> Any:
        """The real ``McpServer`` object, for cases that look inside a stream or the keeper."""
        stderr = io.StringIO()
        server = MODULES["server"].McpServer(self.server_env(), self.home, io.BytesIO(b""), io.BytesIO(),
                                             stderr)
        server.test_stderr = stderr
        self.addCleanup(server.shutdown)
        self._schemas = {item["name"]: item for item in server.definitions}
        return server

    def call_in(self, server: Any, name: str, arguments: Dict[str, Any],
                cancelled: Optional[threading.Event] = None) -> Dict[str, Any]:
        result = server.call_tool(server.tools[name], arguments, cancelled or threading.Event())
        self.check(result, name)
        return result

    def call(self, server: ServerProcess, name: str, arguments: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        result = server.call(name, arguments, timeout=timeout)
        self.check(result, name)
        return result

    def check(self, result: Dict[str, Any], name: str) -> Dict[str, Any]:
        structured = result["structuredContent"]
        self.assertEqual(structured["tool"], name)
        self.assertIs(result["isError"], not structured["ok"])
        self.assertEqual(result["content"][0]["text"], json.dumps(structured, ensure_ascii=False, sort_keys=True))
        problems = MODULES["common"].validate(self._schemas[name]["outputSchema"], structured)
        self.assertEqual(problems, [], structured)
        for key in (result.get("_meta") or {}):
            self.assertIn(key, ("autonom/frames", "autonom/artifact"))
        return structured

    def ok(self, result: Dict[str, Any]) -> Dict[str, Any]:
        structured = result["structuredContent"]
        self.assertTrue(structured["ok"], structured)
        self.assertEqual(structured["outcome"], "completed")
        return structured

    def refused(self, result: Dict[str, Any], code: str, outcome: str = "rejected") -> Dict[str, Any]:
        structured = result["structuredContent"]
        self.assertFalse(structured["ok"], structured)
        self.assertEqual((structured["outcome"], structured["error_code"]), (outcome, code), structured)
        self.assertIsInstance(structured["hint"], (str, type(None)))
        return structured

    def ref(self, session_id: str) -> Dict[str, str]:
        return {"host_id": self.host_id, "session_id": session_id}

    # -- fake Canvas evidence

    def canvas_events(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        try:
            lines = self.canvas_log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        events = [json.loads(line) for line in lines if line.strip()]
        return [event for event in events if kind is None or event.get("t") == kind]

    def requests(self, path_suffix: Optional[str] = None, method: Optional[str] = None) -> List[Dict[str, Any]]:
        return [event for event in self.canvas_events("http")
                if (path_suffix is None or event["path"].endswith(path_suffix))
                and (method is None or event["method"] == method)]

    def discovery(self) -> List[Dict[str, Any]]:
        out = []
        for path in sorted((self.state_base / "canvas").glob("*.json")):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        return out

    def token(self) -> str:
        documents = self.discovery()
        self.assertEqual(len(documents), 1, documents)
        return documents[0]["token"]

    def cli_calls(self, key: Optional[str] = None) -> List[Dict[str, Any]]:
        return [call for call in FAKE.read_log(self.cli_log) if key is None or call["key"] == key]

    def assert_explicit_origins(self) -> None:
        events = self.canvas_events("http")
        self.assertTrue(events)
        for event in events:
            self.assertIn(event["origin_header"], ("human", "agent"), event)

    def assert_only_keyframe_requests(self) -> List[Dict[str, Any]]:
        """Every message on every control connection is a key-frame request, at most one per second per device."""
        messages = self.canvas_events("control_message")
        by_device: Dict[str, List[float]] = {}
        for message in messages:
            self.assertFalse(message["binary"], message)
            self.assertEqual(json.loads(message["data"]), KEYFRAME, message)
            by_device.setdefault(message["device"], []).append(message["at"])
        for times in by_device.values():
            for earlier, later in zip(times, times[1:]):
                self.assertGreaterEqual(later - earlier, 0.9, times)
        return messages

    def assert_token_hidden(self, token: str, server: Optional[ServerProcess] = None, extra: str = "") -> None:
        texts = [extra]
        if server is not None:
            texts += [line.decode("utf-8", "replace") for line in server.raw_lines]
            texts.append(server.stderr_text())
        try:
            texts.append(self.cli_log.read_text(encoding="utf-8"))
        except OSError:
            pass
        texts.append(self.canvas_log_path.read_text(encoding="utf-8") if self.canvas_log_path.exists() else "")
        for text in texts:
            self.assertNotIn(token, text)
            self.assertNotIn("#token=", text)

    def _kill_leftovers(self) -> None:
        pids = {event.get("pid") for event in self.canvas_events("serve")}
        pids |= {document.get("pid") for document in self.discovery()}
        for call in FAKE.read_log(self.cli_log):
            pids.add(call.get("pid"))
        for pid in pids:
            if not pid_alive(pid) or pid == os.getpid():
                continue
            command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True, check=False).stdout
            if FAKE_CANVAS.name in command or FAKE_CLI.name in command:  # never a reused pid
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    # -- pulls

    def pull(self, call: Callable[..., Dict[str, Any]], target: Any, stream_id: str, after_seq: int,
             **extra: Any) -> Dict[str, Any]:
        arguments = dict({"stream_id": stream_id, "after_seq": after_seq}, **extra)
        result = call(target, "get_frames", arguments)
        structured = self.ok(result)
        frames = result.get("_meta", {}).get("autonom/frames", {}).get("frames", [])
        self.assertEqual(len(frames), structured["frames"])
        self.assertEqual(sum(len(base64.b64decode(item["data_b64"])) for item in frames), structured["bytes"])
        if frames:
            self.assertEqual((frames[0]["seq"], frames[-1]["seq"]), (structured["first_seq"], structured["last_seq"]))
            seqs = [item["seq"] for item in frames]
            self.assertEqual(seqs, list(range(seqs[0], seqs[0] + len(seqs))), "gap in %r" % seqs)
        return {"structured": structured, "frames": frames, "meta": result.get("_meta", {}).get("autonom/frames")}


# ====================================================================== streams

class StreamTests(MediaCase):
    def test_ordered_delivery_resync_bearer_and_no_token(self) -> None:
        """p3-013-S01: gap-free pulls within the budget, then resync at a key frame after eviction."""
        self.write_config({"stream_buffer_bytes": 1048576})
        # 20 packets, a pause for the first pulls, then the rest fast enough to evict.
        self.write_canvas({"video": {"packets": 300, "packet_bytes": 20480, "key_every": 30, "interval_ms": 2,
                                     "pause_after": 20, "pause_ms": 1500}})
        session_id = self.add_session()
        server = self.started()
        opened = self.ok(self.call(server, "open_stream", dict(self.ref(session_id), profile="focus")))
        stream = opened["stream"]
        self.assertEqual((stream["state"], stream["profile"], stream["session"]),
                         ("open", "focus", self.ref(session_id)))
        self.assertEqual((stream["width"], stream["height"], stream["codec"], stream["epoch"]),
                         (1080, 2400, "avc1.64001F", 0))
        token = self.token()
        after = 0
        for index in range(3):
            got = self.pull(self.call, server, stream["stream_id"], after, max_bytes=131072, wait_ms=1000)
            frames = got["frames"]
            self.assertTrue(frames, "pull %d returned nothing" % index)
            self.assertEqual(frames[0]["seq"], after + 1)
            self.assertFalse(got["structured"]["resync"])
            self.assertTrue(got["structured"]["bytes"] <= 131072 or len(frames) == 1)
            if index == 0:
                self.assertTrue(frames[0]["key"])
                self.assertEqual(base64.b64decode(frames[0]["data_b64"])[:5], b"\x00\x00\x00\x01\x65")
                self.assertEqual(base64.b64decode(got["meta"]["config_b64"]), CONFIG_FIXTURE)
            after = got["structured"]["last_seq"]
        self.assertTrue(wait_until(lambda: self.canvas_events("video_done"), 20), "the fake never finished")
        late = self.pull(self.call, server, stream["stream_id"], after, max_bytes=131072, wait_ms=0)
        self.assertTrue(late["structured"]["resync"])
        self.assertTrue(late["frames"][0]["key"])
        self.assertGreater(late["structured"]["first_seq"], after + 1)
        self.assertGreater(late["structured"]["dropped"], 0)
        self.assertLessEqual(late["structured"]["stream"]["buffer_bytes"], 1048576)
        self.assertEqual((late["structured"]["stream"]["width"], late["structured"]["stream"]["height"]),
                         (1080, 2400))
        self.assertEqual(base64.b64decode(late["meta"]["config_b64"]), CONFIG_FIXTURE)
        # The video upgrade is an API client: Bearer, no Origin header, no ?origin= and no query token.
        upgrades = self.canvas_events("upgrade")
        video = [item for item in upgrades if item["kind"] == "video"]
        control = [item for item in upgrades if item["kind"] == "control"]
        self.assertEqual(len(video), 1)
        self.assertEqual((video[0]["auth"], video[0]["origin_header"], video[0]["origin_param"],
                          video[0]["query_token"]), (True, False, None, False))
        self.assertEqual([(item["origin_param"], item["origin_header"]) for item in control], [("human", False)])
        self.assert_only_keyframe_requests()
        self.assert_explicit_origins()
        # The fake applies the Canvas fail-closed rule: a request without an origin counts as agent.
        documents = self.discovery()
        probe = urllib.request.Request("http://127.0.0.1:%d/api/devices" % documents[0]["port"],
                                       headers={"Authorization": "Bearer " + token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(probe, timeout=10) as answer:
            self.assertEqual(answer.status, 200)
        last = self.canvas_events("http")[-1]
        self.assertEqual((last["origin_header"], last["origin"]), (None, "agent"))
        self.ok(self.call(server, "close_stream", {"stream_id": stream["stream_id"]}))
        self.refused(self.call(server, "get_frames", {"stream_id": stream["stream_id"], "after_seq": 0}),
                     "stream_not_found")
        serve = self.cli_calls("canvas_serve")
        self.assertEqual(len(serve), 1)
        self.assertEqual(serve[0]["argv"], ["canvas", "serve", "--workspace", "chatgpt", "--ephemeral",
                                            "--bootable-only", "--port", "0", "--control-idle", "120"])
        self.assertEqual(server.close(), 0)
        self.assert_token_hidden(token, server)

    def test_resync_after_a_new_video_session(self) -> None:
        self.write_canvas({"video": {"packets": 120, "packet_bytes": 4096, "key_every": 30, "interval_ms": 5,
                                     "pause_after": 30, "pause_ms": 1500, "new_session_at": 60,
                                     "new_width": 720, "new_height": 1280}})
        session_id = self.add_session()
        server = self.started()
        stream = self.ok(self.call(server, "open_stream", dict(self.ref(session_id), profile="focus")))["stream"]
        first = self.pull(self.call, server, stream["stream_id"], 0, epoch=0, wait_ms=1000)
        self.assertFalse(first["structured"]["resync"])
        after = first["structured"]["last_seq"]
        self.assertLessEqual(after, 30)
        self.assertTrue(wait_until(lambda: self.canvas_events("video_done"), 20))
        late = self.pull(self.call, server, stream["stream_id"], after, epoch=0, wait_ms=0)
        view = late["structured"]["stream"]
        self.assertTrue(late["structured"]["resync"])
        self.assertEqual((view["epoch"], view["width"], view["height"]), (1, 720, 1280))
        self.assertTrue(late["frames"][0]["key"])
        self.assertGreater(late["structured"]["first_seq"], 60)
        # With the new epoch and its own last seq, the next pull is no resync.
        again = self.pull(self.call, server, stream["stream_id"], late["structured"]["last_seq"], epoch=1, wait_ms=0)
        self.assertFalse(again["structured"]["resync"])
        self.assert_only_keyframe_requests()

    def test_binding_mismatch_detaches_only_a_device_this_call_attached(self) -> None:
        """p3-013-S03."""
        self.write_canvas({"bind_session": "s_other", "preattach": [{"platform": "ios", "target": IOS_UDID}]})
        android = self.add_session()
        ios = self.add_session("ios", IOS_UDID)
        server = self.started()
        result = self.refused(self.call(server, "open_stream", dict(self.ref(android), profile="focus")),
                              "session_binding_mismatch")
        self.assertIn("s_other", result["error"])
        self.refused(self.call(server, "open_stream", dict(self.ref(ios), profile="thumb")),
                     "session_binding_mismatch")
        detached = [event["device"] for event in self.canvas_events("detach")]
        self.assertEqual(detached, [ANDROID_DEVICE])
        self.assertEqual(len(self.cli_calls("canvas_detach")), 1)
        self.assertEqual(self.canvas_events("upgrade"), [])

    def test_thumb_key_frames_on_demand(self) -> None:
        """p3-013-S04: key frames every 5 s only, yet a thumb every 1-2 s, one request per second at most."""
        self.write_canvas({"video": {"packets": None, "packet_bytes": 2000, "key_every": 0,
                                     "key_interval_ms": 5000, "interval_ms": 50}})
        session_id = self.add_session()
        # In process, so the limits are checked on the times the server itself records: a gap
        # measured by the caller also holds the reply latency, which a loaded machine stretches.
        server = self.in_process()
        stream = self.ok(self.call_in(server, "open_stream", dict(self.ref(session_id), profile="thumb")))["stream"]
        live = server.services.extensions["mcp_media"].streams[stream["stream_id"]]
        thumb_times: List[float] = []
        request_times: List[float] = []
        thumb, request = live._thumb, live._maybe_request_keyframe

        def recorded_thumb(after_seq: int, epoch: Optional[int]) -> Any:
            frames, resync = thumb(after_seq, epoch)
            if frames:
                thumb_times.append(live.last_thumb)
            return frames, resync

        def recorded_request() -> None:
            sent = live.keyframe_requests
            request()
            if live.keyframe_requests > sent:
                request_times.append(live.last_keyframe_request)

        live._thumb, live._maybe_request_keyframe = recorded_thumb, recorded_request
        deliveries: List[float] = []
        after = 0
        end = time.monotonic() + 10.0
        while time.monotonic() < end:
            got = self.pull(self.call_in, server, stream["stream_id"], after, wait_ms=1000)
            if got["frames"]:
                self.assertEqual(len(got["frames"]), 1)
                self.assertTrue(got["frames"][0]["key"])
                self.assertGreater(got["frames"][0]["seq"], after)
                after = got["structured"]["last_seq"]
                deliveries.append(time.monotonic())
        self.ok(self.call_in(server, "close_stream", {"stream_id": stream["stream_id"]}))
        # The product limits, on the server's own clock: one thumb and one key-frame request
        # per second at most.
        self.assertEqual(len(thumb_times), len(deliveries))
        server_gaps = [later - earlier for earlier, later in zip(thumb_times, thumb_times[1:])]
        for gap in server_gaps:
            self.assertGreaterEqual(gap, MODULES["media"].THUMB_INTERVAL_S, server_gaps)
        request_gaps = [later - earlier for earlier, later in zip(request_times, request_times[1:])]
        self.assertGreaterEqual(len(request_times), 5, request_gaps)
        for gap in request_gaps:
            self.assertGreaterEqual(gap, MODULES["media"].KEYFRAME_INTERVAL_S, request_gaps)
        # The caller's view only as a loose bound over the whole run, so one late reply cannot fail it.
        self.assertGreaterEqual(len(deliveries), 6, server_gaps)
        mean_gap = (deliveries[-1] - deliveries[0]) / (len(deliveries) - 1)
        self.assertGreaterEqual(mean_gap, 0.9, server_gaps)
        self.assertLessEqual(mean_gap, 2.0, server_gaps)
        self.assertTrue(wait_until(lambda: len(self.canvas_events("control_message")) >= len(request_times), 10))
        messages = self.canvas_events("control_message")
        self.assertEqual(len(messages), len(request_times))
        for message in messages:
            self.assertEqual((message["origin"], message["binary"], json.loads(message["data"])),
                             ("agent", False, KEYFRAME))
        times = [message["at"] for message in messages]
        self.assertGreaterEqual((times[-1] - times[0]) / (len(times) - 1), 0.9, times)
        control = [item for item in self.canvas_events("upgrade") if item["kind"] == "control"]
        self.assertEqual([item["origin_param"] for item in control], ["agent"])

    def test_stream_limit_close_presence_and_cancellation(self) -> None:
        self.write_config({"max_streams": 1})
        self.write_canvas({"video": {"packets": 1, "packet_bytes": 1000}})
        session_id = self.add_session()
        server = self.started()
        focus = self.ok(self.call(server, "open_stream", dict(self.ref(session_id), profile="focus")))["stream"]
        self.refused(self.call(server, "open_stream", dict(self.ref(session_id), profile="thumb")), "stream_limit")
        upgrades = [item for item in self.canvas_events("upgrade") if item["kind"] == "control"]
        self.assertEqual([item["origin_param"] for item in upgrades], ["human"])
        # A waiting get_frames that is cancelled sends no answer, and the server goes on at once.
        msg_id = server.new_id()
        server.send({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                     "params": {"name": "get_frames",
                                "arguments": {"stream_id": focus["stream_id"], "after_seq": 1, "wait_ms": 1000}}})
        time.sleep(0.1)
        server.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": msg_id}})
        started = time.monotonic()
        self.assertIn("result", server.request("ping"))
        self.assertLess(time.monotonic() - started, 1.0)
        self.ok(self.call(server, "close_stream", {"stream_id": focus["stream_id"]}))
        self.assertTrue(wait_until(lambda: [item for item in self.canvas_events("ws_close")
                                            if item["kind"] == "control" and item["origin"] == "human"], 10),
                        "the presence connection stayed open")
        self.refused(self.call(server, "close_stream", {"stream_id": focus["stream_id"]}), "stream_not_found")
        thumb = self.ok(self.call(server, "open_stream", dict(self.ref(session_id), profile="thumb")))["stream"]
        self.assertEqual(thumb["profile"], "thumb")
        time.sleep(1.2)
        answers = [line for line in server.raw_lines if b'"id":%d,' % msg_id in line or
                   line.endswith(b'"id":%d}\n' % msg_id)]
        self.assertEqual(answers, [], "the cancelled get_frames was answered")
        upgrades = [item for item in self.canvas_events("upgrade") if item["kind"] == "control"]
        self.assertEqual([item["origin_param"] for item in upgrades], ["human", "agent"])


class StreamInProcessTests(MediaCase):
    def test_backpressure_bound_and_idle_close(self) -> None:
        """p3-013-S02: a never-pulled focus stream stays under its cap while 64 MiB arrive, then closes."""
        self.write_config({"stream_buffer_bytes": 1048576, "stream_idle_s": 6})
        self.write_canvas({"video": {"packets": 1024, "packet_bytes": 65536, "key_every": 30}})
        session_id = self.add_session()
        server = self.in_process()
        stream_id = self.ok(self.call_in(server, "open_stream",
                                         dict(self.ref(session_id), profile="focus")))["stream"]["stream_id"]
        media = server.services.extensions["mcp_media"]
        stream = media.streams[stream_id]
        # The idle clock of a never-pulled stream starts when the stream is made (same monotonic clock,
        # in process), so a slow Canvas start on a loaded machine does not count as idle time.
        with stream.cond:
            idle_from = stream.last_pull
        samples: List[int] = []
        counts: List[int] = []
        while stream_id in media.streams and time.monotonic() - idle_from < 15:
            with stream.cond:
                samples.append(stream.bytes)
                counts.append(len(stream.packets))
            time.sleep(0.1)
        closed_after = time.monotonic() - idle_from
        done = self.canvas_events("video_done")
        self.assertTrue(done, "the fake did not finish sending")
        self.assertGreaterEqual(done[0]["bytes"], 64 * 1024 * 1024)
        self.assertTrue(samples)
        self.assertLessEqual(max(samples), 1048576)
        self.assertLessEqual(max(counts), 2000)
        self.assertGreater(max(samples), 0)
        self.assertNotIn(stream_id, media.streams)
        self.assertGreaterEqual(closed_after, 5.9)
        self.assertLess(closed_after, 9.5)
        self.refused(self.call_in(server, "get_frames", {"stream_id": stream_id, "after_seq": 0}), "stream_not_found")
        self.assertTrue(wait_until(lambda: [item for item in self.canvas_events("ws_close")
                                            if item["kind"] == "control"], 10))

    def test_parallel_opens_never_pass_max_streams(self) -> None:
        self.write_config({"max_streams": 1})
        self.write_canvas({"video": {"packets": 1, "packet_bytes": 1000}})
        session_id = self.add_session()
        server = self.in_process()
        media = server.services.extensions["mcp_media"]
        for round_index in range(2):
            barrier = threading.Barrier(3)
            results: List[Dict[str, Any]] = []

            def open_one(profile: str) -> None:
                barrier.wait()
                arguments = dict(self.ref(session_id), profile=profile)
                results.append(server.call_tool(server.tools["open_stream"], arguments, threading.Event()))

            threads = [threading.Thread(target=open_one, args=(profile,)) for profile in ("focus", "thumb", "thumb")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
            self.assertEqual(len(results), 3)
            structured = [self.check(result, "open_stream") for result in results]
            opened = [item for item in structured if item["ok"]]
            limited = [item for item in structured if not item["ok"]]
            self.assertEqual(len(opened), 1, structured)
            self.assertEqual([(item["outcome"], item["error_code"]) for item in limited],
                             [("rejected", "stream_limit")] * 2, structured)
            self.assertEqual(list(media.streams), [opened[0]["stream"]["stream_id"]])
            self.assertEqual(media._reserved, 0)
            self.ok(self.call_in(server, "close_stream", {"stream_id": opened[0]["stream"]["stream_id"]}))
            self.assertEqual(media.streams, {}, round_index)

    def test_no_canvas_starts_after_the_canvas_hook(self) -> None:
        self.write_canvas({"video": {"packets": 1, "packet_bytes": 1000}})
        session_id = self.add_session()
        server = self.in_process()
        media = server.services.extensions["mcp_media"]
        self.ok(self.call_in(server, "open_stream", dict(self.ref(session_id), profile="focus")))
        self.assertEqual(len(self.cli_calls("canvas_serve")), 1)
        token = self.token()
        # The shutdown hooks in their order (streams 20, canvas 40), while handlers may still run.
        media.shutdown_streams()
        media.shutdown_canvas()
        self.assertEqual(len(self.cli_calls("canvas_stop")), 1)
        self.assertTrue(wait_until(lambda: self.discovery() == [], 10), "the started Canvas still runs")
        tap = self.call_in(server, "send_input", dict(self.ref(session_id), event={"op": "tap", "x": 0.5, "y": 0.5}))
        self.refused(tap, "canvas_unavailable")
        self.refused(self.call_in(server, "pause_session", self.ref(session_id)), "canvas_unavailable")
        self.refused(self.call_in(server, "open_stream", dict(self.ref(session_id), profile="thumb")),
                     "canvas_unavailable")
        with self.assertRaises(MODULES["common"].ToolError):
            media.link.ensure()
        time.sleep(0.5)
        self.assertEqual(len(self.cli_calls("canvas_serve")), 1)
        self.assertEqual(len(self.cli_calls("canvas_attach")), 1)
        self.assertEqual(self.discovery(), [])
        self.assertEqual((media.link.doc, media.link.child, media.link.started_here), (None, None, False))
        self.assertEqual(media.streams, {})
        self.assert_token_hidden(token, extra=server.test_stderr.getvalue())

    def test_cancellation_ends_a_waiting_pull_at_once(self) -> None:
        self.write_canvas({"video": {"packets": 1, "packet_bytes": 1000}})
        session_id = self.add_session()
        server = self.in_process()
        stream_id = self.ok(self.call_in(server, "open_stream",
                                         dict(self.ref(session_id), profile="focus")))["stream"]["stream_id"]
        first = self.pull(self.call_in, server, stream_id, 0, wait_ms=1000)
        self.assertEqual(first["structured"]["last_seq"], 1)
        cancelled = threading.Event()
        timer = threading.Timer(0.15, cancelled.set)
        timer.start()
        started = time.monotonic()
        result = self.call_in(server, "get_frames", {"stream_id": stream_id, "after_seq": 1, "wait_ms": 1000},
                              cancelled)
        took = time.monotonic() - started
        timer.join()
        self.assertEqual(self.ok(result)["frames"], 0)
        self.assertLess(took, 0.6)
        idle = self.call_in(server, "get_frames", {"stream_id": stream_id, "after_seq": 1, "wait_ms": 300})
        self.assertEqual(self.ok(idle)["frames"], 0)
        self.assertGreaterEqual(self.ok(idle)["server_ms"], 250)
        refused = self.call_in(server, "get_frames", {"stream_id": stream_id, "after_seq": 1,
                                                      "max_bytes": 1048576})
        self.assertEqual(self.refused(refused, "invalid_arguments")["field"], "max_bytes")


# ====================================================================== input

class InputTests(MediaCase):
    def test_input_mapping_outcomes_and_one_request_each(self) -> None:
        """p3-014-S01/S02 and the 5.4 answer table, with exactly one request per call."""
        self.write_config({"cli_timeout_scale": 0.2})
        self.write_canvas({"script": {"tap": ["200", "409:control_held", "hang", "400", "409", "401", "403",
                                              "404", "500"]}})
        session_id = self.add_session()
        server = self.started()
        ref = self.ref(session_id)

        def tap(x: float = 0.5, y: float = 0.25) -> Dict[str, Any]:
            return self.call(server, "send_input", dict(ref, event={"op": "tap", "x": x, "y": y}), timeout=40)

        done = self.ok(tap())
        self.assertEqual(done["input"], {"op": "tap", "operation_id": done["operation_id"]})
        self.assertEqual(done["session"], ref)
        first = self.requests("/tap")
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["body"], {"x": 540, "y": 600, "dw": 1080, "dh": 2400})
        self.assertEqual((first[0]["origin_header"], first[0]["path"]), ("human", "/d/%s/tap" % ANDROID_DEVICE))
        held = self.refused(tap(), "control_held")
        self.assertIsInstance(held["control"], dict)
        self.assertIn(held["control"]["state"], ("none", "free", "held", "expired", "unknown"))
        self.assertTrue(held["retry_safe"])
        lost = self.refused(tap(), "input_failed", "outcome_unknown")
        self.assertFalse(lost["retry_safe"])
        bad = self.refused(tap(), "invalid_arguments")
        self.assertEqual(bad["field"], "event")
        paused = self.refused(tap(), "canvas_refused")
        self.assertIn("Canvas input is paused", paused["error"])
        for _status in (401, 403, 404):
            self.refused(tap(), "canvas_unavailable")
        self.refused(tap(), "input_failed", "outcome_unknown")
        self.assertEqual(len(self.requests("/tap", "POST")), 9)

        self.ok(self.call(server, "send_input", dict(ref, event={"op": "swipe", "x1": 0.1, "y1": 0.9, "x2": 0.1,
                                                                 "y2": 0.1, "duration_ms": 300})))
        self.assertEqual(self.requests("/swipe")[-1]["body"],
                         {"x1": 108, "y1": 2160, "x2": 108, "y2": 240, "duration": 300, "dw": 1080, "dh": 2400})
        self.ok(self.call(server, "send_input", dict(ref, event={"op": "key", "key": "BACK"})))
        self.ok(self.call(server, "send_input", dict(ref, event={"op": "system", "button": "home"})))
        self.ok(self.call(server, "send_input", dict(ref, event={"op": "system", "button": "volume-up"})))
        self.assertEqual([item["body"] for item in self.requests("/key")],
                         [{"key": "KEYCODE_BACK"}, {"key": "KEYCODE_HOME"}, {"key": "KEYCODE_VOLUME_UP"}])
        secret = "hunter2 very secret"
        typed = self.ok(self.call(server, "send_input",
                                  dict(ref, event={"op": "text", "text": secret, "sensitive": True})))
        self.assertEqual(typed["input"]["op"], "text")
        self.assertEqual(self.requests("/text")[-1]["body"], {"text": secret, "sensitive": True})
        for route in ("/swipe", "/key", "/text"):
            self.assertEqual(len(self.requests(route, "POST")), {"/swipe": 1, "/key": 3, "/text": 1}[route])

        before = len(self.canvas_events("http"))
        missing = self.refused(self.call(server, "send_input", dict(ref, event={"op": "tap", "x": 0.5})),
                               "invalid_arguments")
        self.assertEqual(missing["field"], "event.y")
        self.refused(self.call(server, "send_input", dict(ref, event={"op": "tap", "x": 1.5, "y": 0.5})),
                     "invalid_arguments")
        self.refused(self.call(server, "send_input", dict(ref, event={"op": "text", "text": "a\u0001b"})),
                     "invalid_arguments")
        self.refused(self.call(server, "send_input", dict(ref, event={"op": "key", "key": "BACK", "x": 0.1})),
                     "invalid_arguments")
        self.refused(self.call(server, "send_input", dict(ref, event={"op": "scroll"})), "invalid_arguments")
        self.assertEqual(len(self.canvas_events("http")), before)
        self.assert_explicit_origins()
        token = self.token()
        self.assertEqual(server.close(), 0)
        output = b"".join(server.raw_lines).decode("utf-8") + server.stderr_text()
        self.assertNotIn(secret, output)
        self.assert_token_hidden(token, server)

    def test_unknown_session_and_stopped_session_send_nothing(self) -> None:
        stopped = FAKE.add_session(self.fake_state, "android", ANDROID, live=False)
        server = self.started()
        self.refused(self.call(server, "send_input", dict(self.ref("s_missing"), event={"op": "key", "key": "HOME"})),
                     "session_not_found")
        self.refused(self.call(server, "send_input", dict(self.ref(stopped), event={"op": "key", "key": "HOME"})),
                     "session_stopped")
        self.refused(self.call(server, "pause_session", self.ref(stopped)), "session_stopped")
        self.assertEqual(self.cli_calls("canvas_serve"), [])
        self.assertEqual(self.canvas_events("http"), [])


# ====================================================================== pause and resume

class PauseTests(MediaCase):
    def test_pause_and_resume_while_a_run_holds_the_guard(self) -> None:
        """p3-014-S03: both completed (no session_busy), takeover then release as human, fresh acquire after."""
        session_id = self.add_session()
        server = self.in_process()
        services = server.services
        self.ok(self.call_in(server, "test_hold_run", dict(self.ref(session_id), ms=30000)))
        self.assertIsNotNone(services.guard.busy(session_id))
        ref = services.refs.resolve(self.host_id, session_id)
        keeper = services.keeper
        keeper.ensure(ref)
        keeper.ensure(ref)
        self.assertEqual(len(self.cli_calls("control_acquire")), 1)

        paused = self.ok(self.call_in(server, "pause_session", self.ref(session_id)))
        port, pid = self.discovery()[0]["port"], self.discovery()[0]["pid"]
        control = paused["control"]
        self.assertEqual((control["state"], control["role"], control["mine"], control["generation"]),
                         ("held", "human", True, 1))
        self.assertEqual(control["holder"]["id"], "canvas:%d:%d" % (port, pid))
        self.assertIsNone(keeper.cached(ref["platform"], ref["target_id"]))
        keeper.ensure(ref)
        self.assertEqual(len(self.cli_calls("control_acquire")), 2)

        resumed = self.ok(self.call_in(server, "resume_session", self.ref(session_id)))
        self.assertEqual((resumed["control"]["state"], resumed["control"]["holder"]), ("free", None))
        keeper.ensure(ref)
        self.assertEqual(len(self.cli_calls("control_acquire")), 3)
        bodies = [(event["body"], event["origin_header"]) for event in self.requests("/control", "POST")]
        self.assertEqual(bodies, [({"mode": "takeover"}, "human"), ({"mode": "release"}, "human")])
        self.assertIsNotNone(services.guard.busy(session_id))
        self.assert_explicit_origins()

    def test_pause_refusals_map_as_input_and_drop_the_cache(self) -> None:
        self.write_config({"cli_timeout_scale": 0.2})
        self.write_canvas({"script": {"control": ["409:control_held", "503", "hang"]}})
        session_id = self.add_session()
        server = self.in_process()
        keeper = server.services.keeper
        ref = server.services.refs.resolve(self.host_id, session_id)
        keeper.ensure(ref)
        self.refused(self.call_in(server, "pause_session", self.ref(session_id)), "control_held")
        self.assertIsNone(keeper.cached(ref["platform"], ref["target_id"]))
        self.refused(self.call_in(server, "resume_session", self.ref(session_id)), "control_failed",
                     "outcome_unknown")
        self.refused(self.call_in(server, "pause_session", self.ref(session_id)), "control_failed",
                     "outcome_unknown")
        self.assertEqual(len(self.requests("/control", "POST")), 3)


# ====================================================================== shutdown

class ShutdownTests(MediaCase):
    def test_shutdown_stops_a_canvas_this_server_started(self) -> None:
        self.write_canvas({"video": {"packets": 5, "packet_bytes": 1000}})
        session_id = self.add_session()
        server = self.started()
        self.ok(self.call(server, "open_stream", dict(self.ref(session_id), profile="focus")))
        documents = self.discovery()
        self.assertEqual(len(documents), 1)
        pid = documents[0]["pid"]
        token = documents[0]["token"]
        self.assertTrue(pid_alive(pid))
        self.assertEqual(server.close(), 0)
        self.assertEqual(len(self.cli_calls("canvas_stop")), 1)
        self.assertTrue(wait_until(lambda: not pid_alive(pid), 10), "the started Canvas still runs")
        self.assertEqual(self.discovery(), [])
        self.assert_token_hidden(token, server)

    def test_shutdown_keeps_a_canvas_it_did_not_start(self) -> None:
        self.write_canvas({"video": {"packets": 5, "packet_bytes": 1000}})
        session_id = self.add_session()
        env = self.server_env()
        canvas = subprocess.Popen([NODE, str(FAKE_CANVAS), "canvas", "serve", "--workspace", "chatgpt", "--port", "0"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  env=env, start_new_session=True)
        self.addCleanup(lambda: canvas.poll() is None and canvas.kill())
        self.assertTrue(wait_until(self.discovery, 15), "the fake Canvas did not start")
        server = self.started()
        self.ok(self.call(server, "open_stream", dict(self.ref(session_id), profile="thumb")))
        self.assertEqual(self.cli_calls("canvas_serve"), [])
        self.assertEqual(len(self.cli_calls("canvas_attach")), 1)
        self.assertEqual(server.close(), 0)
        self.assertEqual(self.cli_calls("canvas_stop"), [])
        self.assertIsNone(canvas.poll())
        self.assertEqual(len(self.discovery()), 1)
        canvas.terminate()
        canvas.wait(timeout=10)


# ====================================================================== WebSocket client

def server_frame(opcode: int, payload: bytes, fin: bool = True, masked: bool = False) -> bytes:
    first = (0x80 if fin else 0) | opcode
    size = len(payload)
    mask_bit = 0x80 if masked else 0
    if size < 126:
        header = struct.pack("!BB", first, mask_bit | size)
    elif size < 65536:
        header = struct.pack("!BBH", first, mask_bit | 126, size)
    else:
        header = struct.pack("!BBQ", first, mask_bit | 127, size)
    if masked:
        key = b"\x01\x02\x03\x04"
        return header + key + bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return header + payload


def read_client_frame(conn: socket.socket) -> Dict[str, Any]:
    def exact(size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    first, second = exact(2)
    size = second & 0x7F
    if size == 126:
        size = struct.unpack("!H", exact(2))[0]
    elif size == 127:
        size = struct.unpack("!Q", exact(8))[0]
    masked = bool(second & 0x80)
    key = exact(4) if masked else b""
    payload = exact(size)
    if masked:
        payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return {"fin": bool(first & 0x80), "opcode": first & 0x0F, "masked": masked, "payload": payload}


class OwnershipRunner:
    """A runner seam for CanvasLink: ``spawn`` starts ``argv`` (a stand-in for the CLI),
    ``run`` records the verb and answers ok."""

    def __init__(self, argv: List[str], scale: float) -> None:
        self.argv = argv
        self._scale = scale
        self.calls: List[str] = []
        self.children: List[subprocess.Popen] = []

    def scale(self) -> float:
        return self._scale

    def spawn(self, key: str, params: Any = None, **_: Any) -> subprocess.Popen:
        self.calls.append(key)
        child = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        self.children.append(child)
        return child

    def run(self, key: str, params: Any = None, **_: Any) -> Any:
        self.calls.append(key)
        if key == "canvas_stop":  # the CLI stops the workspace Canvas, so our CLI ends too
            for child in self.children:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
        return type("Result", (), {"ok": True, "error_code": None})()


class CanvasOwnershipTests(unittest.TestCase):
    """Shutdown stops only a Canvas this server's own CLI started (contract 5.1, 5.6)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mcp-media-own-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.processes: List[subprocess.Popen] = []
        self.addCleanup(self._reap)

    def _reap(self) -> None:
        for proc in self.processes:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
            proc.wait(timeout=10)

    def link(self, argv: List[str], scale: float) -> Any:
        runner = OwnershipRunner(argv, scale)
        self.addCleanup(lambda: self.processes.extend(runner.children))
        services = type("Services", (), {})()
        services.config = {"canvas_workspace": "chatgpt"}
        services.runner = runner
        services.env = {"AUTONOM_HOME": str(self.tmp)}
        services.home = self.tmp
        services.keeper = None
        services.log = lambda message: None
        return MODULES["media"].CanvasLink(services), runner

    def other_canvas(self) -> subprocess.Popen:
        proc = subprocess.Popen(["sleep", "60"], stdin=subprocess.DEVNULL, start_new_session=True)
        self.processes.append(proc)
        return proc

    def write_document(self, pid: int, delay: float = 0.0) -> None:
        def write() -> None:
            time.sleep(delay)
            folder = self.tmp / "canvas"
            folder.mkdir(parents=True, exist_ok=True)
            document = {"schema": MODULES["media"].DISCOVERY_SCHEMA, "mode": "workspace", "workspace": "chatgpt",
                        "port": 45999, "pid": pid, "token": "own-test-token"}
            (folder / ("%d.json" % pid)).write_text(json.dumps(document), encoding="utf-8")
        if delay:
            thread = threading.Thread(target=write, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 5)
        else:
            write()

    def test_a_canvas_that_won_the_start_race_is_not_stopped(self) -> None:
        # Our CLI is still starting when another server's Canvas writes its document; it then
        # leaves with already_running (here: exits after 0.6 s).
        link, runner = self.link(["sleep", "0.6"], scale=1.0)
        other = self.other_canvas()
        self.write_document(other.pid, delay=0.15)
        doc = link.ensure()
        self.assertEqual(doc["pid"], other.pid)
        self.assertEqual((link.started_here, link.child), (False, None))
        self.assertIsNone(link.stop())
        self.assertEqual(runner.calls, ["canvas_serve"])
        self.assertIsNone(other.poll())

    def test_an_adopted_canvas_is_not_stopped_after_our_cli_ended(self) -> None:
        # The claim wait runs out while our CLI still runs, so the Canvas is taken for ours;
        # once our CLI has ended, the Canvas cannot be ours, and shutdown keeps it.
        link, runner = self.link(["sleep", "60"], scale=0.1)
        other = self.other_canvas()
        self.write_document(other.pid, delay=0.15)
        link.ensure()
        self.assertTrue(link.started_here)
        child = link.child
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=10)
        self.assertEqual(link.ensure()["pid"], other.pid)
        self.assertEqual((link.started_here, link.child), (False, None))
        self.assertIsNone(link.stop())
        self.assertNotIn("canvas_stop", runner.calls)
        self.assertIsNone(other.poll())
        # Without the ensure() in between, stop() itself tells.
        (self.tmp / "canvas" / ("%d.json" % other.pid)).unlink()
        link2, runner2 = self.link(["sleep", "60"], scale=0.1)
        self.write_document(other.pid, delay=0.15)
        link2.ensure()
        self.assertTrue(link2.started_here)
        os.killpg(link2.child.pid, signal.SIGKILL)
        link2.child.wait(timeout=10)
        self.assertIsNone(link2.stop())
        self.assertNotIn("canvas_stop", runner2.calls)
        self.assertIsNone(other.poll())

    def test_a_canvas_our_cli_started_is_ours_at_once_and_stopped(self) -> None:
        # The document names a child of our CLI (as run_supervised starts node): no claim wait.
        pid_file = self.tmp / "grandchild.pid"
        script = "sleep 60 & echo $! > %s; wait" % pid_file
        link, runner = self.link(["sh", "-c", script], scale=10.0)

        def document_when_ready() -> None:
            text = wait_until(lambda: pid_file.exists() and pid_file.read_text().strip(), 10)
            self.write_document(int(text))
        thread = threading.Thread(target=document_when_ready, daemon=True)
        thread.start()
        started = time.monotonic()
        doc = link.ensure()
        thread.join(10)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(doc["pid"], int(pid_file.read_text().strip()))
        self.assertTrue(link.started_here)
        self.assertIsNotNone(link.child)
        self.assertEqual(link.stop(), "stopped")
        self.assertEqual(runner.calls, ["canvas_serve", "canvas_stop"])
        self.assertEqual((link.doc, link.child, link.started_here), (None, None, False))


class FakeWsServer:
    """A one-connection raw RFC 6455 server that runs ``script(conn)`` after the handshake."""

    def __init__(self, script: Callable[[socket.socket], None], accept: Optional[str] = None) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.script = script
        self.accept_override = accept
        self.request_head = ""
        self.received: List[Dict[str, Any]] = []
        self.error: Optional[BaseException] = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            conn, _addr = self.listener.accept()
            conn.settimeout(10)
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
            self.request_head = head
            key = next(line.split(":", 1)[1].strip() for line in head.split("\r\n")
                       if line.lower().startswith("sec-websocket-key:"))
            accept = self.accept_override or base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode("latin-1"))
            self.script(conn)
            conn.close()
        except BaseException as exc:  # noqa: BLE001 - reported by the test
            self.error = exc
        finally:
            self.listener.close()

    def join(self) -> None:
        self.thread.join(timeout=10)


class WebSocketClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = MODULES["ws"]

    def connect(self, server: FakeWsServer, **options: Any) -> Any:
        return self.ws.WebSocketClient.connect("127.0.0.1", server.port, "/d/x/ws/video",
                                               {"Authorization": "Bearer abc"}, timeout=5, **options)

    def test_handshake_fragmentation_ping_text_and_masked_send(self) -> None:
        def script(conn: socket.socket) -> None:
            conn.sendall(server_frame(0x2, b"abc", fin=False) + server_frame(0x9, b"hi") +
                         server_frame(0x0, b"def", fin=False) + server_frame(0x0, b"ghi"))
            server.received.append(read_client_frame(conn))  # the pong
            conn.sendall(server_frame(0x1, "héllo".encode("utf-8")))
            server.received.append(read_client_frame(conn))  # the client's text

        server = FakeWsServer(script)
        client = self.connect(server)
        self.assertEqual(client.recv(), (self.ws.OP_BINARY, b"abcdefghi"))
        self.assertEqual(client.recv(), (self.ws.OP_TEXT, "héllo".encode("utf-8")))
        client.send_text(json.dumps(KEYFRAME))
        server.join()
        self.assertIsNone(server.error)
        head = server.request_head.lower()
        self.assertIn("authorization: bearer abc", head)
        self.assertNotIn("\r\norigin:", head)
        self.assertTrue(head.startswith("get /d/x/ws/video http/1.1"))
        pong, text = server.received
        self.assertEqual((pong["opcode"], pong["payload"], pong["masked"]), (0xA, b"hi", True))
        self.assertEqual((text["opcode"], json.loads(text["payload"]), text["masked"]), (0x1, KEYFRAME, True))
        client.close()

    def test_oversize_message_is_refused_with_1009(self) -> None:
        def script(conn: socket.socket) -> None:
            conn.sendall(server_frame(0x2, b"x" * 2048))
            server.received.append(read_client_frame(conn))

        server = FakeWsServer(script)
        client = self.connect(server, max_message_bytes=1024)
        with self.assertRaises(self.ws.WebSocketError):
            client.recv()
        server.join()
        close = server.received[0]
        self.assertEqual((close["opcode"], struct.unpack("!H", close["payload"][:2])[0]), (0x8, 1009))

    def test_fragments_beyond_the_cap_are_refused(self) -> None:
        def script(conn: socket.socket) -> None:
            conn.sendall(server_frame(0x2, b"x" * 800, fin=False) + server_frame(0x0, b"y" * 800))
            server.received.append(read_client_frame(conn))

        server = FakeWsServer(script)
        client = self.connect(server, max_message_bytes=1024)
        with self.assertRaises(self.ws.WebSocketError):
            client.recv()
        server.join()
        self.assertEqual(struct.unpack("!H", server.received[0]["payload"][:2])[0], 1009)

    def test_close_handshake_and_protocol_errors(self) -> None:
        def script(conn: socket.socket) -> None:
            conn.sendall(server_frame(0x8, struct.pack("!H", 1000) + b"bye"))
            server.received.append(read_client_frame(conn))

        server = FakeWsServer(script)
        client = self.connect(server)
        with self.assertRaises(self.ws.WebSocketClosed) as caught:
            client.recv()
        self.assertEqual((caught.exception.code, caught.exception.reason), (1000, "bye"))
        server.join()
        self.assertEqual(struct.unpack("!H", server.received[0]["payload"][:2])[0], 1000)
        with self.assertRaises(self.ws.WebSocketClosed):
            client.recv()

        def masked(conn: socket.socket) -> None:
            conn.sendall(server_frame(0x2, b"abc", masked=True))
            server_two.received.append(read_client_frame(conn))

        server_two = FakeWsServer(masked)
        client = self.connect(server_two)
        with self.assertRaises(self.ws.WebSocketError):
            client.recv()
        server_two.join()
        self.assertEqual(struct.unpack("!H", server_two.received[0]["payload"][:2])[0], 1002)

    def test_handshake_with_a_wrong_accept_fails(self) -> None:
        server = FakeWsServer(lambda conn: None, accept="d3Jvbmc=")
        with self.assertRaises(self.ws.WebSocketError):
            self.connect(server)
        server.join()
        with self.assertRaises(self.ws.WebSocketError):
            self.ws.WebSocketClient.connect("127.0.0.1", 9, "/x", {"Origin": "http://evil"}, timeout=1)

    def test_read_timeout_keeps_the_connection(self) -> None:
        release = threading.Event()

        def script(conn: socket.socket) -> None:
            release.wait(5)
            conn.sendall(server_frame(0x2, b"late"))

        server = FakeWsServer(script)
        client = self.connect(server, read_timeout=0.2)
        with self.assertRaises(self.ws.WebSocketTimeout):
            client.recv()
        release.set()
        self.assertEqual(client.recv(), (self.ws.OP_BINARY, b"late"))
        server.join()
        client.close()


# ====================================================================== units

class UnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = MODULES["ws"]
        self.media = MODULES["media"]

    def test_video_messages_and_codec_string(self) -> None:
        parse = self.ws.parse_video_message
        self.assertEqual(parse(b"\x01" + struct.pack("!II", 1080, 2400)), ("session", 1080, 2400))
        self.assertEqual(parse(b"\x02abc"), ("config", b"abc"))
        self.assertEqual(parse(b"\x03\x01" + struct.pack("!Q", 99) + b"data"), ("packet", True, 99, b"data"))
        self.assertEqual(parse(b"\x09xyz"), ("unknown", 9))
        self.assertEqual(parse(b""), ("unknown", None))
        self.assertEqual(self.ws.codec_from_sps(CONFIG_FIXTURE), "avc1.64001F")
        # An emulation prevention byte inside the profile bytes is skipped, as the page does.
        self.assertEqual(self.ws.codec_from_sps(bytes.fromhex("0000016700000301")), "avc1.000001")
        self.assertIsNone(self.ws.codec_from_sps(bytes.fromhex("0000000168ebe3cb")))
        self.assertIsNone(self.ws.codec_from_sps(None))

    def stream(self, cap: int = 1048576, profile: str = "focus") -> Any:
        ref = {"host_id": "h_" + "a" * 26, "session_id": "s_abc", "platform": "android", "target_id": ANDROID}
        return self.media.Stream("st_" + "0" * 16, ref, profile, ANDROID_DEVICE, cap, lambda message: None)

    def test_buffer_caps_eviction_and_oversize_packets(self) -> None:
        stream = self.stream()
        stream.on_session(100, 200)
        for index in range(100):
            stream.on_packet(index % 10 == 0, index, b"k" * 100000)
            self.assertLessEqual(stream.bytes, 1048576)
        self.assertEqual(stream.evicted_through, 100 - len(stream.packets))
        stream.on_packet(True, 1, b"x" * 2000000)
        # The oversize packet goes, and every older one with it: nothing buffered is older than a gap.
        self.assertEqual((len(stream.packets), stream.bytes, stream.evicted_through), (0, 0, 101))
        small = self.stream(cap=67108864)
        small.on_session(1, 1)
        for index in range(2500):
            small.on_packet(False, index, b"s")
        self.assertEqual(len(small.packets), 2000)
        self.assertEqual(small.packets[0].seq, 501)

    def test_focus_pull_resync_and_dropped(self) -> None:
        stream = self.stream(cap=1048576)
        stream.on_session(100, 200)
        stream.on_config(CONFIG_FIXTURE)
        for index in range(30):
            stream.on_packet(index % 10 == 0, index, b"p" * 100000)
        answer = stream.pull(0, None, 1048576, 0, threading.Event())
        self.assertTrue(answer["resync"])
        self.assertTrue(answer["frames"][0].key)
        self.assertEqual(answer["frames"][0].seq, 21)
        self.assertEqual(answer["dropped"], 20)
        follow = stream.pull(answer["frames"][-1].seq, 0, 1048576, 0, threading.Event())
        self.assertEqual((follow["frames"], follow["resync"], follow["dropped"]), ([], False, 0))
        stream.on_session(50, 60)
        stream.on_packet(True, 1, b"n")
        fresh = stream.pull(30, 0, 1048576, 0, threading.Event())
        self.assertTrue(fresh["resync"])
        self.assertEqual((stream.epoch, [item.seq for item in fresh["frames"]]), (1, [31]))

    def test_oversize_drop_resyncs_after_the_gap(self) -> None:
        stream = self.stream(cap=1048576)
        stream.on_session(100, 200)
        stream.on_config(CONFIG_FIXTURE)
        stream.on_packet(True, 1, b"k")
        stream.on_packet(False, 2, b"p")
        stream.on_packet(False, 3, b"p")
        first = stream.pull(0, 0, 1048576, 0, threading.Event())
        self.assertEqual(([item.seq for item in first["frames"]], first["resync"]), ([1, 2, 3], False))
        stream.on_packet(False, 4, b"x" * 2000000)  # seq 4 can never fit: dropped
        stream.on_packet(False, 5, b"p")
        # No key frame after the gap yet: a resync with no frames, never [1, 2, 3, 5].
        gap = stream.pull(3, 0, 1048576, 0, threading.Event())
        self.assertEqual((gap["frames"], gap["resync"], gap["dropped"]), ([], True, 1))
        stream.on_packet(True, 6, b"k")
        stream.on_packet(False, 7, b"p")
        again = stream.pull(3, 0, 1048576, 0, threading.Event())
        self.assertEqual(([item.seq for item in again["frames"]], again["resync"]), ([6, 7], True))
        self.assertTrue(again["frames"][0].key)
        follow = stream.pull(7, 0, 1048576, 0, threading.Event())
        self.assertEqual((follow["frames"], follow["resync"]), ([], False))

    def test_new_session_resyncs_a_client_that_read_everything(self) -> None:
        stream = self.stream(cap=1048576)
        stream.on_session(1080, 2400)
        stream.on_config(CONFIG_FIXTURE)
        for seq in (1, 2, 3):
            stream.on_packet(seq == 1, seq, b"p")
        first = stream.pull(0, None, 1048576, 0, threading.Event())
        self.assertEqual(([item.seq for item in first["frames"]], first["resync"]), ([1, 2, 3], False))
        stream.on_session(720, 1280)  # the device rotated: a new video session
        stream.on_config(CONFIG_FIXTURE)
        stream.on_packet(True, 4, b"k")
        stream.on_packet(False, 5, b"p")
        # after_seq 3 is from the old session: a resync even without epoch.
        late = stream.pull(3, None, 1048576, 0, threading.Event())
        self.assertEqual(([item.seq for item in late["frames"]], late["resync"], stream.epoch), ([4, 5], True, 1))
        self.assertEqual(stream.pull(5, None, 1048576, 0, threading.Event())["resync"], False)
        stream.on_packet(False, 6, b"p")
        self.assertEqual(([item.seq for item in stream.pull(4, None, 1048576, 0, threading.Event())["frames"]]),
                         [5, 6])

    def test_event_checks_and_point_mapping(self) -> None:
        media = self.media
        self.assertEqual((media._point(0.5, 1080), media._point(0.25, 2400), media._point(1.0, 1080),
                          media._point(0.0005, 1000)), (540, 600, 1080, 1))
        for event, field in (({"op": "tap", "x": 0.5}, "event.y"),
                             ({"op": "swipe", "x1": 0, "y1": 0, "x2": 1, "y2": 1}, "event.duration_ms"),
                             ({"op": "key", "key": "HOME", "text": "x"}, "event.text"),
                             ({"op": "text", "text": "a\rb"}, "event.text")):
            with self.assertRaises(MODULES["common"].ToolError) as caught:
                media.check_event(event)
            self.assertEqual(caught.exception.extras["field"], field)
        media.check_event({"op": "text", "text": "line\nnext\ttab", "sensitive": True})
        self.assertEqual(sorted(media.SYSTEM_KEYS), ["app-switch", "back", "home", "power", "volume-down",
                                                     "volume-up"])

    def test_catalog_entries_and_module_rules(self) -> None:
        names = [tool.name for tool in self.media.TOOLS]
        self.assertEqual(names, MEDIA_TOOLS)
        for tool in self.media.TOOLS:
            self.assertEqual(tool.meta["ui"], {"visibility": ["app"]}, tool.name)
            self.assertIs(tool.input_schema["additionalProperties"], False)
            self.assertEqual(tool.mutating, tool.name in ("send_input", "pause_session", "resume_session"))
        stdlib = set(sys.stdlib_module_names)
        siblings = {path.stem for path in SERVER_DIR.glob("*.py")}
        for name in ("mcp_media.py", "mcp_ws.py"):
            source = (SERVER_DIR / name).read_text(encoding="utf-8")
            tree = ast.parse(source, feature_version=(3, 9))
            body = [node for node in tree.body if not (isinstance(node, ast.Expr) and
                                                       isinstance(node.value, ast.Constant))]
            self.assertEqual((body[0].module, [alias.name for alias in body[0].names]),
                             ("__future__", ["annotations"]), name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [(node.module or "").split(".")[0]]
                else:
                    continue
                for module in modules:
                    self.assertTrue(module in stdlib or module in siblings or module == "__future__", module)
        fake = FAKE_CANVAS.read_text(encoding="utf-8")
        self.assertIn('from "../../plugins/autonom/skills/android-emulator-browser/scripts/ws.mjs"', fake)
        self.assertIn("acceptWebSocket", fake)


if __name__ == "__main__":
    unittest.main()
