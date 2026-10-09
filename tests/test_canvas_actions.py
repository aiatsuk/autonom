"""Canvas Actions ops of the tools process (contract section 5.1-5.3, md-009, md-011):
captures store, screenshot, recording, install roots, launch, open URL, app
language, tool health and the journal.

Fakes only: `fake_adb.py` / `fake_simctl.py`, plus small wrappers written per
test that keep a fake recorder running until it is signalled. `AUTONOM_HOME`
and `AUTONOM_CAPTURES_DIR` point at temporary folders, so nothing is written to
the real ~/Downloads, ~/.autonom or ~/.local/state/autonom. No device is used.
"""
from __future__ import annotations

import io
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TOOLS = SCRIPTS / "autonom_canvas_tools.py"
FAKES = ROOT / "tests" / "fakes"
sys.path.insert(0, str(SCRIPTS))

import autonom_canvas_tools as tools_mod  # noqa: E402
from autonom_lib import captures, doctor, errors, session, tool_health  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

FAKE_ADB = str(FAKES / "fake_adb.py")
FAKE_SIMCTL = str(FAKES / "fake_simctl.py")
SERIAL = "emulator-5580"
UDID = "00000000-0000-0000-0000-00000000F00D"
APP = "com.example.app"
TOOL_ENV = ("AUTONOM_ADB", "AUTONOM_SIMCTL", "AUTONOM_IDB", "AUTONOM_MITMDUMP",
            "AUTONOM_FAKE_STATE", "AUTONOM_FAKE_LOG", "AUTONOM_CAPTURES_DIR")
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006"
    "00057dd8b7c40000000049454e44ae426082"
)
REMOTE = re.compile(r"/sdcard/autonom-canvas-[0-9a-f]{12}\.mp4")

# A fake adb whose `shell screenrecord` runs until a signal (like the real one),
# and whose `shell pkill -INT -f <file>` signals it; everything else is fake_adb.
RECORDING_ADB = textwrap.dedent(f"""\
    #!{sys.executable}
    import os, signal, sys, time
    sys.path.insert(0, {str(FAKES)!r})
    import fake_adb
    argv = sys.argv[1:]
    args = argv[2:] if argv[:1] == ["-s"] else argv
    pidfile = os.environ["FAKE_REC_PIDFILE"]
    if args[:2] == ["shell", "screenrecord"]:
        fake_adb.record(argv)
        stop = []
        signal.signal(signal.SIGINT, lambda *_: stop.append(1))
        signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
        with open(pidfile, "w") as handle:
            handle.write(str(os.getpid()))
        while not stop:
            time.sleep(0.02)
        sys.exit(0)
    if args[:2] == ["shell", "pkill"]:
        fake_adb.record(argv)
        try:
            os.kill(int(open(pidfile).read()), signal.SIGINT)
        except Exception:
            sys.exit(1)
        sys.exit(0)
    sys.exit(fake_adb.main(argv))
    """)

# RECORDING_ADB, but the `shell` calls named in FAKE_ADB_HANG (comma separated, such
# as "pkill,rm") hang like a wedged device until adb's timeout kills them.
HANGING_ADB = RECORDING_ADB.replace(
    'pidfile = os.environ["FAKE_REC_PIDFILE"]\n',
    'pidfile = os.environ["FAKE_REC_PIDFILE"]\n'
    'if args[:1] == ["shell"] and len(args) > 1 and args[1] in os.environ.get("FAKE_ADB_HANG", "").split(","):\n'
    '    fake_adb.record(argv)\n'
    '    time.sleep(60)\n'
    '    sys.exit(1)\n', 1)
assert HANGING_ADB != RECORDING_ADB

# A fake xcrun whose `simctl io <udid> recordVideo ... <file>` writes the file when signalled.
RECORDING_XCRUN = textwrap.dedent(f"""\
    #!{sys.executable}
    import os, signal, sys, time
    sys.path.insert(0, {str(FAKES)!r})
    import fake_simctl
    argv = sys.argv[1:]
    if argv[:2] == ["simctl", "io"] and "recordVideo" in argv:
        fake_simctl.record(argv)
        stop = []
        signal.signal(signal.SIGINT, lambda *_: stop.append(1))
        with open(os.environ["FAKE_REC_PIDFILE"], "w") as handle:
            handle.write(str(os.getpid()))
        while not stop:
            time.sleep(0.02)
        open(argv[-1], "wb").write(b"\\x00\\x00\\x00\\x18ftypmp42fake-video")
        sys.exit(0)
    sys.exit(fake_simctl.main(argv))
    """)


def android(tool: str = FAKE_ADB) -> Target:
    return Target("android", SERIAL, tool, {"serial": SERIAL})


def ios(tool: str = FAKE_SIMCTL) -> Target:
    return Target("ios", UDID, tool, {"udid": UDID})


