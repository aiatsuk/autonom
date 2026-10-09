"""`autonom canvas` and the workspace CLI (run canvas-multi-device, contract 6.1 and 6.4).

Fakes only. `node` is a script that records its argv and the process registry row
the supervisor wrote for it; adb and xcrun are sentinels that log any call; a running
Canvas is a small Python HTTP server saved under the Canvas server's file name, so the
CLI's "this pid runs the Canvas" check holds for it, plus its discovery file. Every
state path points into a temporary directory.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "autonom.py"
SCRIPT = (ROOT / "plugins/autonom/skills/android-emulator-browser/scripts/"
          "android-emulator-browser.mjs")
SERIAL = "emulator-5580"
SERIAL_B = "emulator-5584"
UDID = "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5"
TOKEN = "tok-SECRET-1f2e3d"
TOOL_ENV = ("AUTONOM_ADB", "AUTONOM_SIMCTL", "AUTONOM_IDB", "AUTONOM_MITMDUMP", "AUTONOM_AXE",
            "AUTONOM_IOS_HID", "AUTONOM_IDB_COMPANION", "AUTONOM_IDB_COMPANION_BIN",
            "AUTONOM_IDB_STATE_FILE", "AUTONOM_FAKE_STATE", "AUTONOM_FAKE_LOG", "AUTONOM_EMULATOR",
            "DEVELOPER_DIR", "AUTONOM_SCRCPY_SERVER", "SCRCPY_SERVER_PATH", "XDG_STATE_HOME",
            "AUTONOM_CAPTURES_DIR", "PYTHON")

FAKE_NODE = """#!{python}
import json, os, sys, time
# The supervisor registers this child right after starting it: wait for the row.
registry = os.path.join(os.environ["AUTONOM_HOME"], "processes", "processes.json")
rows = None
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    try:
        with open(registry, encoding="utf-8") as handle:
            rows = json.load(handle)
    except (OSError, ValueError):
        rows = None
    if rows and '"canvas"' in json.dumps(rows):
        break
    time.sleep(0.05)
with open(os.environ["FAKE_NODE_OUT"], "w", encoding="utf-8") as handle:
    json.dump({{"argv": sys.argv[1:], "registry": rows}}, handle)
