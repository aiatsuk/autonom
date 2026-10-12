"""ChatGPT probe package: a self-contained tree and the page's fields from the real server.

Contract 1, 5.7 and 8 (harness):

- the real plugin tree ``P`` is self-contained (imports and paths stay inside
  ``P``; only the Autonom root is resolved at run time), the manifest,
  ``.mcp.json`` and the launcher agree, and every version string is 0.1.0;
- ``resources/read`` of the real UI files inlines ``probe-core.mjs`` and the
  config JSON;
- the 5.7 round trip: every field the page reads is present in real server
  results (the server started exactly as ``.mcp.json`` says, with the fake CLI
  and the fake media endpoint), and the page's own parsing functions in
  ``probe-core.mjs`` accept those results;
- with Node available, the real media endpoint answers the frames, ping and
  status fields the page reads;
- ``node --check`` and ``--self-test`` of the fake-host harness pass.

No device is touched. ``HOME`` and ``AUTONOM_HOME`` are temporary directories.
"""
from __future__ import annotations

import ast
import base64
import http.client
import json
import os
import queue
import re
import shutil
import signal
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
except ImportError:  # python3 -m unittest tests.test_chatgpt_probe_package
    from tests.env_isolation import EnvSandboxMixin

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / "integrations" / "chatgpt" / "autonom-chatgpt-probe"
CHECKER = ROOT / "integrations" / "chatgpt" / "scripts" / "probe_results.py"
HARNESS = ROOT / "tests" / "live" / "chatgpt_probe_host_live.mjs"
DEV_MARKETPLACE = ROOT / "integrations" / "chatgpt" / ".agents" / "plugins" / "marketplace.json"
FAKE_CLI = ROOT / "tests" / "fakes" / "fake_probe_cli.py"
FAKE_MEDIA = ROOT / "tests" / "fakes" / "fake_probe_media.py"
NODE = shutil.which("node")
NAME = "autonom-chatgpt-probe"
VERSION = "0.1.0"
UI_URI = "ui://autonom-probe/probe.html"
SANDBOX_ORIGIN = "https://web-sandbox.oaiusercontent.com"
REMOTE = {"label": "forwarded", "origin": "http://127.0.0.1:47812"}
DEVICE = "android:emulator-5584"

# Contract 5.7: the fields the page reads, per tool (dotted paths into structuredContent).
OPEN_FIELDS = ["ok", "probe_version", "media.state", "media.local_origin", "media.port", "media.remote_routes",
               "media.allowed_devices", "media.error_code", "media.error", "media.hint", "selection.target_ref",
               "selection.revision", "note.revision"]
READ_FIELDS = ["ok", "server.protocol_version", "server.client", "server.client_ui_extension",
               "server.methods_seen", "autonom.version", "devices", "media.refused_origins", "note.revision",
               "note.text", "selection.revision"]
FRAMES_FIELDS = ["frames", "codec", "width", "height", "last_seq"]
MISSING = object()


def get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return MISSING
        cur = cur[part]
    return cur


def load_checker() -> Any:
    """Import probe_results.py without leaving bytecode next to it."""
    import importlib.util
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("probe_results_pkg", CHECKER)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def command_of(pid: int) -> str:
    out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, check=False)
    return out.stdout.strip()


