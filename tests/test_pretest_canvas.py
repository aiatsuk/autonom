"""Canvas in a pre-test (tp-017, tp-006): captures as session evidence and
human input that the guard never counts.

Fakes only: `fake_adb.py` (and a small idb script for the iOS volume buttons)
record every argv they receive, so a refusal is proven to have sent nothing.
`AUTONOM_HOME` and `AUTONOM_CAPTURES_DIR` point at temporary folders: nothing
is written to the real ~/Downloads, ~/.autonom or ~/.local/state/autonom.
"""
from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FAKES = ROOT / "tests" / "fakes"
sys.path.insert(0, str(SCRIPTS))

import autonom_canvas_bridge as bridge  # noqa: E402
import autonom_canvas_tools as tools_mod  # noqa: E402
from autonom_lib import captures, device_state, errors  # noqa: E402
from autonom_lib import screenshot as shot_mod  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402
from autonom_lib.metrics import snapshot as snapshot_mod  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402
from autonom_lib.pretest import checks, guard, store  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

FAKE_ADB = str(FAKES / "fake_adb.py")
SERIAL = "emulator-5590"
UDID = "AAAAAAAA-1111-2222-3333-CCCCCCCCCCCC"
APP = "com.example.app"
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006"
    "00057dd8b7c40000000049454e44ae426082"
)
TOOL_ENV = ("AUTONOM_ADB", "AUTONOM_SIMCTL", "AUTONOM_IDB", "AUTONOM_IDB_COMPANION",
            "AUTONOM_MITMDUMP", "AUTONOM_FAKE_STATE", "AUTONOM_FAKE_LOG",
            "AUTONOM_CAPTURES_DIR")
CAPTURE_KEYS = {"name", "kind", "platform", "target_id", "device_name", "size",
                "created_at", "duration_ms", "width", "height"}

