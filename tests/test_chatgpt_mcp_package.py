"""Autonom MCP adapter package: a self-contained tree, the full catalog and the page's fields from the real server.

Contract run ws-p3-mcp-adapter, section 8 (task ``wire``):

- the plugin tree ``P`` is self-contained (imports and literal paths stay inside ``P``), the
  manifest, ``.mcp.json`` and the launcher agree, every version string is 0.1.0, no package
  files or vendored Canvas modules, every file below 256 KiB;
- ``tools/list`` of the real server started exactly as ``.mcp.json`` says (no partial-catalog
  environment) lists the 28 tools in table order with valid schemas, also under
  ``/usr/bin/python3`` when that is 3.9 or 3.10;
- every field of ``tests/fixtures/chatgpt_mcp_ui_fields.json`` is present in real results
  (fake CLI and fake Canvas) and the parsers of ``workspace-dev-core.mjs`` accept them (run
  through ``node``); the ``artifact_id`` of ``get_screen`` fetched with ``get_artifact`` gives
  the same PNG bytes (p3-008-S01);
- the diagnostics round trip: ``buildDiagnostics`` (through ``node``) from recorded results ->
  ``save_diagnostics`` of the real server -> ``chatgpt_mcp_live.py check-chatgpt`` with three
  placeholder PNGs (a copy with one check ``fail`` is refused);
- the results template passes ``check-doc`` only once every ``pending`` cell is filled from
  synthetic results files; ``fill_doc`` writes every results-backed value as one block between
  two marker lines and ``check-doc`` renders it again from the given files and requires the
  same bytes (any changed byte, a missing, repeated or reordered marker, a ``|`` line outside
  the block, a results file left out, a forbidden or different target or version, a malformed
  results field and a bearer value are each a named problem), and the saved results file
  carries no Canvas token seen during the run and no temporary root, and is written without
  following a link;
- the release bundle, the root marketplaces and the ``validate_plugin.py`` output do not
  change.

No device is touched: ``HOME``, ``AUTONOM_HOME`` and ``XDG_STATE_HOME`` are temporary for the
server and every child, and failing ``adb``/``emulator``/``xcrun``/``simctl``/``idb`` stubs come
first on the child ``PATH`` (the test asserts none was called).
"""
from __future__ import annotations

import ast
import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / "integrations" / "chatgpt" / "autonom-chatgpt"
LIVE = ROOT / "tests" / "live"
FIXTURE = ROOT / "tests" / "fixtures" / "chatgpt_mcp_ui_fields.json"
TEMPLATE = ROOT / "docs" / "plans" / "CHATGPT_P3_RESULTS.md"
NODE = shutil.which("node")
NAME = "autonom-chatgpt"
VERSION = "0.1.0"
ANDROID = "emulator-5584"
FILE_LIMIT = 256 * 1024
TABLE_TOOLS = [
    "list_hosts", "open_workspace", "save_diagnostics",
    "list_devices", "list_sessions", "get_session", "start_session", "stop_session",
    "get_screen", "get_ui_tree", "tap_element", "type_text", "press_key",
    "acquire_control", "release_control", "get_operation",
    "list_flows", "start_flow_run", "get_run", "cancel_run", "list_artifacts", "get_artifact",
    "open_stream", "get_frames", "close_stream", "send_input", "pause_session", "resume_session",
]


def load(name: str, path: Path) -> Any:
    """Import a script as a module without writing bytecode next to it."""
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


CONF = load("chatgpt_mcp_conformance", LIVE / "chatgpt_mcp_conformance.py")
LIVE_SCRIPT = load("chatgpt_mcp_live_package", LIVE / "chatgpt_mcp_live.py")
MISSING = CONF.MISSING