class Server:
    """The probe server started as .mcp.json says: ``sh ./bin/launch.sh`` with cwd ``P``."""

    def __init__(self, env: Dict[str, str]) -> None:
        manifest = json.loads((P / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][NAME]
        self.stderr_file = tempfile.TemporaryFile()
        self.proc = subprocess.Popen([manifest["command"], *manifest["args"]], cwd=str(P / manifest["cwd"]),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr_file, env=env)
        self.messages: "queue.Queue[Any]" = queue.Queue()
        self.next_id = 1
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in iter(self.proc.stdout.readline, b""):
            self.messages.put(json.loads(line.decode("utf-8")))
        self.messages.put(None)

    def send(self, message: Dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: Any, timeout: float = 30.0) -> Dict[str, Any]:
        msg_id = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            reply = self.messages.get(timeout=max(0.1, deadline - time.monotonic()))
            if reply is None:
                raise AssertionError("server closed stdout during %s; stderr: %s" % (method, self.stderr_text()))
            if reply.get("id") == msg_id:
                return reply

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        reply = self.request("tools/call", {"name": name, "arguments": arguments})
        if "error" in reply:
            raise AssertionError("tools/call %s: %r" % (name, reply["error"]))
        return reply["result"]

    def stderr_text(self) -> str:
        self.stderr_file.seek(0)
        return self.stderr_file.read().decode("utf-8", "replace")

    def close(self) -> None:
        if self.proc.stdin is not None and not self.proc.stdin.closed:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        self.reader.join(timeout=2)
        self.stderr_file.close()


class PackageCase(EnvSandboxMixin, unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.state_root = self.sandbox_home()
        self.tmp = Path(tempfile.mkdtemp(prefix="probe-package-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.probe_dir = self.state_root / "chatgpt-probe"
        self.probe_dir.mkdir(mode=0o700)
        self.write_config({"media_port": 0, "remote_routes": [REMOTE], "allowed_devices": [DEVICE]})

    def write_config(self, config: Dict[str, Any]) -> None:
        (self.probe_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

    def env(self, **overrides: Optional[str]) -> Dict[str, str]:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AUTONOM_", "FAKE_PROBE_", "PYTHON")) and key != "XDG_STATE_HOME"}
        env.update({
            "HOME": str(self.home),
            "AUTONOM_HOME": str(self.state_root),
            "AUTONOM_CLI": str(FAKE_CLI),
            "AUTONOM_PROBE_CLI_PYTHON": sys.executable,
            "AUTONOM_PROBE_NODE": sys.executable,
            "AUTONOM_PROBE_MEDIA_SCRIPT": str(FAKE_MEDIA),
            # A slow CI runner gets this long to start the fake media child; the test of
            # the real endpoint keeps the product default (5 s).
            "AUTONOM_PROBE_MEDIA_READY_TIMEOUT": "60",
            "AUTONOM_ROOT": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def start(self, **overrides: Optional[str]) -> Server:
        server = Server(self.env(**overrides))
        self.addCleanup(self._finish, server)
        reply = server.request("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}},
            "clientInfo": {"name": "probe-package-test", "version": "1.0"}})
        self.assertIn("result", reply, server.stderr_text())
        self.assertEqual(reply["result"]["serverInfo"]["name"], NAME)
        self.assertEqual(reply["result"]["serverInfo"]["version"], VERSION)
        server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def _finish(self, server: Server) -> None:
        media = self.probe_dir / "media.json"
        pid = None
        if media.exists():
            try:
                pid = json.loads(media.read_text(encoding="utf-8")).get("pid")
            except ValueError:
                pid = None
        server.close()
        if isinstance(pid, int) and pid_alive(pid):
            command = command_of(pid)
            if "probe-media" in command or "fake_probe_media" in command:
                os.kill(pid, signal.SIGKILL)

    def assert_fields(self, structured: Dict[str, Any], fields: List[str], tool: str) -> None:
        missing = [path for path in fields if get_path(structured, path) is MISSING]
        self.assertEqual(missing, [], "%s lacks fields the page reads: %s" % (tool, missing))


# ====================================================================== tree

class TreeTests(PackageCase):
    def test_manifest_mcp_json_and_launcher_agree(self) -> None:
        manifest = json.loads((P / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], NAME)
        self.assertEqual(manifest["version"], VERSION)
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        self.assertTrue((P / manifest["mcpServers"]).is_file())
        if "skills" in manifest:
            self.assertTrue((P / manifest["skills"]).is_dir())
        mcp = json.loads((P / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(list(mcp["mcpServers"]), [NAME])
        entry = mcp["mcpServers"][NAME]
        self.assertEqual(entry, {"command": "sh", "args": ["./bin/launch.sh"], "cwd": "."})
        self.assertTrue((P / entry["cwd"] / entry["args"][0]).is_file())
        launcher = (P / "bin" / "launch.sh").read_text(encoding="utf-8")
        self.assertIn('"$PLUGIN_DIR/server/probe_server.py"', launcher)
        self.assertTrue((P / "server" / "probe_server.py").is_file())
        dev = json.loads(DEV_MARKETPLACE.read_text(encoding="utf-8"))
        entries = [plugin for plugin in dev["plugins"] if plugin["name"] == NAME]
        self.assertEqual(len(entries), 1)
        self.assertEqual((DEV_MARKETPLACE.parents[2] / entries[0]["source"]["path"]).resolve(), P.resolve())

    def test_every_version_string_is_the_probe_version(self) -> None:
        sources = {
            "server/probe_env.py": r'^PROBE_VERSION = "([^"]+)"$',
            "ui/probe-core.mjs": r'^export const PROBE_VERSION = "([^"]+)";$',
            "media/media-lib.mjs": r'^export const PROBE_VERSION = "([^"]+)";$',
            "ui/probe-page.html": r'id="pb-version">([^<]+)<',
        }
        for rel, pattern in sources.items():
            with self.subTest(file=rel):
                found = re.findall(pattern, (P / rel).read_text(encoding="utf-8"), re.M)
                self.assertEqual(found, [VERSION])
        checker = (CHECKER).read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'^PROBE_VERSION = "([^"]+)"$', checker, re.M), [VERSION])

    def test_no_package_files_or_vendored_copies(self) -> None:
        names = {path.name for path in P.rglob("*")}
        for forbidden in ("package.json", "package-lock.json", "node_modules", "requirements.txt"):
            self.assertNotIn(forbidden, names)
        repo_modules = {path.name for path in (ROOT / "plugins" / "autonom" / "skills" / "android-emulator-browser"
                                               / "scripts").glob("*.mjs")}
        self.assertFalse(names & repo_modules, "vendored Canvas modules inside P: %s" % (names & repo_modules))

    def test_python_imports_stay_inside_the_server_dir_and_the_stdlib(self) -> None:
        siblings = {path.stem for path in (P / "server").glob("*.py")}
        stdlib = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
        for path in sorted((P / "server").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    self.assertEqual(node.level, 0, "%s: relative import" % path.name)
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                for name in names:
                    with self.subTest(file=path.name, module=name):
                        if stdlib:
                            self.assertIn(name, siblings | stdlib)
                        else:  # Python < 3.10: no stdlib list; only refuse the repository's own packages
                            self.assertNotIn(name, {"autonom_lib", "scripts", "tests"})

    def test_node_imports_stay_inside_p(self) -> None:
        pattern = re.compile(r"""^\s*(?:import|export)\b[^;"'`]*?\bfrom\s*(["'])([^"']+)\1"""
                             r"""|^\s*import\s*(["'])([^"']+)\3|\bimport\s*\(\s*(["'])([^"']+)\5""", re.M)
        files = sorted(P.rglob("*.mjs")) + [P / "ui" / "probe-page.html"]
        self.assertGreaterEqual(len(files), 6)
        for path in files:
            text = path.read_text(encoding="utf-8")
            for match in pattern.finditer(text):
                spec = match.group(2) or match.group(4) or match.group(6)
                with self.subTest(file=path.name, spec=spec):
                    if spec.startswith("node:"):
                        continue
                    self.assertTrue(spec.startswith("./") or spec.startswith("../"), spec)
                    target = (path.parent / spec).resolve()
                    self.assertTrue(str(target).startswith(str(P.resolve()) + os.sep), target)
                    self.assertTrue(target.is_file(), target)

    def test_no_literal_path_leaves_p(self) -> None:
        climbing = re.compile(r"""["'`](?:\.\./){2,}|["'`]\.\./(?!ui/|media/|server/|bin/)""")
        for path in sorted(p for p in P.rglob("*") if p.is_file() and p.suffix in (".py", ".mjs", ".html", ".sh", ".json")):
            with self.subTest(file=str(path.relative_to(P))):
                self.assertIsNone(climbing.search(path.read_text(encoding="utf-8")))


# ====================================================================== real server

class ResourceTests(PackageCase):
    def test_resources_read_inlines_the_real_core_and_config(self) -> None:
        server = self.start()
        opened = server.call("open_workspace", {})
        token = opened["_meta"]["autonom/probe"]["media_token"]
        reply = server.request("resources/read", {"uri": UI_URI})
        content = reply["result"]["contents"][0]
        self.assertEqual(content["mimeType"], "text/html;profile=mcp-app")
        html = content["text"]
        core = (P / "ui" / "probe-core.mjs").read_text(encoding="utf-8")
        self.assertIn('<script type="module">\n' + core + "\n</script>", html)
        self.assertNotIn("<!-- PROBE_CORE -->", html)
        self.assertNotIn("__PROBE_CONFIG_JSON__", html)
        match = re.search(r'<script type="application/json" id="probe-config">(.*?)</script>', html, re.S)
        self.assertIsNotNone(match)
        config = json.loads(match.group(1).replace("<\\/", "</"))
        self.assertEqual(set(config), {"probe_version", "local_origin", "remote_routes", "allowed_devices", "media_port"})
        self.assertEqual(config["probe_version"], VERSION)
        self.assertEqual(config["remote_routes"], [REMOTE])
        self.assertEqual(config["allowed_devices"], [DEVICE])
        self.assertIsInstance(token, str)
        self.assertGreaterEqual(len(token), 32)
        self.assertNotIn(token, html)
        self.assertNotIn(opened["_meta"]["autonom/probe"]["context_nonce"], html)
        connect = content["_meta"]["ui"]["csp"]["connectDomains"]
        self.assertIn(REMOTE["origin"], connect)
        self.assertIn("ws://127.0.0.1:47812", connect)
        self.assertEqual(content["_meta"]["openai/widgetCSP"]["connect_domains"], connect)


class RoundTripTests(PackageCase):
    """Contract 5.7: every field the page reads survives from the real server."""

    def collect(self, server: Server) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        out["open"] = server.call("open_workspace", {})
        out["read"] = server.call("probe_read", {})
        note_rev = out["read"]["structuredContent"]["note"]["revision"]
        out["write"] = server.call("probe_write", {"text": "round trip", "expected_revision": note_rev})
        out["write_stale"] = server.call("probe_write", {"text": "stale", "expected_revision": note_rev})
        sel_rev = out["read"]["structuredContent"]["selection"]["revision"]
        out["select"] = server.call("probe_select", {"target_ref": "fixture~probe-a", "expected_revision": sel_rev})
        out["select_stale"] = server.call("probe_select", {"target_ref": "fixture~probe-b", "expected_revision": sel_rev})
        out["write_stale_selection"] = server.call("probe_write", {
            "text": "stale selection", "expected_revision": note_rev + 1, "selection_revision": sel_rev})
        out["fail"] = server.call("probe_fail", {})
        out["frames"] = server.call("probe_media_frames", {"count": 3, "after_seq": -1})
        self.results = load_checker().stub_results("round trip of probe_save_results")
        out["save"] = server.call("probe_save_results", {"results": self.results})
        return out

    def test_every_field_the_page_reads_is_present(self) -> None:
        server = self.start()
        got = self.collect(server)
        sc = {name: result["structuredContent"] for name, result in got.items()}

        self.assert_fields(sc["open"], OPEN_FIELDS, "open_workspace")
        self.assertTrue(sc["open"]["ok"])
        self.assertEqual(sc["open"]["media"]["state"], "running", server.stderr_text())
        self.assertEqual(sc["open"]["media"]["remote_routes"], [REMOTE])
        self.assertEqual(sc["open"]["media"]["allowed_devices"], [DEVICE])
        meta = got["open"]["_meta"]["autonom/probe"]
        self.assertIsInstance(meta["media_token"], str)
        self.assertRegex(meta["context_nonce"], r"^probe-[0-9a-f]{4}-[a-z]+$")
        for result in got.values():
            self.assertNotIn(meta["media_token"], json.dumps(result["structuredContent"]))
            self.assertNotIn(meta["media_token"], result["content"][0]["text"])

        self.assert_fields(sc["read"], READ_FIELDS, "probe_read")
        self.assertIs(sc["read"]["server"]["client_ui_extension"], True)
        self.assertIn("initialize", sc["read"]["server"]["methods_seen"])
        self.assertEqual(sc["read"]["autonom"]["version"], "0.35.1")
        self.assertEqual(sc["read"]["devices"][0]["target_id"], "emulator-5584")

        self.assertTrue(sc["write"]["ok"])
        self.assertEqual(sc["write"]["note"]["revision"], sc["read"]["note"]["revision"] + 1)
        self.assertIn("revision", sc["write"]["selection"])
        self.assertEqual((sc["write_stale"]["ok"], sc["write_stale"]["error_code"]), (False, "stale_revision"))
        self.assertEqual(sc["write_stale"]["current_revision"], sc["write"]["note"]["revision"])
        self.assertTrue(sc["select"]["ok"])
        self.assertEqual(sc["select"]["selection"]["revision"], sc["read"]["selection"]["revision"] + 1)
        self.assertEqual(sc["select_stale"]["error_code"], "stale_selection")
        self.assertEqual(sc["select_stale"]["current_selection_revision"], sc["select"]["selection"]["revision"])
        self.assertEqual(sc["write_stale_selection"]["error_code"], "stale_selection")
        self.assertIn("current_selection_revision", sc["write_stale_selection"])

        self.assertIs(got["fail"]["isError"], True)
        for key in ("ok", "error_code", "error", "hint"):
            self.assertIn(key, sc["fail"])
        self.assertEqual(sc["fail"]["error_code"], "probe_deliberate_failure")

        self.assert_fields(sc["frames"], FRAMES_FIELDS, "probe_media_frames")
        frames_meta = got["frames"]["_meta"]["autonom/probe"]
        self.assertIsInstance(frames_meta["config_b64"], str)
        self.assertEqual(len(frames_meta["frames"]), 3)
        for frame in frames_meta["frames"]:
            self.assertEqual(set(frame) >= {"seq", "key", "t_server_ms", "data_b64"}, True, frame)
        self.assertNotIn("data_b64", json.dumps(sc["frames"]))

        self.assertTrue(sc["save"]["ok"], sc["save"])
        self.assertEqual(sc["save"]["saved"]["run_id"], self.results["run_id"])
        saved = self.probe_dir / "results" / ("%s.json" % self.results["run_id"])
        check = subprocess.run([sys.executable, str(CHECKER), "check", "--json", "--results", str(saved)],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
                               env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), check=False)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

        if NODE is not None:
            self.page_parses(got)

    def page_parses(self, got: Dict[str, Any]) -> None:
        """The page's own readers in probe-core.mjs accept the real results."""
        dump = self.tmp / "results.json"
        dump.write_text(json.dumps(got), encoding="utf-8")
        script = r"""
const core = await import(process.argv[1]);
const got = JSON.parse((await import("node:fs")).readFileSync(process.argv[2], "utf8"));
const out = {};
const ws = core.readOpenWorkspace(got.open);
out.open_missing = core.missingFields(core.toolOutcome(got.open).sc, core.OPEN_WORKSPACE_FIELDS);
out.open_ok = ws.ok && Boolean(ws.media_token) && Boolean(ws.context_nonce) && core.isOpenWorkspaceResult(got.open);
out.local_origin = ws.media.local_origin;
out.remote = ws.media.remote_routes;
out.read_missing = core.missingFields(core.toolOutcome(got.read).sc, core.PROBE_READ_FIELDS);
out.frames_missing = core.missingFields(core.toolOutcome(got.frames).sc, core.MEDIA_FRAMES_FIELDS);
out.fail = core.toolOutcome(got.fail);
out.stale = core.toolOutcome(got.write_stale_selection).errorCode;
out.write_rev = core.toolOutcome(got.write).sc.note.revision;
out.save_ok = core.toolOutcome(got.save).ok;
out.frame = core.base64ToBytes(core.toolOutcome(got.frames).meta.frames[0].data_b64).slice(0, 4);
console.log(JSON.stringify(out));
"""
        core = (P / "ui" / "probe-core.mjs").as_uri()
        proc = subprocess.run([NODE, "--input-type=module", "-e", script, core, str(dump)], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["open_missing"], [])
        self.assertTrue(out["open_ok"])
        self.assertRegex(out["local_origin"], r"^http://127\.0\.0\.1:\d+$")
        self.assertEqual(out["remote"], [REMOTE])
        self.assertEqual(out["read_missing"], [])
        self.assertEqual(out["frames_missing"], [])
        self.assertTrue(out["fail"]["isError"])
        self.assertEqual(out["fail"]["errorCode"], "probe_deliberate_failure")
        self.assertEqual(out["stale"], "stale_selection")
        self.assertTrue(out["save_ok"])
        self.assertEqual(out["frame"], {"0": 0, "1": 0, "2": 0, "3": 1})


@unittest.skipIf(NODE is None, "node is not on PATH")
class RealMediaTests(PackageCase):
    """The server with the real media endpoint (Node, --port 0): the 3.3 fields the page reads."""

    def http_get(self, port: int, path: str, token: str, origin: Optional[str]) -> Dict[str, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Authorization": "Bearer " + token}
        if origin:
            headers["Origin"] = origin
        try:
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            body = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200, body)
            if origin:
                self.assertEqual(response.getheader("Access-Control-Allow-Origin"), origin)
            return body
        finally:
            conn.close()

    def test_frames_ping_and_status_from_the_real_endpoint(self) -> None:
        server = self.start(AUTONOM_PROBE_NODE=NODE, AUTONOM_PROBE_MEDIA_SCRIPT=None,
                            AUTONOM_PROBE_MEDIA_READY_TIMEOUT=None)
        opened = server.call("open_workspace", {})
        media = opened["structuredContent"]["media"]
        self.assertEqual(media["state"], "running", server.stderr_text())
        port = media["port"]
        self.assertGreater(port, 0)
        self.assertEqual(media["local_origin"], "http://127.0.0.1:%d" % port)
        token = opened["_meta"]["autonom/probe"]["media_token"]

        frames = server.call("probe_media_frames", {"count": 2})
        sc = frames["structuredContent"]
        self.assertTrue(sc["ok"], sc)
        self.assert_fields(sc, FRAMES_FIELDS, "probe_media_frames")
        self.assertEqual((sc["frames"], sc["codec"], sc["width"], sc["height"]), (2, "avc1.42C01E", 96, 208))
        meta = frames["_meta"]["autonom/probe"]
        config = base64.b64decode(meta["config_b64"])
        self.assertEqual(config[:5], b"\x00\x00\x00\x01\x67")
        for frame in meta["frames"]:
            data = base64.b64decode(frame["data_b64"])
            self.assertEqual(data[:5], b"\x00\x00\x00\x01\x67")
            self.assertIs(frame["key"], True)
            self.assertIsInstance(frame["t_server_ms"], (int, float))
        self.assertEqual(sc["last_seq"], meta["frames"][-1]["seq"])

        ping = self.http_get(port, "/probe/ping?nonce=pkg-test", token, SANDBOX_ORIGIN)
        for key in ("ok", "schema_version", "nonce", "server_time_ms", "origin_seen", "host_seen"):
            self.assertIn(key, ping)
        self.assertEqual((ping["nonce"], ping["origin_seen"], ping["host_seen"]),
                         ("pkg-test", SANDBOX_ORIGIN, "127.0.0.1:%d" % port))
        self.assertEqual(ping["schema_version"], "autonom.chatgpt-probe.media/v1")
        status = self.http_get(port, "/probe/status", token, None)
        for key in ("ok", "schema_version", "uptime_ms", "clients", "sources", "refused_origins"):
            self.assertIn(key, status)

        record = json.loads((self.probe_dir / "media.json").read_text(encoding="utf-8"))
        self.assertIn("probe-media", command_of(record["pid"]))
        server.close()
        deadline = time.monotonic() + 5
        while pid_alive(record["pid"]) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(pid_alive(record["pid"]), "the media child outlived the server")
        self.assertFalse((self.probe_dir / "media.json").exists())


# ====================================================================== harness

@unittest.skipIf(NODE is None, "node is not on PATH")
class HarnessScriptTests(unittest.TestCase):
    def run_node(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([NODE, *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=120, check=False, cwd=str(ROOT))

    def test_node_check_self_test_and_dry_run(self) -> None:
        self.assertEqual(self.run_node("--check", str(HARNESS)).returncode, 0)
        proc = self.run_node(str(HARNESS), "--self-test")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"^self-test ok \(\d+ assertions\)")
        proc = self.run_node(str(HARNESS), "--dry-run", "--browser", "both", "--remote-proxy",
                             "--device", "android:emulator-5584")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plan = json.loads(proc.stdout)
        self.assertEqual(plan["browsers"], ["chromium", "webkit"])
        self.assertEqual(plan["remote"]["kind"], "proxy")
        self.assertEqual(plan["devices"], ["android:emulator-5584"])
        self.assertEqual(len(plan["files"]), 8)

    def test_refusals_before_anything_starts(self) -> None:
        cases = [
            (["--evidence-dir", "/nonexistent/x", "--device", "android:emulator-5554"], "device_not_allowed"),
            (["--dry-run", "--device", "ios:00000000-0000-0000-0000-000000000000"], "device_not_allowed"),
            (["--dry-run", "--remote-proxy", "--ssh-forward", "localhost"], "invalid_arguments"),
            (["--dry-run", "--ssh-forward=-oProxyCommand=touch"], "invalid_arguments"),
            (["--dry-run", "--browser", "firefox"], "invalid_arguments"),
            ([], "invalid_arguments"),
        ]
        for args, code in cases:
            with self.subTest(args=args):
                proc = self.run_node(str(HARNESS), *args)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertEqual(proc.stdout, "")
                payload = json.loads(proc.stderr)
                self.assertEqual((payload["ok"], payload["error_code"]), (False, code))
                self.assertTrue(payload["error"])
                self.assertTrue(payload["hint"])
        self.assertFalse(Path("/nonexistent/x").exists())


if __name__ == "__main__":
    unittest.main()