UI_DUMP = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
    <hierarchy rotation="0">
      <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.example.app" content-desc="" clickable="false" enabled="true" focusable="false" scrollable="false" bounds="[0,0][1080,1920]">
        <node index="0" text="" resource-id="com.example.app:id/buy" class="android.view.View" package="com.example.app" content-desc="Купить" clickable="true" enabled="true" focusable="true" scrollable="false" bounds="[100,300][500,420]" />
        <node index="1" text="Continue" resource-id="com.example.app:id/next" class="android.widget.Button" package="com.example.app" content-desc="" clickable="true" enabled="true" focusable="true" scrollable="false" bounds="[100,600][500,720]" />
      </node>
    </hierarchy>
    """)


def itxt(key: str, value: str) -> bytes:
    """The payload of the `iTXt` chunk `screenshot.embed_metadata` writes. (The
    fake device's 1x1 PNG has a short IDAT length, so `read_metadata`'s chunk
    walk cannot be used on it.)"""
    return (shot_mod.METADATA_PREFIX + key).encode() + b"\x00\x00\x00\x00\x00" + value.encode()


class CanvasPretestCase(EnvSandboxMixin, unittest.TestCase):
    """A sandboxed home and captures root, a live session on the fake device."""

    platform = "android"

    def setUp(self) -> None:
        self.home = self.sandbox_home()
        self.set_env(**{key: None for key in TOOL_ENV})
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state = self.tmp / "state.json"
        self.log = self.tmp / "calls.jsonl"
        self.dump = self.tmp / "dump.xml"
        self.dump.write_text(UI_DUMP, encoding="utf-8")
        self.state.write_text(json.dumps({"devices": [[SERIAL, "device", ""]],
                                          "ui_dump": str(self.dump)}), encoding="utf-8")
        self.captures_root = self.tmp / "captures"
        self.set_env(AUTONOM_FAKE_STATE=str(self.state), AUTONOM_FAKE_LOG=str(self.log),
                     AUTONOM_CAPTURES_DIR=str(self.captures_root))
        if self.platform == "ios":
            self.target = Target("ios", UDID, "/nonexistent/xcrun", {"udid": UDID})
        else:
            self.target = Target("android", SERIAL, FAKE_ADB, {"serial": SERIAL})
        self.addCleanup(session_mod.select, None)
        self.session: dict | None = None
        self.tools = tools_mod.Tools(self.target, device_name="Pixel 8")
        self.addCleanup(self.tools.shutdown)

    # --- set-up helpers -------------------------------------------------------------

    def start_session(self) -> dict:
        home = session_mod.sessions_home()
        artifacts = home / "s_canvas"
        artifacts.mkdir(parents=True, mode=0o700)
        self.session = {"session_id": "s_canvas", "schema_version": 2,
                        "artifacts_dir": str(artifacts), "platform": self.platform,
                        "target_id": self.target.target_id, "app_id": APP,
                        "network": {"enabled": False}}
        text = json.dumps(self.session)
        (artifacts / "session.json").write_text(text, encoding="utf-8")
        (home / "current.json").write_text(text, encoding="utf-8")
        return self.session

    def start_pretest(self, *, open_check: bool = True, **budget) -> dict:
        session = self.session or self.start_session()
        target = SimpleNamespace(platform=self.platform, target_id=self.target.target_id)
        with mock.patch.object(device_state, "crash_list", return_value=[]), \
                mock.patch.object(snapshot_mod, "take",
                                  side_effect=errors.AutonomError("x", "no metrics")):
            record = store.create(session, target=target, ticket_text="t",
                                  ticket_ref=None, title=None, repo=None, base=None,
                                  head=None, flows_dir=None, app_id=None,
                                  budget=budget or None,
                                  context={"schema": "autonom.pretest-context/v1"})
        if open_check:
            check = checks.add(session, record, title="Buy flow", kind="ticket")
            checks.begin(session, store.load(session, record["pretest_id"]), check["check_id"])
        self.clear_log()
        return record

    # --- observation helpers --------------------------------------------------------

    def clear_log(self) -> None:
        self.log.write_text("", encoding="utf-8")

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"] for line in
                self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def inputs(self) -> list[list[str]]:
        return [argv for argv in self.calls() if "dumpsys" not in argv
                and ("input" in argv or ("am" in argv and "start" in argv))]

    def used(self, record: dict) -> int:
        return store.read_budget(self.session, record["pretest_id"])["actions_used"]

    def refusals(self, record: dict) -> list[dict]:
        return store.read_budget(self.session, record["pretest_id"])["refusals"]

    def journal(self) -> list[dict]:
        path = Path(self.session["artifacts_dir"]) / "journal.ndjson"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def shots(self) -> list[dict]:
        return shot_mod.load_index(self.session)

    def folder(self) -> Path:
        return self.captures_root / "Pixel 8"

    def downloads(self) -> list[str]:
        if not self.folder().is_dir():
            return []
        return sorted(name for name in os.listdir(self.folder()) if not name.startswith("."))

    def downloads_index(self) -> dict:
        return json.loads((self.folder() / captures.INDEX).read_text(encoding="utf-8"))

    def tool(self, op: str, payload: object = None, origin: str = "human") -> dict:
        message = {"id": 4, "op": op, "origin": origin, "payload": payload or {}}
        return json.loads(tools_mod.encode(tools_mod.respond(self.tools, json.dumps(message))))

    def bridge(self, op: str, payload: dict, origin: str) -> dict:
        return bridge.dispatch(self.target, {"id": 1, "op": op, "origin": origin,
                                             "payload": payload})

    def assert_refusal(self, code: str, op: str, payload: dict, origin: str = "agent") -> dict:
        with self.assertRaises(errors.AutonomError) as caught:
            self.bridge(op, payload, origin)
        error = caught.exception.as_dict()
        self.assertFalse(error["ok"])
        self.assertEqual(error["error_code"], code, error)
        self.assertTrue(error.get("hint"), f"{code} has no hint")
        return error


# --- tp-017: Canvas screenshots as session evidence ---------------------------------


class CaptureInSessionTests(CanvasPretestCase):
    def test_capture_with_an_open_check_lands_in_downloads_and_in_shots(self) -> None:
        record = self.start_pretest()
        response = self.tool("capture.screenshot")
        self.assertTrue(response["ok"], response)
        result = response["result"]

        # The Downloads copy and its index are exactly what they were before.
        capture = result["capture"]
        self.assertEqual(set(capture), CAPTURE_KEYS)
        self.assertEqual(self.downloads(), [capture["name"]])
        self.assertEqual((self.folder() / capture["name"]).read_bytes(), PNG)
        self.assertEqual([set(entry) for entry in self.downloads_index()["captures"]],
                         [CAPTURE_KEYS])

        # The session copy: under the check's folder, 0600 in 0700 folders.
        shot = result["shot"]
        self.assertEqual(shot["task"], "chk-1")
        self.assertEqual(shot["mocks_active"], 0)
        self.assertTrue(shot["metadata_embedded"])
        self.assertNotIn("warnings", result)
        path = Path(shot["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, Path(self.session["artifacts_dir"]) / "shots" / "chk-1")
        self.assertRegex(path.name, r"^0001_\d{6}_canvas\.png$")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for folder in (path.parent, path.parent.parent):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700, folder)
        self.assertTrue(path.read_bytes().startswith(PNG[:-12]))

        (row,) = self.shots()
        self.assertEqual(row["file"], "shots/chk-1/" + path.name)
        self.assertEqual(shot["file"], row["file"])
        expected = {"task": "chk-1", "origin": "human", "source": "canvas",
                    "pretest_id": record["pretest_id"], "check_id": "chk-1",
                    "mocks_active": 0, "session_id": "s_canvas", "platform": "android",
                    "target_id": SERIAL, "app_id": APP, "label": "canvas",
                    "width": 1, "height": 1}
        self.assertEqual({key: row.get(key) for key in expected}, expected)
        self.assertIn("captured_at", row)
        self.assertIn("foreground", row)
        data = path.read_bytes()
        for key, value in (("task", "chk-1"), ("origin", "human"), ("check_id", "chk-1"),
                           ("pretest_id", record["pretest_id"]), ("mocks_active", "0")):
            self.assertIn(itxt(key, value), data)
        self.assertTrue(data.endswith(PNG[-12:]))  # IEND stays last

        # A capture is not an input: nothing counted. The journal entry carries the ids.
        self.assertEqual(self.used(record), 0)
        (entry,) = [e for e in self.journal() if e.get("verb") == "canvas capture.screenshot"]
        self.assertEqual((entry["origin"], entry["pretest_id"], entry["check_id"], entry["ok"]),
                         ("human", record["pretest_id"], "chk-1", True))

    def test_a_second_capture_takes_the_next_number(self) -> None:
        self.start_pretest()
        first = self.tool("capture.screenshot")["result"]["shot"]
        second = self.tool("capture.screenshot", origin="agent")["result"]["shot"]
        self.assertRegex(Path(first["path"]).name, r"^0001_")
        self.assertRegex(Path(second["path"]).name, r"^0002_")
        self.assertEqual([row["origin"] for row in self.shots()], ["human", "agent"])
        self.assertEqual(len(self.downloads()), 2)

    def test_without_a_session_nothing_new_happens(self) -> None:
        result = self.tool("capture.screenshot")["result"]
        self.assertEqual(set(result), {"capture", "pruned"})
        self.assertEqual(self.downloads(), [result["capture"]["name"]])
        self.assertFalse(any(path.name == "shots" for path in self.home.rglob("*")))

    def test_with_a_stopped_session_nothing_new_happens(self) -> None:
        session = self.start_session()
        session["stopped_at"] = "2026-10-10T00:00:00Z"
        text = json.dumps(session)
        (Path(session["artifacts_dir"]) / "session.json").write_text(text, encoding="utf-8")
        (session_mod.sessions_home() / "current.json").write_text(text, encoding="utf-8")
        result = self.tool("capture.screenshot")["result"]
        self.assertEqual(set(result), {"capture", "pruned"})
        self.assertFalse((Path(session["artifacts_dir"]) / "shots").exists())

    def test_a_session_without_a_pretest_gets_an_unattributed_copy(self) -> None:
        self.start_session()
        result = self.tool("capture.screenshot")["result"]
        self.assertIsNone(result["shot"]["task"])
        (row,) = self.shots()
        self.assertRegex(row["file"], r"^shots/0001_\d{6}_canvas\.png$")
        self.assertIsNone(row["task"])
        self.assertNotIn("pretest_id", row)
        self.assertNotIn("check_id", row)
        self.assertEqual((row["origin"], row["mocks_active"]), ("human", 0))
        self.assertFalse((Path(self.session["artifacts_dir"]) / "pretests").exists())

    def test_a_pretest_without_an_open_check_has_no_task(self) -> None:
        record = self.start_pretest(open_check=False)
        self.tool("capture.screenshot")
        (row,) = self.shots()
        self.assertIsNone(row["task"])
        self.assertEqual(row["pretest_id"], record["pretest_id"])
        self.assertNotIn("check_id", row)

    def test_active_mocks_mark_the_copy_and_warn(self) -> None:
        self.start_pretest()
        with mock.patch.object(shot_mod, "active_mocks",
                               return_value={"active": 2, "ids": ["m1", "m2"]}):
            result = self.tool("capture.screenshot")["result"]
        self.assertEqual(result["shot"]["mocks_active"], 2)
        self.assertNotIn("warnings", result["shot"])
        (warning,) = result["warnings"]
        self.assertEqual(warning["code"], "screenshot_shows_mocked_data")
        self.assertTrue(warning["hint"])
        (row,) = self.shots()
        self.assertEqual((row["mocks_active"], row["mocks"]), (2, "m1,m2"))
        self.assertIn(itxt("mocks", "m1,m2"), Path(result["shot"]["path"]).read_bytes())

    def test_a_failed_session_copy_keeps_the_downloads_capture_and_warns(self) -> None:
        self.start_pretest()
        # `shots` is a file: the session copy cannot be made (an OSError).
        (Path(self.session["artifacts_dir"]) / "shots").write_text("x", encoding="utf-8")
        response = self.tool("capture.screenshot")
        self.assertTrue(response["ok"], response)
        result = response["result"]
        self.assertNotIn("shot", result)
        (warning,) = result["warnings"]
        self.assertEqual(warning["code"], "session_copy_failed")
        self.assertIn("saved to Downloads but not to the session", warning["error"])
        self.assertTrue(warning["hint"])
        self.assertEqual(self.downloads(), [result["capture"]["name"]])
        (entry,) = [e for e in self.journal() if e.get("verb") == "canvas capture.screenshot"]
        self.assertTrue(entry["ok"])

    def test_a_session_without_its_folder_warns_with_the_refusal(self) -> None:
        self.start_session()
        with mock.patch.object(captures, "file_in_session",
                               side_effect=errors.AutonomError(errors.BACKEND_FAILED, "gone",
                                                               "hint")):
            result = self.tool("capture.screenshot")["result"]
        (warning,) = result["warnings"]
        self.assertEqual(warning["code"], "session_copy_failed")
        self.assertTrue(warning["error"].endswith("gone"))
        self.assertEqual(len(self.downloads()), 1)

    def test_an_unexpected_session_copy_error_is_a_warning_too(self) -> None:
        self.start_session()
        with mock.patch.object(captures, "file_in_session", side_effect=ValueError("odd path")):
            response = self.tool("capture.screenshot")
        self.assertTrue(response["ok"], response)
        (warning,) = response["result"]["warnings"]
        self.assertEqual(warning["code"], "session_copy_failed")
        self.assertIn("odd path", warning["error"])
        self.assertEqual(len(self.downloads()), 1)

    def test_a_failed_device_capture_writes_no_session_copy(self) -> None:
        self.start_pretest()
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["fail"] = {f"-s {SERIAL} exec-out screencap": [1, "error: device offline"]}
        self.state.write_text(json.dumps(state), encoding="utf-8")
        response = self.tool("capture.screenshot")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error_code"], errors.BACKEND_FAILED)
        self.assertEqual(self.shots(), [])
        self.assertEqual(self.downloads(), [])


class FileInSessionRefusalTests(CanvasPretestCase):
    """`captures.file_in_session` on its own: every refusal and the cleanup."""

    def setUp(self) -> None:
        super().setUp()
        self.start_session()
        self.source = self.tmp / "capture.png"
        self.source.write_bytes(PNG)

    def refused(self, *args, **kwargs) -> dict:
        with self.assertRaises(errors.AutonomError) as caught:
            captures.file_in_session(*args, **kwargs)
        payload = caught.exception.as_dict()
        self.assertEqual(payload["error_code"], errors.BACKEND_FAILED)
        self.assertTrue(payload.get("hint"))
        return payload

    def test_a_missing_source_is_refused(self) -> None:
        payload = self.refused(self.tmp / "nope.png", self.session, self.target)
        self.assertIn("missing", payload["error"])
        self.assertEqual(self.shots(), [])

    def test_a_symlinked_source_is_refused(self) -> None:
        link = self.tmp / "link.png"
        link.symlink_to(self.source)
        self.refused(link, self.session, self.target)

    def test_a_source_that_is_not_a_png_is_refused(self) -> None:
        self.source.write_bytes(b"GIF89a....")
        payload = self.refused(self.source, self.session, self.target)
        self.assertIn("not a PNG", payload["error"])
        self.assertFalse((Path(self.session["artifacts_dir"]) / "shots").exists())

    def test_an_oversized_source_is_refused(self) -> None:
        with mock.patch.object(captures, "SCREENSHOT_MAX", len(PNG) - 1):
            self.refused(self.source, self.session, self.target)

    def test_a_session_without_an_artifacts_folder_is_refused(self) -> None:
        for session in ({"session_id": "s"}, {"session_id": "s",
                                              "artifacts_dir": str(self.tmp / "gone")}):
            with self.subTest(session=session):
                payload = self.refused(self.source, session, self.target)
                self.assertIn("artifacts folder", payload["error"])

    def test_a_failed_index_write_removes_the_copy(self) -> None:
        with mock.patch.object(shot_mod, "append_index", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                captures.file_in_session(self.source, self.session, self.target)
        shots = Path(self.session["artifacts_dir"]) / "shots"
        self.assertEqual([p.name for p in shots.iterdir()], [])
        self.assertEqual(self.source.read_bytes(), PNG)

    def test_no_free_name_is_refused(self) -> None:
        shots = Path(self.session["artifacts_dir"]) / "shots"
        shots.mkdir(mode=0o700)
        (shots / "taken.png").write_bytes(b"")
        with mock.patch.object(shot_mod, "build_filename", return_value="taken.png"):
            payload = self.refused(self.source, self.session, self.target)
        self.assertIn("no free screenshot name", payload["error"])
        self.assertEqual((shots / "taken.png").read_bytes(), b"")

    def test_an_existing_number_is_skipped_not_overwritten(self) -> None:
        shots = Path(self.session["artifacts_dir"]) / "shots"
        shots.mkdir(mode=0o700)
        names = iter(["0001_000000_canvas.png", "0002_000000_canvas.png"])
        (shots / "0001_000000_canvas.png").write_bytes(b"older")
        with mock.patch.object(shot_mod, "next_index", return_value=1), \
                mock.patch.object(shot_mod, "build_filename", side_effect=lambda *_: next(names)):
            shot = captures.file_in_session(self.source, self.session, self.target)
        self.assertEqual(Path(shot["path"]).name, "0002_000000_canvas.png")
        self.assertEqual((shots / "0001_000000_canvas.png").read_bytes(), b"older")


# --- tp-006: human input is exempt, agent input is guarded ----------------------------


class BridgeGuardTests(CanvasPretestCase):
    def test_a_human_tap_is_sent_journaled_with_the_ids_and_not_counted(self) -> None:
        record = self.start_pretest()
        result = self.bridge("tap", {"x": 300, "y": 650}, "human")
        self.assertEqual((result["x"], result["y"]), (300, 650))
        self.assertEqual(self.inputs(), [["-s", SERIAL, "shell", "input", "tap", "300", "650"]])
        self.assertEqual(self.used(record), 0)
        (entry,) = self.journal()
        self.assertEqual((entry["verb"], entry["origin"], entry["ok"]), ("ui tap", "human", True))
        self.assertEqual((entry["pretest_id"], entry["check_id"]),
                         (record["pretest_id"], "chk-1"))
        self.assertFalse(guard.is_exempt())

    def test_human_input_of_every_kind_is_never_counted(self) -> None:
        record = self.start_pretest(max_actions=1)
        self.bridge("swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4}, "human")
        self.bridge("key", {"key": "KEYCODE_BACK"}, "human")
        self.bridge("text", {"text": "hello"}, "human")
        self.bridge("tap", {"x": 300, "y": 650}, "human")
        self.assertEqual(len(self.inputs()), 4)
        self.assertEqual(self.used(record), 0)
        self.assertEqual(self.refusals(record), [])

    def test_a_human_tap_on_a_forbidden_label_is_the_persons_choice(self) -> None:
        record = self.start_pretest()
        self.bridge("tap", {"x": 300, "y": 360}, "human")
        self.assertEqual(self.inputs(), [["-s", SERIAL, "shell", "input", "tap", "300", "360"]])
        self.assertFalse(any("uiautomator" in argv for argv in self.calls()))
        self.assertEqual(self.refusals(record), [])

    def test_an_agent_tap_is_counted(self) -> None:
        record = self.start_pretest()
        self.bridge("tap", {"x": 300, "y": 650}, "agent")
        self.assertEqual(self.used(record), 1)
        self.assertIn(["-s", SERIAL, "shell", "input", "tap", "300", "650"], self.inputs())
        (entry,) = self.journal()
        self.assertEqual((entry["origin"], entry["ok"], entry["check_id"]),
                         ("agent", True, "chk-1"))

    def test_agent_input_of_every_kind_is_counted(self) -> None:
        record = self.start_pretest()
        self.bridge("swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4}, "agent")
        self.bridge("key", {"key": "KEYCODE_BACK"}, "agent")
        self.bridge("text", {"text": "hello"}, "agent")
        self.assertEqual(self.used(record), 3)

    def test_a_tap_from_a_client_without_an_origin_is_counted_as_agent(self) -> None:
        # RISK-018: the Canvas forwards a tap whose client named no origin as `agent`
        # (normalizeOrigin), so the guard counts it and can refuse it.
        record = self.start_pretest(max_actions=1)
        forwarded = {"id": 1, "op": "tap", "origin": "agent", "payload": {"x": 300, "y": 650}}
        bridge.dispatch(self.target, forwarded)
        self.assertEqual(self.used(record), 1)
        (entry,) = self.journal()
        self.assertEqual((entry["origin"], entry["ok"]), ("agent", True))
        self.clear_log()
        self.assert_refusal(errors.BUDGET_EXHAUSTED, "tap", {"x": 300, "y": 650})
        self.assertEqual(self.inputs(), [])

    def test_a_bridge_message_without_an_origin_is_refused_never_exempt(self) -> None:
        record = self.start_pretest()
        for message in ({"id": 1, "op": "tap", "payload": {"x": 300, "y": 650}},
                        {"id": 2, "op": "tap", "origin": None, "payload": {"x": 300, "y": 650}},
                        {"id": 3, "op": "tap", "origin": "", "payload": {"x": 300, "y": 650}}):
            with self.subTest(origin=message.get("origin", "missing")):
                with self.assertRaises(errors.AutonomError) as caught:
                    bridge.dispatch(self.target, message)
                self.assertEqual(caught.exception.code, errors.FLOW_COMMAND_INVALID)
        self.assertEqual(self.inputs(), [])
        self.assertEqual(self.used(record), 0)
        self.assertFalse(guard.is_exempt())

    def test_replay_and_system_origins_are_guarded_too(self) -> None:
        record = self.start_pretest()
        self.bridge("key", {"key": "KEYCODE_BACK"}, "replay")
        self.bridge("key", {"key": "KEYCODE_BACK"}, "system")
        self.assertEqual(self.used(record), 2)

    def test_an_agent_tap_past_the_budget_is_refused_journaled_and_sends_nothing(self) -> None:
        record = self.start_pretest(max_actions=1)
        self.bridge("tap", {"x": 300, "y": 650}, "agent")
        self.clear_log()
        error = self.assert_refusal(errors.BUDGET_EXHAUSTED, "tap", {"x": 300, "y": 650})
        self.assertIn("no input was sent", error["error"])
        self.assertEqual(self.inputs(), [])
        self.assertEqual(self.used(record), 1)
        self.assertEqual([(r["code"], r["verb"]) for r in self.refusals(record)],
                         [(errors.BUDGET_EXHAUSTED, "tap")])
        refused = self.journal()[-1]
        self.assertEqual((refused["verb"], refused["origin"], refused["ok"],
                          refused["error_code"], refused["check_id"]),
                         ("ui tap", "agent", False, errors.BUDGET_EXHAUSTED, "chk-1"))
        # The human can still act once the agent's budget is gone.
        self.bridge("tap", {"x": 300, "y": 650}, "human")
        self.assertEqual(len(self.inputs()), 1)

    def test_an_agent_tap_on_a_forbidden_label_is_refused(self) -> None:
        record = self.start_pretest()
        error = self.assert_refusal(errors.FORBIDDEN_ACTION, "tap", {"x": 300, "y": 360})
        self.assertEqual(error["pattern"].lower(), "купить")
        self.assertEqual(self.inputs(), [])
        self.assertEqual(self.used(record), 0)
        refused = self.journal()[-1]
        self.assertEqual((refused["ok"], refused["error_code"]),
                         (False, errors.FORBIDDEN_ACTION))

    def test_every_agent_refusal_reaches_the_bridge_reply_with_a_hint(self) -> None:
        record = self.start_pretest(max_actions=1)
        self.bridge("key", {"key": "KEYCODE_BACK"}, "agent")
        self.clear_log()
        for op, payload in (("swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4}),
                            ("key", {"key": "KEYCODE_BACK"}),
                            ("text", {"text": "hello"})):
            with self.subTest(op=op):
                self.assert_refusal(errors.BUDGET_EXHAUSTED, op, payload)
        self.assertEqual(self.inputs(), [])
        self.assertEqual([entry["verb"] for entry in self.journal()
                          if entry.get("error_code") == errors.BUDGET_EXHAUSTED],
                         ["ui swipe", "ui key", "ui text"])
        self.assertEqual(len(self.refusals(record)), 3)

    def test_the_bridge_process_answers_a_refusal_as_an_error_line(self) -> None:
        self.start_pretest(max_actions=1)
        self.bridge("tap", {"x": 300, "y": 650}, "agent")
        lines = "\n".join(json.dumps(m) for m in (
            {"id": 8, "op": "tap", "origin": "agent", "payload": {"x": 300, "y": 650}},
            {"id": 9, "op": "tap", "origin": "human", "payload": {"x": 300, "y": 650}},
        )) + "\n"
        out = io.StringIO()
        argv = ["bridge", "--platform", "android", "--target", SERIAL, "--tool", FAKE_ADB]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(sys, "stdin", io.StringIO(lines)), \
                mock.patch.object(sys, "stdout", out):
            self.assertEqual(bridge.main(), 0)
        refused, sent = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual((refused["id"], refused["ok"], refused["error_code"]),
                         (8, False, errors.BUDGET_EXHAUSTED))
        self.assertTrue(refused["hint"])
        self.assertEqual((sent["id"], sent["ok"]), (9, True))

    def test_a_record_is_journaled_and_never_counted(self) -> None:
        record = self.start_pretest(max_actions=1)
        self.bridge("tap", {"x": 300, "y": 650}, "agent")
        result = self.bridge("record", {"kind": "gesture", "pointers": 1}, "agent")
        self.assertTrue(result["ok"])
        self.assertEqual(self.used(record), 1)
        self.assertEqual(self.journal()[-1]["verb"], "ui gesture")

    def test_an_unknown_operation_is_refused_before_the_guard(self) -> None:
        record = self.start_pretest()
        with self.assertRaises(errors.AutonomError) as caught:
            self.bridge("fling", {}, "agent")
        self.assertEqual(caught.exception.code, errors.FLOW_COMMAND_INVALID)
        self.assertEqual(self.used(record), 0)
        self.assertEqual(self.journal(), [])

    def test_a_backend_failure_is_not_journaled_as_a_refusal(self) -> None:
        self.start_pretest()
        with mock.patch.object(bridge.ui, "tap",
                               side_effect=errors.AutonomError(errors.BACKEND_FAILED, "x")):
            with self.assertRaises(errors.AutonomError):
                self.bridge("tap", {"x": 1, "y": 1}, "agent")
        self.assertEqual(self.journal(), [])


class BridgeWithoutPretestTests(CanvasPretestCase):
    """tp-028: without an active pre-test the bridge behaves exactly as before."""

    def test_agent_input_without_a_pretest_is_not_counted_and_unattributed(self) -> None:
        self.start_session()
        self.bridge("tap", {"x": 300, "y": 360}, "agent")
        self.assertEqual(self.inputs(), [["-s", SERIAL, "shell", "input", "tap", "300", "360"]])
        self.assertFalse(any("uiautomator" in argv for argv in self.calls()))
        (entry,) = self.journal()
        self.assertNotIn("pretest_id", entry)
        self.assertNotIn("check_id", entry)
        self.assertFalse((Path(self.session["artifacts_dir"]) / "pretests").exists())

    def test_agent_input_without_any_session_is_sent(self) -> None:
        self.bridge("tap", {"x": 300, "y": 650}, "agent")
        self.assertEqual(self.inputs(), [["-s", SERIAL, "shell", "input", "tap", "300", "650"]])


class IosBridgeGuardTests(CanvasPretestCase):
    platform = "ios"

    def setUp(self) -> None:
        super().setUp()
        self.trace = self.tmp / "idb.log"
        idb = self.tmp / "bin" / "idb"
        idb.parent.mkdir()
        idb.write_text(f'#!/bin/sh\necho "$*" >> "{self.trace}"\nexit 0\n', encoding="utf-8")
        idb.chmod(0o755)
        self.set_env(AUTONOM_IDB=str(idb), AUTONOM_IDB_COMPANION=None)

    def pressed(self) -> list[str]:
        if not self.trace.exists():
            return []
        return self.trace.read_text(encoding="utf-8").splitlines()

    def test_an_agent_volume_button_is_counted_then_refused_without_idb(self) -> None:
        record = self.start_pretest(max_actions=1)
        self.bridge("key", {"key": "KEYCODE_VOLUME_UP"}, "agent")
        self.assertEqual(self.used(record), 1)
        self.assertEqual(self.pressed(), [f"ui button VOLUME_UP --udid {UDID}"])
        self.assert_refusal(errors.BUDGET_EXHAUSTED, "key", {"key": "KEYCODE_VOLUME_DOWN"})
        self.assertEqual(self.pressed(), [f"ui button VOLUME_UP --udid {UDID}"])
        self.assertEqual(self.journal()[-1]["error_code"], errors.BUDGET_EXHAUSTED)

    def test_a_human_volume_button_is_never_counted(self) -> None:
        record = self.start_pretest(max_actions=1)
        for _ in range(3):
            self.bridge("key", {"key": "KEYCODE_VOLUME_DOWN"}, "human")
        self.assertEqual(len(self.pressed()), 3)
        self.assertEqual(self.used(record), 0)


# --- the Actions drawer follows the same rule ---------------------------------------


class ToolsGuardTests(CanvasPretestCase):
    def test_a_human_launch_outside_the_allow_list_runs_and_is_not_counted(self) -> None:
        record = self.start_pretest()
        response = self.tool("app.launch", {"app_id": "com.other.app"}, origin="human")
        self.assertTrue(response["ok"], response)
        self.assertTrue(any("com.other.app" in " ".join(argv) for argv in self.calls()))
        self.assertEqual(self.used(record), 0)
        self.assertFalse(guard.is_exempt())

    def test_an_agent_launch_outside_the_allow_list_is_refused_and_journaled(self) -> None:
        record = self.start_pretest()
        response = self.tool("app.launch", {"app_id": "com.other.app"}, origin="agent")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error_code"], errors.APP_NOT_ALLOWED)
        self.assertTrue(response["hint"])
        self.assertFalse(any("com.other.app" in " ".join(argv) for argv in self.calls()))
        entry = self.journal()[-1]
        self.assertEqual((entry["verb"], entry["origin"], entry["ok"], entry["error_code"]),
                         ("canvas app.launch", "agent", False, errors.APP_NOT_ALLOWED))
        self.assertEqual([r["code"] for r in self.refusals(record)], [errors.APP_NOT_ALLOWED])

    def test_an_agent_launch_of_the_session_app_is_counted(self) -> None:
        record = self.start_pretest()
        response = self.tool("app.launch", {"app_id": APP}, origin="agent")
        self.assertTrue(response["ok"], response)
        self.assertEqual(self.used(record), 1)


if __name__ == "__main__":
    unittest.main()