"""

# A running Canvas: answers the routes the CLI calls, logs every request (method, path,
# authorization, body) as one JSON line, and exits on POST /api/stop unless told not to.
FAKE_CANVAS = r'''
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ["FAKE_CANVAS_LOG"]
IGNORE_STOP = os.environ.get("FAKE_CANVAS_IGNORE_STOP") == "1"
TABS = [{"id": "t_aaaaaaaa", "name": "Canvas 1"}, {"id": "t_bbbbbbbb", "name": "Second"}]
DEVICES = [{"id": "android~emulator-5580", "platform": "android", "target": "emulator-5580",
            "tab": "t_aaaaaaaa"}]

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def record(self, body=None):
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"method": self.command, "path": self.path,
                                     "authorization": self.headers.get("Authorization"),
                                     "host": self.headers.get("Host"), "body": body}) + "\n")

    def do_GET(self):
        self.record()
        if self.path == "/api/workspace":
            return self.answer(200, {"ok": True, "mode": "workspace", "tabs": TABS})
        if self.path == "/api/targets":
            return self.answer(200, {"ok": True, "running": [
                {"platform": "android", "target": "emulator-5580"},
                {"platform": "ios", "target": "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5"}],
                "bootable": []})
        if self.path == "/api/devices":
            return self.answer(200, {"ok": True, "devices": DEVICES})
        self.answer(404, {"ok": False, "error": "Not found", "error_code": "not_found", "hint": None})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.record(body)
        if self.path == "/api/stop":
            self.answer(202, {"ok": True, "stopping": True})
            if not IGNORE_STOP:
                threading.Timer(0.2, lambda: os._exit(0)).start()
            return
        if self.path == "/api/devices":
            if body.get("target") == "clash-target":
                return self.answer(409, {"ok": False, "error": "In use", "error_code": "workspace_in_use",
                                         "hint": None, "port": 1, "message": "x", "code": "y"})
            if body.get("target") == "busy-target":
                return self.answer(409, {"ok": False, "error": "Tab Canvas 1 is full",
                                         "error_code": "tab_full", "hint": "Pick another tab",
                                         "tab": "t_aaaaaaaa"})
            device = {"id": body["platform"] + "~" + body["target"], "platform": body["platform"],
                      "target": body["target"], "tab": body.get("tab") or "t_aaaaaaaa"}
            return self.answer(201, {"ok": True, "device": device, "attached": True})
        if self.path.startswith("/api/devices/") and self.path.endswith("/detach"):
            from urllib.parse import unquote
            device = unquote(self.path[len("/api/devices/"):-len("/detach")])
            return self.answer(200, {"ok": True, "detached": device, "session_stopped": True})
        self.answer(404, {"ok": False, "error": "Not found", "error_code": "not_found", "hint": None})

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
with open(os.environ["FAKE_CANVAS_PORT"], "w", encoding="utf-8") as handle:
    handle.write(str(server.server_address[1]))
server.serve_forever()
'''


def write_script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def load_cli():
    spec = importlib.util.spec_from_file_location("autonom_cli_multi", CLI)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class CanvasCliCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.trace = self.root / "device-tool.log"
        bin_dir = self.root / "bin"
        self.adb = write_script(bin_dir / "adb", f'#!/bin/sh\necho "adb $*" >> "{self.trace}"\nexit 1\n')
        self.xcrun = write_script(bin_dir / "xcrun", f'#!/bin/sh\necho "xcrun $*" >> "{self.trace}"\nexit 1\n')
        write_script(bin_dir / "node", FAKE_NODE.format(python=sys.executable))
        ps = shutil.which("ps")
        if ps:
            (bin_dir / "ps").symlink_to(ps)
        self.node_out = self.root / "node.json"
        self.env = {key: value for key, value in os.environ.items() if key not in TOOL_ENV}
        self.env.update({"AUTONOM_HOME": str(self.state), "HOME": str(self.home),
                         "PATH": str(bin_dir), "FAKE_NODE_OUT": str(self.node_out)})
        self.home.mkdir()

    # --- running the CLI -----------------------------------------------------------

    def cli(self, *argv: str, tools: bool = True, timeout: float = 60) -> subprocess.CompletedProcess:
        prefix = ["--adb", str(self.adb), "--simctl", str(self.xcrun)] if tools else []
        completed = subprocess.run(
            [sys.executable, str(CLI), *prefix, *argv], cwd=self.root, env=self.env, text=True,
            stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=timeout)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        self.assertFalse(self.trace.exists(), "the CLI ran a device tool itself")
        return completed

    def node_run(self, completed: subprocess.CompletedProcess) -> dict:
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(self.node_out.exists(), "node was not started")
        return json.loads(self.node_out.read_text(encoding="utf-8"))

    def refused(self, completed: subprocess.CompletedProcess, code: str) -> dict:
        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        self.assertFalse(self.node_out.exists(), "node started despite the refusal")
        payload = json.loads(completed.stderr)
        self.assertEqual(payload["error_code"], code, payload)
        return payload

    def ok(self, completed: subprocess.CompletedProcess) -> dict:
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    # --- a running Canvas --------------------------------------------------------------

    def fake_canvas(self, *, workspace: str | None = "default", mode: str = "workspace",
                    token: str | None = TOKEN, ignore_stop: bool = False,
                    name: str = "a") -> dict:
        folder = self.root / f"canvas-{name}"
        script = write_script(folder / "android-emulator-browser.mjs", FAKE_CANVAS)
        log, port_file = folder / "requests.log", folder / "port"
        env = dict(self.env, FAKE_CANVAS_LOG=str(log), FAKE_CANVAS_PORT=str(port_file),
                   FAKE_CANVAS_IGNORE_STOP="1" if ignore_stop else "0")
        process = subprocess.Popen([sys.executable, str(script)], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._kill, process)
        deadline = time.monotonic() + 15
        while not port_file.exists() or not port_file.read_text(encoding="utf-8").strip():
            self.assertLess(time.monotonic(), deadline, "the fake Canvas did not start")
            self.assertIsNone(process.poll(), "the fake Canvas exited")
            time.sleep(0.05)
        port = int(port_file.read_text(encoding="utf-8"))
        document = self.discovery(port, process.pid, workspace=workspace, mode=mode, token=token)
        return {"process": process, "port": port, "log": log, "document": document}

    def discovery(self, port: int, pid: int, *, workspace: str | None = "default",
                  mode: str = "workspace", token: str | None = TOKEN) -> dict:
        document = {
            "schema": "autonom-canvas/v1", "pid": pid, "port": port,
            "url": f"http://127.0.0.1:{port}/", "token": token,
            "started_at": "2026-10-07T10:00:00.000Z", "mode": mode, "workspace": workspace,
            "tabs": [{"id": "t_aaaaaaaa", "name": "Canvas 1", "devices": ["android~emulator-5580"]}],
            "devices": [{"id": "android~emulator-5580", "platform": "android",
                         "target": "emulator-5580", "tab": "t_aaaaaaaa"}],
            "booted": [],
        }
        folder = self.state / "canvas"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{port}.json").write_text(json.dumps(document), encoding="utf-8")
        return document

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)

    @staticmethod
    def requests(canvas: dict) -> list[dict]:
        if not canvas["log"].exists():
            return []
        return [json.loads(line) for line in canvas["log"].read_text(encoding="utf-8").splitlines()]


class ModeSelectionTests(CanvasCliCase):
    def legacy_argv(self, *, platform: str = "android", target: str = SERIAL) -> list[str]:
        tool = ["--adb", str(self.adb)] if platform == "android" else ["--simctl", str(self.xcrun)]
        return [str(SCRIPT), "--platform", platform, "--target", target, "--port", "3277",
                "--transport", "auto", *tool]

    def canvas_row(self, run: dict) -> dict:
        rows = run["registry"]
        if isinstance(rows, dict):
            rows = rows.get("entries") or rows.get("processes") or list(rows.values())
        canvas = [row for row in rows or [] if isinstance(row, dict) and row.get("kind") == "canvas"]
        self.assertTrue(canvas, f"no canvas row in the registry: {rows}")
        return canvas[0]

    def test_bare_canvas_starts_a_workspace_with_no_session_owner(self) -> None:
        run = self.node_run(self.cli("canvas"))
        argv = run["argv"]
        self.assertEqual(argv[0], str(SCRIPT))
        self.assertEqual(argv[argv.index("--workspace") + 1], "default")
        self.assertEqual(argv[argv.index("--port") + 1], "3277")
        self.assertEqual(argv[argv.index("--adb") + 1], str(self.adb))
        self.assertEqual(argv[argv.index("--simctl") + 1], str(self.xcrun))
        self.assertEqual(argv[argv.index("--autonom") + 1], str(CLI.resolve()))
        for absent in ("--platform", "--target", "--serial", "--device", "--ephemeral"):
            self.assertNotIn(absent, argv)
        row = self.canvas_row(run)
        self.assertIsNone(row.get("owner"))
        self.assertIsNone(row.get("session_id"))
        self.assertEqual(row.get("workspace"), "default")

    def test_serve_without_target_is_the_same_workspace(self) -> None:
        argv = self.node_run(self.cli("canvas", "serve"))["argv"]
        self.assertIn("--workspace", argv)
        self.assertNotIn("--target", argv)

    def test_a_target_flag_keeps_the_single_canvas_command_byte_for_byte(self) -> None:
        argv = self.node_run(self.cli("--serial", SERIAL, "canvas", "serve"))["argv"]
        self.assertEqual(argv, self.legacy_argv())
        self.node_out.unlink()
        argv_leaf = self.node_run(self.cli("canvas", "serve", "--serial", SERIAL))["argv"]
        self.assertEqual(argv_leaf, self.legacy_argv())

    def test_one_device_is_the_single_canvas(self) -> None:
        argv = self.node_run(self.cli("canvas", "serve", "--device", f"android:{SERIAL}"))["argv"]
        self.assertEqual(argv, self.legacy_argv())
        self.node_out.unlink()
        argv = self.node_run(self.cli("canvas", "serve", "--device", f"ios:{UDID}"))["argv"]
        self.assertEqual(argv, self.legacy_argv(platform="ios", target=UDID))

    def test_single_canvas_adds_only_the_action_flags_it_was_given(self) -> None:
        build = self.root / "build"
        build.mkdir()
        argv = self.node_run(self.cli("--serial", SERIAL, "canvas", "serve", "--install-root",
                                      str(build), "--captures-dir", str(self.root / "caps")))["argv"]
        self.assertEqual(argv, self.legacy_argv() + [
            "--install-root", str(build.absolute()), "--captures-dir", str((self.root / "caps").absolute())])

    def test_two_devices_or_split_make_a_workspace(self) -> None:
        argv = self.node_run(self.cli("canvas", "serve", "--device", f"android:{SERIAL}",
                                      "--device", f"ios:{UDID}"))["argv"]
        devices = [argv[i + 1] for i, item in enumerate(argv) if item == "--device"]
        self.assertEqual(devices, [f"android:{SERIAL}", f"ios:{UDID}"])
        self.assertEqual(argv[argv.index("--workspace") + 1], "default")
        self.node_out.unlink()
        argv = self.node_run(self.cli("canvas", "serve", "--device", f"android:{SERIAL}", "--split"))["argv"]
        self.assertEqual([argv[i + 1] for i, item in enumerate(argv) if item == "--device"],
                         [f"android:{SERIAL}"])
        self.node_out.unlink()
        argv = self.node_run(self.cli("--serial", SERIAL, "canvas", "serve", "--split"))["argv"]
        self.assertEqual(argv[argv.index("--device") + 1], f"android:{SERIAL}")
        self.assertNotIn("--target", argv)

    def test_workspace_flags_reach_node(self) -> None:
        build = self.root / "build"
        build.mkdir()
        argv = self.node_run(self.cli(
            "canvas", "serve", "--workspace", "qa.1", "--ephemeral", "--port", "0",
            "--bootable", "avd:Autonom_Split2_API36@5586", "--bootable", f"simulator:{UDID}",
            "--shutdown-booted", "--install-root", str(build), "--captures-dir", "caps",
            "--token", TOKEN))["argv"]
        values = list(zip(argv, argv[1:]))
        self.assertIn(("--workspace", "qa.1"), values)
        self.assertIn(("--port", "0"), values)
        self.assertIn(("--bootable", "avd:Autonom_Split2_API36@5586"), values)
        self.assertIn(("--bootable", f"simulator:{UDID}"), values)
        self.assertIn(("--install-root", str(build.absolute())), values)
        captures = Path(argv[argv.index("--captures-dir") + 1])
        self.assertTrue(captures.is_absolute(), captures)
        self.assertEqual(captures.resolve(), (self.root / "caps").resolve())
        self.assertIn(("--token", TOKEN), values)
        for flag in ("--ephemeral", "--shutdown-booted"):
            self.assertIn(flag, argv)

    def test_port_zero_is_accepted_in_both_modes(self) -> None:
        argv = self.node_run(self.cli("--serial", SERIAL, "canvas", "serve", "--port", "0"))["argv"]
        self.assertEqual(argv[argv.index("--port") + 1], "0")


class FlagValidationTests(CanvasCliCase):
    def test_bad_values_are_refused_before_node(self) -> None:
        missing = str(self.root / "no-such-folder")
        cases = [
            (("canvas", "serve", "--port", "-1"), "invalid_value"),
            (("canvas", "serve", "--port", "70000"), "invalid_value"),
            (("canvas", "serve", "--device", "windows:x"), "invalid_value"),
            (("canvas", "serve", "--device", "android:"), "invalid_value"),
            (("canvas", "serve", "--device", "android:a/b"), "invalid_value"),
            (("canvas", "serve", "--device", "android:x", "--device", "android:x"), "invalid_value"),
            (("canvas", "serve", *sum((("--device", f"android:e{i}") for i in range(9)), ())),
             "invalid_value"),
            (("canvas", "serve", "--workspace", "bad name"), "invalid_value"),
            (("canvas", "serve", "--workspace", "x" * 41), "invalid_value"),
            (("canvas", "serve", "--bootable", "avd:Pixel@5555"), "invalid_value"),
            (("canvas", "serve", "--bootable", "avd:Pixel@5700"), "invalid_value"),
            (("canvas", "serve", "--bootable", "simulator:nope"), "invalid_value"),
            (("canvas", "serve", "--bootable", "phone:x"), "invalid_value"),
            (("canvas", "serve", *sum((("--bootable", f"avd:A{i}") for i in range(9)), ())),
             "invalid_value"),
            (("canvas", "serve", "--install-root", missing), "invalid_value"),
            (("--serial", SERIAL, "canvas", "serve", "--ephemeral"), "invalid_value"),
            (("--serial", SERIAL, "canvas", "serve", "--shutdown-booted"), "invalid_value"),
            (("canvas", "serve", "--device", f"android:{SERIAL}", "--bootable", "avd:A"), "invalid_value"),
            (("--serial", SERIAL, "canvas", "serve", "--device", "android:x"), "usage_error"),
            (("canvas", "serve", "--device", f"android:{SERIAL}", "--device", f"ios:{UDID}",
              "--transport", "screencap"), "invalid_value"),
            (("canvas", "serve", "--device", f"android:{SERIAL}", "--split", "--transport", "idb"),
             "unsupported_on_platform"),
            (("canvas", "serve", "--device", f"ios:{UDID}", "--split", "--transport", "scrcpy"),
             "unsupported_on_platform"),
            (("--platform", "ios", "canvas", "serve", "--device", f"android:{SERIAL}"), "usage_error"),
        ]
        for argv, code in cases:
            with self.subTest(argv=argv):
                self.refused(self.cli(*argv), code)

    def test_new_flags_keep_the_token_abbreviations(self) -> None:
        """`--to` and `--tok` still reach --token (no new flag starts with --to)."""
        parser = load_cli().build_parser()
        for argv in (["--to", TOKEN], ["--tok", TOKEN], [f"--to={TOKEN}"]):
            with self.subTest(argv=argv):
                args = parser.parse_args(["canvas", "serve", *argv])
                self.assertEqual(args.token, TOKEN)
        for parser_path in (("serve",), ("attach",), ("detach",), ("stop",), ("list",)):
            with self.subTest(verb=parser_path):
                options = self._options(parser, ("canvas", *parser_path))
                self.assertEqual([item for item in options if item.startswith("--to") and item != "--token"], [])

    def test_token_starting_with_a_dash_reaches_node_as_one_item(self) -> None:
        """A random token may start with "-": as a separate argv item it read as an
        option (`argument --token: expected one argument`); it now goes as --token=VALUE."""
        dash = "--" + TOKEN
        for argv in (("--serial", SERIAL, "canvas", "serve", f"--token={dash}"),
                     ("canvas", "serve", "--workspace", "qa.1", "--port", "0", f"--token={dash}")):
            with self.subTest(argv=argv):
                if self.node_out.exists():
                    self.node_out.unlink()
                node_argv = self.node_run(self.cli(*argv))["argv"]
                self.assertIn(f"--token={dash}", node_argv)
                self.assertNotIn(dash, node_argv)
                self.assertNotIn("--token", node_argv)

    def test_abbreviated_token_reaches_node(self) -> None:
        argv = self.node_run(self.cli("--serial", SERIAL, "canvas", "serve", "--tok", TOKEN))["argv"]
        self.assertEqual(argv[-2:], ["--token", TOKEN])

    @staticmethod
    def _options(parser, path: tuple[str, ...]) -> list[str]:
        import argparse
        node = parser
        for word in path:
            node = next(action for action in node._actions  # noqa: SLF001
                        if isinstance(action, argparse._SubParsersAction)).choices[word]  # noqa: SLF001
        return [option for action in node._actions for option in action.option_strings]  # noqa: SLF001


class AlreadyRunningTests(CanvasCliCase):
    def test_a_live_canvas_of_the_workspace_is_reported_not_started_again(self) -> None:
        canvas = self.fake_canvas()
        payload = self.ok(self.cli("canvas"))
        self.assertFalse(self.node_out.exists(), "a second server was started")
        self.assertEqual(payload["already_running"], True)
        self.assertEqual(payload["port"], canvas["port"])
        self.assertEqual(payload["pid"], canvas["process"].pid)
        self.assertEqual(payload["url"], f"http://127.0.0.1:{canvas['port']}/#token={TOKEN}")

    def test_another_workspace_or_a_single_canvas_does_not_count(self) -> None:
        self.fake_canvas(workspace="other", name="a")
        self.fake_canvas(workspace=None, mode="single", name="b")
        run = self.node_run(self.cli("canvas"))
        self.assertIn("--workspace", run["argv"])

    def test_a_dead_canvas_file_is_removed_and_a_new_one_starts(self) -> None:
        self.discovery(45678, dead_pid())
        run = self.node_run(self.cli("canvas"))
        self.assertIn("--workspace", run["argv"])
        self.assertFalse((self.state / "canvas" / "45678.json").exists())

    def test_a_reused_pid_that_is_not_a_canvas_is_ignored(self) -> None:
        self.discovery(45679, os.getpid())
        self.node_run(self.cli("canvas"))

    def test_a_held_workspace_lock_is_workspace_in_use(self) -> None:
        canvas = self.fake_canvas(name="a")
        (self.state / "canvas" / f"{canvas['port']}.json").unlink()
        lock = self.state / "canvas" / "workspaces" / "default.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": canvas["process"].pid, "port": None}), encoding="utf-8")
        payload = self.refused(self.cli("canvas"), "workspace_in_use")
        self.assertEqual(payload["pid"], canvas["process"].pid)


class RunningCanvasTests(CanvasCliCase):
    def test_list_shows_live_canvases_without_tokens(self) -> None:
        canvas = self.fake_canvas()
        self.discovery(45680, dead_pid(), workspace="gone")
        payload = self.ok(self.cli("canvas", "list", tools=False))
        self.assertNotIn(TOKEN, json.dumps(payload))
        self.assertEqual(payload["canvases"], [{
            "port": canvas["port"], "pid": canvas["process"].pid,
            "url": f"http://127.0.0.1:{canvas['port']}/", "mode": "workspace", "workspace": "default",
            "tabs": [{"id": "t_aaaaaaaa", "name": "Canvas 1", "devices": ["android~emulator-5580"]}],
        }])

    def test_list_with_nothing_running_is_empty(self) -> None:
        self.assertEqual(self.ok(self.cli("canvas", "list", tools=False))["canvases"], [])

    def test_selection_errors(self) -> None:
        self.refused(self.cli("canvas", "stop", tools=False), "canvas_not_found")
        self.refused(self.cli("canvas", "attach", "--serial", SERIAL), "canvas_not_found")
        self.fake_canvas(name="a")
        self.fake_canvas(workspace="two", name="b")
        payload = self.refused(self.cli("canvas", "detach", "--serial", SERIAL), "canvas_ambiguous")
        self.assertEqual(len(payload["canvases"]), 2)
        self.refused(self.cli("canvas", "stop", "--port", "1", tools=False), "canvas_not_found")
        self.refused(self.cli("canvas", "stop", "--workspace", "nope", tools=False), "canvas_not_found")

    def test_attach_posts_the_target_with_the_bearer_token(self) -> None:
        canvas = self.fake_canvas()
        payload = self.ok(self.cli("canvas", "attach", "--serial", SERIAL))
        self.assertEqual(payload["port"], canvas["port"])
        self.assertEqual(payload["attached"], True)
        self.assertEqual(payload["tab"], "t_aaaaaaaa")
        self.assertEqual(payload["device"]["id"], f"android~{SERIAL}")
        posts = [item for item in self.requests(canvas) if item["method"] == "POST"]
        self.assertEqual(posts[-1]["path"], "/api/devices")
        self.assertEqual(posts[-1]["body"], {"platform": "android", "target": SERIAL})
        self.assertEqual(posts[-1]["authorization"], f"Bearer {TOKEN}")
        self.assertEqual(posts[-1]["host"], f"127.0.0.1:{canvas['port']}")

    def test_api_calls_never_go_through_a_configured_proxy(self) -> None:
        import socket
        import threading

        seen: list[bytes] = []
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(0.2)
        stop = threading.Event()

        def serve() -> None:
            # A proxy that records what reaches it and claims success.
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except OSError:
                    continue
                with conn:
                    conn.settimeout(2)
                    try:
                        seen.append(conn.recv(65536))
                    except OSError:
                        seen.append(b"")
                    body = b'{"ok": true, "attached": false}'
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                 b"Content-Length: " + str(len(body)).encode() +
                                 b"\r\nConnection: close\r\n\r\n" + body)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        def close() -> None:
            stop.set()
            thread.join(timeout=5)
            listener.close()

        self.addCleanup(close)
        proxy = f"http://127.0.0.1:{listener.getsockname()[1]}"
        for key in ("no_proxy", "NO_PROXY"):
            self.env.pop(key, None)
        self.env.update(http_proxy=proxy, HTTP_PROXY=proxy, all_proxy=proxy, ALL_PROXY=proxy)
        canvas = self.fake_canvas()
        payload = self.ok(self.cli("canvas", "attach", "--serial", SERIAL))
        self.assertEqual(payload["attached"], True)
        self.ok(self.cli("canvas", "list", tools=False))
        self.assertEqual(seen, [], "a Canvas API call went to the proxy")
        posts = [item for item in self.requests(canvas) if item["method"] == "POST"]
        self.assertEqual(posts[-1]["path"], "/api/devices")
        self.assertEqual(posts[-1]["authorization"], f"Bearer {TOKEN}")

    def test_attach_by_tab_name_and_bare_target(self) -> None:
        canvas = self.fake_canvas()
        payload = self.ok(self.cli("canvas", "attach", "--workspace", "default",
                                   "--target", UDID, "--tab", "Second"))
        self.assertEqual(payload["tab"], "t_bbbbbbbb")
        posts = [item for item in self.requests(canvas) if item["method"] == "POST"]
        self.assertEqual(posts[-1]["body"], {"platform": "ios", "target": UDID, "tab": "t_bbbbbbbb"})

    def test_api_errors_keep_their_code_and_hint(self) -> None:
        self.fake_canvas()
        payload = self.refused(self.cli("canvas", "attach", "--serial", "busy-target"), "tab_full")
        self.assertEqual(payload["hint"], "Pick another tab")
        self.assertEqual(payload["tab"], "t_aaaaaaaa")
        self.assertEqual(payload["status"], 409)

    def test_api_extras_that_clash_with_the_error_fields_are_safe(self) -> None:
        canvas = self.fake_canvas()
        payload = self.refused(self.cli("canvas", "attach", "--serial", "clash-target"), "workspace_in_use")
        self.assertEqual(payload["port"], canvas["port"])
        self.assertNotIn("message", payload)

    def test_a_canvas_that_does_not_answer_is_backend_failed(self) -> None:
        canvas = self.fake_canvas()
        (self.state / "canvas" / f"{canvas['port']}.json").unlink()
        # A live Canvas pid whose discovery file names a port nobody listens on.
        self.discovery(_closed_port(), canvas["process"].pid)
        payload = self.refused(self.cli("canvas", "attach", "--serial", SERIAL), "backend_failed")
        self.assertNotIn(TOKEN, json.dumps(payload))

    def test_detach_by_target_and_by_id(self) -> None:
        canvas = self.fake_canvas()
        payload = self.ok(self.cli("canvas", "detach", "--serial", SERIAL))
        self.assertEqual(payload, {"ok": True, "port": canvas["port"],
                                   "detached": f"android~{SERIAL}", "session_stopped": True})
        payload = self.ok(self.cli("canvas", "detach", "--port", str(canvas["port"]),
                                   "--device-id", f"ios~{UDID}", tools=False))
        self.assertEqual(payload["detached"], f"ios~{UDID}")
        paths = [item["path"] for item in self.requests(canvas) if item["method"] == "POST"]
        self.assertEqual(paths, [f"/api/devices/android~{SERIAL}/detach", f"/api/devices/ios~{UDID}/detach"])
        self.refused(self.cli("canvas", "detach", tools=False), "usage_error")

    def test_stop_posts_and_waits_for_the_exit(self) -> None:
        canvas = self.fake_canvas()
        payload = self.ok(self.cli("canvas", "stop", tools=False))
        self.assertEqual(payload["stopped"], [{"port": canvas["port"], "pid": canvas["process"].pid,
                                               "workspace": "default"}])
        canvas["process"].wait(timeout=10)
        stops = [item for item in self.requests(canvas) if item["path"] == "/api/stop"]
        self.assertEqual(stops[0]["authorization"], f"Bearer {TOKEN}")

    def test_stop_without_token_sends_no_authorization(self) -> None:
        canvas = self.fake_canvas(token=None)
        self.ok(self.cli("canvas", "stop", "--port", str(canvas["port"]), tools=False))
        stops = [item for item in self.requests(canvas) if item["path"] == "/api/stop"]
        self.assertIsNone(stops[0]["authorization"])

    def test_stop_terminates_a_canvas_that_does_not_exit(self) -> None:
        canvas = self.fake_canvas(ignore_stop=True)
        payload = self.ok(self.cli("canvas", "stop", "--all", tools=False, timeout=90))
        self.assertEqual(payload["stopped"][0]["pid"], canvas["process"].pid)
        self.assertTrue(payload["stopped"][0].get("terminated"))
        self.assertEqual(canvas["process"].wait(timeout=10), -signal.SIGTERM)

    def test_stop_all_with_nothing_running_stops_nothing(self) -> None:
        self.assertEqual(self.ok(self.cli("canvas", "stop", "--all", tools=False))["stopped"], [])


def _closed_port() -> int:
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