def child_env(root: Path) -> Dict[str, str]:
    """A child's environment with a temporary HOME, AUTONOM_HOME and XDG_STATE_HOME under ``root``."""
    env = CONF.clean_env()
    env.update({"HOME": str(root / "home"), "AUTONOM_HOME": str(root / "state"), "XDG_STATE_HOME": str(root / "xdg"),
                "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def system_python(root: Path) -> Optional[str]:
    """``/usr/bin/python3`` when it is 3.9 or 3.10; the version probe runs with the temporary HOME under ``root``."""
    candidate = "/usr/bin/python3"
    if not os.path.exists(candidate):
        return None
    out = subprocess.run([candidate, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                         stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, env=child_env(root))
    if out.returncode != 0 or out.stdout.strip() not in ("3.9", "3.10"):
        return None
    return candidate


class PackageCase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.world = CONF.FakeWorld(prefix="mcp-package-test-")
        self.addCleanup(self.world.cleanup)
        self.clients: List[Any] = []

    def start(self, **overrides: Optional[str]) -> Any:
        env = self.world.env(**overrides)
        self.assertNotIn("AUTONOM_MCP_PARTIAL_CATALOG", env)
        self.assertNotIn("AUTONOM_MCP_UI_DIR", env)
        client = CONF.McpClient.from_plugin(P, env)
        self.clients.append(client)
        self.addCleanup(client.kill)
        reply = client.initialize(name="mcp-package-test")
        self.assertIn("result", reply, client.stderr_text())
        self.assertEqual(reply["result"]["serverInfo"], {"name": NAME, "title": "Autonom", "version": VERSION})
        return client

    def assert_no_device_tool(self) -> None:
        self.assertEqual(self.world.stub_calls(), [])


# ====================================================================== tree

class TreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mcp-package-tree-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_manifest_mcp_json_and_launcher_agree(self) -> None:
        manifest = json.loads((P / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["name"], manifest["version"], manifest["mcpServers"]), (NAME, VERSION, "./.mcp.json"))
        self.assertEqual(manifest["interface"]["displayName"], "Autonom (dev)")
        mcp = json.loads((P / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(mcp, {"mcpServers": {NAME: {"command": "sh", "args": ["./bin/launch.sh"], "cwd": "."}}})
        launcher = (P / "bin" / "launch.sh").read_text(encoding="utf-8")
        self.assertIn('exec "$py" "$PLUGIN_DIR/server/mcp_server.py"', launcher)
        self.assertIn("AUTONOM_MCP_PYTHON", launcher)
        self.assertTrue((P / "server" / "mcp_server.py").is_file())
        proc = subprocess.run(["sh", "-n", str(P / "bin" / "launch.sh")], stdin=subprocess.DEVNULL,
                              capture_output=True, check=False, env=child_env(self.tmp))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_every_version_string_is_the_adapter_version(self) -> None:
        sources = {
            "server/mcp_common.py": r'^ADAPTER_VERSION = "([^"]+)"$',
            "ui/workspace-dev-core.mjs": r'^export const WORKSPACE_CORE_VERSION = "([^"]+)";$',
            ".codex-plugin/plugin.json": r'^  "version": "([^"]+)",$',
        }
        for rel, pattern in sources.items():
            with self.subTest(file=rel):
                self.assertEqual(re.findall(pattern, (P / rel).read_text(encoding="utf-8"), re.M), [VERSION])

    def test_no_package_files_vendored_copies_or_large_files(self) -> None:
        # Bytecode caches (run_checks.sh compiles integrations/) are ignored by git and never shipped.
        files = [path for path in P.rglob("*") if path.is_file() and "__pycache__" not in path.parts]
        names = {path.name for path in P.rglob("*") if "__pycache__" not in path.parts}
        for forbidden in ("package.json", "package-lock.json", "node_modules", "requirements.txt", "pyproject.toml"):
            self.assertNotIn(forbidden, names)
        canvas = {path.name for path in (ROOT / "plugins" / "autonom" / "skills" / "android-emulator-browser"
                                         / "scripts").glob("*.mjs")}
        self.assertFalse(names & canvas, "vendored Canvas modules inside P: %s" % (names & canvas))
        for path in files:
            with self.subTest(file=str(path.relative_to(P))):
                self.assertLess(path.stat().st_size, FILE_LIMIT)

    def test_python_imports_stay_inside_the_server_dir_and_the_stdlib(self) -> None:
        siblings = {path.stem for path in (P / "server").glob("*.py")}
        stdlib = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
        for path in sorted((P / "server").glob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, feature_version=(3, 9))
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
                        else:
                            self.assertNotIn(name, {"autonom_lib", "scripts", "tests"})

    def test_node_imports_stay_inside_p(self) -> None:
        pattern = re.compile(r"""^\s*(?:import|export)\b[^;"'`]*?\bfrom\s*(["'])([^"']+)\1"""
                             r"""|^\s*import\s*(["'])([^"']+)\3|\bimport\s*\(\s*(["'])([^"']+)\5""", re.M)
        files = sorted(P.rglob("*.mjs")) + sorted(P.rglob("*.html"))
        self.assertGreaterEqual(len(files), 2)
        for path in files:
            for match in pattern.finditer(path.read_text(encoding="utf-8")):
                spec = match.group(2) or match.group(4) or match.group(6)
                with self.subTest(file=path.name, spec=spec):
                    if spec.startswith("node:"):
                        continue
                    self.assertTrue(spec.startswith("./") or spec.startswith("../"), spec)
                    target = (path.parent / spec).resolve()
                    self.assertTrue(str(target).startswith(str(P.resolve()) + os.sep), target)

    def test_no_literal_path_leaves_p(self) -> None:
        climbing = re.compile(r"""["'`](?:\.\./){2,}|["'`]\.\./(?!ui/|server/|bin/)""")
        for path in sorted(p for p in P.rglob("*") if p.is_file() and p.suffix in (".py", ".mjs", ".html", ".sh", ".json")):
            with self.subTest(file=str(path.relative_to(P))):
                text = path.read_text(encoding="utf-8")
                self.assertIsNone(climbing.search(text))
                self.assertNotIn("autonom_lib", text if path.suffix != ".py" else "")
        for path in sorted((P / "server").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?m)^\s*(?:from|import)\s+(?:autonom_lib|scripts|tests)\b")

    def test_release_bundle_root_marketplaces_and_validator_unchanged(self) -> None:
        build = (ROOT / "scripts" / "build_release.sh").read_text(encoding="utf-8")
        copied = set(re.findall(r'^cp -R ([^\s]+) "\$stage/', build, re.M))
        self.assertTrue(copied)
        self.assertNotIn("integrations", copied)
        self.assertNotIn("tests", copied)
        for rel in (".agents/plugins/marketplace.json", ".claude-plugin/marketplace.json"):
            with self.subTest(marketplace=rel):
                data = json.loads((ROOT / rel).read_text(encoding="utf-8"))
                self.assertEqual([plugin.get("name") for plugin in data["plugins"]], ["autonom"])
        dev = json.loads((ROOT / "integrations" / "chatgpt" / ".agents" / "plugins" / "marketplace.json")
                         .read_text(encoding="utf-8"))
        self.assertNotIn(NAME, [plugin.get("name") for plugin in dev["plugins"]])
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_plugin.py"), str(ROOT)],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False,
                              env=child_env(self.tmp))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        skills = [path.parent.name for path in (ROOT / "plugins" / "autonom" / "skills").glob("*/SKILL.md")]
        self.assertEqual(proc.stdout.strip(), "Validated Codex and Claude manifests and %d skills." % len(skills))
        self.assertNotIn(NAME, proc.stdout + proc.stderr)


# ====================================================================== the full catalog

class CatalogTests(PackageCase):
    def assert_full_catalog(self, client: Any) -> List[Dict[str, Any]]:
        tools = client.tools()
        self.assertEqual([tool["name"] for tool in tools], TABLE_TOOLS)
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                self.assertEqual(CONF.tool_definition_problems(tool), [])
        return tools

    def test_tools_list_of_the_server_started_as_mcp_json_says(self) -> None:
        client = self.start()
        self.assert_full_catalog(client)
        hosts = client.call("list_hosts", {})["structuredContent"]
        self.assertTrue(hosts["ok"], hosts)
        self.assertEqual(client.close(), 0)
        self.assert_no_device_tool()

    def test_full_catalog_under_the_system_python(self) -> None:
        old = system_python(self.world.tmp)
        if old is None:
            self.skipTest("/usr/bin/python3 is absent or not 3.9/3.10")
        client = self.start(AUTONOM_MCP_PYTHON=old)
        self.assert_full_catalog(client)
        hosts = client.call("list_hosts", {})["structuredContent"]
        self.assertEqual(hosts["hosts"][0]["state"], "online", hosts)
        self.assertIn("result", client.request("resources/read", {"uri": CONF.UI_URI}))
        self.assertEqual(client.close(), 0)
        self.assert_no_device_tool()


# ====================================================================== the page's fields

ROUND_TRIP_NODE = r"""
const core = await import(process.argv[1]);
const fs = await import("node:fs");
const got = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const out = { outcomes: {}, problems: [] };
const fail = (message) => out.problems.push(message);
for (const [name, result] of Object.entries(got.results)) {
  const outcome = core.toolOutcome(result);
  out.outcomes[name] = { ok: outcome.ok, errorCode: outcome.errorCode };
}
const opened = core.parseOpenStreamResult(got.results.open_stream);
if (!opened.ok || !opened.stream || !opened.stream.stream_id) fail("parseOpenStreamResult refused open_stream");
const frames = core.parseFramesResult(got.results.get_frames);
if (!frames.ok || frames.invalid !== 0 || frames.countMismatch || !frames.frames.length || !frames.config) {
  fail("parseFramesResult: " + JSON.stringify({ ok: frames.ok, invalid: frames.invalid, n: frames.frames.length }));
}
const chunks = core.annexBChunks(frames.frames, frames.config);
if (!chunks.length || chunks.length !== frames.frames.length) fail("annexBChunks");
const key = chunks.find((chunk) => chunk.type === "key");
if (key && !core.sameBytes(key.data.slice(0, frames.config.length), frames.config)) fail("the config is not prepended");
const state = core.nextPullState({ afterSeq: 0, epoch: null, config: null }, frames);
if (state.afterSeq !== frames.lastSeq) fail("nextPullState");
const sessions = core.readSessions(got.results.list_sessions);
const workspaceSessions = core.readSessions(got.results.open_workspace);
if (!sessions.ok || sessions.sessions.length < 1) fail("readSessions(list_sessions)");
if (!workspaceSessions.ok || workspaceSessions.sessions.length < 1) fail("readSessions(open_workspace)");
if (core.readHostId(got.results.list_hosts) !== got.host_id) fail("readHostId(list_hosts)");
if (core.readHostId(got.results.open_workspace) !== got.host_id) fail("readHostId(open_workspace)");
const paused = core.controlView(core.toolOutcome(got.results.pause_session).sc);
if (!paused || !paused.holder || paused.holder.kind !== "human" || !paused.mine) fail("controlView(pause_session)");
if (!core.controlView(core.toolOutcome(got.results.resume_session).sc)) fail("controlView(resume_session)");
const refused = core.toolOutcome(got.refused_tap);
if (refused.ok || !core.agentHasControl(refused)) fail("agentHasControl(refused tap)");
// The diagnostics the page would save from these results.
const meter = core.createMeter("focus", 1000);
core.recordPull(meter, frames, 12.5, 0);
meter.frames_painted = core.FOCUS_PASS_FRAMES;
meter.ended_at = 3000;
const thumb = core.createMeter("thumb", 1000);
core.recordPull(thumb, frames, 40, 0);
thumb.frames_painted = core.THUMB_PASS_FRAMES;
thumb.ended_at = 4000;
const checks = core.emptyChecks();
for (const id of core.CHECK_IDS) core.setCheck(checks, id, "pass", "round trip " + id);
const firstSession = sessions.sessions[0];
out.diagnostics = core.buildDiagnostics({
  runId: core.makeRunId(Date.UTC(2026, 9, 21, 12, 0, 0)), nowMs: Date.UTC(2026, 9, 21, 12, 0, 5),
  adapterVersion: core.WORKSPACE_CORE_VERSION, environment: { user_agent: "node round trip", webcodecs: true },
  host: { host_id: got.host_id, name: "ChatGPT", version: "test", protocol: core.UI_PROTOCOL_VERSION, entry: "thread" },
  session: firstSession, measurements: { focus: core.summarizeMeter(meter, 3000), thumb: core.summarizeMeter(thumb, 4000) },
  checks,
});
console.log(JSON.stringify(out));
"""


@unittest.skipIf(NODE is None, "node is required for the fake Canvas and the page parsers")
class RoundTripTests(PackageCase):
    """Every fixture field from the real server; the page parsers; the artifact and diagnostics round trips."""

    def setUp(self) -> None:
        super().setUp()
        self.flows = self.world.tmp / "flows"
        self.flows.mkdir()
        (self.flows / "settings-scroll.yaml").write_text(LIVE_SCRIPT.flow_text("android", 2), encoding="utf-8")
        (self.flows / "long-scroll.yaml").write_text(LIVE_SCRIPT.flow_text("android", 60), encoding="utf-8")
        self.world.write_config({"flow_roots": [str(self.flows)], "control_idle_s": 30})
        self.world.write_canvas({"video": {"packets": None, "packet_bytes": 1200, "key_every": 10, "interval_ms": 30}})
        self.world.extra_env["FAKE_MCP_STEP_MS"] = "100"

    def call(self, client: Any, name: str, arguments: Dict[str, Any], tools: Dict[str, Any]) -> Dict[str, Any]:
        result = client.call(name, arguments, timeout=90)
        self.assertEqual(CONF.envelope_problems(result, tools[name]), [], name)
        return result

    def ok(self, client: Any, name: str, arguments: Dict[str, Any], tools: Dict[str, Any]) -> Dict[str, Any]:
        result = self.call(client, name, arguments, tools)
        structured = result["structuredContent"]
        self.assertTrue(structured["ok"] and structured["outcome"] == "completed", "%s: %s" % (name, structured))
        return result

    def wait_run(self, client: Any, tools: Dict[str, Any], host: str, run_id: str, statuses: List[str]) -> Dict[str, Any]:
        deadline = time.monotonic() + 30
        while True:
            result = self.ok(client, "get_run", {"host_id": host, "run_id": run_id}, tools)
            if result["structuredContent"]["run"]["status"] in statuses or time.monotonic() > deadline:
                return result
            time.sleep(0.2)

    def collect(self) -> Dict[str, Any]:
        client = self.start()
        tools = {tool["name"]: tool for tool in client.tools()}
        self.assertEqual(list(tools), TABLE_TOOLS)
        got: Dict[str, Any] = {}

        def ok(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
            got[name] = self.ok(client, name, arguments, tools)
            return got[name]["structuredContent"]

        host = ok("list_hosts", {})["hosts"][0]["host_id"]
        ok("list_devices", {})
        started = ok("start_session", {"host_id": host, "platform": "android", "target_id": ANDROID, "label": "package"})
        ref = {"host_id": host, "session_id": started["session"]["session_id"]}
        ok("open_workspace", {})
        ok("list_sessions", {})
        ok("get_screen", dict(ref))
        ok("get_artifact", dict(ref, artifact_id=got["get_screen"]["structuredContent"]["screen"]["artifact_id"]))
        ok("get_ui_tree", dict(ref))
        tap = ok("tap_element", dict(ref, selector={"text": "Settings"}, mode="exact"))
        ok("type_text", dict(ref, text="hello"))
        ok("press_key", dict(ref, key="BACK"))
        ok("get_operation", dict(ref, operation_id=tap["action"]["operation_id"]))
        ok("acquire_control", dict(ref))
        flows = {flow["name"]: flow["flow_id"] for flow in ok("list_flows", {})["flows"]}
        run = ok("start_flow_run", dict(ref, flow_id=flows["settings-scroll.yaml"]))["run"]
        got["get_run"] = self.wait_run(client, tools, host, run["run_id"], ["passed", "failed", "cancelled"])
        self.assertEqual(got["get_run"]["structuredContent"]["run"]["status"], "passed")
        ok("list_artifacts", dict(ref))
        long_run = self.ok(client, "start_flow_run", dict(ref, flow_id=flows["long-scroll.yaml"]), tools)
        time.sleep(0.5)
        ok("cancel_run", {"host_id": host, "run_id": long_run["structuredContent"]["run"]["run_id"]})
        self.wait_run(client, tools, host, long_run["structuredContent"]["run"]["run_id"], ["cancelled"])
        stream = ok("open_stream", dict(ref, profile="focus"))["stream"]
        ok("get_session", dict(ref))
        # The server answers as soon as one frame is ready, so frames are collected across successive
        # get_frames calls (advancing after_seq) until at least 3 have arrived and one answer carried a config.
        after = 0
        total = 0
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = self.ok(client, "get_frames", {"stream_id": stream["stream_id"], "after_seq": after,
                                                    "wait_ms": 500}, tools)
            structured = result["structuredContent"]
            if structured["frames"]:
                total += structured["frames"]
                after = structured["last_seq"]
                if "get_frames" not in got and (result.get("_meta") or {}).get("autonom/frames", {}).get("config_b64"):
                    got["get_frames"] = result
            if total >= 3 and "get_frames" in got:
                break
        self.assertIn("get_frames", got, "no frames with a config arrived")
        self.assertGreaterEqual(total, 3, "fewer than 3 frames arrived within the time limit")
        ok("release_control", dict(ref))
        ok("send_input", dict(ref, event={"op": "tap", "x": 0.5, "y": 0.25}))
        ok("pause_session", dict(ref))
        ok("resume_session", dict(ref))
        ok("close_stream", {"stream_id": stream["stream_id"]})
        # A lease another agent holds: the refusal the page shows as "the agent has control".
        self.world.fake.set_lease(self.world.fake_state, "android", ANDROID, generation=40,
                                  holder={"kind": "agent", "id": "other-agent:1", "label": "Other agent"})
        refused = self.call(client, "tap_element", dict(ref, selector={"text": "Settings"}), tools)
        self.assertEqual((refused["structuredContent"]["outcome"], refused["structuredContent"]["error_code"]),
                         ("rejected", "control_held"), refused["structuredContent"])
        self.world.fake.set_lease(self.world.fake_state, "android", ANDROID, generation=40, holder=None)
        self.client, self.tools, self.host, self.ref = client, tools, host, ref
        return {"results": got, "refused_tap": refused, "host_id": host, "session_id": ref["session_id"]}

    def run_page(self, recorded: Dict[str, Any]) -> Dict[str, Any]:
        dump = self.world.tmp / "recorded.json"
        dump.write_text(json.dumps(recorded), encoding="utf-8")
        core = (P / "ui" / "workspace-dev-core.mjs").as_uri()
        proc = subprocess.run([NODE, "--input-type=module", "-e", ROUND_TRIP_NODE, core, str(dump)],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False,
                              env=child_env(self.world.tmp))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def check_chatgpt(self, results: Path, screenshots: Path) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(LIVE / "chatgpt_mcp_live.py"), "check-chatgpt", "--results",
                               str(results), "--screenshots", str(screenshots)], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60, check=False, env=self.world.env())

    def test_fields_parsers_artifact_and_diagnostics_round_trips(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(sorted(fixture["tools"]), sorted(TABLE_TOOLS))
        recorded = self.collect()
        got = recorded["results"]
        page = self.run_page(recorded)
        self.assertEqual(page["problems"], [])
        # save_diagnostics with what buildDiagnostics made, then check-chatgpt on the saved file.
        diagnostics = page["diagnostics"]
        got["save_diagnostics"] = self.ok(self.client, "save_diagnostics", {"results": diagnostics}, self.tools)
        saved = self.world.mcp_dir / "diagnostics" / ("%s.json" % diagnostics["run_id"])
        self.assertEqual(json.loads(saved.read_text(encoding="utf-8")), diagnostics)
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        stop = self.ok(self.client, "stop_session", dict(self.ref), self.tools)
        got["stop_session"] = stop
        # Every fixture field is present in the real results.
        self.assertEqual(sorted(got), sorted(TABLE_TOOLS))
        page["outcomes"] = self.run_page(recorded)["outcomes"]
        for name in TABLE_TOOLS:
            structured = got[name]["structuredContent"]
            for path in fixture["envelope"] + fixture["tools"][name]:
                with self.subTest(tool=name, field=path):
                    self.assertIsNot(CONF.get_path(structured, path), MISSING)
            for path in fixture["meta"].get(name, []):
                with self.subTest(tool=name, meta=path):
                    self.assertIsNot(CONF.get_path(got[name].get("_meta") or {}, path), MISSING)
            with self.subTest(tool=name, parser="toolOutcome"):
                self.assertEqual(page["outcomes"][name], {"ok": True, "errorCode": None})
        refused = recorded["refused_tap"]["structuredContent"]
        for path in fixture["envelope"] + fixture["failure"] + fixture["refusals"]["tap_element"]:
            with self.subTest(refusal="tap_element", field=path):
                self.assertIsNot(CONF.get_path(refused, path), MISSING)
        self.assertEqual(refused["control"]["role"], "agent")
        # p3-008-S01: get_screen's artifact id fetched with get_artifact is the shot's PNG.
        screen = got["get_screen"]
        artifact = got["get_artifact"]
        data = base64.b64decode(artifact["_meta"]["autonom/artifact"]["data_b64"])
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(artifact["structuredContent"]["artifact"]["artifact_id"],
                         screen["structuredContent"]["screen"]["artifact_id"])
        self.assertEqual(len(data), screen["structuredContent"]["screen"]["bytes"])
        images = [block for block in screen["content"] if block.get("type") == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(base64.b64decode(images[0]["data"]), data)
        shots = sorted((self.world.fake_state / "sessions" / recorded["session_id"] / "screenshots").glob("*.png"))
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0].read_bytes(), data)
        for name, result in got.items():
            text = json.dumps(result)
            with self.subTest(no_path=name):
                self.assertNotIn(str(self.world.tmp), text)
        # The diagnostics round trip ends in check-chatgpt.
        screenshots = self.world.tmp / "screenshots"
        screenshots.mkdir()
        for name in LIVE_SCRIPT.SCREENSHOTS:
            (screenshots / name).write_bytes(LIVE_SCRIPT.PNG_MAGIC + b"\0" * 16)
        proc = self.check_chatgpt(saved, screenshots)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"ok": True, "problems": []})
        failed = json.loads(saved.read_text(encoding="utf-8"))
        failed["checks"][2]["status"] = "fail"
        copy = self.world.tmp / "diag-failed.json"
        copy.write_text(json.dumps(failed), encoding="utf-8")
        proc = self.check_chatgpt(copy, screenshots)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("page check media.thumb is fail", json.loads(proc.stdout)["problems"])
        self.assertEqual(self.client.close(), 0)
        self.assert_no_device_tool()


# ====================================================================== the results document

class ResultsDocumentTests(unittest.TestCase):
    """``check-doc`` renders the marker block again from the given files and requires the same bytes."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mcp-package-doc-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.template = TEMPLATE.read_text(encoding="utf-8")
        self.files = self.synthetic(self.root)
        self.doc = self.root / "CHATGPT_P3_RESULTS.md"

    def run_live(self, *argv: str, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(LIVE / "chatgpt_mcp_live.py"), *argv], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60, check=False,
                              env=child_env(self.root) if env is None else env)

    def check_doc(self, *argv: str) -> subprocess.CompletedProcess:
        return self.run_live("check-doc", "--doc", str(self.doc), *argv)

    def repeated(self, files: Optional[List[Path]] = None) -> List[str]:
        """The plan's (CASE-010) form: one ``--results`` per file."""
        argv: List[str] = []
        for path in self.files if files is None else files:
            argv += ["--results", str(path)]
        return argv

    def problems_of(self, text: str, files: Optional[List[Path]] = None) -> List[str]:
        self.doc.write_bytes(text.encode("utf-8"))
        proc = self.check_doc(*self.repeated(files))
        self.assertNotIn("Traceback", proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 1 if payload["problems"] else 0, proc.stdout)
        return payload["problems"]

    def synthetic(self, root: Path) -> List[Path]:
        files = []
        for platform in ("android", "ios"):
            target = LIVE_SCRIPT.ALLOWED[platform][0]
            checks = [{"id": check_id, "status": "pass", "detail": "observed %s" % check_id}
                      for check_id in LIVE_SCRIPT.CHECK_IDS]
            if platform == "ios":
                checks[13] = {"id": "media_thumb", "status": "not_exercised", "detail": "no thumb on this run"}
            measurements = {
                "focus": {"frames_per_s": 31.2, "bytes_per_s": 523456.7, "call_ms_median": 14.0, "call_ms_p95": 61.5,
                          "largest_packet": 81234, "key_frame_bytes": 80111, "resyncs": 1},
                "thumb": {"frames_per_s": 0.7, "bytes_per_s": 51234.0, "call_ms_median": 1001.2, "call_ms_p95": 1010.9,
                          "largest_packet": 80000, "key_frame_bytes": 80000, "resyncs": 0} if platform == "android" else None,
            }
            path = root / ("mcp-live-%s.json" % platform)
            path.write_text(json.dumps({"schema": LIVE_SCRIPT.RESULTS_SCHEMA, "platform": platform, "target_id": target,
                                        "adapter_version": VERSION, "ok": True, "checks": checks,
                                        "measurements": measurements}), encoding="utf-8")
            files.append(path)
        diag = {"schema_version": 1, "run_id": "diag-20261021-120000-0a1b", "host_id": "h_" + "b" * 26,
                "checks": [{"id": check_id, "status": "pass", "detail": "page %s" % check_id}
                           for check_id in LIVE_SCRIPT.PAGE_CHECK_IDS],
                "measurements": {"focus": {"frames_per_s": 24.0, "bytes_per_s": 300000, "call_ms_median": 30.1,
                                           "call_ms_p95": 90.0, "largest_packet": 70000, "key_frame_bytes": 70000,
                                           "resyncs": 0}, "thumb": None}}
        path = root / "diag-20261021-120000-0a1b.json"
        path.write_text(json.dumps(diag), encoding="utf-8")
        files.append(path)
        return files

    def filled(self, files: Optional[List[Path]] = None) -> str:
        """The template with the block written from ``files`` and the coordinator's lists filled in."""
        return LIVE_SCRIPT.coordinator_filled(LIVE_SCRIPT.fill_doc(self.template, self.files if files is None else files))

    def span(self, text: str) -> Any:
        lines = text.split("\n")
        return lines, lines.index(LIVE_SCRIPT.BLOCK_BEGIN), lines.index(LIVE_SCRIPT.BLOCK_END)

    def differs_at(self, line: int, expected: str, found: str) -> str:
        return "the results block differs from the results files at line %d: expected %s, found %s" % (
            line, expected, found)

    def test_the_template_passes_only_once_every_pending_cell_is_filled(self) -> None:
        template = self.template
        self.assertTrue(template.isascii())
        lines, begin, end = self.span(template)
        values = []
        for line in lines[begin + 1:end]:
            if not line.startswith("| ") or line.startswith(("| Item |", "| Check |", "| Profile |", "| File |")):
                continue
            cells = [cell.strip() for cell in line.strip()[1:-1].split("|")]
            values += cells[2:] if cells[0] in ("focus", "thumb") else cells[1:]
            if cells[0] == "pending":
                values.append(cells[0])
        self.assertTrue(values)
        self.assertEqual(set(values), {"pending"}, "every result cell of the template is pending")
        for item in LIVE_SCRIPT.ENVIRONMENT_ROWS + LIVE_SCRIPT.COORDINATOR_ROWS:
            self.assertIn("\n- %s: pending\n" % item, template)
        self.assertFalse([line for line in lines[:begin] + lines[end + 1:] if "|" in line])
        problems = self.problems_of(template)
        self.assertTrue(any(item.startswith("the results block differs from the results files at line") for item in problems),
                        problems)
        first = lines.index("- Date (UTC): pending") + 1
        self.assertIn("line %d outside the results block is still pending" % first, problems)
        # The block alone filled: the coordinator's items are still to do.
        block_only = LIVE_SCRIPT.fill_doc(template, self.files)
        problems = self.problems_of(block_only)
        self.assertTrue(problems)
        self.assertTrue(all(item.endswith("outside the results block is still pending") for item in problems), problems)
        filled = self.filled()
        self.assertNotIn("pending", filled.split(LIVE_SCRIPT.BLOCK_BEGIN, 1)[1])
        self.assertEqual(self.problems_of(filled), [])
        # The fill-doc command writes the same document.
        self.doc.write_text(template, encoding="utf-8")
        proc = self.run_live("fill-doc", "--doc", str(self.doc), *self.repeated())
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIs(json.loads(proc.stdout)["written"], True)
        self.assertEqual(self.doc.read_text(encoding="utf-8"), block_only)

    def test_a_correct_document_passes_with_both_results_forms(self) -> None:
        android, ios, diag = self.files
        self.doc.write_text(self.filled(), encoding="utf-8")
        for argv in (self.repeated(), ["--results", str(android), str(ios), str(diag)],
                     ["--results", str(android), str(ios), "--results", str(diag)],
                     ["--results", str(diag), str(ios), str(android)]):
            with self.subTest(argv=argv):
                proc = self.check_doc(*argv)
                self.assertEqual((proc.returncode, json.loads(proc.stdout)), (0, {"problems": []}), proc.stdout)
        # Text outside the block is free (no '|', no marker copy, nothing pending).
        self.assertEqual(self.problems_of(self.filled().replace("Status: template.", "Status: filled from the evidence.", 1)),
                         [])
        # A run with no iOS file is documented as not exercised, check by check, and passes with those files.
        partial = self.filled([android, diag])
        for check_id in LIVE_SCRIPT.CHECK_IDS:
            self.assertIn("| ios %s | no results file for iOS |" % check_id, partial)
        self.assertIn("| ios media_thumb | no results file for iOS |", partial)
        self.assertEqual(self.problems_of(partial, [android, diag]), [])

    def test_any_byte_change_inside_the_block_is_a_named_problem(self) -> None:
        filled = self.filled()
        lines, begin, end = self.span(filled)
        self.assertGreater(end - begin, 80)
        for index in range(begin + 1, end):
            for changed_line in (lines[index] + " ", lines[index][:-1] if lines[index] else "x"):
                changed = lines[:index] + [changed_line] + lines[index + 1:]
                with self.subTest(line=index + 1, text=changed_line):
                    problems = LIVE_SCRIPT.doc_problems("\n".join(changed), self.files)
                    self.assertEqual(len(problems), 1, problems)
                    self.assertTrue(problems[0].startswith(
                        "the results block differs from the results files at line %d: expected " % (index + 1)),
                        problems)
        row = lines.index("| media_focus | pass | observed media_focus |")
        problems = self.problems_of(filled.replace("| media_focus | pass |", "| media_focus | fail |", 1))
        self.assertEqual(problems, [self.differs_at(row + 1, "'| media_focus | pass | observed media_focus |'",
                                                    "'| media_focus | fail | observed media_focus |'")])
        # A line removed, added at the end, or a carriage return.
        self.assertEqual(self.problems_of("\n".join(lines[:end - 1] + lines[end:])),
                         [self.differs_at(end, "''", "the end of the block")])
        self.assertEqual(self.problems_of("\n".join(lines[:end] + ["extra"] + lines[end:])),
                         [self.differs_at(end + 1, "the end of the block", "'extra'")])
        crlf = filled.replace("| hosts | pass | observed hosts |\n", "| hosts | pass | observed hosts |\r\n", 1)
        self.assertEqual(len(self.problems_of(crlf)), 1)
        # Every Markdown trick inside the block is just a different byte.
        for extra in ("| hosts | pass | observed hosts\n", "```\n", "Check | Status\n", "<!-- -->\n"):
            with self.subTest(extra=extra):
                problems = self.problems_of(filled.replace("## Measurements\n", extra + "## Measurements\n", 1))
                self.assertEqual(len(problems), 1, problems)
                self.assertTrue(problems[0].startswith("the results block differs"), problems)

    def test_markers_must_appear_once_each_alone_and_in_order(self) -> None:
        begin_marker, end_marker = LIVE_SCRIPT.BLOCK_BEGIN, LIVE_SCRIPT.BLOCK_END
        filled = self.filled()
        lines, begin, end = self.span(filled)
        missing_begin = "the begin marker %s is missing (it stands alone on its own line)" % begin_marker
        missing_end = "the end marker %s is missing (it stands alone on its own line)" % end_marker
        swapped = list(lines)
        swapped[begin], swapped[end] = end_marker, begin_marker
        cases = (
            (filled.replace(begin_marker + "\n", "", 1), [missing_begin]),
            (filled.replace(end_marker + "\n", "", 1), [missing_end]),
            (filled.replace(begin_marker + "\n", " " + begin_marker + "\n", 1),
             [missing_begin, "line %d holds a copy of a results marker; only the two marker lines may" % (begin + 1)]),
            (filled.replace(end_marker + "\n", end_marker + " \n", 1),
             [missing_end, "line %d holds a copy of a results marker; only the two marker lines may" % (end + 1)]),
            (filled + begin_marker + "\n", ["the begin marker appears 2 times (lines %d, %d); it appears once"
                                            % (begin + 1, len(lines))]),
            ("\n".join(lines[:2] + [end_marker] + lines[2:]),
             ["the end marker appears 2 times (lines 3, %d); it appears once" % (end + 2)]),
            ("\n".join(swapped), ["the end marker (line %d) comes before the begin marker (line %d)"
                                  % (begin + 1, end + 1)]),
            (filled.replace("Status: template.", "Status: template. <!-- AUTONOM-RESULTS:end -->", 1),
             ["line 3 holds a copy of a results marker; only the two marker lines may"]),
        )
        for text, expected in cases:
            with self.subTest(expected=expected):
                self.assertNotEqual(text, filled)
                self.assertEqual(self.problems_of(text), expected)
        inside = filled.replace("## Evidence\n", "## Evidence\n%s\n" % end_marker.replace("end", "end "), 1)
        problems = self.problems_of(inside)
        self.assertTrue(any(item.endswith("holds a copy of a results marker; only the two marker lines may")
                            for item in problems), problems)
        # A detail that holds a marker's text is written neutralized, so the document still passes.
        android = json.loads(self.files[0].read_text(encoding="utf-8"))
        android["checks"][0]["detail"] = end_marker
        self.files[0].write_text(json.dumps(android), encoding="utf-8")
        self.assertEqual(self.problems_of(self.filled()), [])
        with self.assertRaises(LIVE_SCRIPT.DocRefused):
            LIVE_SCRIPT.fill_doc(filled.replace(end_marker + "\n", "", 1), self.files)

    def test_a_pipe_line_outside_the_block_is_refused(self) -> None:
        filled = self.filled()
        lines, begin, _ = self.span(filled)
        for extra in ("| Check | Status | Detail |\n|---|---|---|\n| hosts | pass | x |\n",
                      "Check | Status\n--- | ---\nhosts | pass\n",
                      "| hosts | pass | x\n",
                      "hosts | pass\n",
                      "```\n| hosts | pass |\n```\n",
                      "    | indented | code |\n",
                      "A sentence with a | in it.\n"):
            with self.subTest(extra=extra):
                text = filled.replace("## Coordinator record\n", extra + "\n## Coordinator record\n", 1)
                first = filled.split("\n").index("## Coordinator record") + 1
                problems = self.problems_of(text)
                pipes = [first + offset for offset, line in enumerate(extra.split("\n")) if "|" in line]
                self.assertEqual(problems, ["line %d outside the results block contains '|' (tables belong in the block)"
                                            % line for line in pipes])
        problems = self.problems_of(filled + "\n| after | the block |\n")
        self.assertEqual(problems, ["line %d outside the results block contains '|' (tables belong in the block)"
                                    % (len(lines) + 1)])

    def test_a_results_file_left_out_is_a_named_problem(self) -> None:
        android, ios, diag = self.files
        filled = self.filled()
        lines, _, _ = self.span(filled)
        target_row = lines.index("| iOS target | %s |" % LIVE_SCRIPT.IOS_UDID) + 1
        self.assertEqual(self.problems_of(filled, [android, diag]), [self.differs_at(
            target_row, "'| iOS target | no results file |'", "'| iOS target | %s |'" % LIVE_SCRIPT.IOS_UDID)])
        problems = self.problems_of(filled, [android, ios])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("expected '| focus | chatgpt | n/a | n/a | n/a | n/a | n/a | n/a | n/a |'", problems[0])
        problems = self.problems_of(filled, [ios, diag])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("expected '| Android target | no results file |'", problems[0])

    def test_a_forbidden_or_different_target_or_version_is_a_named_problem(self) -> None:
        android, ios, diag = self.files
        filled = self.filled()
        lines, _, _ = self.span(filled)
        # The doc may not write a different (or a longer) value than the files hold.
        for old, new in (("| Adapter version | %s |" % VERSION, "| Adapter version | %s-dev |" % VERSION),
                         ("| Adapter version | %s |" % VERSION, "| Adapter version | %s and 0.2.0 |" % VERSION),
                         ("| Android target | emulator-5584 |", "| Android target | emulator-5584, emulator-5554 |"),
                         ("| iOS target | %s |" % LIVE_SCRIPT.IOS_UDID, "| iOS target | %s |" % LIVE_SCRIPT.IOS_UDID.lower())):
            with self.subTest(new=new):
                self.assertEqual(self.problems_of(filled.replace(old, new, 1)),
                                 [self.differs_at(lines.index(old) + 1, repr(old), repr(new))])
        folder = self.root / "changed"
        folder.mkdir()
        data = json.loads(android.read_text(encoding="utf-8"))
        data["target_id"] = "emulator-5554"
        forbidden = folder / android.name
        forbidden.write_text(json.dumps(data), encoding="utf-8")
        problems = self.problems_of(self.filled([forbidden, ios, diag]), [forbidden, ios, diag])
        self.assertEqual(problems, ["results mcp-live-android.json: target_id emulator-5554 is not an allowed device"])
        data = json.loads(ios.read_text(encoding="utf-8"))
        data["adapter_version"] = "0.2.0"
        other = folder / ios.name
        other.write_text(json.dumps(data), encoding="utf-8")
        refilled = self.filled([android, other, diag])
        self.assertIn("| Adapter version | the files disagree: android %s, ios 0.2.0 |" % VERSION, refilled)
        self.assertEqual(self.problems_of(refilled, [android, other, diag]),
                         ["the results files disagree on adapter_version: android %s, ios 0.2.0" % VERSION])
        del data["adapter_version"]
        other.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.problems_of(self.filled([android, other, diag]), [android, other, diag]),
                         ["results mcp-live-ios.json lacks adapter_version"])

    def test_malformed_results_fields_are_problems_not_tracebacks(self) -> None:
        android, ios, diag = self.files
        filled = self.filled()
        base = json.loads(android.read_text(encoding="utf-8"))
        cases = (
            ({"checks": 5}, "results mcp-live-android.json: checks is a number, not a list"),
            ({"checks": "x"}, "results mcp-live-android.json: checks is text, not a list"),
            ({"measurements": ["x"]}, "results mcp-live-android.json: measurements is a list, not an object"),
            ({"measurements": {"focus": "fast"}}, "results mcp-live-android.json: measurements.focus is text, not an object"),
            ({"measurements": {"focus": {"frames_per_s": "fast"}}},
             "results mcp-live-android.json: measurements.focus.frames_per_s is text, not a number"),
            ({"measurements": {"focus": {"resyncs": True}}},
             "results mcp-live-android.json: measurements.focus.resyncs is a boolean, not a number"),
            ({"checks": ["x"] + base["checks"][1:]}, "results mcp-live-android.json: checks[0] is text, not an object"),
            ({"checks": [{"id": 3, "status": "pass"}] + base["checks"][1:]},
             "results mcp-live-android.json: checks[0].id is a number, not text"),
            ({"checks": [{"id": "hosts", "status": "bogus"}] + base["checks"][1:]},
             "results mcp-live-android.json: checks[0].status is bogus, not pass, fail or not_exercised"),
            ({"checks": [{"id": "hosts", "status": "pass", "detail": {"x": 1}}] + base["checks"][1:]},
             "results mcp-live-android.json: checks[0].detail is an object, not text"),
            ({"checks": base["checks"][1:]}, "results mcp-live-android.json lacks the check hosts"),
            ({"adapter_version": 1}, "results mcp-live-android.json: adapter_version is a number, not text"),
            ({"target_id": ["x"]}, 'results mcp-live-android.json: target_id ["x"] is not an allowed device'),
        )
        folder = self.root / "malformed"
        folder.mkdir()
        for change, problem in cases:
            with self.subTest(problem=problem):
                data = dict(base)
                data.update(change)
                path = folder / "mcp-live-android.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                self.assertIn(problem, self.problems_of(filled, [path, ios, diag]))
                self.assertIn(problem, self.problems_of(self.filled([path, ios, diag]), [path, ios, diag]))
        diag_data = json.loads(diag.read_text(encoding="utf-8"))
        diag_data["measurements"] = 7
        bad_diag = folder / diag.name
        bad_diag.write_text(json.dumps(diag_data), encoding="utf-8")
        self.assertIn("results %s: measurements is a number, not an object" % diag.name,
                      self.problems_of(filled, [android, ios, bad_diag]))
        # A platform that is not text is a named problem for check, check-doc and fill-doc, never a traceback.
        for platform, kind in ((["android"], "a list"), ({"android": 1}, "an object"), (5, "a number")):
            with self.subTest(platform=platform):
                data = dict(base, platform=platform)
                path = folder / "mcp-live-android.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                proc = self.run_live("check", "--results", str(path))
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("platform is %s, not text" % kind, json.loads(proc.stdout)["files"][0]["problems"])
                problem = "results mcp-live-android.json: platform is %s, not text" % kind
                self.assertIn(problem, self.problems_of(filled, [path, ios, diag]))
                self.doc.write_text(self.template, encoding="utf-8")
                proc = self.run_live("fill-doc", "--doc", str(self.doc), *self.repeated([path, ios, diag]))
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn(problem, json.loads(proc.stdout)["problems"])
        (folder / "broken.json").write_text("{", encoding="utf-8")
        self.assertIn("results broken.json cannot be read (JSONDecodeError)",
                      self.problems_of(filled, [android, ios, diag, folder / "broken.json"]))
        self.assertIn("results missing.json cannot be read (FileNotFoundError)",
                      self.problems_of(filled, [android, ios, diag, folder / "missing.json"]))

    def test_a_bearer_value_blocks_fill_doc_and_check_doc(self) -> None:
        android, ios, diag = self.files
        filled = self.filled()
        for secret, hit in (("Bearer abc123", "a bearer value"), ("https://x/?token=abc123", "token=")):
            with self.subTest(secret=secret):
                data = json.loads(android.read_text(encoding="utf-8"))
                data["checks"][2]["detail"] = "sent %s" % secret
                leaky = self.root / "leaky" / android.name
                leaky.parent.mkdir(exist_ok=True)
                leaky.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(LIVE_SCRIPT.DocRefused) as caught:
                    LIVE_SCRIPT.fill_doc(self.template, [leaky, ios, diag])
                self.assertEqual(caught.exception.problems, ["the filled document would hold %s" % hit])
                self.doc.write_text(self.template, encoding="utf-8")
                proc = self.run_live("fill-doc", "--doc", str(self.doc), *self.repeated([leaky, ios, diag]))
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertEqual(json.loads(proc.stdout), {"written": False,
                                                           "problems": ["the filled document would hold %s" % hit]})
                self.assertEqual(self.doc.read_text(encoding="utf-8"), self.template)
                self.assertNotIn("abc123", proc.stdout + proc.stderr)
                written = filled.replace("| start | pass | observed start |", "| start | pass | sent %s |" % secret, 1)
                self.assertNotEqual(written, filled)
                problems = self.problems_of(written, [leaky, ios, diag])
                self.assertIn("the document holds %s" % hit, problems)
                self.assertIn("results mcp-live-android.json holds a bearer or token= string", problems)
                self.assertNotIn("abc123", json.dumps(problems))
                # Outside the block too.
                problems = self.problems_of(filled.replace("Status: template.", "Status: %s." % secret, 1))
                self.assertEqual(problems, ["the document holds %s" % hit])
        # A differing line is reported redacted.
        problems = self.problems_of(filled.replace("| start | pass | observed start |", "| start | pass | Bearer abc123 |", 1))
        self.assertIn("the document holds a bearer value", problems)
        self.assertTrue(any("'| start | pass | Bearer <removed> |'" in item for item in problems), problems)
        self.assertNotIn("abc123", json.dumps(problems))

    def test_the_results_file_on_disk_has_no_token_or_temporary_root(self) -> None:
        # Every token a discovery document showed during the run is remembered (the Canvas restarted once).
        state = self.root / "state"
        (state / "canvas").mkdir(parents=True)
        run = LIVE_SCRIPT.DeviceRun(SimpleNamespace(platform="android", target=ANDROID, state=state), 1.0)
        tokens = ["canvas-" + "k" * 32, "canvas-" + "m" * 32]
        for token in tokens:
            (state / "canvas" / "workspace.json").write_text(json.dumps(
                {"mode": "workspace", "workspace": LIVE_SCRIPT.WORKSPACE, "pid": os.getpid(), "port": 1,
                 "token": token}), encoding="utf-8")
            run.remember_token()
        self.assertEqual(run.tokens, sorted(tokens))
        temp_root = str(self.root / "mcp-live-run")
        payload = json.loads(self.files[0].read_text(encoding="utf-8"))
        payload["checks"][5]["detail"] = "Bearer %s from %s/state/canvas" % (tokens[0], temp_root)
        payload["checks"][6]["detail"] = "after the restart %s" % tokens[1]
        payload["checks"][16] = {"id": "no_secrets", "status": "fail",
                                 "detail": "found after the run: the Canvas token, the temporary root path"}
        payload["measurements"]["focus"]["notes"] = [tokens[1], {temp_root: tokens[0]}]
        self.assertEqual(LIVE_SCRIPT.secret_hits([json.dumps(payload)], run.tokens, [temp_root])[:1], ["the Canvas token"])
        evidence = self.root / "evidence"
        evidence.mkdir(mode=0o700)
        saved = evidence / "mcp-live-android.json"
        returned = LIVE_SCRIPT.save_results(saved, payload, run.tokens, [temp_root])
        text = saved.read_text(encoding="utf-8")
        for token in tokens:
            self.assertNotIn(token, text)
        self.assertNotIn(temp_root, text)
        self.assertIn(LIVE_SCRIPT.REDACTED_TOKEN, text)
        self.assertIn(LIVE_SCRIPT.REDACTED_ROOT, text)
        self.assertEqual(json.loads(text), returned)
        self.assertEqual(returned["checks"][16]["detail"],
                         "found after the run: the Canvas token, the temporary root path")
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(LIVE_SCRIPT.redact({"a": "b"}, None, []), {"a": "b"})
        self.assertEqual(sorted(os.listdir(str(evidence))), ["mcp-live-android.json"])
        # A link at the results path is replaced, not followed.
        victim = self.root / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        link = evidence / "mcp-live-ios.json"
        link.symlink_to(victim)
        LIVE_SCRIPT.save_results(link, {"x": 1}, None, [])
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
        self.assertFalse(link.is_symlink())
        self.assertEqual(json.loads(link.read_text(encoding="utf-8")), {"x": 1})
        self.assertEqual(link.stat().st_mode & 0o777, 0o600)
        # An evidence folder others can write, or a link in place of the folder, is refused.
        for mode in (0o770, 0o707, 0o777):
            with self.subTest(mode=oct(mode)):
                shared = self.root / ("shared-%o" % mode)
                shared.mkdir(mode=0o700)
                os.chmod(str(shared), mode)
                with self.assertRaises(LIVE_SCRIPT.UsageError) as caught:
                    LIVE_SCRIPT.save_results(shared / "x.json", {}, None, [])
                self.assertEqual(caught.exception.code, "evidence_dir_unsafe")
                self.assertEqual(os.listdir(str(shared)), [])
        linked = self.root / "linked-evidence"
        linked.symlink_to(evidence)
        with self.assertRaises(LIVE_SCRIPT.UsageError):
            LIVE_SCRIPT.save_results(linked / "x.json", {}, None, [])
        # A live run: failing adb/emulator/xcrun/simctl/idb stubs come first on the child PATH, so the
        # refusal can never reach a real device; none of them may be called.
        world = CONF.FakeWorld(prefix="mcp-package-live-refusal-")
        self.addCleanup(world.cleanup)
        proc = self.run_live("--android", ANDROID, "--evidence-dir", str(self.root / "shared-777"), env=world.env())
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stderr)["error_code"], "evidence_dir_unsafe")
        self.assertEqual(world.stub_calls(), [])


if __name__ == "__main__":
    unittest.main()