class ActionsCase(EnvSandboxMixin, unittest.TestCase):
    """A sandboxed home and captures root, fake tools, and a Tools with options."""

    platform = "android"

    def setUp(self) -> None:
        self.home = self.sandbox_home()
        self.set_env(**{key: None for key in TOOL_ENV})
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state = self.tmp / "state.json"
        self.log = self.tmp / "calls.jsonl"
        self.state.write_text("{}", encoding="utf-8")
        self.captures_root = self.tmp / "captures"
        self.set_env(AUTONOM_FAKE_STATE=str(self.state), AUTONOM_FAKE_LOG=str(self.log),
                     AUTONOM_CAPTURES_DIR=str(self.captures_root),
                     FAKE_REC_PIDFILE=str(self.tmp / "recorder.pid"))
        self.roots = self.tmp / "roots"
        self.roots.mkdir()
        self.tools = self.make_tools()
        self.addCleanup(self.tools.shutdown)
        patcher = mock.patch.object(tools_mod, "RECORD_SETTLE_S", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def target(self, tool: str | None = None) -> Target:
        if self.platform == "ios":
            return ios(tool or FAKE_SIMCTL)
        return android(tool or FAKE_ADB)

    def make_tools(self, tool: str | None = None, *, roots: list[str] | None = None,
                   name: str | None = "Pixel 8") -> tools_mod.Tools:
        return tools_mod.Tools(self.target(tool),
                               install_roots=[str(self.roots)] if roots is None else roots,
                               device_name=name)

    def wrapper(self, text: str, name: str) -> str:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def wait_recorder(self) -> None:
        """The wrapper recorder has installed its signal handlers (its pid file)."""
        pidfile = self.tmp / "recorder.pid"
        deadline = time.time() + 10
        while time.time() < deadline:
            if pidfile.exists() and pidfile.read_text().strip():
                return
            time.sleep(0.02)
        self.fail("the fake recorder did not start")

    def fake_state(self, **values: object) -> None:
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state.update(values)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"]
                for line in self.log.read_text(encoding="utf-8").splitlines()]

    def call(self, op: str, payload: object = None, origin: str = "human") -> dict:
        message = {"id": 3, "op": op, "origin": origin}
        if payload is not None:
            message["payload"] = payload
        return json.loads(tools_mod.encode(tools_mod.respond(self.tools, json.dumps(message))))

    def ok(self, op: str, payload: object = None, origin: str = "human") -> dict:
        response = self.call(op, payload, origin)
        self.assertTrue(response["ok"], response)
        return response["result"]

    def refused(self, op: str, payload: object, code: str) -> dict:
        response = self.call(op, payload)
        self.assertFalse(response["ok"], response)
        self.assertEqual(response["error_code"], code, response)
        return response

    def folder(self) -> Path:
        return self.captures_root / "Pixel 8"

    def visible(self) -> list[str]:
        if not self.folder().is_dir():
            return []
        return sorted(name for name in os.listdir(self.folder()) if not name.startswith("."))

    def hidden(self) -> list[str]:
        if not self.folder().is_dir():
            return []
        return sorted(name for name in os.listdir(self.folder())
                      if name.startswith(".") and name != captures.INDEX)

    def journal(self, record: dict) -> list[dict]:
        path = Path(record["artifacts_dir"]) / "journal.ndjson"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- op lists ---------------------------------------------------------------------------


class OpListTests(ActionsCase):
    def test_the_action_tuples_are_the_contract_and_the_tools_lists_are_unchanged(self) -> None:
        self.assertEqual(tools_mod.ACTION_READ_OPS,
                         ("captures.list", "record.status", "apps.candidates", "health"))
        self.assertEqual(tools_mod.ACTION_DEVICE_OPS,
                         ("capture.screenshot", "record.start", "record.stop", "app.install",
                          "app.launch", "app.open_url", "app.locale"))
        self.assertEqual(tools_mod.ACTION_LOCAL_OPS, ("captures.delete",))
        self.assertEqual(tools_mod.INTERNAL_OPS, ("captures.file",))
        self.assertEqual(tools_mod.OPS, tools_mod.READ_OPS + tools_mod.MUTATING_OPS
                         + tools_mod.ACTION_READ_OPS + tools_mod.ACTION_DEVICE_OPS
                         + tools_mod.ACTION_LOCAL_OPS + tools_mod.INTERNAL_OPS)
        self.assertEqual(len(set(tools_mod.OPS)), len(tools_mod.OPS))
        self.assertEqual(len(tools_mod.READ_OPS), 7)
        self.assertEqual(len(tools_mod.MUTATING_OPS), 14)
        self.assertEqual(set(self.tools.handlers), set(tools_mod.OPS))

    def test_the_new_process_arguments_parse_and_default_to_nothing(self) -> None:
        parsed = tools_mod.parser().parse_args(
            ["--platform", "android", "--target", SERIAL, "--tool", "adb",
             "--install-root", "/a", "--install-root", "/b", "--captures-dir", "/c",
             "--device-name", "Pixel 8"])
        self.assertEqual(parsed.install_root, ["/a", "/b"])
        self.assertEqual(parsed.captures_dir, "/c")
        self.assertEqual(parsed.device_name, "Pixel 8")
        bare = tools_mod.parser().parse_args(["--platform", "ios", "--target", UDID, "--tool", "x"])
        self.assertEqual((bare.install_root, bare.captures_dir, bare.device_name), ([], None, None))

    def test_a_tools_object_built_the_old_way_touches_no_folder(self) -> None:
        tools_mod.Tools(android())
        self.assertFalse(self.captures_root.exists())
        result = self.ok("captures.list")
        self.assertEqual(result["captures"], [])
        self.assertFalse(self.captures_root.exists(), "listing never creates the folder")


# --- captures store --------------------------------------------------------------------


class CapturesStoreTests(ActionsCase):
    def write_png(self, name: str = "shot.png", data: bytes = PNG) -> Path:
        directory = captures.device_dir(self.captures_root, "Pixel 8", SERIAL)
        temp = captures.temp_path(directory, "png")
        temp.write_bytes(data)
        return temp

    def commit(self, created: float | None = None, data: bytes = PNG) -> tuple[dict, list[str]]:
        temp = self.write_png(data=data)
        meta = {"platform": "android", "target_id": SERIAL, "device_name": "Pixel 8"}
        if created is not None:
            meta["created"] = created
        return captures.commit(temp, temp.parent, "screenshot", meta=meta)

    def test_root_prefers_the_flag_then_the_environment_then_downloads(self) -> None:
        self.assertEqual(captures.root("/x/y"), Path("/x/y"))
        self.assertEqual(captures.root(None), self.captures_root)
        self.set_env(AUTONOM_CAPTURES_DIR=None)
        self.assertEqual(captures.root(None), Path.home() / "Downloads" / "Autonom")

    def test_folder_names_are_finder_safe(self) -> None:
        self.assertEqual(captures.folder_name("Pixel 8 (API 35)", SERIAL), "Pixel 8 (API 35)")
        self.assertEqual(captures.folder_name("a/b:c*d", SERIAL), "a_b_c_d")
        self.assertEqual(captures.folder_name("  ../.. ", SERIAL), "emulator-5580")
        self.assertEqual(captures.folder_name(None, SERIAL), "emulator-5580")
        self.assertEqual(captures.folder_name("", "R5:8/M"), "R5_8_M")
        self.assertEqual(len(captures.folder_name("x" * 200, SERIAL)), 60)
        self.assertEqual(captures.folder_name("Пиксель", ""), "device")

    def test_a_commit_names_the_file_by_time_with_a_counter_on_a_clash(self) -> None:
        moment = time.mktime((2026, 10, 7, 9, 5, 3, 0, 0, -1))
        first, pruned = self.commit(created=moment)
        second, _ = self.commit(created=moment)
        self.assertEqual(first["name"], "Pixel 8 2026-10-07 09.05.03.png")
        self.assertEqual(second["name"], "Pixel 8 2026-10-07 09.05.03 (2).png")
        self.assertEqual(pruned, [])
        self.assertEqual(first["kind"], "screenshot")
        self.assertEqual((first["width"], first["height"]), (1, 1))
        self.assertEqual(first["size"], len(PNG))
        self.assertIsNone(first["duration_ms"])
        self.assertTrue(first["created_at"].endswith("Z"))
        self.assertEqual(self.hidden(), [], "no partial file is left behind")
        folder = self.folder()
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        self.assertEqual((folder / captures.INDEX).stat().st_mode & 0o777, 0o600)

    def test_a_malformed_or_oversized_capture_is_refused_and_nothing_is_saved(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            self.commit(data=b"not a png")
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        with mock.patch.object(captures, "SCREENSHOT_MAX", 10):
            with self.assertRaises(errors.AutonomError) as caught:
                self.commit()
        self.assertEqual(caught.exception.code, errors.CAPTURE_TOO_LARGE)
        directory = self.folder()
        empty = captures.temp_path(directory, "mp4")
        empty.write_bytes(b"")
        with self.assertRaises(errors.AutonomError):
            captures.commit(empty, directory, "video", meta={})
        self.assertEqual(self.visible(), [])
        self.assertEqual(self.hidden(), [])

    def test_at_the_limit_only_the_oldest_indexed_capture_goes_and_user_files_stay(self) -> None:
        directory = captures.device_dir(self.captures_root, "Pixel 8", SERIAL)
        user_file = directory / "my notes.png"
        user_file.write_bytes(b"mine")
        names = []
        base = time.time() - 10_000
        for index in range(captures.MAX_ITEMS):
            capture, pruned = self.commit(created=base + index)
            self.assertEqual(pruned, [])
            names.append(capture["name"])
        newest, pruned = self.commit(created=base + 5000)
        self.assertEqual(pruned, [names[0]], "exactly the oldest indexed capture")
        self.assertFalse((directory / names[0]).exists())
        self.assertEqual(user_file.read_bytes(), b"mine")
        listing = captures.list_entries(directory, SERIAL)
        self.assertEqual(listing["count"], captures.MAX_ITEMS)
        self.assertEqual(listing["captures"][0]["name"], newest["name"])
        self.assertEqual(listing["last_pruned"]["count"], 1)
        self.assertNotIn("my notes.png", [entry["name"] for entry in listing["captures"]])

    def test_the_byte_limit_prunes_too(self) -> None:
        first, _ = self.commit(created=1000)
        second, _ = self.commit(created=1001)
        with mock.patch.object(captures, "MAX_BYTES", len(PNG) * 2):
            third, pruned = self.commit(created=1002)
        self.assertEqual(pruned, [first["name"]])
        listing = captures.list_entries(self.folder(), SERIAL)
        self.assertEqual([entry["name"] for entry in listing["captures"]],
                         [third["name"], second["name"]])

    def test_removed_files_leave_the_index_and_only_indexed_files_resolve(self) -> None:
        first, _ = self.commit(created=1000)
        second, _ = self.commit(created=2000)
        directory = self.folder()
        (directory / first["name"]).unlink()
        listing = captures.list_entries(directory, SERIAL)
        self.assertEqual([entry["name"] for entry in listing["captures"]], [second["name"]])
        index = json.loads((directory / captures.INDEX).read_text())
        self.assertEqual([entry["name"] for entry in index["captures"]], [second["name"]])
        (directory / "user.png").write_bytes(PNG)
        outside = self.tmp / "outside.png"
        outside.write_bytes(PNG)
        (directory / "link.png").symlink_to(outside)
        for name in ("user.png", "link.png", "../outside.png", ".autonom-captures.json", "",
                     first["name"], "a/b.png"):
            with self.subTest(name=name), self.assertRaises(errors.AutonomError) as caught:
                captures.resolve(directory, name)
            self.assertEqual(caught.exception.code, errors.CAPTURE_NOT_FOUND)
        # An indexed name replaced by a symlink is refused too.
        (directory / second["name"]).unlink()
        (directory / second["name"]).symlink_to(outside)
        with self.assertRaises(errors.AutonomError):
            captures.resolve(directory, second["name"])

    def test_listing_filters_by_device_and_limit(self) -> None:
        for index in range(3):
            self.commit(created=1000 + index)
        temp = self.write_png()
        captures.commit(temp, temp.parent, "screenshot",
                        meta={"platform": "android", "target_id": "other", "device_name": "Pixel 8"})
        listing = captures.list_entries(self.folder(), SERIAL, limit=2)
        self.assertEqual(listing["count"], 3)
        self.assertEqual(len(listing["captures"]), 2)
        self.assertEqual(listing["limits"], {"max_items": 200, "max_bytes": 2 * 1024**3})
        self.assertEqual(listing["total_bytes"], 3 * len(PNG))

    def test_sweep_removes_only_stale_partial_files(self) -> None:
        directory = captures.device_dir(self.captures_root, "Pixel 8", SERIAL)
        stale = captures.temp_path(directory, "mp4")
        fresh = captures.temp_path(directory, "png")
        other = directory / ".partial-notours.txt"
        for path in (stale, fresh, other):
            path.write_bytes(b"x")
        old = time.time() - 600
        os.utime(stale, (old, old))
        os.utime(other, (old, old))
        self.assertEqual(captures.sweep(directory), 1)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(other.exists())
        self.assertEqual(captures.sweep(self.tmp / "missing"), 0)
        self.assertFalse((self.tmp / "missing").exists())


# --- capture ops -----------------------------------------------------------------------


class ScreenshotOpTests(ActionsCase):
    def test_a_screenshot_lands_in_the_device_folder_and_is_listed_served_and_deleted(self) -> None:
        result = self.ok("capture.screenshot", {})
        capture = result["capture"]
        self.assertEqual(result["pruned"], [])
        self.assertRegex(capture["name"], r"^Pixel 8 \d{4}-\d\d-\d\d \d\d\.\d\d\.\d\d\.png$")
        self.assertEqual((capture["platform"], capture["target_id"], capture["device_name"]),
                         ("android", SERIAL, "Pixel 8"))
        self.assertIn(["-s", SERIAL, "exec-out", "screencap", "-p"], self.calls())
        self.assertEqual(self.visible(), [capture["name"]])

        listing = self.ok("captures.list", {"limit": 5})
        self.assertEqual([entry["name"] for entry in listing["captures"]], [capture["name"]])
        self.assertEqual(listing["dir"], str(self.folder()))
        served = self.ok("captures.file", {"name": capture["name"]})
        self.assertEqual(served, {"name": capture["name"], "path": str(self.folder() / capture["name"]),
                                  "content_type": "image/png", "size": len(PNG)})
        self.assertEqual(self.ok("captures.delete", {"name": capture["name"]}),
                         {"removed": capture["name"]})
        self.assertEqual(self.visible(), [])
        self.refused("captures.file", {"name": capture["name"]}, errors.CAPTURE_NOT_FOUND)
        self.refused("captures.delete", {"name": capture["name"]}, errors.CAPTURE_NOT_FOUND)

    def test_list_and_name_payloads_are_checked(self) -> None:
        for limit in (0, 501, "5", 1.5, True):
            with self.subTest(limit=limit):
                self.refused("captures.list", {"limit": limit}, errors.FLOW_COMMAND_INVALID)
        for payload in ({}, {"name": 3}, {"name": "x" * 300}):
            with self.subTest(payload=payload):
                self.refused("captures.delete", payload, errors.FLOW_COMMAND_INVALID)

    def test_a_failed_screenshot_leaves_no_partial_file(self) -> None:
        self.fake_state(fail={f"-s {SERIAL} exec-out screencap": [1, "error: device offline"]})
        self.refused("capture.screenshot", {}, errors.BACKEND_FAILED)
        self.assertEqual(self.visible(), [])
        self.assertEqual(self.hidden(), [])

    def test_the_captures_dir_option_wins_over_the_environment(self) -> None:
        chosen = self.tmp / "chosen"
        self.tools = tools_mod.Tools(android(), captures_dir=str(chosen), device_name=None)
        capture = self.ok("capture.screenshot", {})["capture"]
        self.assertTrue((chosen / SERIAL / capture["name"]).is_file())
        self.assertFalse(self.captures_root.exists())

    def test_device_ops_are_journaled_with_names_and_no_contents(self) -> None:
        record = session.start_session(FAKE_ADB, serial=SERIAL, app_id=APP)
        self.ok("capture.screenshot", {}, origin="agent")
        self.ok("app.open_url", {"url": "https://user:pw@example.com/x?token=SECRET#frag"})
        self.refused("app.launch", {"app_id": "bad id"}, errors.INVALID_VALUE)
        self.ok("captures.list")
        entries = self.journal(record)
        verbs = [(entry["verb"], entry["origin"], entry["ok"]) for entry in entries]
        self.assertEqual(verbs, [("canvas capture.screenshot", "agent", True),
                                 ("canvas app.open_url", "human", True),
                                 ("canvas app.launch", "human", False)])
        text = json.dumps(entries)
        for secret in ("SECRET", "pw@", "/x", "frag"):
            self.assertNotIn(secret, json.dumps(entries[1]))
        self.assertEqual(entries[1]["argv"], ["canvas", "app.open_url", "https://example.com"])
        self.assertNotIn("SECRET", text)
        self.assertEqual(entries[2]["error_code"], errors.INVALID_VALUE)


class IosScreenshotTests(ActionsCase):
    platform = "ios"

    def test_ios_screenshots_come_from_simctl(self) -> None:
        capture = self.ok("capture.screenshot", {})["capture"]
        self.assertEqual(capture["platform"], "ios")
        self.assertTrue(any(argv[:3] == ["simctl", "io", UDID] and "screenshot" in argv
                            for argv in self.calls()))


# --- recording -------------------------------------------------------------------------


class AndroidRecordingTests(ActionsCase):
    def recording_tools(self) -> None:
        self.tools.shutdown()
        self.tools = self.make_tools(self.wrapper(RECORDING_ADB, "adb"))
        self.addCleanup(self.tools.shutdown)

    def test_start_status_stop_saves_the_video_and_removes_the_device_file(self) -> None:
        self.recording_tools()
        started = self.ok("record.start", {})
        self.assertEqual(started["recording"]["limit_s"], 180)
        self.wait_recorder()
        self.refused("record.start", {}, errors.RECORDING_ALREADY_ACTIVE)
        status = self.ok("record.status")
        self.assertTrue(status["recording"])
        self.assertEqual(status["started_at"], started["recording"]["started_at"])
        self.assertGreaterEqual(status["elapsed_ms"], 0)
        time.sleep(0.1)
        stopped = self.ok("record.stop", {})
        capture = stopped["capture"]
        self.assertEqual(capture["kind"], "video")
        self.assertTrue(capture["name"].endswith(".mp4"))
        self.assertFalse(stopped["ended_early"])
        self.assertGreaterEqual(stopped["duration_ms"], 100)
        self.assertEqual(capture["duration_ms"], stopped["duration_ms"])
        calls = self.calls()
        record = next(argv for argv in calls if "screenrecord" in argv)
        remote = record[-1]
        self.assertRegex(remote, REMOTE)
        self.assertEqual(record, ["-s", SERIAL, "shell", "screenrecord", "--time-limit", "180", remote])
        self.assertIn(["-s", SERIAL, "shell", "pkill", "-INT", "-f", remote], calls)
        pull = next(argv for argv in calls if argv[2:3] == ["pull"])
        self.assertEqual(pull[:4], ["-s", SERIAL, "pull", remote])
        self.assertEqual(Path(pull[4]).parent, self.folder())
        self.assertRegex(Path(pull[4]).name, r"^\.partial-[0-9a-f]{12}\.mp4$")
        self.assertIn(["-s", SERIAL, "shell", "rm", "-f", remote], calls)
        self.assertNotIn("/sdcard/autonom-recording.mp4", json.dumps(calls))
        self.assertEqual(self.visible(), [capture["name"]])
        self.assertEqual(self.hidden(), [])
        self.refused("record.stop", {}, errors.RECORDING_NOT_ACTIVE)
        status = self.ok("record.status")
        self.assertFalse(status["recording"])
        self.assertEqual(status["last"]["capture"]["name"], capture["name"])

    def test_a_recording_that_reached_its_limit_is_saved_as_ended_early(self) -> None:
        # The plain fake adb's screenrecord returns at once: the limit was reached.
        self.ok("record.start", {})
        time.sleep(0.3)
        status = self.ok("record.status")
        self.assertFalse(status["recording"])
        self.assertTrue(status["last"]["ended_early"])
        self.assertEqual(status["last"]["capture"]["kind"], "video")
        remote = next(argv for argv in self.calls() if "screenrecord" in argv)[-1]
        self.assertIn(["-s", SERIAL, "shell", "rm", "-f", remote], self.calls())
        self.refused("record.stop", {}, errors.RECORDING_NOT_ACTIVE)
        self.ok("record.start", {})

    def test_a_failed_pull_still_removes_the_device_file(self) -> None:
        self.recording_tools()
        self.ok("record.start", {})
        self.wait_recorder()
        self.fake_state(fail={f"-s {SERIAL} pull": [1, "adb: error: failed to stat remote object"]})
        self.refused("record.stop", {}, errors.BACKEND_FAILED)
        remote = next(argv for argv in self.calls() if "screenrecord" in argv)[-1]
        self.assertIn(["-s", SERIAL, "shell", "rm", "-f", remote], self.calls())
        self.assertEqual(self.visible(), [])
        self.assertEqual(self.hidden(), [])

    def test_shutdown_stops_the_recorder_and_saves_nothing(self) -> None:
        self.recording_tools()
        self.ok("record.start", {})
        self.wait_recorder()
        self.tools.shutdown()
        remote = next(argv for argv in self.calls() if "screenrecord" in argv)[-1]
        self.assertIn(["-s", SERIAL, "shell", "rm", "-f", remote], self.calls())
        self.assertEqual(self.visible(), [])
        self.assertEqual(self.hidden(), [])


class AndroidRecordingFailureTests(ActionsCase):
    """A slow or wedged device during stop: the recorder and the device file never leak."""

    def hanging_tools(self, hang: str) -> None:
        self.set_env(FAKE_ADB_HANG=hang)
        self.tools.shutdown()
        self.tools = self.make_tools(self.wrapper(HANGING_ADB, "adb"))
        self.addCleanup(self.tools.shutdown)

    def recorder_gone(self) -> bool:
        pid = int((self.tmp / "recorder.pid").read_text())
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False

    def test_a_hung_device_pkill_falls_back_to_sigint_and_still_removes_the_device_file(self) -> None:
        self.hanging_tools("pkill")
        with mock.patch.object(tools_mod, "RECORD_STOP_WAIT_S", 0.5):
            self.ok("record.start", {})
            self.wait_recorder()
            stopped = self.ok("record.stop", {})
        calls = self.calls()
        remote = next(argv for argv in calls if "screenrecord" in argv)[-1]
        self.assertIn(["-s", SERIAL, "shell", "pkill", "-INT", "-f", remote], calls)
        self.assertIn(["-s", SERIAL, "shell", "rm", "-f", remote], calls)
        self.assertTrue(self.recorder_gone(), "the local recorder was left running")
        self.assertEqual(self.visible(), [stopped["capture"]["name"]])
        self.assertEqual(self.hidden(), [])
        self.assertFalse(self.ok("record.status")["recording"])

    def test_a_stop_that_raises_drops_the_recording_and_cleans_up(self) -> None:
        self.hanging_tools("")
        self.ok("record.start", {})
        self.wait_recorder()
        with mock.patch.object(self.tools, "_stop_recorder", side_effect=RuntimeError("wedged")):
            response = self.call("record.stop", {})
        self.assertFalse(response["ok"], response)
        remote = next(argv for argv in self.calls() if "screenrecord" in argv)[-1]
        self.assertIn(["-s", SERIAL, "shell", "rm", "-f", remote], self.calls())
        self.assertTrue(self.recorder_gone(), "the local recorder was left running")
        self.assertEqual(self.hidden(), [])
        self.assertFalse(self.ok("record.status")["recording"])
        self.ok("record.start", {})

    def test_shutdown_on_a_wedged_device_fits_inside_the_close_grace(self) -> None:
        self.hanging_tools("pkill,rm")
        self.ok("record.start", {})
        self.wait_recorder()
        began = time.monotonic()
        self.tools.shutdown()
        took = time.monotonic() - began
        # Node gives the process TOOLS_CLOSE_GRACE_MS (6 s) before it is killed.
        self.assertLess(took, tools_mod.SHUTDOWN_BUDGET_S + 1.0)
        self.assertLess(tools_mod.SHUTDOWN_BUDGET_S + 1.0, 6.0)
        remote = next(argv for argv in self.calls() if "screenrecord" in argv)[-1]
        self.assertIn(["-s", SERIAL, "shell", "rm", "-f", remote], self.calls())
        self.assertTrue(self.recorder_gone(), "the local recorder was left running")
        self.assertEqual(self.hidden(), [])


class IosRecordingTests(ActionsCase):
    platform = "ios"

    def setUp(self) -> None:
        super().setUp()
        self.tools.shutdown()
        self.tools = self.make_tools(self.wrapper(RECORDING_XCRUN, "xcrun"))
        self.addCleanup(self.tools.shutdown)

    def test_simctl_records_until_stopped(self) -> None:
        self.ok("record.start", {})
        self.wait_recorder()
        stopped = self.ok("record.stop", {})
        self.assertFalse(stopped["ended_early"])
        argv = next(argv for argv in self.calls() if "recordVideo" in argv)
        self.assertEqual(argv[:8], ["simctl", "io", UDID, "recordVideo", "--codec", "h264", "--force",
                                    argv[7]])
        self.assertRegex(Path(argv[7]).name, r"^\.partial-[0-9a-f]{12}\.mp4$")
        self.assertEqual(self.visible(), [stopped["capture"]["name"]])

    def test_the_limit_timer_stops_simctl_and_marks_the_video_ended_early(self) -> None:
        with mock.patch.object(tools_mod, "RECORD_LIMIT_S", 1.5):
            self.ok("record.start", {})
            self.wait_recorder()
            deadline = time.time() + 10
            while time.time() < deadline:
                status = self.ok("record.status")
                if not status["recording"]:
                    break
                time.sleep(0.05)
        self.assertFalse(status["recording"])
        self.assertTrue(status["last"]["ended_early"])
        self.assertEqual(self.visible(), [status["last"]["capture"]["name"]])


class ProcessCleanupTests(ActionsCase):
    """The real tools process: SIGTERM or stdin EOF drops a running recording."""

    def run_tools(self) -> subprocess.Popen:
        adb = self.wrapper(RECORDING_ADB, "adb")
        process = subprocess.Popen(
            [sys.executable, str(TOOLS), "--platform", "android", "--target", SERIAL,
             "--tool", adb, "--device-name", "Pixel 8"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process

    def start_recording(self, process: subprocess.Popen) -> dict:
        process.stdin.write(json.dumps({"id": 1, "op": "record.start", "payload": {},
                                        "origin": "human"}).encode() + b"\n")
        process.stdin.flush()
        reply = json.loads(process.stdout.readline())
        self.assertTrue(reply["ok"], reply)
        self.wait_recorder()
        return reply

    def assert_cleaned(self) -> None:
        calls = self.calls()
        remote = next(argv for argv in calls if "screenrecord" in argv)[-1]
        self.assertIn(["-s", SERIAL, "shell", "rm", "-f", remote], calls)
        self.assertEqual(self.visible(), [])
        self.assertEqual(self.hidden(), [])
        pid = int((self.tmp / "recorder.pid").read_text())
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail("the recorder outlived the tools process")

    def test_sigterm_removes_the_device_file_and_saves_nothing(self) -> None:
        process = self.run_tools()
        self.start_recording(process)
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=20)
        process.stdout.close()
        process.stdin.close()
        process.stderr.close()
        self.assert_cleaned()

    def test_stdin_eof_removes_the_device_file_and_saves_nothing(self) -> None:
        process = self.run_tools()
        self.start_recording(process)
        process.stdin.close()
        self.assertEqual(process.wait(timeout=20), 0)
        process.stdout.close()
        process.stderr.close()
        self.assert_cleaned()


# --- install -----------------------------------------------------------------------------


class InstallTests(ActionsCase):
    def apk(self, relative: str = "app/build/app-debug.apk", data: bytes = b"PK-apk") -> Path:
        path = self.roots / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_without_roots_install_is_off_and_candidates_say_so(self) -> None:
        self.tools = self.make_tools(roots=[])
        response = self.refused("app.install", {"path": str(self.apk())}, errors.INSTALL_NOT_CONFIGURED)
        self.assertIn("--install-root", response["hint"])
        candidates = self.ok("apps.candidates")
        self.assertEqual((candidates["configured"], candidates["candidates"]), (False, []))
        self.assertFalse(any("install" in argv for argv in self.calls()))

    def test_an_apk_inside_a_root_installs(self) -> None:
        apk = self.apk()
        result = self.ok("app.install", {"path": str(apk)})
        self.assertEqual(result["installed"], os.path.realpath(apk))
        self.assertIsNone(result["app_id"])
        self.assertEqual(result["size"], len(b"PK-apk"))
        self.assertGreaterEqual(result["duration_ms"], 0)
        self.assertIn(["-s", SERIAL, "install", "-r", os.path.realpath(apk)], self.calls())
        self.ok("app.install", {"path": str(apk), "allow_downgrade": True})
        self.assertIn(["-s", SERIAL, "install", "-r", "-d", os.path.realpath(apk)], self.calls())

    def test_paths_outside_the_roots_are_refused_before_any_install(self) -> None:
        outside = self.tmp / "elsewhere" / "x.apk"
        outside.parent.mkdir()
        outside.write_bytes(b"PK")
        link = self.roots / "link.apk"
        link.symlink_to(outside)
        dir_link = self.roots / "dirlink"
        dir_link.symlink_to(outside.parent)
        sibling = self.tmp / "roots-evil" / "x.apk"
        sibling.parent.mkdir()
        sibling.write_bytes(b"PK")
        cases = [
            str(self.roots / ".." / "elsewhere" / "x.apk"),
            str(link),
            str(dir_link / "x.apk"),
            str(sibling),
            "relative/x.apk",
            str(self.roots / "a\nb.apk"),
            "/" + "a" * 5000,
        ]
        for path in cases:
            with self.subTest(path=path[:80]):
                response = self.refused("app.install", {"path": path}, errors.INSTALL_PATH_NOT_ALLOWED)
                self.assertNotIn("PK", json.dumps(response))
        self.assertFalse(any("install" in argv for argv in self.calls()))

    def test_missing_wrong_kind_and_bad_payloads(self) -> None:
        self.refused("app.install", {"path": str(self.roots / "nope.apk")}, errors.INSTALL_PATH_NOT_FOUND)
        text = self.roots / "notes.txt"
        text.write_text("x")
        self.refused("app.install", {"path": str(text)}, errors.INVALID_VALUE)
        self.refused("app.install", {"path": str(self.roots)}, errors.INVALID_VALUE)
        for payload in ({}, {"path": 3}, {"path": ""}):
            with self.subTest(payload=payload):
                self.refused("app.install", payload, errors.FLOW_COMMAND_INVALID)
        self.refused("app.install", {"path": str(self.apk()), "allow_downgrade": "yes"},
                     errors.FLOW_COMMAND_INVALID)

    def test_a_refused_install_names_its_reason(self) -> None:
        apk = self.apk()
        self.fake_state(fail={f"-s {SERIAL} install": [1, "Performing Streamed Install\n"
                                                        "adb: failed to install x.apk: Failure "
                                                        "[INSTALL_FAILED_VERSION_DOWNGRADE]"]})
        response = self.refused("app.install", {"path": str(apk)}, errors.INSTALL_FAILED)
        self.assertEqual(response["reason"], "INSTALL_FAILED_VERSION_DOWNGRADE")

    def test_candidates_list_the_newest_builds_and_skip_hidden_and_vendor_folders(self) -> None:
        old = self.apk("a/old.apk")
        new = self.apk("b/new.apk")
        os.utime(old, (1000, 1000))
        self.apk(".hidden/x.apk")
        self.apk("node_modules/y.apk")
        self.apk("Pods/z.apk")
        self.apk("1/2/3/4/5/6/7/8/deep.apk")
        (self.roots / "linked.apk").symlink_to(new)
        result = self.ok("apps.candidates")
        self.assertTrue(result["configured"])
        self.assertEqual(result["roots"], [str(self.roots)])
        self.assertEqual([entry["path"] for entry in result["candidates"]],
                         [os.path.realpath(new), os.path.realpath(old)])
        self.assertEqual(result["candidates"][0]["kind"], "apk")
        self.assertFalse(result["truncated"])
        with mock.patch.object(tools_mod, "CANDIDATE_MAX_ENTRIES", 2):
            self.assertTrue(self.ok("apps.candidates")["truncated"])


class IosInstallTests(ActionsCase):
    platform = "ios"

    def app(self, bundle: str = "com.example.app") -> Path:
        path = self.roots / "DerivedData" / "Runner.app"
        path.mkdir(parents=True)
        with open(path / "Info.plist", "wb") as handle:
            plistlib.dump({"CFBundleIdentifier": bundle}, handle)
        (path / "Runner").write_bytes(b"x" * 10)
        return path

    def test_an_app_bundle_installs_with_its_bundle_id(self) -> None:
        app = self.app()
        result = self.ok("app.install", {"path": str(app)})
        self.assertEqual(result["app_id"], "com.example.app")
        self.assertIn(["simctl", "install", UDID, os.path.realpath(app)], self.calls())
        candidates = self.ok("apps.candidates")["candidates"]
        self.assertEqual([(entry["path"], entry["kind"]) for entry in candidates],
                         [(os.path.realpath(app), "app")])

    def test_a_folder_without_info_plist_is_not_an_app(self) -> None:
        bare = self.roots / "Bare.app"
        bare.mkdir()
        self.refused("app.install", {"path": str(bare)}, errors.INVALID_VALUE)

    def test_a_simctl_failure_is_install_failed(self) -> None:
        app = self.app()
        self.fake_state(simctl_fail={"simctl install": [1, "An error was encountered processing the command"]})
        self.refused("app.install", {"path": str(app)}, errors.INSTALL_FAILED)


# --- launch, open URL, language -------------------------------------------------------


class AppOpsTests(ActionsCase):
    def test_launch_resumes_or_starts_fresh(self) -> None:
        result = self.ok("app.launch", {"app_id": APP})
        self.assertEqual((result["launched"], result["mode"]), (APP, "resume"))
        fresh = self.ok("app.launch", {"app_id": APP, "fresh": True})
        self.assertEqual(fresh["mode"], "fresh")
        starts = [argv for argv in self.calls() if argv[2:4] == ["shell", "am"]]
        self.assertTrue(any("0x10008000" in argv for argv in starts))
        for value in ("", "app", "com..x", "com.example app", "com.ex;rm", 3, None, "a." + "b" * 300):
            with self.subTest(app_id=value):
                self.refused("app.launch", {"app_id": value}, errors.INVALID_VALUE)

    def test_open_url_uses_the_cli_path_and_checks_the_url(self) -> None:
        result = self.ok("app.open_url", {"url": "myapp://profile/42"})
        self.assertTrue(result["opened"])
        self.assertIn("handled_by", result)
        self.assertTrue(any("-W" in argv and "android.intent.action.VIEW" in argv for argv in self.calls()))
        for value in ("", "no scheme", "https://a b", "x" * 2049, "https://x\n", 7, "/path"):
            with self.subTest(url=value if isinstance(value, int) else value[:20]):
                self.refused("app.open_url", {"url": value}, errors.INVALID_VALUE)

    def test_url_origin_keeps_only_scheme_and_host(self) -> None:
        self.assertEqual(tools_mod.url_origin("https://u:p@Example.com:8443/a?b=c#d"),
                         "https://example.com")
        self.assertEqual(tools_mod.url_origin("myapp://profile/42?token=x"), "myapp://profile")
        self.assertEqual(tools_mod.url_origin("tel:+15550100"), "tel:")

    def test_app_language_on_android_13_and_refused_before(self) -> None:
        self.fake_state(getprop={"ro.build.version.sdk": "34"})
        result = self.ok("app.locale", {"app_id": APP, "locale": "de-DE"})
        self.assertEqual(result, {"app_id": APP, "locale": "de-DE", "restarted": False})
        self.assertIn(["-s", SERIAL, "shell", "cmd", "locale", "set-app-locales", APP, "--locales", "de-DE"],
                      self.calls())
        self.fake_state(getprop={"ro.build.version.sdk": "30"})
        response = self.refused("app.locale", {"app_id": APP, "locale": "de-DE"},
                                errors.UNSUPPORTED_CAPABILITY)
        self.assertIn("33", response["error"])
        for value in ("de_DE", "DE", "de-de", "english", "zh-Hant-TW-x", 5, "d" * 21):
            with self.subTest(locale=value):
                self.refused("app.locale", {"app_id": APP, "locale": value}, errors.INVALID_VALUE)
        for value in ("zh-Hant-TW", "es-419", "fil", "pt-BR"):
            with self.subTest(locale=value):
                self.fake_state(getprop={"ro.build.version.sdk": "35"})
                self.ok("app.locale", {"app_id": APP, "locale": value})


class IosAppOpsTests(ActionsCase):
    platform = "ios"

    def test_language_relaunches_with_apple_arguments(self) -> None:
        result = self.ok("app.locale", {"app_id": APP, "locale": "de-DE"})
        self.assertTrue(result["restarted"])
        calls = self.calls()
        self.assertIn(["simctl", "terminate", UDID, APP], calls)
        self.assertIn(["simctl", "launch", UDID, APP, "-AppleLanguages", "(de)", "-AppleLocale", "de_DE"],
                      calls)
        self.ok("app.locale", {"app_id": APP, "locale": "zh-Hant-TW"})
        self.assertIn(["simctl", "launch", UDID, APP, "-AppleLanguages", "(zh-Hant)",
                       "-AppleLocale", "zh_Hant_TW"], self.calls())

    def test_launch_and_open_url(self) -> None:
        result = self.ok("app.launch", {"app_id": APP, "fresh": True})
        self.assertEqual((result["pid"], result["mode"]), (4242, "fresh"))
        opened = self.ok("app.open_url", {"url": "https://example.com/x"})
        self.assertEqual(opened["handled_by"], "unknown")
        self.assertIn(["simctl", "openurl", UDID, "https://example.com/x"], self.calls())


# --- tool health -------------------------------------------------------------------------


class ToolHealthTests(ActionsCase):
    def setUp(self) -> None:
        super().setUp()
        tool_health.reset_cache()
        self.addCleanup(tool_health.reset_cache)

    def test_every_tool_is_reported_with_the_tool_health_shape(self) -> None:
        self.set_env(AUTONOM_ADB=FAKE_ADB, AUTONOM_SIMCTL=FAKE_SIMCTL)
        result = self.ok("health", {"refresh": True})
        self.assertTrue(result["checked_at"].endswith("Z"))
        self.assertEqual([entry["name"] for entry in result["tools"]], list(tool_health.TOOL_NAMES))
        for entry in result["tools"]:
            self.assertEqual(set(entry), {"name", "found", "path", "version", "ok", "needed_for",
                                          "error", "hint"})
            if entry["ok"]:
                self.assertTrue(entry["found"])
        adb = next(entry for entry in result["tools"] if entry["name"] == "adb")
        self.assertEqual((adb["found"], adb["ok"], adb["path"]), (True, True, FAKE_ADB))
        self.assertIn("Android Debug Bridge", adb["version"])

    def test_results_are_cached_for_a_minute_unless_refreshed(self) -> None:
        counter = {"n": 0}

        def probe() -> dict:
            counter["n"] += 1
            return tool_health._entry("adb", found=True, path="/x/adb", version="1", ok=True)  # noqa: SLF001

        with mock.patch.dict(tool_health.PROBES, {name: probe for name in tool_health.TOOL_NAMES}):
            first = tool_health.check()
            second = tool_health.check()
            self.assertIs(first, second)
            self.assertEqual(counter["n"], 7)
            tool_health.check(refresh=True)
            self.assertEqual(counter["n"], 14)

    def test_ios_tools_are_macos_only_and_a_failing_probe_is_contained(self) -> None:
        def boom() -> dict:
            raise RuntimeError("probe exploded")

        with mock.patch.object(tool_health, "_macos", return_value=False), \
                mock.patch.dict(tool_health.PROBES, {"ffmpeg": boom}):
            tools = {entry["name"]: entry for entry in tool_health.check(refresh=True)["tools"]}
        for name in ("xcrun_simctl", "idb_companion"):
            self.assertEqual((tools[name]["found"], tools[name]["ok"], tools[name]["error"]),
                             (False, False, "macOS only"))
        self.assertFalse(tools["ffmpeg"]["ok"])
        self.assertIn("probe exploded", tools["ffmpeg"]["error"])

    def test_scrcpy_server_is_ok_only_at_the_protocol_version(self) -> None:
        server = self.tmp / f"scrcpy-server-v{doctor.SCRCPY_PROTOCOL_VERSION}"
        server.write_bytes(b"jar")
        self.set_env(AUTONOM_SCRCPY_SERVER=str(server), SCRCPY_SERVER_PATH=None)
        entry = tool_health._scrcpy_server()  # noqa: SLF001
        self.assertEqual((entry["found"], entry["ok"], entry["version"]),
                         (True, True, doctor.SCRCPY_PROTOCOL_VERSION))
        other = self.tmp / "scrcpy-server-v3.3"
        other.write_bytes(b"jar")
        self.set_env(AUTONOM_SCRCPY_SERVER=str(other))
        entry = tool_health._scrcpy_server()  # noqa: SLF001
        self.assertEqual((entry["found"], entry["ok"]), (True, False))
        self.assertTrue(entry["error"])
        self.set_env(AUTONOM_SCRCPY_SERVER=str(self.tmp / "missing"))
        entry = tool_health._scrcpy_server()  # noqa: SLF001
        self.assertEqual((entry["found"], entry["ok"]), (False, False))

    def test_the_module_prints_one_json_line(self) -> None:
        self.set_env(AUTONOM_ADB=FAKE_ADB)
        completed = subprocess.run(
            [sys.executable, "-m", "autonom_lib.tool_health"], cwd=str(SCRIPTS),
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, stdin=subprocess.DEVNULL)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(set(payload), {"checked_at", "tools"})
        self.assertEqual(len(payload["tools"]), 7)


if __name__ == "__main__":
    unittest.main()
